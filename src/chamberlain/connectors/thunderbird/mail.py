"""MailPort over the Thunderbird bridge. Prepared intents select one of three write shapes."""

from collections.abc import Mapping

from pydantic import JsonValue

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import BridgeFailure
from chamberlain.connectors.thunderbird.identity import (
    MessageLocator,
    bracketed,
    decode_locator,
    normalise_body,
)
from chamberlain.connectors.thunderbird.mail_reads import MailLimits as MailLimits
from chamberlain.connectors.thunderbird.mail_reads import MailReads, milliseconds
from chamberlain.connectors.thunderbird.mail_reads import ThunderbirdBinding as ThunderbirdBinding
from chamberlain.connectors.thunderbird.mail_writes import (
    ALLOW_DUPLICATE_REF as ALLOW_DUPLICATE_REF,
)
from chamberlain.connectors.thunderbird.mail_writes import (
    DISPLAY_ONLY_REF as DISPLAY_ONLY_REF,
)
from chamberlain.connectors.thunderbird.mail_writes import (
    ChangePlan,
    ComposePlan,
    DiscardPlan,
    MoveBaseline,
    PlanAttachment,
    by_id,
)
from chamberlain.connectors.thunderbird.protocol import (
    Fingerprint,
    MessageRef,
)
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.intents import (
    IntentKind,
    MailDraftDiscard,
    MailDraftSave,
    MailDraftUpdate,
    MailIntent,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
)
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    Prepared,
    ReconcileResult,
    ReconcileStatus,
)
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_SHAPES = {
    _K.MAIL_SEND: ComposePlan,
    _K.MAIL_DRAFT_SAVE: ComposePlan,
    _K.MAIL_DRAFT_UPDATE: ComposePlan,
    _K.MAIL_MOVE: ChangePlan,
    _K.MAIL_TRASH: ChangePlan,
    _K.MAIL_MARK_READ: ChangePlan,
    _K.MAIL_DRAFT_DISCARD: DiscardPlan,
}


def _shape(prepared: Prepared) -> ComposePlan | ChangePlan | DiscardPlan:
    try:
        return _SHAPES[prepared.intent_kind].model_validate(prepared.data)
    except (KeyError, ValueError):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "Invalid Thunderbird write plan."
        ) from None


