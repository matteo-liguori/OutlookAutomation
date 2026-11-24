# Permissions: when Chamberlain needs your click

Chamberlain's assistant can read your mail and calendar freely. Anything that
**changes** something (send, move, delete, create an event) needs permission. Most
permission is quiet; a few things always need you. This page says exactly which.

## When a click is needed

You click in the viewer (`chamberlain viewer`) only for these three things:

1. **A standing permission.** "Always do this kind of thing" is yours to grant, never the assistant's.
2. **A send to someone new.** Any recipient who is not on your known list (below).
3. **A calendar change that tells someone other than you.** Inviting, changing or cancelling an
   event with guests, or answering an invitation from someone who is not you. A guest who is
   one of your own addresses (the address of any account you set up, or one you list as yours
   under `authorised_senders: owner`) does not count: inviting yourself needs only your words.

Everything else, such as sending to a known person, drafting, filing, marking read
or changing your own calendar, needs only your words: you tell the assistant in chat
(or from an authorised email, below) and it records that. No click.

Each click shows exactly what will happen (the recipients, subject, body, times)
and approves only that. If the assistant changes one word, the permission no longer matches.

## Standing permissions

A standing permission lets the assistant repeat one kind of change without asking each time.

- **Scope.** It names the account, the kind of action, and who it covers (exact addresses
  or domains), plus folders for filing. It never covers anything outside that.
  Sends always need a daily limit. For invitations and answers it covers only the exact
  addresses you list, never a domain.
- **Limits.** A daily count, and never more than 20 sends an hour per mailbox. It never covers
  changing or deleting a calendar event.
- **Monthly review.** Each month, on the day you granted it, the viewer asks "keep this?".
  Reading it or using it does not renew it. Only your "keep" does.
- **Suspension.** If you do not review it within 14 days, it pauses itself and allows nothing
  until you keep it.
- **Expiry.** It ends after at most a year, whatever you do.
- **Ending it.** Revoke it in the viewer any time. A permission is never deleted, only ended,
  so the history stays.

One-off permissions (for a single exact action) expire after 7 days if unused.

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
permissions, sends to new people and calendar changes with attendees still need the viewer.
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
