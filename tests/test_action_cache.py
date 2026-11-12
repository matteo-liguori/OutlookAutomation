"""Action completion releases local copies without disturbing unfinished work."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_card_retirement as retirement
from monitor.threadcache import put_message, register_attachment, folder, bind_assets, sweep


class ActionCacheTests(unittest.TestCase):
    setUp = retirement.CardRetirementTests.setUp
    tearDown = retirement.CardRetirementTests.tearDown
    create = retirement.CardRetirementTests.create
    replacement = retirement.CardRetirementTests.replacement
    complete = retirement.CardRetirementTests.complete
    approval = retirement.CardRetirementTests.approval
    def assets(self, owners):
        path = folder(self.h, 'A') / 'attachments' / 'receipt.pdf'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'local receipt fixture')
        asset = register_attachment(self.h, dict(case_id='A', path=str(path), action_ids=owners))
        msg = put_message(self.h, dict(case_id='A', action_ids=owners,
            message=dict(EntryId='mail', StoreId='store', Body='Cached full email',
                         CachedAttachments=[dict(path=str(path), sha256=asset['sha256'], downloaded=True)])))
        return path, asset, msg

    def test_completed_owner_purges_mail_and_attachment_on_open_card(self):
        path, _, _ = self.assets(['A-approval'])
        p = self.replacement(); self.complete(p); self.h.update(p)
        self.assertEqual(self.h.case('A')['status'], 'open')
        self.assertEqual(len(self.h.case('A')['actions']), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM thread_messages').fetchone()[0], 0)
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM thread_attachments').fetchone()[0], 0)

    def test_shared_owners_keep_copy_until_final_action_ends(self):
        path, _, _ = self.assets(['A-approval', 'A-payment'])
        p = self.replacement(); self.complete(p); self.h.update(p)
        self.assertTrue(path.exists())
        metadata = json.loads(self.h.db.execute('SELECT metadata FROM thread_messages').fetchone()[0])
        self.assertEqual(metadata['CacheActionIds'], ['A-payment'])
        p = self.replacement(key='finish-payment'); self.complete(p); self.h.update(p)
        self.assertFalse(path.exists())

    def test_pending_send_protects_assets_until_reconciled(self):
        path, _, _ = self.assets(['A-approval'])
        self.approval('send', 'approved')
        p = self.replacement(); self.complete(p); self.h.update(p)
        self.assertTrue(path.exists())
        self.approval('send', 'verified-sent')
        self.h.update(self.replacement(key='reconcile-send'))
        self.assertFalse(path.exists())

    def test_locked_file_is_visible_and_retried_after_completion(self):
        path, _, _ = self.assets(['A-approval'])
        original = Path.unlink
        def locked(target, *args, **kwargs):
            if target == path: raise PermissionError('File in use')
            return original(target, *args, **kwargs)
        p = self.replacement(); self.complete(p)
        with patch.object(Path, 'unlink', locked): self.h.update(p)
        self.assertTrue(path.exists())
        self.assertTrue(self.h.db.execute('SELECT 1 FROM thread_cleanup').fetchone())
        result = sweep(self.h)
        self.assertFalse(result['failures'])
        self.assertFalse(path.exists())

    def test_changed_file_and_escape_are_preserved(self):
        path, _, _ = self.assets(['A-approval'])
        path.write_bytes(b'changed content')
        p = self.replacement(); self.complete(p); self.h.update(p)
        self.assertTrue(path.exists())
        self.assertTrue(sweep(self.h)['failures'])
        elsewhere = Path(self.tmp.name) / 'important.pdf'; elsewhere.write_bytes(b'keep')
        with self.h.write():
            self.h.db.execute('UPDATE thread_attachments SET path=?', (str(elsewhere),))
        self.assertTrue(sweep(self.h)['failures'])
        self.assertTrue(elsewhere.exists())

    def test_explicit_release_and_invalid_owner_rollback(self):
        path, asset, msg = self.assets(['A-approval'])
        payload = dict(case_id='A', message_keys=[msg['message_key']],
            attachment_sha256=[asset['sha256']], action_ids=['other-card-action'],
            reason='Reconcile exact completed preparation', evidence_ids=['request'])
        with self.assertRaisesRegex(ValueError, 'unfinished actions'): bind_assets(self.h, payload)
        self.assertTrue(path.exists())
        bind_assets(self.h, dict(payload, action_ids=[]))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.h.case('A')['actions']), 2)

    def test_stale_completion_does_not_delete_assets(self):
        path, _, _ = self.assets(['A-approval'])
        p = self.replacement(); self.complete(p)
        self.h.update(self.replacement(key='intervening'))
        with self.assertRaisesRegex(ValueError, 'Stale revision'): self.h.update(p)
        self.assertTrue(path.exists())

    def test_startup_reconciles_interrupted_action_cleanup(self):
        from monitor.core import Harness
        path, _, _ = self.assets(['A-approval'])
        p = self.replacement(); self.complete(p)
        with patch('monitor.threadcache.sweep', return_value={}): self.h.update(p)
        self.assertTrue(path.exists())
        self.h.close(); self.h = Harness(Path(self.tmp.name) / 'monitor.db')
        self.assertFalse(path.exists())
