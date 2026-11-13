"""Installed smoke validation must fail on unusable configuration or health."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tools.smoke_harness import COMMANDS, run_smoke


class SmokeHarnessTests(unittest.TestCase):
    def result(self,data,returncode=0):
        return subprocess.CompletedProcess([],returncode,json.dumps(data),'')

    def test_real_configuration_error_blocks_success(self):
        failure=subprocess.CompletedProcess([],2,'',json.dumps({'error':'Removed/unknown setting needs migration: instructions.watches'}))
        with patch('tools.smoke_harness.subprocess.run',return_value=failure) as run:
            result=run_smoke(Path('.'))
        self.assertFalse(result['ok']);self.assertEqual(run.call_count,1)
        self.assertIn('instructions.watches',result['checks'][0]['error'])

    def test_paging_finishes_and_only_safe_commands_are_used(self):
        responses=[self.result({}),self.result({}),self.result({}),
                   self.result({'items':[], 'next_cursor':'next'}),
                   self.result({'items':[], 'next_cursor':None}),
                   self.result({'items':[{'ok':True}]})]
        with patch('tools.smoke_harness.subprocess.run',side_effect=responses) as run:
            result=run_smoke(Path('.'))
        self.assertTrue(result['ok']);self.assertEqual(result['checks'][3]['pages'],2)
        self.assertEqual(len(result['checks']),len(COMMANDS))
        for call in run.call_args_list:
            self.assertNotIn('collect',call.args[0]);self.assertNotIn('send',call.args[0])

    def test_doctor_failure_cannot_pass_on_exit_zero(self):
        responses=[self.result({})for _ in COMMANDS[:-1]]+[self.result({'items':[{'ok':False}]})]
        with patch('tools.smoke_harness.subprocess.run',side_effect=responses):
            self.assertFalse(run_smoke(Path('.'))['ok'])
