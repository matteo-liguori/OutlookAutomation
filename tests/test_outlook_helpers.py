import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(PS, "PowerShell unavailable")
class OutlookHelperTests(unittest.TestCase):
    def test_thread_cache_collects_reuses_and_explicitly_fetches_large_assets(self):
        from monitor.core import Harness
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);h=Harness(folder/'monitor.db',create=True)
            h.put_evidence(dict(id='u',source='user',body='Track task'))
            h.update(dict(id='case',expected_revision=0,idempotency_key='create',evidence_ids=['u'],case=dict(title='Task',position='Open',status='open'),actions=[dict(id='a',owner='Example user',status='needs-user',next_step='Review',completion_condition='Done',reopening_condition='New work')]))
            h.close()
            shutil.copyfile(ROOT/'monitor/adapters/windows/Get-OutlookMessage.ps1',folder/'Get-OutlookMessage.ps1')
            (folder/'OutlookAutomation.Common.ps1').write_text("function Get-OutlookAutomationNamespace { if(Test-Path (Join-Path $PSScriptRoot 'no-com')){throw 'Cache must avoid COM'}; return @{} }\nfunction Convert-OutlookComDate($value){return $value}",encoding='utf-8')
            (folder/'OutlookHarness.Common.ps1').write_text(". '"+str(ROOT/'monitor/adapters/windows/OutlookHarness.Common.ps1')+"'\nSet-Item Function:Invoke-FixtureHarness ${function:Invoke-OutlookHarness}\nfunction Invoke-OutlookHarness {param($Arguments,$InputObject) Invoke-FixtureHarness -Arguments $Arguments -InputObject $InputObject -Database '"+str(folder/'monitor.db')+"'}",encoding='utf-8')
            (folder/'OutlookMessageLookup.ps1').write_text(r'''
function Resolve-OutlookExactMessage {
    $small=[pscustomobject]@{FileName='small.pdf';Size=30}
    $large=[pscustomobject]@{FileName='large.pdf';Size=11MB}
    foreach($item in @($small,$large)){$item | Add-Member ScriptMethod SaveAsFile {param($path);[IO.File]::WriteAllText($path,$this.FileName)}}
    $attachments=[pscustomobject]@{Count=2;Values=@($small,$large)}
    $attachments | Add-Member ScriptMethod Item {param($index);return $this.Values[$index-1]}
    $message=[pscustomobject]@{EntryID='message';UnRead=$true;Attachments=$attachments;MessageClass='IPM.Note';Subject='Relevant';SenderName='Sender';SenderEmailAddress='sender@example.invalid';To='me@example.invalid';CC='';ReceivedTime='2026-10-26T09:00:00Z';Body='Useful café correspondence'}
    return [pscustomobject]@{Message=$message;ActualStoreId='store';FolderPath='Inbox';ReadPath='fixture';Scanned=1;FoldersScanned=1;DirectFailure=''}
}''',encoding='utf-8')
            args=['-File',folder/'Get-OutlookMessage.ps1','-EntryId','message','-StoreId','store','-CaseId','case','-Output','Json']
            first=self.run_ps(*args);self.assertEqual(first.returncode,0,first.stderr)
            data=json.loads(first.stdout);self.assertEqual(data['Body'],'Useful café correspondence')
            self.assertTrue(data['CachedAttachments'][0]['downloaded']);self.assertFalse(data['CachedAttachments'][1]['downloaded']);self.assertTrue(data['UnreadUnchanged'])
            (folder/'no-com').touch()
            cached=self.run_ps(*args);self.assertEqual(cached.returncode,0,cached.stderr)
            self.assertEqual(json.loads(cached.stdout)['CacheEvidenceId'],data['CacheEvidenceId'])
            (folder/'no-com').unlink()
            large=self.run_ps(*args,'-DownloadLargeAttachments');self.assertEqual(large.returncode,0,large.stderr)
            self.assertTrue(json.loads(large.stdout)['CachedAttachments'][1]['downloaded'])
            h=Harness(folder/'monitor.db')
            try:
                self.assertEqual(h.db.execute('SELECT count(*) FROM thread_messages').fetchone()[0],1)
                self.assertEqual(h.db.execute('SELECT count(*) FROM thread_attachments').fetchone()[0],2)
            finally:h.close()
            self.assertFalse(list(folder.rglob('*.json')))

    def run_ps(self, *args):
        return subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

    def test_monitoring_decisions(self):
        p = self.run_ps("-File", ROOT / "tests/Test-OutlookMonitoring.ps1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreaterEqual(json.loads(p.stdout)["assertions"], 20)

    def fixture(self, tmp, script, common):
        folder = Path(tmp)
        shutil.copyfile(ROOT / 'monitor/adapters/windows' / script, folder / script)
        shutil.copyfile(
            ROOT/'monitor/adapters/windows/OutlookMonitoring.Common.ps1', folder / "OutlookMonitoring.Common.ps1"
        )
        shutil.copyfile(ROOT/'monitor/adapters/windows/AutomationEnvironment.Common.ps1', folder / "AutomationEnvironment.Common.ps1")
        (folder / "OutlookAutomation.Common.ps1").write_text(common, encoding="utf-8")
        return folder / script

    def test_refresh_com_failure_records_result_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self.fixture(
                tmp,
                "Start-OutlookSendReceive.ps1",
                "function Get-OutlookAutomationNamespace { throw 'fixture COM unavailable' }",
            )
            result = Path(tmp) / "result.json"
            p = self.run_ps("-File", script, "-ResultPath", result)
            self.assertNotEqual(p.returncode, 0)
            data = json.loads(result.read_text(encoding="utf-8-sig"))
            self.assertFalse(data["success"])
            self.assertEqual(data["status"], "failed")
            self.assertIn("fixture COM unavailable", data["error"])
            self.assertNotIn("completed successfully", p.stdout)

    def test_refresh_stops_on_all_group_completion_or_error(self):
        common = r'''
Add-Type @"
using System;
using System.Collections.Generic;
public sealed class OutlookSyncObserver : IDisposable {
    static readonly List<OutlookSyncObserver> observers = new List<OutlookSyncObserver>();
    static int ticks;
    public static bool Fail;
    public string Name {get;private set;}
    public DateTime? StartedAt {get;private set;}
    public DateTime? EndedAt {get;private set;}
    public string[] Errors {get;private set;}
    public OutlookSyncObserver(object value,string name) {Name=name;Errors=new string[0];observers.Add(this);}
    public static void Begin() {foreach(var o in observers)o.StartedAt=DateTime.UtcNow;}
    public static void Pump() {
        if(++ticks==1)observers[0].EndedAt=DateTime.UtcNow;
        if(ticks==2) {observers[1].EndedAt=DateTime.UtcNow;if(Fail)observers[1].Errors=new[]{"fixture sync error"};}
    }
    public void Dispose() {}
}
"@
function Get-OutlookAutomationNamespace {
    $ns=[pscustomobject]@{Offline=$false;SyncObjects=@([pscustomobject]@{Name='first'},[pscustomobject]@{Name='second'})}
    $ns | Add-Member ScriptMethod SendAndReceive {param($all);if($global:triggered){throw 'Duplicate trigger'};$global:triggered=$true;[OutlookSyncObserver]::Begin()}
    $ns
}
'''
        for fail in (False,True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                script=self.fixture(tmp,'Start-OutlookSendReceive.ps1',common+('\n[OutlookSyncObserver]::Fail=$true' if fail else ''))
                path=Path(tmp)/'result.json'
                args=['-File',script,'-ResultPath',path]
                if fail:args+=['-WaitSeconds','600']
                p=self.run_ps(*args)
                data=json.loads(path.read_text(encoding='utf-8-sig'))
                self.assertEqual(data['wait_seconds'],600 if fail else 300)
                self.assertLess(data['elapsed_seconds'],3)
                self.assertEqual(len(data['groups']),2)
                self.assertTrue(all(g['StartedAt'] and g['EndedAt'] for g in data['groups']))
                self.assertEqual(data['success'],not fail)
                self.assertEqual(p.returncode==0,not fail,p.stdout+p.stderr)
                if fail:self.assertIn('fixture sync error',data['error'])

    def calendar(self, events, limit=300):
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.json"
            events_path.write_text(json.dumps(events), encoding="utf-8")
            common = r"""
$global:fixtureEvents=@(Get-Content (Join-Path $PSScriptRoot 'events.json') -Raw | ConvertFrom-Json)
function Get-OutlookAutomationNamespace { [pscustomobject]@{Fixture=$true} }
function Get-OutlookStoreByName($ns,$storeName){
    $items=[pscustomobject]@{IncludeRecurrences=$false;Index=0;Values=$global:fixtureEvents}
    $items | Add-Member ScriptMethod Sort {param($key)}
    $items | Add-Member ScriptMethod Restrict {param($filter)
        if($filter -notmatch 'Oct 2026' -or -not $this.IncludeRecurrences){throw 'Wrong filter or recurrences disabled'}
        return $this
    }
    $items | Add-Member ScriptMethod GetFirst { $this.Index=0;if($this.Values.Count){return $this.Values[0]};return $null }
    $items | Add-Member ScriptMethod GetNext { $this.Index++;if($this.Index -lt $this.Values.Count){return $this.Values[$this.Index]};return $null }
    $store=[pscustomobject]@{Items=$items}
    $store | Add-Member ScriptMethod GetDefaultFolder {param($id);[pscustomobject]@{Items=$this.Items}}
    return $store
}
"""
            script = self.fixture(tmp, "Read-OutlookMonitorCalendar.ps1", common)
            p = self.run_ps(
                "-File",
                script,
                "-Start",
                "2026-10-01T00:00:00",
                "-End",
                "2026-10-04T00:00:00",
                "-Limit",
                limit,
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout)

    def event(self, id, start, end, all_day=False):
        return dict(EntryID=id, Start=start, End=end, Subject=id, Location="", AllDayEvent=all_day)

    def test_calendar_rejects_wrong_month_without_claiming_completion(self):
        data = self.calendar(
            [
                self.event("wrong", "2026-06-01", "2026-06-02"),
                self.event("right", "2026-10-02", "2026-10-03", True),
            ]
        )
        self.assertFalse(data["complete"])
        self.assertEqual([e["id"] for e in data["events"]], ["right"])
        self.assertTrue(data["errors"])
        self.assertIn("01 Oct 2026", data["filter"])

    def test_calendar_recurrence_enabled_all_day_and_cap(self):
        events = [
            self.event("day", "2026-10-02", "2026-10-03", True),
            self.event("meeting", "2026-10-03T10:00", "2026-10-03T11:00"),
        ]
        good = self.calendar(events)
        self.assertTrue(good["complete"])
        self.assertEqual(len(good["events"]), 2)
        capped = self.calendar(events, 1)
        self.assertFalse(capped["complete"])
        self.assertIn("Calendar scan limit reached.", capped["errors"])

    def test_calendar_separates_cancelled_residuals_without_title_guessing(self):
        cancelled=[dict(self.event('cancel-'+str(status),'2026-10-02T09:00','2026-10-02T10:00'),MeetingStatus=status) for status in (5,7)]
        active=dict(self.event('replacement','2026-10-03T09:00','2026-10-03T10:00'),Subject='Canceled discussion: next steps',MeetingStatus=3)
        result=self.calendar(cancelled+[active])
        self.assertTrue(result['complete'])
        self.assertEqual([e['id'] for e in result['events']],['replacement'])
        self.assertEqual([e['meeting_status'] for e in result['explicit_cancellations']],[5,7])

    def test_restart_waits_with_immediately_ready_com(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = r"""
$global:fakeTimer=[pscustomobject]@{Elapsed=[pscustomobject]@{TotalSeconds=0.0}}
$global:startCount=0
function Get-OutlookAutomationNamespace { [pscustomobject]@{Stores=@('fixture')} }
function Assert-OutlookRestartSession { [pscustomobject]@{RestartEligible=$true;Status='active'} }
function Get-ItemProperty {param($Path,$ErrorAction);[pscustomobject]@{'(default)'=(Join-Path $PSScriptRoot 'OUTLOOK.EXE')}}
function Get-Process {param($Name,$ErrorAction);@()}
function Start-Process {param($FilePath,$WindowStyle);if($WindowStyle -ne 'Hidden'){throw 'Expected hidden startup'};$global:startCount++}
function Start-Sleep {param($Seconds=0,$Milliseconds=0);$global:fakeTimer.Elapsed.TotalSeconds += $Seconds + $Milliseconds/1000.0}
function Get-Date { [datetime]::Parse('2026-10-13T12:00:00').AddSeconds($global:fakeTimer.Elapsed.TotalSeconds) }
"""
            script = self.fixture(tmp, "Restart-ClassicOutlook.ps1", common)
            (Path(tmp) / "OUTLOOK.EXE").write_text("fixture only; never executed")
            with (Path(tmp) / "OutlookMonitoring.Common.ps1").open("a", encoding="utf-8") as f:
                f.write("\nfunction Start-OutlookInitializationTimer { $global:fakeTimer }\n")
            p = self.run_ps("-File", script)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            data = json.loads(p.stdout[p.stdout.index("{") :])
            self.assertGreaterEqual(data["initialized_seconds"], 60)
            self.assertTrue(data["success"])

    def test_access_decisions_and_exact_fallback(self):
        p = self.run_ps("-File", ROOT / "tests/Test-OutlookAccess.ps1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreaterEqual(json.loads(p.stdout)["assertions"], 14)

    def test_transport_signatures_process_guards_and_scan_caps(self):
        p = self.run_ps("-File", ROOT / "tests/Test-OutlookTransport.ps1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreaterEqual(json.loads(p.stdout)["assertions"], 15)

    def test_snapshot_continuation_survives_inbox_changes(self):
        p = self.run_ps("-File", ROOT / "tests/Test-OutlookSnapshot.ps1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_diagnostic_never_stops_unowned_outlook(self):
        source = (ROOT/'monitor/adapters/windows/Test-OutlookAutomationEnvironment.ps1').read_text(encoding="utf-8")
        self.assertNotIn("Stop-Process -Id $outlookProcess.Id", source)

    def test_secure_prompt_encryption_with_synthetic_credentials(self):
        p = self.run_ps("-File", ROOT / "tests/Test-CredentialPrompt.ps1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_freshness_stops_after_all_stores_have_stable_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = r"""
$global:elapsed=0
function Get-Date {[datetime]::Parse('2026-10-13T12:05:00').AddSeconds($global:elapsed)}
function Start-Sleep {param($Seconds);$global:elapsed += $Seconds}
function Convert-OutlookComDate($value){$value.ToString('yyyy-MM-dd HH:mm:ss')}
function Get-OutlookAutomationNamespace {[pscustomobject]@{Accounts=@();Offline=$false}}
function Get-OutlookStoreByName {param($ns,$store)
    $obj=[pscustomobject]@{StoreID=$store;Root=[pscustomobject]@{Folders=@()}}
    $obj|Add-Member ScriptMethod GetRootFolder {$this.Root};$obj
}
function Find-OutlookFolder {param($store,$folder)
    $items=[pscustomobject]@{Count=1}
    $items|Add-Member ScriptMethod Sort {param($key,$reverse)}
    $items|Add-Member ScriptMethod Item {param($index);[pscustomobject]@{EntryID='recent';ReceivedTime=[datetime]'2026-10-13T12:00:00';MessageClass='IPM.Note';Subject='mock'}}
    [pscustomobject]@{Items=$items;FolderPath='Inbox'}
}
"""
            script = self.fixture(tmp, "Test-OutlookMailboxFreshness.ps1", common)
            p = self.run_ps("-File", script, "-Store", "fixture", "-MaxWaitSeconds", "180")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            result = json.loads(p.stdout)
            self.assertTrue(result["Current"])
            self.assertEqual(result["Samples"], 2)
            self.assertEqual(result["WaitedSeconds"], 15)

    def test_restart_refuses_missing_session_before_process_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = "function Assert-OutlookRestartSession {throw 'desktop-session-unavailable fixture'}; function Get-Process {throw 'must not inspect processes'}; function Start-Process {throw 'must not launch'}"
            script = self.fixture(tmp, "Restart-ClassicOutlook.ps1", common)
            p = self.run_ps("-File", script)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("desktop-session-unavailable fixture", p.stderr)
            self.assertNotIn("must not launch", p.stderr)


if __name__ == "__main__":
    unittest.main()
