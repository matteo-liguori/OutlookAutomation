"""Bounded checks for maintainer-tagged main updates; never pull or alter local work."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request,urlopen
from urllib.parse import quote

from .paths import ROOT
from .configuration import resolve,location,atomic


def git(*args):
    result=subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,timeout=8)
    if result.returncode:raise ValueError('Git update metadata unavailable')
    return result.stdout.strip()


def github_compare(repository,base,head):
    request=Request('https://api.github.com/repos/'+repository+'/compare/'+quote(base,safe='')+'...'+quote(head,safe=''),headers={'Accept':'application/vnd.github+json','User-Agent':'OutlookAutomation-update-check'})
    with urlopen(request,timeout=5) as response:return json.load(response)


def version(tag,prefix):
    match=re.fullmatch(re.escape(prefix)+r'(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?',tag)
    if not match:return None
    major,minor,patch,stage,number=match.groups()
    return (int(major),int(minor),int(patch),{'alpha':0,'beta':1,'rc':2,None:3}[stage],int(number or 0))


def check(force=False,root=None,runner=git,compare=github_compare):
    folder=location(root)
    if folder is None:return dict(status='disabled',reason='No private update state selected')
    path=folder/'.state/update-check.json'
    previous=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    settings=resolve().values['updates']
    if not settings['enabled']:return dict(status='disabled')
    now=datetime.now(timezone.utc)
    try:
        installed=runner('rev-parse','HEAD')
        interval=min(settings['interval_hours'],1) if previous.get('status')=='offline' else settings['interval_hours']
        if not force and previous.get('installed')==installed and previous.get('checked_at') and now-datetime.fromisoformat(previous['checked_at'])<timedelta(hours=interval):return previous
        origin=runner('remote','get-url',settings['remote'])
        repo=re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?',origin)
        if not repo:raise ValueError('Tagged update checks require a GitHub origin')
        repository=repo[1];prefix=settings['tag_prefix']
        refs={ref:sha for sha,ref in (line.split() for line in runner('ls-remote',settings['remote'],'refs/heads/main','refs/tags/'+prefix+'*').splitlines())}
        candidates=[]
        for ref,sha in refs.items():
            if not ref.startswith('refs/tags/') or ref.endswith('^{}'):continue
            tag=ref[len('refs/tags/'):];key=version(tag,prefix)
            if key:candidates.append((key,tag,refs.get(ref+'^{}',sha)))
        result=dict(status='current',installed=installed,checked_at=now.isoformat(),notified=previous.get('notified'))
        if candidates:
            _,tag,target=max(candidates)
            # Only tags whose commit is on remote main qualify, not arbitrary branch tags.
            proof=compare(repository,target,refs['refs/heads/main'])
            if proof.get('status') not in ('ahead','identical'):raise ValueError('Latest update tag is not on main')
            try:
                runner('merge-base','--is-ancestor',target,'HEAD');installed_tag=True
            except ValueError:installed_tag=False
            result.update(tag=tag,target=target,status='current' if installed_tag else 'available',url='https://github.com/'+repository+'/releases/tag/'+quote(tag,safe=''))
        atomic(path,json.dumps(result,indent=2)+'\n');return result
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        result=dict(previous,status='offline',checked_at=now.isoformat(),error=str(exc))
        atomic(path,json.dumps(result,indent=2)+'\n');return result


def notice(root=None):
    result=check(root=root)
    if result.get('status')!='available' or result.get('notified')==result.get('tag'):return []
    result['notified']=result['tag']
    atomic(location(root)/'.state/update-check.json',json.dumps(result,indent=2)+'\n')
    return [dict(type='harness_update',tag=result['tag'],url=result['url'],instruction='A tagged main update is available. Offer to upgrade; preserve local work, review release notes, run migration preview and tests. No automatic pull was performed.')]
