"""One-off native-host registration and reproducible extension packaging."""
import argparse
import json
from pathlib import Path
import shlex
import sys
import zipfile

from automation_paths import ROOT, data_root

HOST = 'outlookautomation_thunderbird_pilot'
EXTENSION_ID = 'mail-pilot@outlookautomation.local'


def prepare(*, calendar=False, manifest_dir=None, replace_host_path=None):
    root = data_root() / 'thunderbird'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    addon = Path(__file__).with_name('addon')
    manifest = json.loads((addon / 'manifest.json').read_text())
    files = ['mail.js','background.js','client-schema.json','client-api.js']
    manifest['description']='Local mail harness bridge. Draft editing and sync use a privileged Experiment API.'
    manifest['experiment_apis']={'harnessMail':{'schema':'client-schema.json','parent':{
        'scopes':['addon_parent'],'paths':[['harnessMail']],'script':'client-api.js'}}}
    if calendar:
        manifest['name'] += ' and Calendar'
        manifest['description'] = 'Local mail and calendar harness bridge. Calendar access uses a privileged Experiment API.'
        manifest['experiment_apis']['harnessCalendar'] = {
            'schema': 'calendar-schema.json', 'parent': {
                'scopes': ['addon_parent'], 'paths': [['harnessCalendar']], 'script': 'calendar-api.js'}}
        files += ['calendar-schema.json', 'calendar-api.js']
    package = root / (('bridge-calendar-' if calendar else 'bridge-mail-')+manifest['version']+'.xpi')
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, indent=2)+'\n')
        for name in files:archive.write(addon / name, name)
    launcher = root / 'native-host'
    launcher.write_text('#!/bin/sh\ncd '+shlex.quote(str(ROOT))+' || exit 1\n'
                        'export OUTLOOK_AUTOMATION_DATA='+shlex.quote(str(data_root()))+'\n'
                        'exec '+shlex.quote(sys.executable)+' -m outlook_platforms.thunderbird.native_host\n')
    launcher.chmod(0o700)
    # Verified with the official macOS Thunderbird 159 build; not Application Support/Mozilla.
    directory = Path(manifest_dir) if manifest_dir else Path.home() / 'Library/Mozilla/NativeMessagingHosts'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (HOST+'.json')
    registration = dict(name=HOST, description='Local Thunderbird harness bridge',path=str(launcher),
                        type='stdio',allowed_extensions=[EXTENSION_ID])
    if target.exists():
        old = json.loads(target.read_text())
        if old.get('name') != HOST or old.get('allowed_extensions') != [EXTENSION_ID]:
            raise ValueError('Existing native host registration is unrelated; preserved')
        # Do not silently take another installation's live bridge.
        if old.get('path') != str(launcher):
            if replace_host_path is None or old.get('path')!=str(Path(replace_host_path).resolve()):
                raise ValueError('Native host belongs to another data directory; explicitly name --replace-host-path to migrate it')
            import uuid
            (root/('previous-native-host-'+uuid.uuid4().hex+'.json')).write_text(json.dumps(old,indent=2)+'\n')
    target.write_text(json.dumps(registration,indent=2)+'\n');target.chmod(0o600)
    return dict(package=str(package),native_manifest=str(target),calendar_experiment=calendar,client_mail_experiment=True,
                instruction='Install the XPI through Thunderbird Add-ons Manager once; keep Thunderbird running for commands.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calendar',action='store_true',help='Include optional privileged calendar module')
    parser.add_argument('--manifest-dir',type=Path,help='Override native-host directory for an isolated installation')
    parser.add_argument('--replace-host-path',type=Path,help='Explicit previous launcher path when migrating an existing registration')
    args=parser.parse_args()
    print(json.dumps(prepare(calendar=args.calendar,manifest_dir=args.manifest_dir,replace_host_path=args.replace_host_path),indent=2))


if __name__=='__main__':main()
