"""Synthetic retention/migration tests. No Outlook, live files or calendar writes."""

import copy
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.core import Harness, MAIL, dumps
from monitor.maintenance import clean, daily, register_file, safe_file, status, verified_backup
from monitor.__main__ import parser, execute

AT = "2026-10-14T12:00:00+00:00"
OLD = "2026-01-01T12:00:00+00:00"


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.h = Harness(self.root / "monitor.db", create=True)
        self.h.put_evidence(dict(id="proof", source="test", body="Original evidence"))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def age(self, kind, item_id, at=OLD):
        with self.h.db:
            self.h.db.execute(
                "UPDATE retention_activity SET at=? WHERE kind=? AND id=?", (at, kind, item_id)
            )

    def technical(self, eid, kind="collector-page", body="Raw diagnostic"):
        self.h.put_evidence(dict(id=eid, kind=kind, source="test", body=body))
        self.age("evidence", eid)

    def candidate(self, cid="message", **changes):
        data = dict(
            kind="candidate",
            id=cid,
            source=MAIL[0],
            entry_id=cid,
            store_id="store",
            subject="Example",
            sender="test@example.invalid",
            received="2026-01-01T11:00:00+00:00",
            message_class="IPM.Note",
            evidence_ids=["proof"],
            disposition="routine",
            review_reason="Reviewed",
        )
        data.update(changes)
        return data

    def old_run(self, candidates=()):
        rid = self.h.start_run(at=OLD)["id"]
        for c in candidates:
            self.h.record_run(rid, c)
        with self.h.db:
            run = self.h.run(rid)
            run["details"]["last_verification"] = dict(
                id=rid,
                status="partial",
                fully_verified=False,
                failures=["mail:personal: unavailable"],
                pending_candidates=1,
                action_coverage={"missing_action_ids": []},
            )
            self.h.db.execute(
                "UPDATE runs SET status='partial',details=? WHERE id=?",
                (dumps(run["details"]), rid),
            )
        self.age("run", rid)
        return rid

    def scratch(self, name, kind="temporary", **kwargs):
        path = self.root / "scratch" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Disposable fixture", encoding="utf-8")
        register_file(self.h, path, kind, **kwargs)
        self.age_file(path)
        return path

    def age_file(self, path):
        os.utime(path, (946684800, 946684800))
        row = self.h.db.execute(
            "SELECT details FROM maintenance_files WHERE path=?", (str(path.resolve()),)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            data["at"] = OLD
            with self.h.db:
                self.h.db.execute(
                    "UPDATE maintenance_files SET details=? WHERE path=?",
                    (dumps(data), str(path.resolve())),
                )

    def test_message_versions_shared_and_commands_hydrate(self):
        c = self.candidate()
        for _ in range(3):
            rid = self.h.start_run()["id"]
            self.h.record_run(rid, c)
        self.assertEqual(
            self.h.db.execute("SELECT count(*) FROM message_versions").fetchone()[0], 1
        )
        raw = self.h.db.execute("SELECT details FROM candidates LIMIT 1").fetchone()[0]
        self.assertNotIn("subject", json.loads(raw))
        self.assertEqual(self.h.decode_message(raw), c)
        output = execute(self.h, parser().parse_args(["run", "candidates", rid]))
        self.assertEqual(output["items"], [c])
        changed = dict(c, subject="Changed")
        self.h.record_run(rid, changed)
        self.assertEqual(
            self.h.db.execute("SELECT count(*) FROM message_versions").fetchone()[0], 2
        )
        self.assertFalse(
            self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone()
        )


    def test_dry_run_changes_no_database_or_files(self):
        path = self.scratch("old.json")
        rid = self.old_run([self.candidate()])
        before = self.h.db.iterdump()
        before = list(before)
        result = clean(self.h, dry_run=True, at=AT)
        self.assertEqual(result["files_eligible"], 1)
        self.assertEqual(list(self.h.db.iterdump()), before)
        self.assertTrue(path.exists())
        self.assertFalse(self.h.run(rid)["details"].get("retention"))

    def test_known_scratch_deleted_unknown_files_and_documents_retained(self):
        path = self.scratch("old.json")
        unknown = path.with_name("unknown.json")
        unknown.write_text("Unknown ownership")
        self.age_file(unknown)
        document = path.with_name("declaration.pdf")
        document.write_bytes(b"User document")
        self.age_file(document)
        with self.assertRaisesRegex(ValueError, "Documents"):
            register_file(self.h, document)
        result = clean(self.h, at=AT)
        self.assertEqual(result["files_deleted"], 1)
        self.assertFalse(path.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(document.exists())

    def test_referenced_modified_and_active_scratch_retained(self):
        referenced = self.scratch("reference.json")
        self.h.put_evidence(dict(id="file-ref", body=str(referenced)))
        modified = self.scratch("modified.json")
        modified.write_text("Changed")
        self.age_file(modified)
        rid = self.old_run()
        active = self.scratch("active.json", run_id=rid)
        self.age("run", rid, AT)
        clean(self.h, at=AT)
        self.assertTrue(all(p.exists() for p in [referenced, modified, active]))

    def test_preview_source_is_retained_and_checked(self):
        source = self.root / "inventory.pdf"
        source.write_bytes(b"Retained document")
        preview = self.scratch("preview.png", kind="preview", source=source)
        clean(self.h, at=AT)
        self.assertFalse(preview.exists())
        self.assertTrue(source.exists())
        preview = self.scratch("preview2.png", kind="preview", source=source)
        source.write_bytes(b"Changed document")
        clean(self.h, at=AT)
        self.assertTrue(preview.exists())

    def test_unsafe_paths_and_links_refused(self):
        with self.assertRaises(ValueError):
            safe_file(self.root, self.root.parent / "outside.json")
        path = self.scratch("linked.json")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p == path or original(p)):
            self.assertEqual(clean(self.h, dry_run=True, at=AT)["files_eligible"], 0)
        self.assertTrue(path.exists())
        with self.assertRaisesRegex(ValueError, "Live database"):
            register_file(self.h, self.h.path, "recovery")

    def test_relative_file_references_are_protected(self):
        path = self.scratch("referenced.json")
        self.h.put_evidence(
            dict(id="relative-ref", body="Stored at .local/monitor/scratch/referenced.json")
        )
        clean(self.h, at=AT)
        self.assertTrue(path.exists())

    def test_failed_file_delete_keeps_warning_and_retries_safely(self):
        path = self.scratch("busy.json")
        original = Path.unlink

        def unlink(p, *args, **kwargs):
            if p == path:
                raise PermissionError("File in use")
            return original(p, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            result = clean(self.h, at=AT)
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(path.exists())
        self.assertTrue(status(self.h)["warning"])
        self.assertEqual(clean(self.h, at=AT)["files_deleted"], 1)

    def test_backup_rotation_enforces_two_two_one_despite_old_references(self):
        names = (
            [f"daily-2025-01-{n:02}" for n in range(1, 10)]
            + [f"weekly-2025-{n:02}" for n in range(1, 7)]
            + [f"schema-test-{n}" for n in range(1, 5)]
        )
        seed = self.root/'seed.db'
        out=sqlite3.connect(seed);self.h.db.backup(out);out.close()
        old_path=self.root/'backups'/'schema-test-1.db'
        self.h.put_evidence(dict(id='old-backup-reference',source='test',body=str(old_path)))
        paths = []
        for n, name in enumerate(names):
            p = self.root/'backups'/(name+'.db')
            shutil.copy2(seed,p)
            os.utime(p, (946684800 + n, 946684800 + n))
            paths.append(p)
        corrupt = self.root / "backups" / "schema-corrupt.db"
        corrupt.write_bytes(b"Broken recovery file")
        self.h.backup()
        self.assertEqual(len(list((self.root / "backups").glob("daily-*.db"))), 2)
        self.assertEqual(len(list((self.root / "backups").glob("weekly-*.db"))), 2)
        self.assertEqual(sum(p.exists() for p in paths[15:]), 1)
        self.assertFalse(corrupt.exists());self.assertFalse(old_path.exists())
        self.assertTrue(verified_backup(paths[-1]))

    def test_new_version_checkpoint_replaces_previous_and_rotation_retries_failures(self):
        first=Path(self.h.backup('schema-first')[0])
        original=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==first:raise PermissionError('Busy backup')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink):
            second=Path(self.h.backup('schema-second')[0])
        self.assertTrue(first.exists());self.assertTrue(second.exists())
        self.assertTrue(status(self.h)['backup_rotation']['failures'])
        self.assertTrue(any(i.get('backup_rotation_warning') for i in self.h.current_items(True)))
        self.h.backup()
        self.assertFalse(first.exists());self.assertTrue(second.exists())
        self.assertFalse(status(self.h)['backup_rotation']['failures'])
        db=sqlite3.connect(second)
        try:self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
        finally:db.close()

    def test_development_recovery_requires_validation_and_identical_backup(self):
        backup = Path(self.h.backup("retained-recovery")[0])
        recovery = self.root / "scratch" / "before.db"
        recovery.parent.mkdir(exist_ok=True)
        shutil.copy2(backup, recovery)
        register_file(self.h, recovery, "recovery", recovery_backup=backup)
        self.age_file(recovery)
        self.assertEqual(clean(self.h, dry_run=True, at=AT)["files_eligible"], 0)
        register_file(self.h, recovery, "recovery", recovery_backup=backup, validated_at=OLD)
        self.age_file(recovery)
        clean(self.h, at=AT)
        self.assertFalse(recovery.exists())
        self.assertTrue(backup.exists())

    def test_daily_claim_uses_local_date_and_failure_does_not_change_run(self):
        rid = self.old_run()
        original = self.h.run(rid)
        with patch("monitor.maintenance.clean", side_effect=OSError("Disk unavailable")) as run:
            with patch("monitor.maintenance.now", return_value="2026-10-24T23:30:00+00:00"):
                daily(self.h)
            with patch("monitor.maintenance.now", return_value="2026-10-25T00:30:00+00:00"):
                daily(self.h)
            self.assertEqual(run.call_count, 1)
            with patch("monitor.maintenance.now", return_value="2026-10-26T00:01:00+00:00"):
                daily(self.h)
            self.assertEqual(run.call_count, 2)
        self.assertEqual(self.h.run(rid), original)
        self.assertIn("Disk unavailable", status(self.h)["warning"]["error"])

    def test_daily_cleanup_failure_does_not_change_finalize_result(self):
        rid = self.h.start_run()["id"]
        with patch("monitor.maintenance.clean", side_effect=OSError("Disk unavailable")):
            result = self.h.finalize(rid)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(status(self.h)["warning"])

    def test_working_cleanup_failure_rolls_back_candidates_and_evidence(self):
        rid = self.old_run([self.candidate()])
        self.technical("orphan")
        original = self.h.doctor
        count = [0]

        def doctor():
            count[0] += 1
            result = original()
            if count[0] > 1:
                result.update(ok=False, problems=["Synthetic new reference failure"])
            return result

        with patch.object(self.h, "doctor", doctor):
            with self.assertRaisesRegex(ValueError, "rolled back"):
                clean(self.h, at=AT)
        self.assertFalse(self.h.run(rid)["details"].get("working_expired"))
        self.assertTrue(
            self.h.db.execute("SELECT 1 FROM candidates WHERE run_id=?", (rid,)).fetchone()
        )
        self.assertTrue(self.h.db.execute("SELECT 1 FROM evidence WHERE id='orphan'").fetchone())

    def test_compact_collection_receipt_resolves_metadata_and_retry_reuses_evidence(self):
        from test_simplification import SimplificationTests
        from monitor.collect import import_pages

        page, fresh = SimplificationTests.mail(self)
        rid = self.h.start_run(at=page["StartedAt"])["id"]
        for _ in range(2):
            result = import_pages(self.h, rid, MAIL[0], [page], page["StartedAt"], fresh)
        self.assertEqual(
            self.h.db.execute("SELECT count(*) FROM message_versions").fetchone()[0], 1
        )
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM evidence").fetchone()[0], 3)
        eid = result["evidence_ids"][0]
        row = self.h.db.execute("SELECT metadata,body FROM evidence WHERE id=?", (eid,)).fetchone()
        self.assertEqual(json.loads(row[0])["kind"], "collector-index")
        self.assertNotIn("Subject", json.loads(row[1])["Items"][0])
        shown = execute(self.h, parser().parse_args(["evidence", "show", eid]))["items"][0]
        self.assertEqual(shown["resolved_messages"][0]["subject"], "Original")

    def test_scratch_json_inputs_registered_only_after_success(self):
        path = self.root / "scratch" / "proof.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(dumps(dict(id="new-proof", body="Source text")), encoding="utf-8")
        execute(self.h, parser().parse_args(["evidence", "put", "--input", str(path)]))
        self.assertTrue(
            self.h.db.execute(
                "SELECT 1 FROM maintenance_files WHERE path=?", (str(path),)
            ).fetchone()
        )
        bad = path.with_name("bad.json")
        bad.write_text("{}")
        with self.assertRaises(ValueError):
            execute(self.h, parser().parse_args(["run", "record", "missing", "--input", str(bad)]))
        self.assertFalse(
            self.h.db.execute(
                "SELECT 1 FROM maintenance_files WHERE path=?", (str(bad),)
            ).fetchone()
        )


    def test_missing_or_corrupt_message_version_is_reported(self):
        rid = self.h.start_run()["id"]
        self.h.record_run(rid, self.candidate())
        with self.h.db:
            self.h.db.execute("UPDATE message_versions SET details='{}'")
        self.assertFalse(self.h.doctor()["ok"])
        with self.assertRaisesRegex(ValueError, "Corrupt"):
            execute(self.h, parser().parse_args(["run", "candidates", rid]))


if __name__ == "__main__":
    unittest.main()
