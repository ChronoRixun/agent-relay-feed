# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
from tests.helpers import temporary_directory
import unittest
from pathlib import Path

import install_agents as I

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def run_installer(self, home, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return I.main(list(args), home=home, delegate=ROOT / 'delegate.py')

    def test_all_variants_and_home_paths(self):
        with temporary_directory() as td:
            home = Path(td)
            self.assertEqual(self.run_installer(home), 0)
            agents = home / '.claude' / 'agents'
            self.assertEqual(len(list(agents.glob('*.md'))), len(I.SPECS))
            self.assertTrue((home / '.claude' / 'delegate' / 'inbox').is_dir())
            for spec in I.SPECS:
                text = (agents / f"{spec['name']}.md").read_text(encoding='utf-8')
                self.assertTrue(I.is_generated(text))
                self.assertIn((home / '.claude' / 'delegate' / 'inbox').as_posix(), text)
                self.assertIn((ROOT / 'delegate.py').as_posix(), text)
                self.assertTrue(text.startswith('---\n'))
                if spec['name'] == 'gpt-sol-high':
                    self.assertIn('run gpt-sol-med --effort high', text)
            self.assertEqual(self.run_installer(home), 0)

    def test_modified_files_require_force_for_install_and_uninstall(self):
        with temporary_directory() as td:
            home = Path(td)
            self.run_installer(home, '--only', 'gpt-sol-high')
            path = home / '.claude' / 'agents' / 'gpt-sol-high.md'
            changed = path.read_text(encoding='utf-8') + '\nCustom instruction\n'
            path.write_text(changed, encoding='utf-8')
            self.assertEqual(path.read_text(encoding='utf-8'), changed)
            self.assertFalse(I.is_generated(changed))
            self.assertEqual(self.run_installer(home, '--only', 'gpt-sol-high'), 1)
            self.assertEqual(path.read_text(encoding='utf-8'), changed)
            self.assertEqual(self.run_installer(home, '--uninstall', '--only', 'gpt-sol-high'), 1)
            self.assertTrue(path.exists())
            self.assertEqual(self.run_installer(home, '--force', '--only', 'gpt-sol-high'), 0)
            self.assertTrue(I.is_generated(path.read_text(encoding='utf-8')))
            self.assertEqual(self.run_installer(home, '--uninstall', '--only', 'gpt-sol-high'), 0)
            self.assertFalse(path.exists())

    def test_unmanaged_and_list(self):
        with temporary_directory() as td:
            home = Path(td)
            self.assertEqual(self.run_installer(home, '--list'), 0)
            self.assertEqual(list(home.iterdir()), [])
            agents = home / '.claude' / 'agents'
            agents.mkdir(parents=True)
            path = agents / 'kimi-k3.md'
            path.write_text('user file', encoding='utf-8')
            self.assertEqual(path.read_text(), 'user file')
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3'), 1)
            self.assertEqual(path.read_text(), 'user file')
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3', '--force'), 0)
            self.assertTrue(I.is_generated(path.read_text(encoding='utf-8')))

    def test_only_and_unknown_names(self):
        with temporary_directory() as td:
            home = Path(td)
            self.assertEqual(self.run_installer(home, '--only', 'gpt-astra-med,kimi-k3'), 0)
            self.assertEqual(sorted(p.stem for p in (home / '.claude' / 'agents').glob('*.md')), ['gpt-astra-med', 'kimi-k3'])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                self.run_installer(home, '--only', 'unknown')
            self.assertEqual(exc.exception.code, 2)

    def test_keep_description_survives_this_and_later_installs(self):
        with temporary_directory() as td:
            home = Path(td)
            agents = home / '.claude' / 'agents'
            agents.mkdir(parents=True)
            path = agents / 'kimi-k3.md'
            mine = 'Kimi K3: my own notes on when to use it, with "quotes".'
            path.write_text('---\nname: kimi-k3\ndescription: ' + I.json.dumps(mine) +
                            '\nmodel: sonnet\ntools: Bash, Read, Write\n---\n\nold hand-written body\n',
                            encoding='utf-8', newline='\n')
            # Without the flag a forced reinstall loses the owner's text.
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3', '--force', '--keep-description'), 0)
            text = path.read_text(encoding='utf-8')
            self.assertTrue(I.is_generated(text))
            self.assertEqual(I.front_matter(text), {'description': mine, 'model': 'sonnet'})
            self.assertIn('--name "<3-6 word title of the task>"', text)
            self.assertNotIn('old hand-written body', text)
            saved = I.load_overrides(home)
            self.assertEqual(saved, {'kimi-k3': {'description': mine, 'model': 'sonnet'}})
            # A later plain install keeps it and reports no change.
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3'), 0)
            self.assertEqual(path.read_text(encoding='utf-8'), text)
            # Other relays are untouched by the saved entry.
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k28'), 0)
            other = I.front_matter((agents / 'kimi-k28.md').read_text(encoding='utf-8'))
            self.assertEqual(other['model'], 'haiku')
            self.assertNotEqual(other['description'], mine)

    def test_keep_description_ignores_default_text_and_bad_files(self):
        with temporary_directory() as td:
            home = Path(td)
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3'), 0)
            self.assertEqual(self.run_installer(home, '--only', 'kimi-k3', '--force', '--keep-description'), 0)
            self.assertFalse(I.overrides_path(home).exists())
            I.overrides_path(home).write_text('not json', encoding='utf-8')
            self.assertEqual(I.load_overrides(home), {})
            self.assertEqual(I.front_matter('no header at all'), {})
            self.assertEqual(I.front_matter('---\nmodel: bad model; rm\n---\n'), {})

    def test_previous_brand_marker_migrates_without_force(self):
        with temporary_directory() as td:
            home = Path(td)
            spec = I.SPECS[0]
            old = I.generate(spec, home=home).replace(
                'generated-by: agent-relay-feed', 'generated-by: claude-delegate')
            self.assertTrue(I.is_generated(old))
            path = home / '.claude' / 'agents' / (spec['name'] + '.md')
            path.parent.mkdir(parents=True)
            path.write_text(old, encoding='utf-8')
            self.assertEqual(path.read_text(), old)
            self.assertEqual(self.run_installer(home, '--only', spec['name']), 0)
            migrated = path.read_text(encoding='utf-8')
            self.assertIn('generated-by: agent-relay-feed', migrated)
            self.assertTrue(I.is_generated(migrated))
