from pathlib import Path
import tempfile
import unittest
from monitor.configuration import resolve,snapshot
from monitor.updates import check, notice, version
from unittest.mock import patch


class UpdateTests(unittest.TestCase):
    def test_release_versions_use_tags_not_commit_wording(self):
        prefix='harness-update/v'
        self.assertIsNone(version('stable release update',prefix))
        self.assertIsNone(version('v1.0.0',prefix))
        self.assertGreater(version(prefix+'1.0.0',prefix),version(prefix+'1.0.0-rc.9',prefix))
        self.assertGreater(version(prefix+'1.0.0-rc.1',prefix),version(prefix+'1.0.0-beta.9',prefix))
        self.assertGreater(version(prefix+'1.0.0-beta.1',prefix),version(prefix+'1.0.0-alpha.9',prefix))

    def test_notice_once_per_release_then_new_release_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result=dict(status='available',tag='harness-update/v0.1.0-alpha.3',url='https://example.test/release')
            with patch('monitor.updates.check',return_value=result):
                self.assertEqual(notice(root)[0]['type'],'harness_update')
                self.assertEqual(notice(root),[])
                result['tag']='harness-update/v0.1.0-alpha.4'
                self.assertEqual(notice(root)[0]['tag'],result['tag'])
            cached=__import__('json').loads((root/'.state/update-check.json').read_text())
            self.assertEqual(cached['notified'],result['tag'])

    def test_ordinary_main_commit_without_release_tag_does_not_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def git(*args):
                if args[0]=='rev-parse':return 'installed'
                if args[0]=='remote':return 'https://github.com/example/project.git'
                if args[0]=='ls-remote':return 'new-main refs/heads/main\nother refs/tags/stable-release'
                self.fail('Unexpected operation')
            with snapshot(resolve(root,profile={})):
                self.assertEqual(check(root=root,runner=git)['status'],'current')

    def test_tagged_main_update_cached_without_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            def git(*args):
                calls.append(args)
                if args[0]=='rev-parse':return 'installed'
                if args[0]=='remote':return 'https://github.com/example/project.git'
                if args[0]=='ls-remote':return 'main refs/heads/main\nobject refs/tags/harness-update/v0.1.0-alpha.1\nnew refs/tags/harness-update/v0.1.0-alpha.1^{}\nother refs/tags/random'
                if args[0]=='merge-base':raise ValueError('Not installed')
                self.fail('Unexpected Git mutation')
            with snapshot(resolve(root,profile={})):
                result=check(root=root,runner=git,compare=lambda *args:{'status':'ahead'})
                self.assertEqual(result['status'],'available');self.assertEqual(result['target'],'new')
                count=len(calls);self.assertEqual(check(root=root,runner=git)['status'],'available')
                self.assertEqual(len(calls),count+1)
                self.assertFalse(any(c[0] in ('pull','fetch','checkout','switch','reset') for c in calls))

    def test_off_main_tag_and_network_errors_do_not_offer_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def git(*args):
                if args[0]=='rev-parse':return 'installed'
                if args[0]=='remote':return 'git@github.com:example/project.git'
                return 'main refs/heads/main\nbranch refs/tags/harness-update/v1.0.0'
            with snapshot(resolve(root,profile={})):
                result=check(root=root,runner=git,compare=lambda *args:{'status':'diverged'})
                self.assertEqual(result['status'],'offline');self.assertIn('not on main',result['error'])


if __name__=='__main__':unittest.main()
