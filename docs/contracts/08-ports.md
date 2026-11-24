# 08 Connector ports

Binding for every connector and for whoever calls one. Code:
`src/chamberlain/ports/`, shared types in `src/chamberlain/domain/`. The
contract suites in `src/chamberlain/testing/` are the executable form; a
connector is done when it passes them.

## Shape

A connector is one object per source kind that implements one or more of
`MailPort`, `CalendarPort`, `ContactsPort` (typing.Protocol classes). Every
port has:

```python
def capabilities(self, source: SourceId) -> CapabilitySet
def health(self, source: SourceId) -> SourceHealth
```

The router (`app`) holds `{source id -> port}` and is the only caller. The
agent sees sources and capability names, never a connector.

## Capabilities

`CapabilitySet` is a list of `Capability {name, level, limits, absence_provable}`.

Names: `mail.read`, `mail.search`, `mail.attachments`, `mail.send`,
`mail.draft`, `mail.move`, `mail.trash`, `mail.mark_read`, `calendar.read`,
`calendar.write`, `contacts.search`.

Levels: `live_verified` (passed live validation on this installation's
connector version), `implemented` (passes the contract suite against a fake
or recorded transport only), `known_broken`.

- **P-1** A request for a capability the source does not declare, or
  declares `known_broken`, fails with `capability.unsupported`. There is no
  fallback to another connector, source or route.
- **P-2** `limits` declares what the connector cannot exceed (for example
  `max_attachment_bytes`, `max_recipients`, `page_size`). The engine checks
  them before `prepare`.

## Reading

- **P-3** No read method may change remote state. In particular reading,
  listing, searching and fetching attachments MUST leave the read/unread
  flag exactly as it was. The contract suite checks the flag before and
  after every read call.
- **P-4** Lists are paged by an opaque connector cursor and return
  `Page[T] {items, cursor?, complete, gaps[]}`. A connector MUST report
  `complete: false` with a gap when it could not see everything (a folder
  not synchronised, a stale store, a scan that stopped early). An empty
  result from an incomplete scan is not "no mail" (E-3).
- **P-5** `health` returns `{state: ok | stale | sign_in_needed |
  unavailable, checked_at, fresh_as_of?}`. `fresh_as_of` is the time up to
  which the connector can vouch for completeness; a connector that cannot
  vouch returns `stale`, never a guess.
- **P-6** Every item carries a `Locator`: a stable string that resolves to
  the same remote item after the client restarts, re-indexes or renumbers
  its session IDs. `resolve(source, locator)` returns the current item or
  raises `request.unknown_handle`; if the locator now matches more than one
  item where exactly one is required, `remote.ambiguous`.
- **P-7** Two physical copies of one RFC message (same `Message-ID` in two
  folders) are two items with two locators. A connector MUST NOT merge them.
- **P-8** Attachment bytes are returned with their SHA-256 and size; a
  transfer whose bytes do not match the announced hash raises and yields
  nothing.

## Writing

Three methods on each writing port, called by the engine in this order and
never by anything else:

```python
def prepare(self, intent, refs: Mapping[str, str]) -> Prepared          # read-only
def dispatch(self, prepared: Prepared, key: str, ...) -> DispatchResult # the one mutation
def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult    # read-only
```

`refs` maps every handle named in the intent (`message`, `draft`,
`reply_to`, `event_ref`, each of `messages`) to its stable locator. Ports
take and return locators and plain source IDs; they never see a handle's
meaning or the store. `MailPort.dispatch` takes a third argument,
`payloads: Mapping[str, bytes]`, mapping each attachment's SHA-256 to its
bytes, which the engine reads from the artifact store.

- **P-9** `prepare` is read-only. It resolves locators, checks
  preconditions (the message is still where the intent says, the draft still
  has `expected_sha256`, the event still has `expected_etag`, the
  destination does not already hold the message, attachments are readable)
  and returns a JSON-serialisable `Prepared` holding everything `dispatch`
  needs. Failure raises a `ChamberlainError` with `may_have_happened:
  false`.
- **P-10** `dispatch` performs exactly one logical mutation and returns
  `DispatchResult {outcome, receipt?, detail}` with `outcome` one of:
  `applied` (it happened; `receipt` says what and how verified);
  `not_applied` (the connector can prove nothing was mutated);
  `unknown` (anything else). `dispatch` SHOULD return `unknown` rather than
  raise; the engine treats any exception as `unknown` (W-10). A connector
  MUST NOT retry a mutation internally, and MUST NOT report `not_applied`
  on a timeout.
