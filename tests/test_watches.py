"""Synthetic keyword cues; no mailbox, calendar or reminder mutations."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from monitor.core import Harness
from monitor.watches import put, cues
from monitor.__main__ import parser
from monitor.commands import execute


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name)/'monitor.db', create=True)
        self.h.put_evidence(dict(id='authority', source='test', body='Synthetic standing authority'))
        self.rule = dict(id='destination-keys', terms=['Testville', 'XYZ'],
                         text='If travelling there, arrange a keys reminder.', evidence_ids=['authority'])
        put(self.h, self.rule)

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_broad_case_insensitive_matches_and_literal_boundaries(self):
        for data in [{'Subject':'Visit TESTVILLE'}, {'Body':'XYZ is mentioned'},
                     {'events':[{'title':'Return from Testville'}]}]:
            self.assertEqual(len(cues(self.h, data, False)), 1)
        self.assertEqual(cues(self.h, {'title':'XYZ123'}, False), [])
        put(self.h, dict(self.rule, terms=['a+b']))
        self.assertEqual(len(cues(self.h, 'a+b', False)), 1)
        self.assertEqual(cues(self.h, 'aaab', False), [])

    def test_once_per_chat_version_and_restart(self):
        with patch.dict(os.environ, CODEX_THREAD_ID='synthetic-watch-chat'):
            self.assertEqual(len(cues(self.h, 'XYZ')), 1)
            self.h.close(); self.h = Harness(Path(self.tmp.name)/'monitor.db')
            self.assertEqual(cues(self.h, 'XYZ'), [])
            put(self.h, dict(self.rule, text='Updated positive cue.'))
            self.assertEqual(len(cues(self.h, 'XYZ')), 1)
        with patch.dict(os.environ, CODEX_THREAD_ID='different-synthetic-chat'):
            self.assertEqual(len(cues(self.h, 'XYZ')), 1)

    def test_disabled_and_invalid(self):
        put(self.h, dict(self.rule, enabled=False))
        self.assertEqual(cues(self.h, 'XYZ', False), [])
        for change in [dict(terms=[]), dict(terms=['']), dict(enabled='yes'), dict(evidence_ids=[])]:
            with self.assertRaises(ValueError):put(self.h, dict(self.rule, **change))

    def test_actual_command_data_injects_cue_and_leaves_no_match_copy(self):
        self.h.put_record('context', dict(id='trip-reference', entity='Testville', text='XYZ visit', evidence_ids=['authority']))
        result = execute(self.h, parser().parse_args(['briefing']))
        # A relevant card is the ordinary way a trip enters compact briefing data.
        self.h.update(dict(id='trip', expected_revision=0, idempotency_key='trip-fixture', evidence_ids=['authority'],
            case=dict(title='Testville trip', purpose='Synthetic travel', context='XYZ', position='Upcoming', status='open'), actions=[]))
        result = execute(self.h, parser().parse_args(['briefing']))
        self.assertEqual(len(result['watch_prompts']), 1)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM meta WHERE key LIKE '%match%'").fetchone())