class ThunderbirdMail(MailReads):
    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9): resolve, check preconditions, hold everything dispatch needs."""
        binding = self.binding(intent.source)
        if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate):
            return self._prepare_compose(binding, intent, refs)
        if isinstance(intent, MailDraftDiscard):
            draft = self._draft_ref(binding, refs, intent.draft, intent.expected_sha256)
            plan = DiscardPlan(draft=draft, expected_raw_sha256=intent.expected_sha256)
            return self._prepared(intent.source, intent.kind, None, plan)
        located: dict[str, list[str]] = {}
        for handle in intent.messages:
            where = self._ref_of(binding, refs, handle)
            located.setdefault(where.folder_id, []).append(where.message_id)
        groups = tuple(
            protocol.ChangeGroup(folder_id=folder, ids=tuple(ids))
            for folder, ids in located.items()
        )
        if isinstance(intent, MailMarkRead):
            change = ChangePlan(items=groups, set_read=intent.read)
        else:
            if len(groups) != 1 or (intent.from_folder and intent.from_folder not in located):
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "The messages are not in that folder."
                )
            dest = binding.trash_folder_id if isinstance(intent, MailTrash) else intent.to_folder
            if dest in located:
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "The messages are already in that folder."
                )
            if dest not in {folder.id for folder in self.folders(intent.source)}:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "The destination folder does not exist."
                )
            change = ChangePlan(
                items=groups,
                move_to=dest,
                trash=isinstance(intent, MailTrash),
                set_read=True if isinstance(intent, MailMove) and intent.mark_read else None,
            )
            # Reject wholly missing/ambiguous batches before authorisation or dispatch.
            source, destination = change.facts(self, binding, refresh=False).scopes
            if not source.complete or not destination.complete:
                raise ChamberlainError(
                    ErrorCode.SOURCE_UNAVAILABLE, "The folder scan was incomplete."
                )
            here, there = by_id(source), by_id(destination)
            dates = [
                fact.header.date_ms for scope in (source, destination) for fact in scope.copies
            ]
            margin = 2 * 86_400_000  # Match the date tolerance used during the add-on's move.
            date_window = (
                (min(dates) - margin, max(dates) + margin)
                if len(groups[0].ids) > 1
                and set(groups[0].ids) <= here.keys()
                and all(margin <= date <= protocol.MAX_JS_DATE_MS - margin for date in dates)
                else None
            )
            change = change.model_copy(
                update={
                    "date_window": date_window,
                    "before": {
                        mid: MoveBaseline(
                            source=len(here.get(mid, ())),
                            destination=len(there.get(mid, ())),
                            read=here[mid][0].read if len(here.get(mid, ())) == 1 else None,
                        )
                        for mid in groups[0].ids
                    },
                }
            )
            if not any(
                len(here.get(mid, ())) == 1 and (change.trash or mid not in there)
                for mid in groups[0].ids
            ):
                if not here:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_UNKNOWN_HANDLE, "None of the messages is in the folder."
                    )
                raise ChamberlainError(
                    ErrorCode.REMOTE_AMBIGUOUS, "No message could be moved unambiguously."
                )
        return self._prepared(intent.source, intent.kind, None, change)

    @staticmethod
    def _prepared(source: str, kind: IntentKind, token: str | None, plan: Model) -> Prepared:
        data: dict[str, JsonValue] = plan.model_dump(mode="json")
        return Prepared.model_validate(
            {"source": source, "intent_kind": kind, "reconcile_token": token, "data": data}
        )

    def _ref_of(
        self, binding: ThunderbirdBinding, refs: Mapping[str, str], handle: str
    ) -> MessageLocator:
        locator = refs.get(handle)
        if locator is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "A handle in the request has no locator."
            )
        return decode_locator(locator, account_id=binding.account_id)

    def _draft_ref(
        self,
        binding: ThunderbirdBinding,
        refs: Mapping[str, str],
        handle: str,
        expected_sha256: str,
    ) -> MessageRef:
        where = self._ref_of(binding, refs, handle)
        if where.folder_id != binding.draft_folder_id:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "That message is not a draft.")
        scope = self.copies(
            binding,
            [
                protocol.CopyScope(
                    folder_id=where.folder_id, ids=(where.message_id,), inspect_draft=True
                )
            ],
        ).scopes[0]
        if (
            not scope.complete
            or len(scope.copies) != 1
            or scope.copies[0].raw_sha256 != expected_sha256
        ):
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The draft changed since it was read.")
        return MessageRef(folder_id=where.folder_id, internet_message_id=where.message_id)

    def _prepare_compose(
        self,
        binding: ThunderbirdBinding,
        intent: MailSend | MailDraftSave | MailDraftUpdate,
        refs: Mapping[str, str],
    ) -> Prepared:
        if intent.message_id is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "The message has no Message-ID.", field="message_id"
            )
        recipients = len(intent.to) + len(intent.cc) + len(intent.bcc)
        if recipients > self.limits.max_recipients:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Too many recipients.")
        reply_to = None
        if intent.reply_to is not None:
            where = self._ref_of(binding, refs, intent.reply_to)
            self.resolve_one(binding, where)
            reply_to = MessageRef(folder_id=where.folder_id, internet_message_id=where.message_id)
        draft = None
        expected = None
        if isinstance(intent, MailDraftUpdate):
            draft = self._draft_ref(binding, refs, intent.draft, intent.expected_sha256)
            expected = intent.expected_sha256
        own = binding.mailbox.lower()
        everyone = {*intent.to, *intent.cc, *intent.bcc}
        fingerprint = Fingerprint(
            sender=own,
            to=tuple(sorted(a.lower() for a in intent.to)),
            cc=tuple(sorted(a.lower() for a in intent.cc)),
            bcc=tuple(sorted(a.lower() for a in intent.bcc)),
            subject=intent.subject,
            body_sha256=sha256_text(normalise_body(intent.body)),
            attachment_sha256=tuple(sorted(a.sha256 for a in intent.attachments)),
            high_importance=intent.high_importance,
            in_reply_to=bracketed(reply_to.internet_message_id) if reply_to else "",
            token=intent.message_id,
        )
        plan = ComposePlan(
            to=intent.to,
            cc=intent.cc,
            bcc=intent.bcc,
            subject=intent.subject,
            body=intent.body,
            high_importance=intent.high_importance,
            token=intent.message_id,
            sent_folder_id=binding.sent_folder_id,
            draft_folder_id=binding.draft_folder_id,
            reply_to=reply_to,
            display_only=refs.get(DISPLAY_ONLY_REF) == "1",
            attachments=tuple(
                PlanAttachment(sha256=a.sha256, filename=a.filename) for a in intent.attachments
            ),
            fingerprint=fingerprint,
            since_ms=milliseconds(self.clock.now())
            - self.limits.reconcile_window_hours * 3_600_000,
            inbox_folder_id=binding.inbox_folder_id if everyone == {own} else None,
            draft=draft,
            expected_raw_sha256=expected,
        )
        if isinstance(intent, MailSend) and refs.get(ALLOW_DUPLICATE_REF) != "1":
            plan.check_duplicates(self, binding)
        return self._prepared(intent.source, intent.kind, intent.message_id, plan)

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        """The one mutation (P-10). Never retries, and returns `unknown` rather than raising."""
        binding = self.binding(prepared.source)
        try:
            return _shape(prepared).dispatch(self, binding, prepared, payloads)
        except BridgeFailure as error:
            if error.proven_not_applied:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED,
                    detail=f"{error.code.value}: {error.detail or error.message}"[:300],
                )
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN,
                detail=f"{error.code.value}: {error.detail or error.message}"[:300],
            )
        except ChamberlainError as error:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail=f"{error.code.value}: {error.detail}"[:300]
            )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11): needs only `prepared`; may refresh exactly the folders involved."""
        binding = self.binding(prepared.source)
        try:
            return _shape(prepared).reconcile(self, binding, prepared)
        except ChamberlainError as error:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail=f"{error.code.value}: {error.detail}"[:300]
            )