- **P-11** `reconcile` is read-only and returns `ReconcileResult {status,
  receipt?, proof}`: `present` (the write is there; receipt filled in),
  `queued` (an exact copy waits in Outbox or Drafts; no receipt, no proof; the
  operation stays `uncertain`, R2), `absent`, or `unknown`. It MUST NOT need the original attachment files or
  any state other than `Prepared` and the key. It may refresh the client's
  view of exactly the folders or calendar involved before reading.
- **P-12** `absent` with `proof: true` is allowed only for intent kinds the
  capability lists in `absence_provable`, and only when the connector has
  complete, fresh coverage of every place the write could have landed.
  Otherwise `absent` MUST carry `proof: false` and the engine keeps the
  operation `uncertain` (W-12). For sends, coverage means Sent, Outbox and
  Drafts (R2).
- **P-13** A connector keeps no journal and no idempotency state. `key` is
  passed so that a transport which supports request IDs can make the remote
  side recognise a duplicate; it does not move safety out of the engine.
- **P-14** Move and trash: exactly one copy at the destination afterwards
  and none at the source, identified by `Message-ID`, or the result is not
  `applied`. Trash goes only to the folder the binding names as recoverable.
  `move` with `mark_read: false` leaves the flag as it was.
- **P-15** Send: the outgoing message carries the reconcile token (as its
  `Message-ID` where the connector honours one, `message_id_honoured` in the
  capability limits, and always in the header `X-Chamberlain-Write-Token`) and
  is filed in the bound sent folder. `applied`
  with `client_verified` means the client accepted it for sending;
  `server_verified` only after it is read back from the sent folder.
- **P-16** Calendar update and delete use optimistic concurrency
  (`expected_etag`); a mismatch at `prepare` is `remote.changed`. Create
  uses the `uid` from the intent; a second create with the same `uid` is
  `not_applied` plus a `present` reconcile, never a duplicate event. A
  cancelled event is reported as cancelled, not as missing. After a delete the
  read-back accepts either answer: the event read as cancelled or gone
  (`request.unknown_handle`), and `events(include_cancelled)` returns nothing
  or the cancelled one. CalDAV and Owl deletes are real deletes; reconcile of
  a delete is `present` either way.
- **P-17** Time limits: every port call takes its deadline from the
  connector's configuration, and a call that exceeds it raises
  `source.unavailable` for reads and returns `unknown` for `dispatch`.

## Errors

- **P-18** Connectors raise only `ChamberlainError` with codes from the
  `source.*`, `remote.*`, `capability.*`, `request.unknown_handle` and
  `artifact.hash_mismatch` families. Raw client or protocol text goes in
  `detail` for the log (E-15).

## The fake provider

`src/chamberlain/connectors/fake/` implements all three ports in memory and
is the reference implementation. It is shaped like the real things: mail has
folders, session-scoped integer IDs and stable locators like the Thunderbird
bridge; calendar has `etag`, `uid`, `status` and `updated` like Google
Calendar.

Fault switches (`Fault`), each armed for the next N calls:

| Switch | Effect |
| --- | --- |
| `LOSE_RESPONSE_AFTER_APPLY` | The mutation applies, then `dispatch` returns `unknown`. |
| `LOSE_BEFORE_APPLY` | Nothing applies; `dispatch` returns `unknown`. |
| `BUSY_BEFORE_APPLY` | Nothing applies; `dispatch` returns `not_applied`. |
| `RESTART` | Session IDs are renumbered; stable locators still resolve. |
| `DUPLICATE_COPY` | A second physical copy of the next touched message appears. |
| `SLOW` | The call advances the clock past its deadline before answering. |
| `STALE` | Reads report `complete: false` with a gap; `health` is `stale`. |

The fake records every `dispatch` in `dispatch_log` so tests can assert that
nothing was dispatched twice.

## Contract suites

`chamberlain.testing.mail_contract.MailPortContract`,
`calendar_contract.CalendarPortContract` and
`contacts_contract.ContactsPortContract` are pytest base classes. A
connector plugs in by subclassing and providing a `rig` fixture that
returns a `MailRig` (or `CalendarRig`, `ContactsRig`): the port, a source
ID, seeding helpers and, where it can, fault injection. Tests needing a
fault the rig cannot inject are skipped, and the skip is visible. The same
classes run against real connectors in live validation and set the
capability level.
