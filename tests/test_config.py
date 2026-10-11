# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
from tests.helpers import temporary_directory, windows_path
import unittest
from pathlib import Path

import delegate_config as C

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_missing_defaults_are_independent(self):
        with temporary_directory() as td:
            path = Path(td) / 'missing.json'
            a = C.load_config(path)
            self.assertEqual(a, C.DEFAULTS)
            self.assertFalse(a['watched_processes'])
            a['path_shortening'].append(['a', 'b'])
            self.assertEqual(C.load_config(path)['path_shortening'], [])

    def test_partial_config(self):
        with temporary_directory() as td:
            path = Path(td) / 'config.json'
            data = dict(feed_port=9876, stall_seconds=60, loop_count=3,
                        path_shortening=[['/work/projects', 'P/']],
                        watched_processes=[dict(name='editor', owners=[dict(
                            path_patterns=['/personal/'], owner='you')])])
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(json.loads(path.read_text()), data)
            config = C.load_config(path)
            self.assertEqual(config['feed_port'], 9876)
            self.assertEqual(config['polling_seconds'], 30)
            self.assertEqual(config['watched_processes'], data['watched_processes'])

    def test_invalid_config_and_fields(self):
        with temporary_directory() as td, contextlib.redirect_stderr(io.StringIO()) as err:
            path = Path(td) / 'config.json'
            for bad in ('{', '[]'):
                path.write_text(bad, encoding='utf-8')
                self.assertEqual(path.read_text(), bad)
                self.assertEqual(C.load_config(path), C.DEFAULTS)
            data = dict(feed_port=True, loop_count=11, stall_seconds=-1, polling_seconds=float('inf'),
                        path_shortening=[['', 'p']], watched_processes=[{'name': 2}], extra=1)
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(json.loads(path.read_text()), data)
            self.assertEqual(C.load_config(path), C.DEFAULTS)
            self.assertIn('warning:', err.getvalue())

    def test_home_only_and_prefix_boundaries(self):
        self.assertEqual(C.shorten_paths('/home/alex/a /home/alexander/a', home='/home/alex', pairs=[]),
                         '~/a /home/alexander/a')

    def test_longest_prefix_and_windows_separators(self):
        text = ' '.join([windows_path('Work/tasks/repo/a.py', backslashes=True),
                         windows_path('Work/other/a.py'), windows_path('Workish/a.py')])
        self.assertEqual(C.shorten_paths(text, home='/home/alex',
                         pairs=[[windows_path('Work'), 'W/'], [windows_path('Work/tasks'), 'tasks/']]),
                         'tasks' + chr(92) + 'repo' + chr(92) + 'a.py W/other/a.py ' + windows_path('Workish/a.py'))


class RepetitionTests(unittest.TestCase):
    def test_fast_loop(self):
        self.assertEqual(C.repetition([('git status', i) for i in range(6)], 6, 30), (True, False))

    def test_slow_polling(self):
        self.assertEqual(C.repetition([('git status', i * 30) for i in range(6)], 6, 30), (False, True))

    def test_midnight_polling(self):
        self.assertEqual(C.repetition([('poll', 86370), ('poll', 0), ('poll', 30)], 3, 30), (False, True))

    def test_whole_commands_and_window(self):
        self.assertEqual(C.repetition([(f'python task.py {i}', i) for i in range(6)]), (False, False))
        recent = [('old', i) for i in range(20)] + [('new', 21)]
        self.assertEqual(C.repetition(recent), (False, False))
        self.assertEqual(C.repetition([]), (False, False))

    def test_configured_threshold(self):
        self.assertEqual(C.repetition([('poll', 1), ('poll', 2), ('poll', 3)], 3, 1), (False, True))


class RunnerLivenessTest(__import__("unittest").TestCase):
    def test_reused_pid_is_not_alive(self):
        import os
        import time
        import delegate
        me = os.getpid()
        # this process exists but started before the job's recorded start time + 5 s -> alive
        self.assertTrue(delegate.runner_alive({"runner_pid": me, "started": time.time()}))
        # a job that "started" an hour before this process existed: the PID must belong to someone else -> not alive
        born = delegate.process_start_time(me)
        if born is not None:
            self.assertFalse(delegate.runner_alive({"runner_pid": me, "started": born - 3600}))

    def test_missing_pid(self):
        import delegate
        self.assertFalse(delegate.runner_alive({"runner_pid": None, "started": 0}))


class CodexToolOverrideTests(unittest.TestCase):
    def test_only_allowlisted_tools_stay_on(self):
        import delegate
        with temporary_directory() as td:
            path = Path(td) / 'config.toml'
            path.write_text('[mcp_servers.desk]\ncommand = "x"\n[mcp_servers.docs]\ncommand = "y"\n'
                            '[plugins."computer-use@vendor"]\nenabled = true\n'
                            '[plugins."dotted.name@vendor"]\nenabled = true\n', encoding='utf-8')
            saved = dict(delegate.CONFIG)
            try:
                delegate.CONFIG.update(codex_mcp_allow=['docs'], codex_plugins_allow=[])
                got = delegate.codex_tool_overrides(path)[1::2]
                self.assertEqual(got, ['features.apps=false', 'mcp_servers.desk.enabled=false',
                                       'plugins.computer-use@vendor.enabled=false'])
                delegate.CONFIG.update(codex_mcp_allow=['docs', 'codex_apps'])
                self.assertNotIn('features.apps=false', delegate.codex_tool_overrides(path))
                self.assertEqual(delegate.codex_tool_overrides(Path(td) / 'missing.toml'), [])
            finally:
                delegate.CONFIG.clear()
                delegate.CONFIG.update(saved)
