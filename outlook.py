"""Dispatch existing Outlook command names to the appropriate platform adapter."""
import argparse
import platform

COMMANDS = ['Inspect-Outlook', 'Read-OutlookMail', 'Get-OutlookMessage', 'Read-OutlookMonitorCalendar', 'List-OutlookCalendars']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=['macos', 'windows'])
    parser.add_argument('command', choices=COMMANDS)
    args, remaining = parser.parse_known_args(argv)
    selected = args.platform or {'Darwin':'macos', 'Windows':'windows'}.get(platform.system())
    if selected == 'macos':
        from outlook_platforms.macos.commands import main as run
    elif selected == 'windows':
        from outlook_platforms.windows.commands import main as run
    else:
        parser.error('This platform is not supported')
    return run(args.command, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
