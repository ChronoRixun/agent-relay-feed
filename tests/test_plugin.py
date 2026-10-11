# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Portable plugin packaging checks; no provider calls or desktop interaction."""
import contextlib
import io
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import build_plugin as B
import install_agents as I
from tests.helpers import temporary_directory

ROOT = Path(__file__).resolve().parents[1]


class PluginTests(unittest.TestCase):
    def test_packaged_agents_are_current_and_portable(self):
        self.assertEqual(B.main(['--check']), 0)
        for spec in I.SPECS:
            text = (ROOT / 'agents' / (spec['name'] + '.md')).read_text(encoding='utf-8')
            self.assertTrue(I.is_generated(text))
            self.assertIn('python "${CLAUDE_PLUGIN_ROOT}/delegate.py"', text)
            self.assertNotIn(ROOT.as_posix(), text)
            self.assertNotIn(Path.home().as_posix(), text)
            self.assertIn('Path.home()', text)
            self.assertIn('only if the caller explicitly asks', text)
            if spec['profile'].startswith('kimi'):
                self.assertIn('Kimi has no sandbox', text)

    def test_build_detects_missing_stale_and_unexpected_agents(self):
        with temporary_directory() as td, contextlib.redirect_stdout(io.StringIO()):
            root = Path(td)
            self.assertEqual(B.main(['--check'], root=root), 1)
            self.assertFalse((root / 'agents').exists())
            self.assertEqual(B.main([], root=root), 0)
            self.assertEqual(B.main(['--check'], root=root), 0)
            path = root / 'agents' / 'gpt-sol-high.md'
            path.write_text('stale', encoding='utf-8')
            self.assertEqual(path.read_text(), 'stale')
            self.assertEqual(B.main(['--check'], root=root), 1)
            self.assertEqual(B.main([], root=root), 0)
            path = root / 'agents' / 'unexpected.md'
            path.write_text('extra', encoding='utf-8')
            self.assertEqual(path.read_text(), 'extra')
            self.assertEqual(B.main(['--check'], root=root), 1)

    def test_root_substitution_keeps_path_one_argument(self):
        text = I.generate(I.SPECS[0], plugin=True)
        command = next(line for line in text.splitlines() if line.startswith('python "${CLAUDE_PLUGIN_ROOT}/delegate.py" run'))
        with temporary_directory() as td:
            root = Path(td) / 'plugin with spaces'
            root.mkdir()
            script = root / 'delegate.py'
            content = 'import sys; print(sys.argv[1:])'
            script.write_text(content, encoding='utf-8')
            self.assertEqual(script.read_text(), content)
            expanded = command.replace('${CLAUDE_PLUGIN_ROOT}', root.as_posix())
            argv = shlex.split(expanded)
            self.assertEqual(argv[1], script.as_posix())
            result = subprocess.run([sys.executable, *argv[1:3]], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "['run']")

    def test_marketplace_resolves_plugin_root(self):
        market = json.loads((ROOT / '.claude-plugin' / 'marketplace.json').read_text())
        entry = market['plugins'][0]
        source = (ROOT / entry['source']).resolve()
        manifest = json.loads((source / '.claude-plugin' / 'plugin.json').read_text())
        self.assertEqual(entry['name'], manifest['name'])
        self.assertEqual(manifest['license'], 'Apache-2.0')
        self.assertEqual(manifest['author'], {'name': 'ChronoRixun'})
        self.assertTrue((source / 'skills' / 'feed' / 'SKILL.md').is_file())
        self.assertTrue((source / 'skills' / 'delegate' / 'SKILL.md').is_file())
