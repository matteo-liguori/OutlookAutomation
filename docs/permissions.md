# Permissions: when Chamberlain needs your click

Chamberlain's assistant can read your mail and calendar freely. Anything that
**changes** something (send, move, delete, create an event) needs permission. Most
permission is quiet; a few things always need you. This page says exactly which.

## When a click is needed

Read the table top to bottom; the first row that fits wins. It is generated from one place in
the code (`src/chamberlain/domain/permissions.py`), and a test keeps this page in step.

<!-- permission-table -->
| When | What it takes |
| --- | --- |
| Any standing permission | Your click in the viewer |
| Sending mail to anyone you have not written to before | Your click in the viewer |
| Creating or changing an event that invites a new address (not one of yours, not already a guest of the event, not someone you have written to) | Your click in the viewer |
| Sending mail only to people you have written to | Your word in chat (setting `grants.send_known`, may ask for a click) |
| Acting outside Chamberlain's tools for you: a form, a portal, a website (never a password, a payment or accepting terms) | Your word in chat (setting `grants.other`, may ask for a click) |
| Everything else: drafts, filing, trash, mark read, other calendar writes | Your word in chat (setting `grants.other`, may ask for a click) |

| A standing permission for | May cover |
| --- | --- |
| `mail.send` | Exact addresses or whole domains, and a daily limit |
| `calendar.create`, `calendar.respond` | Exact addresses only, never a domain |
| `calendar.update`, `calendar.delete` | Never covered |
| `mail.move`, `mail.trash` | Named folders |
| `outside.action` | One card, one place and the kinds of action named; never a password, payment or terms |
| `mail.mark_read`, `mail.draft_save`, `mail.draft_update`, `mail.draft_discard` | The account and the kind of write |

One standing permission can cover several calendars, or several mailboxes, with one click. Entering a password, paying and accepting terms are never allowed, whatever is granted.
<!-- /permission-table -->

