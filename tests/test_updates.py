from pathlib import Path
import tempfile
import unittest
from monitor.configuration import resolve,snapshot
from monitor.updates import check


class UpdateTests(unittest.TestCase):
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
