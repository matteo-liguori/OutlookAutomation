"""User settings must survive defaults updates without leaking across installations."""
from pathlib import Path
import json
import tempfile
import unittest

from monitor.configuration import defaults, sync, resolve, dump, read_yaml, atomic, notices, review, paths, snapshot, zone


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.public=self.root/'public.yaml'
        self.public.write_text(dump(defaults()),encoding='utf-8')
        self.user=self.root/'user'

    def init(self):sync(self.user,profile={},default_file=self.public)

    def test_prefilled_isolated_and_read_only_resolution(self):
        self.init();path,state=paths(self.user)
        self.assertEqual(read_yaml(path)['reviews']['card_days'],7)
        before=(path.read_bytes(),state.read_bytes())
        a=resolve(self.user,profile={},default_file=self.public)
        self.assertEqual(a.get('reviews.card_days'),7)
        self.assertEqual((path.read_bytes(),state.read_bytes()),before)
        other=self.root/'other';sync(other,profile={},default_file=self.public)
        settings=read_yaml(path);settings['reviews']['card_days']=2;atomic(path,dump(settings))
        self.assertEqual(resolve(other,profile={},default_file=self.public).get('reviews.card_days'),7)

    def test_three_way_update_preserves_comments_and_multiline_edits(self):
        self.init();path,_=paths(self.user)
        user=read_yaml(path);user['reviews']['card_days']=2
        user['instructions']['reporting']='My report\nTwo useful lines\n'
        atomic(path,'# Personal comment stays\n'+dump(user))
        new=read_yaml(self.public);new['reviews']['card_days']=5;new['reviews']['followup_days']=8
        new['instructions']['reporting']='New default report';atomic(self.public,dump(new))
        pending=sync(self.user,profile={},default_file=self.public)
        self.assertEqual(len(pending),2)
        result=resolve(self.user,profile={},default_file=self.public)
        self.assertEqual(result.get('reviews.card_days'),2);self.assertEqual(result.get('reviews.followup_days'),8)
        self.assertIn('# Personal comment stays',path.read_text(encoding='utf-8'))
        self.assertEqual(len(notices(self.user)),2);self.assertEqual(notices(self.user),[])
        ident=next(r['id'] for r in pending if r['key']=='reviews.card_days')
        review(self.user,ident,'default')
        self.assertEqual(read_yaml(path)['reviews']['card_days'],5)

    def test_invalid_configuration_and_card_scope(self):
        self.init();path,_=paths(self.user);settings=read_yaml(path)
        settings['timezone']='Invalid/Nowhere';atomic(path,dump(settings))
        with self.assertRaisesRegex(ValueError,'timezone'):resolve(self.user,profile={},default_file=self.public)
        settings['timezone']='Europe/London';settings['invented']=True;atomic(path,dump(settings))
        with self.assertRaisesRegex(ValueError,'Unknown|unknown'):resolve(self.user,profile={},default_file=self.public)

    def test_card_override_and_dynamic_timezone_schedule(self):
        from monitor.timing import plan,verify
        config=resolve(self.root/'empty',profile={},default_file=self.public)
        config=config.overlay({'timezone':'America/New_York','schedule':{'continuations':['10:30']}},'user')
        with snapshot(config):
            self.assertEqual(zone().key,'America/New_York')
            p=plan('2026-11-11',purpose='continuation',at='2026-11-11T12:00:00Z')
            self.assertEqual(p['eligible'][0]['utc'],'2026-11-11T15:30:00+00:00')
            self.assertTrue(verify({'plan':p,'actual_rrule':p['rrule']})['verified'])
        card=resolve(self.root/'empty',profile={},default_file=self.public,card={'id':'one','customisations':{'reviews':{'card_days':3}}})
        self.assertEqual(card.get('reviews.card_days'),3)
        self.assertEqual(card.origins['reviews.card_days'],'card:one')
        self.assertEqual(config.get('reviews.card_days'),7)

    def test_configured_timezone_gap_and_fold(self):
        from monitor.timing import london_instant
        config=resolve(self.root/'empty',profile={},default_file=self.public).overlay({'timezone':'America/New_York'},'user')
        with snapshot(config):
            with self.assertRaises(ValueError):london_instant('2026-03-08','02:30')
            with self.assertRaises(ValueError):london_instant('2026-11-01','01:30')
            self.assertEqual((london_instant('2026-11-01','01:30',1)-london_instant('2026-11-01','01:30',0)).total_seconds(),3600)

    def test_schedule_edits_prompt_app_reconciliation_once(self):
        self.init();path,_=paths(self.user);user=read_yaml(path)
        user['schedule']['morning']='10:00';atomic(path,dump(user))
        sync(self.user,profile={},default_file=self.public)
        pending=notices(self.user)
        self.assertEqual([n['type'] for n in pending],['schedule_reconciliation'])
        self.assertEqual(notices(self.user),[])


if __name__=='__main__':unittest.main()