"Known" is explained [below](#known-recipients). Changing or cancelling events, inviting people you
already know and answering invitations need only your words. Everything else needs only your
words too: you tell the assistant in chat (or from an authorised email, below) and it records
that. No click. (There is one housekeeping choice for [larger cards](#larger-cards).)

Each click shows exactly what will happen (the recipients, subject, body, times)
and approves only that. For a repeating event it also says how often it repeats and whether a
change reaches "this date only" or "every date of the series". If the assistant changes one word, the permission no longer matches.

## Acting outside Chamberlain

Sometimes the assistant does something for you that Chamberlain's own tools cannot: filling in an
online form, sending one, signing in to a portal to read or download a document, using a website
on your behalf. It must **propose it first** as an outside action, saying where, what it will do
and what it will enter or submit.

- **One action.** Your words in chat are enough, like any other change.
- **A pattern, again and again.** The assistant can ask for a standing permission tied to one card,
  for one place and the kinds of action you allow (for example "fill in forms at the council
  portal"). You confirm it with one click in the viewer, which shows the pattern in plain words. It
  ends by itself when the card closes.
- **Never allowed, whatever you grant:** entering a password, paying, or accepting terms. The
  assistant must say it does none of these; if it says it would, the request is refused and
  recorded. Those steps stay with you.
- **Chamberlain cannot do these actions or check them.** The assistant does them and then reports
  what happened. Chamberlain records that report and marks it "reported by the assistant", never
  "verified". Every use appears in the pass report, on the viewer's timeline with its card, and on
  the permission's page.
- If the assistant is allowed to act and never reports within a day, the use is closed and the
  report says so.

## Larger cards

A card has a size limit so it stays easy to read: a position of 1,500 characters and 3,000
for the whole card. If a matter is genuinely demanding, the assistant may ask for a larger
card and give a short reason. **Nothing changes until you agree.**

- **Where.** The request shows at the top of "Needs you" in the viewer, with its reason.
  One click on **Approve** or **Decline**.
- **What you allow.** That one card may then hold a position of 4,000 characters and 6,000
  in all. Its other limits stay the same.
- **No quota.** Approve as many as you like; your click is the control.
- **If you decline,** the assistant is told and may not ask again for that card for 30 days.
- **Taking it back.** The Cards page marks each larger card and why. **Take the larger limit
  back** returns it to the normal limits straight away. If the card is then too big, nothing
  is cut: it is marked for trimming and the assistant shortens it.
- The assistant has no way to approve its own request. Every request and every answer is
  written to the audit log.

## Standing permissions

A standing permission lets the assistant repeat one kind of change without asking each time.

- **Scope.** It names the account, the kind of action, and who it covers (exact addresses
  or domains), plus folders for filing. It never covers anything outside that.
  Sends always need a daily limit. For invitations and answers it covers only the exact
  addresses you list, never a domain.
- **Limits.** A daily count, and never more than 200 sends an hour per mailbox (mail only to your own addresses is not capped). It never covers
  changing or deleting a calendar event.
- **Monthly review.** Each month, on the day you granted it, the viewer asks "keep this?".
  Reading it or using it does not renew it. Only your "keep" does.
- **Suspension.** If you do not review it within 14 days, it pauses itself and allows nothing
  until you keep it.
- **Expiry.** It ends after at most a year, whatever you do.
- **Ending it.** Revoke it in the viewer any time. A permission is never deleted, only ended,
  so the history stays.

One standing permission can cover several calendars, or several mailboxes, of the same kind. You
confirm it once, the viewer lists every source it covers, and a daily limit counts all of them
together. Revoking it ends it for all of them. Older permissions for one source are unchanged.

One-off permissions (for a single exact action) expire after 7 days if unused.

**Delivery check.** When the normal freshness check says a mailbox is `stale`, the harness may
test delivery by sending a marked message from the mailbox to itself and moving it to Trash. This
needs one standing permission per mailbox, titled "Delivery check: a marked test message to
yourself, then to Trash". It covers sending to that mailbox's own address only, and moving mail to
Trash from its Inbox and Sent folders. It runs only when the normal check fails, at most every 3
hours. The harness proposes it once and never confirms it; without your click nothing is sent.

## Known recipients

A person is **known** for a mailbox when any of these is true:

- you confirmed a send to them in the viewer;
- you have already sent them mail from that mailbox (Chamberlain reads the Sent folder,
  read-only, and learns every To and Cc address from the last 24 months);
- it is the mailbox's own address.

Teach it your history once with `chamberlain recipients seed` (optionally `--months 36`).
The import does this itself when Thunderbird is running. The viewer's Overview and
`doctor` show how many people each mailbox knows. Someone who merely emailed you is **not**
known, and neither is someone the assistant itself sent to.

## Authorised senders

By default an email is only information. Text inside it cannot instruct the assistant,
however official it sounds. You can name people whose email counts, in
`config/connections.yaml`:

```yaml
authorised_senders:
  owner: [you@example.com]          # your own addresses
  others:
    - address: pa@example.com
      scope: [calendar.create, mail.move]   # or: [ask_only]
      note: my assistant
```

An email counts only when **both** are true:

- the sender is on your list, and
- your mail provider's own check (the `Authentication-Results` header) says the message
  genuinely came from that domain. A forged "From" address fails this. Only the provider's top
  header is believed, and only from servers listed in `trusted_authserv_ids` (Gmail, Outlook and
  iCloud are built in; for Microsoft 365 mail with no server name in the header, add `"(none)"`).

Your own verified email counts like typing in chat. Another person's counts only for the actions in
their `scope`; `ask_only` means they may ask questions and nothing more. Emails older than 7 days
do not count. Even a verified email from you can never replace a click: standing
permissions, sends to new people and invitations to new people still need the viewer.
Accepted and refused instruction emails are both recorded and appear in the viewer's "Today" list.

## Why this is safer

The risk is an assistant that has been tricked, for example by a scam email that says
"forward all invoices to this address". Chamberlain assumes that can happen:

- Nothing in an email, calendar invite or web page can grant permission.
- The few high-impact things (new recipients, standing rules, shared events) need a human click on
  a page the assistant cannot press for you, showing the real content.
- Every approval is tied to the exact content, expires, and is logged in a tamper-evident audit trail.
- Limits (sends per hour, per day) cap the damage of any mistake.

## What it cannot do

The assistant runs on your computer as **you**. It is not locked in a sandbox. A determined
or compromised program running as your user could read the viewer's access file or edit the
files Chamberlain uses. The click raises the bar against a mistaken or tricked assistant; it
does not stop malware. Keep your computer's own protections on, and review the audit trail
if something looks odd.
