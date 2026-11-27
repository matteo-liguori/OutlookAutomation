"""MailPort over the Thunderbird bridge. Prepared intents select one of three write shapes."""

from collections.abc import Iterable, Mapping

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import BridgeFailure
from chamberlain.connectors.thunderbird.identity import (
    MessageLocator,
    bracketed,
    decode_locator,
    normalise_body,
)
from chamberlain.connectors.thunderbird.mail_reads import (
    MailReads,
    ThunderbirdBinding,
    milliseconds,
)
from chamberlain.connectors.thunderbird.mail_writes import (
    ALLOW_DUPLICATE_REF,
    DISPLAY_ONLY_REF,
    ChangePlan,
    ComposePlan,
    DiscardPlan,
    MoveBaseline,
    PlanAttachment,
    by_id,
)
from chamberlain.connectors.thunderbird.protocol import Fingerprint, MessageRef
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


def _change_groups(refs: Iterable[tuple[str, str]]) -> tuple[protocol.ChangeGroup, ...]:
    located: dict[str, list[str]] = {}
    for folder, mid in refs:
        located.setdefault(folder, []).append(mid)
    return tuple(
        protocol.ChangeGroup(folder_id=folder, ids=tuple(ids)) for folder, ids in located.items()
    )


def _shape(prepared: Prepared) -> ComposePlan | ChangePlan | DiscardPlan:
    try:
        return _SHAPES[prepared.intent_kind].model_validate(prepared.data)
    except (KeyError, ValueError):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "Invalid Thunderbird write plan."
        ) from None


def _prepared(intent: MailIntent, token: str | None, plan: Model) -> Prepared:
    return Prepared(
        source=intent.source,
        intent_kind=intent.kind,
        reconcile_token=token,
        data=plan.model_dump(mode="json"),
    )


def _ref_of(binding: ThunderbirdBinding, refs: Mapping[str, str], handle: str) -> MessageLocator:
    locator = refs.get(handle)
    if locator is None:
        raise ChamberlainError(
            ErrorCode.REQUEST_UNKNOWN_HANDLE, "A handle in the request has no locator."
        )
    return decode_locator(locator, account_id=binding.account_id)


class ThunderbirdMail(MailReads):
    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9): resolve, check preconditions, hold everything dispatch needs."""
        binding = self.binding(intent.source)
        if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate):
            return self._prepare_compose(binding, intent, refs)
        if isinstance(intent, MailDraftDiscard):
            draft = self._draft_ref(binding, refs, intent.draft, intent.expected_sha256)
            plan = DiscardPlan(draft=draft, expected_raw_sha256=intent.expected_sha256)
            return _prepared(intent, None, plan)
        locations = (_ref_of(binding, refs, handle) for handle in intent.messages)
        groups = _change_groups((where.folder_id, where.message_id) for where in locations)
        if isinstance(intent, MailMarkRead):
            change = ChangePlan(items=groups, set_read=intent.read)
        else:
            change = self._prepare_move(intent, binding, groups)
        return _prepared(intent, None, change)

    def _prepare_move(
        self,
        intent: MailMove | MailTrash,
        binding: ThunderbirdBinding,
        groups: tuple[protocol.ChangeGroup, ...],
    ) -> ChangePlan:
        if len(groups) != 1 or intent.from_folder not in (None, "", groups[0].folder_id):
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The messages are not in that folder.")
        [group] = groups
        trash = isinstance(intent, MailTrash)
        dest = binding.trash_folder_id if trash else intent.to_folder
        if dest == group.folder_id:
            raise ChamberlainError(
                ErrorCode.REMOTE_CHANGED, "The messages are already in that folder."
            )
        if not any(folder.id == dest for folder in self.folders(intent.source)):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "The destination folder does not exist."
            )
        change = ChangePlan(
            items=groups,
            move_to=dest,
            trash=trash,
            set_read=True if isinstance(intent, MailMove) and intent.mark_read else None,
        )
        source, destination = change.facts(self, binding, refresh=False).scopes
        if not source.complete or not destination.complete:
            raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "The folder scan was incomplete.")
        here, there = by_id(source), by_id(destination)
        if not here:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "None of the messages is in the folder."
            )
        before: dict[str, MoveBaseline] = {}
        for mid in group.ids:
            origin = here.get(mid, ())
            before[mid] = MoveBaseline(
                source=len(origin),
                destination=len(there.get(mid, ())),
                read=origin[0].read if len(origin) == 1 else None,
            )
        if not any(item.eligible(trash=trash) for item in before.values()):
            raise ChamberlainError(
                ErrorCode.REMOTE_AMBIGUOUS, "No message could be moved unambiguously."
            )
        dates = [copy.header.date_ms for scope in (source, destination) for copy in scope.copies]
        margin = 2 * 86_400_000
        window = None
        if (
            len(group.ids) > 1
            and set(group.ids) <= here.keys()
            and all(margin <= date <= protocol.MAX_JS_DATE_MS - margin for date in dates)
        ):
            window = (min(dates) - margin, max(dates) + margin)
        return change.model_copy(update={"date_window": window, "before": before})

    def _draft_ref(
        self,
        binding: ThunderbirdBinding,
        refs: Mapping[str, str],
        handle: str,
        expected_sha256: str,
    ) -> MessageRef:
        where = _ref_of(binding, refs, handle)
        if where.folder_id != binding.draft_folder_id:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "That message is not a draft.")
        scope = self.copies_of(binding, where.folder_id, where.message_id, draft=True)
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
            where = _ref_of(binding, refs, intent.reply_to)
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
            sent_copy=binding.sent_copy if isinstance(intent, MailSend) else "client",
        )
        if isinstance(intent, MailSend) and refs.get(ALLOW_DUPLICATE_REF) != "1":
            plan.check_duplicates(self, binding)
        return _prepared(intent, intent.message_id, plan)

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        """The one mutation (P-10). Never retries, and returns `unknown` rather than raising."""
        binding = self.binding(prepared.source)
        try:
            return _shape(prepared).dispatch(self, binding, prepared, payloads)
        except ChamberlainError as error:
            bridge = isinstance(error, BridgeFailure)
            return DispatchResult(
                outcome=(
                    DispatchOutcome.NOT_APPLIED
                    if bridge and error.proven_not_applied
                    else DispatchOutcome.UNKNOWN
                ),
                detail=f"{error.code.value}: {error.detail or (error.message if bridge else '')}"[
                    :300
                ],
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
