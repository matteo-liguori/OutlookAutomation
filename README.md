# Outlook Automation

A collection of tools that let AI coding assistants like Codex and Claude work with Outlook emails, calendars and follow-ups.

I originally built this for my own use, mainly to make managing emails and keeping track of ongoing conversations a bit less painful. I’ve since started sharing it with friends and expanding it to work across different platforms.

This is an **alpha**: features and architecture are still changing, and updates can require migration. Quite a lot of the functionality is already there.

## What it does

The idea is to give an AI assistant access to your emails and calendars, along with a way to remember what it’s been working on between sessions.

Some of the main features:

* Email: Search and read messages, inspect folders, retrieve attachments and organise emails.
* Follow-ups: Keep track of ongoing conversations, deadlines, things that need replies and other outstanding tasks.
* Calendar: Read calendars and help manage appointments, with optional Google Calendar and iCloud support.
* Drafting and sending: Prepare replies, save drafts and send emails with configurable permissions and checks to avoid accidental or duplicate actions.
* Memory: A local SQLite database stores tracked conversations, actions and other useful context so the assistant doesn’t have to start from scratch every time.

Most of the functionality is exposed through a shared command interface, so assistants can use the same tools regardless of the underlying platform.

## Windows and Mac support

Windows is the original and more developed implementation. It uses Classic Outlook through COM.

macOS has native Outlook and Thunderbird adapters behind the same `monitor outlook` commands. The private connection profile selects the client per source.

Thunderbird has live Gmail/KCL mail checks for reads, attachments, drafts, sends, filing, recoverable Trash and freshness; Gmail CalDAV calendar writes and DST recurrence have also been tested. KCL calendar connectivity, invitation responses and recurrence exceptions remain limited. Native Mac Outlook has separate filing/synchronisation limitations. See [platform status](monitor/adapters/README.md) for exact coverage; a listed capability still needs applicable authority.

## Getting started

You’ll need Python 3.12 or later, Outlook or Thunderbird configured with your accounts, and a coding assistant capable of running local commands.

The setup guides are here:

* [Windows setup](docs/setup/windows.md)
* [Mac setup](docs/setup/macos.md)
* [Platform adapters and commands](monitor/adapters/README.md)
* [Instructions for AI assistants](AGENTS.md)
* [Daily automation and follow-up prompts](docs/automations.md)
* [Customisation, private actions and experiments](docs/customisation.md)
* [Tagged updates](docs/updating.md)

Once everything is configured, the assistant can interact with the toolkit through:

Windows:

```powershell
.\monitor.ps1 outlook status
```

macOS:

```sh
.venv/bin/python -m monitor outlook status
```

These commands show which Outlook operations are available on your installation.

### Visual view

Install the optional viewer in the same project environment:

```sh
python -m pip install -r packaging/requirements/requirements-ui.lock
python -m monitor ui
```

Use `.venv/bin/python` on Mac or `.venv\Scripts\python.exe` on Windows for
`python` above; `monitor.ps1 ui` also launches it on Windows.
The local browser view shows a card grid and a linked action timeline, with
consistent card colours, search and filters. Click a card or action for details.
It reads current SQLite records without scanning Outlook. Open a card to edit its
name or recognition/goal, or leave a note for the agent. Notes appear in the next
briefing and stay pending until handled. Label edits preserve task/review clocks;
conflicting edits keep your text for review. Keep the process running while using it;
Ctrl+C stops it. Private data stays in its configured location.

You can also point your coding assistant to [AGENTS.md](AGENTS.md), which explains how to use the tools and maintain the local records.

I’d recommend starting with a few simple read-only operations to check that everything is working before trying drafts, sending or other changes.

## Personal data and permissions

Your emails, credentials, personal settings and local database are kept outside the shared Git repository.

The toolkit also has a permissions system for actions like sending messages or making changes to your mailbox.

One thing worth remembering: although the records are stored locally, information retrieved by your AI assistant may still be processed by whichever AI service you’re using.

## Things I’m working on

A few things I’d like to improve or add:

* Getting the Mac implementation working more reliably across Outlook versions and account types.
* Fixing email filing and improving send verification.
* Making installation and configuration easier.
* Improving the card grid and action timeline.
* Improving testing and general reliability.

No particular timeline for any of these. I’ll work on them as I get around to them.

## Contributions

This is a small personal project, but suggestions, fixes and improvements are very welcome.

Feel free to open an issue or pull request if you find a bug, have an idea or want to contribute something.

If you’re reporting a problem, it’s helpful to mention your operating system, Outlook version and what you were trying to do.

Just remember to leave any private emails, account details or credentials out of anything you upload.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE.md).

You’re welcome to use, modify and share the project for noncommercial purposes and the other uses permitted by the licence.

For commercial uses outside those permissions, just get in touch.
