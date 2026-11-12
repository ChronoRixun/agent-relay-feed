# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""`bg`: a long command run as a job of its own; `save`: a file written without the Write tool."""
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D


class BgTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.td = Path(tmp.name)
        self.spawned = []
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 POLL_S=0.05, WAIT_ANY_POLL_S=0.05,
                                 spawn_runner=lambda jd, meta: self.spawned.append(jd)),
                  patch.dict(D.CONFIG, claude_agents_enabled=False,
                             resources=dict(game=dict(slots=1, values=[str(self.td / 'copy-1')]))),
                  patch.object(D.notify, 'send', return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def bg(self, *command, **kw):
        args = dict(command=['--', *command], shell='none', cwd=str(self.td), lease=None, name=None,
                    max_minutes=5, wait=0)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.cmd_bg(SimpleNamespace(**args))
        return self.spawned[-1], out.getvalue()

    def run_it(self, jd):
        """What the detached runner does, in this process."""
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_runner(SimpleNamespace(jobdir=str(jd)))
        return json.loads((jd / 'status.json').read_text())

    def test_a_command_runs_to_a_result_and_wait_any_reports_it_once(self):
        jd, text = self.bg(sys.executable, '-c', 'print("loop 1"); print("loop 2 ok")', name='Boot loop')
        self.assertIn(f'STARTED: {jd.name} (background command)', text)
        self.assertIn('Do NOT start a timer', text)
        st = self.run_it(jd)
        self.assertEqual((st['state'], st['exit_code']), ('done', 0))
        result = (jd / 'result.md').read_text()
        self.assertIn('Exit code: 0', result)
        self.assertIn('loop 2 ok', result)
        self.assertIn('message: loop 2 ok', Path(json.loads((jd / 'meta.json').read_text())['activity_file']).read_text())
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_any(2)
        self.assertIn(f'OUTCOME: {jd.name} done', o.getvalue())
        self.assertIn('COMMAND:', o.getvalue())
        self.assertIn('AGENT: bg = Background command', o.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_any(1)
        self.assertNotIn('OUTCOME', o.getvalue())

    def test_a_failing_command_is_a_failed_job(self):
        jd, _ = self.bg(sys.executable, '-c', 'import sys; print("boom"); sys.exit(3)')
        st = self.run_it(jd)
        self.assertEqual((st['state'], st['exit_code']), ('failed', 3))
        self.assertIn('exited with code 3', st['errors'][0])
        self.assertIn('boom', (jd / 'result.md').read_text())

    def test_it_holds_its_slot_for_the_run_and_gets_the_slot_value(self):
        code = 'import os, sys; print(os.environ["DELEGATE_SLOT_GAME"]); print(sys.argv[1]); print(os.environ["DELEGATE_JOB"])'
        jd, text = self.bg(sys.executable, '-c', code, '{game}', lease=['game'])
        self.assertIn('holds game', text)
        seen = {}
        real = D.subprocess.Popen

        def popen(*a, **kw):
            seen['held'] = D.leases.held(self.td, 'game', jd.name)
            return real(*a, **kw)

        with patch.object(D.subprocess, 'Popen', popen):
            st = self.run_it(jd)
        self.assertEqual(st['state'], 'done')
        self.assertEqual(seen['held']['slot'], 1)
        copy = str(self.td / 'copy-1')
        self.assertEqual((jd / 'output.log').read_text().split(), [copy, copy, jd.name])
        self.assertIsNone(D.leases.held(self.td, 'game', jd.name))      # released when the job ended

    def test_cancel_stops_it_and_unknown_slots_are_refused(self):
        jd, _ = self.bg(sys.executable, '-c', 'import time; time.sleep(60)')
        (jd / 'cancel.flag').write_text('now')
        started = time.time()
        st = self.run_it(jd)
        self.assertEqual(st['state'], 'cancelled')
        self.assertLess(time.time() - started, 20)
        with self.assertRaises(SystemExit) as e:
            self.bg('x', lease=['nope'])
        self.assertIn('no resource named nope', str(e.exception))
        with self.assertRaises(SystemExit):
            self.bg()

    def test_time_limit(self):
        jd, _ = self.bg(sys.executable, '-c', 'import time; time.sleep(60)', max_minutes=0)
        st = self.run_it(jd)
        self.assertEqual(st['state'], 'timeout')

    def test_shell_forms(self):
        self.assertEqual(D.bg_command(['--', 'python', 'x.py', '--n', '200'], 'none'), ['python', 'x.py', '--n', '200'])
        with patch.object(D.shutil, 'which', return_value=None):
            self.assertEqual(D.bg_command(['a | b'], 'powershell'),
                             ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'a | b'])
        with patch.object(D, '_find_bash', return_value='/bin/bash'):
            self.assertEqual(D.bg_command(['./loop.sh', '5'], 'bash'), ['/bin/bash', '-lc', './loop.sh 5'])
        with self.assertRaises(ValueError):
            D.bg_command(['--'], 'none')


class SaveTests(unittest.TestCase):
    def save(self, **kw):
        args = dict(source=None, append=False, force=False)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.cmd_save(SimpleNamespace(**args))
        return out.getvalue()

    def test_stdin_overwrite_rules_and_append(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'deep' / 'REPORT.md'
            stdin = SimpleNamespace(buffer=io.BytesIO(b'# Report\n$HOME `x` \\n "quotes"\n'))
            with patch.object(D.sys, 'stdin', stdin):
                self.assertIn('SAVED:', self.save(path=str(path)))
            self.assertEqual(path.read_bytes(), b'# Report\n$HOME `x` \\n "quotes"\n')
            with patch.object(D.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'more\n'))), self.assertRaises(SystemExit):
                self.save(path=str(path))
            with patch.object(D.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'more\n'))):
                self.save(path=str(path), append=True)
            self.assertTrue(path.read_bytes().endswith(b'"quotes"\nmore\n'))
            with patch.object(D.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'  \n'))), self.assertRaises(SystemExit):
                self.save(path=str(Path(td) / 'empty.md'))

    def test_from_a_subagents_final_message(self):
        with tempfile.TemporaryDirectory() as td, patch.object(D, 'JOBS_ROOT', Path(td) / 'jobs'), \
             patch.object(D, '_claude_records', return_value=[dict(id='agent-a88abea383c3cd1c5', agent_type='general-purpose',
                                                                   final='# emulator report\nAll good.')]):
            path = Path(td) / 'REPORT.md'
            self.save(path=str(path), source='general-purpose#d1c5')
            self.assertEqual(path.read_text(encoding='utf-8'), '# emulator report\nAll good.')
            with self.assertRaises(SystemExit):
                self.save(path=str(path), source='agent-ffffffffffffffff1', force=True)


if __name__ == '__main__':
    unittest.main()
