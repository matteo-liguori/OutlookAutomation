"""The same source layout must work in a checkout and either platform bundle."""
from pathlib import Path
import re
import unittest
from monitor.paths import ROOT


class LayoutTests(unittest.TestCase):
    def test_root_has_only_bootstrap_files(self):
        allowed={'.gitignore','.gitattributes','README.md','AGENTS.md','LICENSE.md','monitor.ps1','setup_outlook.py','PACKAGE_MANIFEST.json'}
        self.assertFalse({p.name for p in ROOT.iterdir() if p.is_file()}-allowed)

    def test_markdown_links_resolve(self):
        for path in [ROOT/'README.md',ROOT/'AGENTS.md',*(ROOT/'docs').rglob('*.md'),*(ROOT/'monitor/adapters').rglob('*.md')]:
            for target in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
                if re.match(r'[a-z]+:|#',target):continue
                relative=target.split('#')[0]
                if relative:self.assertTrue((path.parent/relative).exists(),str(path)+': '+target)
