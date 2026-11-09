# Outlook Automation

A collection of tools that let AI coding assistants like Codex and Claude work with Outlook emails, calendars and follow-ups.

I originally built this for my own use, mainly to make managing emails and keeping track of ongoing conversations a bit less painful. I’ve since started sharing it with friends and expanding it to work across different platforms.

It’s still very much a work in progress, but quite a lot of the functionality is already there.

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

macOS support has been added more recently, using native Outlook integration rather than the Windows COM interface.

The Mac implementation has already been tested successfully for reading and searching emails, handling attachments, managing drafts and working with calendars.

There are still a few limitations:

* Moving emails between folders isn’t working reliably yet.
* Automatic self-mail delivery passed the latest test; each send still needs independent delivery verification. See [Mac test status](outlook_platforms/macos/STATUS.md).
* Calendar updates and removal can race with synchronization; calendar writes remain outside the normal interface.
* Testing so far has mainly involved a Gmail account in Legacy Outlook. Microsoft 365, Exchange and New Outlook still need more testing.

Both platforms share the same underlying database and assistant interface, although not every operation is available on both yet.

## Getting started

You’ll need Python 3.12 or later, Outlook configured with your accounts, and a coding assistant capable of running local commands.

The setup guides are here:

* [Windows setup](packaging/README.md)
* [Mac setup](packaging/macos/README.md)
* [Platform adapters and commands](outlook_platforms/README.md)
* [Instructions for AI assistants](AGENTS.md)

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
* Adding a simple graphical interface for tracked conversations, tasks and activity.
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
