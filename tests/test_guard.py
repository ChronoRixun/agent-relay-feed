# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D
import delegate_config as C
import results as R
from tests.helpers import temporary_directory
from tests.test_phase import make_job

LOG = [
    '10:00:01  command (exit 0): "powershell.exe" -Command "git status"',
    '10:00:05  command (exit 0): "powershell.exe" -Command "python -m pip install requests"',
    '10:00:09  command (exit 1): pip install pillow',
    '10:01:00  tool Bash: {"command": "git -C /work/repo push origin fix/x"}',
    '10:02:00  message: I will not pip install anything or git push',
    '10:03:00  files changed: update D:/work/a.c',
]


class GuardTests(unittest.TestCase):
    def test_installs_and_publishing_are_flagged_with_an_example(self):
        found = R.guard('write', LOG)
        texts = [f['text'] for f in found]
        self.assertEqual(len(found), 2)
        self.assertTrue(all(not f['ok'] for f in found))
        self.assertTrue(any('installed software (2x)' in t and 'pip install requests' in t for t in texts))
        self.assertTrue(any('publishes outside this machine (1x)' in t and 'push origin' in t for t in texts))

    def test_read_only_job_that_changed_files(self):
        texts = [f['text'] for f in R.guard('read', LOG)]
        self.assertTrue(any('changed files in a read-only job (1x)' in t and 'a.c' in t for t in texts))
        self.assertFalse(any('read-only' in f['text'] for f in R.guard('write', LOG)))

    def test_git_stash_push_is_local(self):
        lines = ['10:00:00  command (exit 0): git stash push -- tools/check.py',
                 '10:00:01  tool Shell: {"command":"cd \\"O:/work\\" && git -C repo stash push -m wip && python t.py"}',
                 '10:00:02  command (exit 0): git stash pop']
        self.assertEqual(R.guard('write', lines), [])
        found = R.guard('write', ['10:00:03  command (exit 0): git stash pop && git push origin main'])
        self.assertEqual(len(found), 1)                   # a real push after a stash on the same line still counts

    def test_clean_log_and_words_in_messages_are_not_flagged(self):
        self.assertEqual(R.guard('write', LOG[:1] + LOG[4:5]), [])

    def test_extra_rules_and_bad_patterns(self):
        extra = [dict(pattern=r'docker (run|start)', text='started a container'), dict(pattern='(', text='broken')]
        found = R.guard('write', ['10:00:00  command (exit 0): docker run -d img'], extra)
        self.assertEqual(len(found), 1)
        self.assertIn('started a container', found[0]['text'])

    def test_config_keys(self):
        self.assertTrue(C._valid('guard_patterns', [dict(pattern='x', text='y')]))
        self.assertFalse(C._valid('guard_patterns', [dict(pattern='x')]))
        self.assertTrue(C._valid('claude_api_monthly_usd', 100))
        self.assertFalse(C._valid('claude_api_monthly_usd', -1))

    def test_finalize_adds_guard_flags_even_without_a_result_block(self):
        with temporary_directory() as td, \
             patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', AGENTS_ROOT=Path(td) / 'agents', DELEGATE_ROOT=Path(td)), \
             patch.object(D.notify, 'send', return_value=[]):
            jd, meta = make_job(td, 'job-a')
            (Path(td) / 'agents' / 'example' / 'reports').mkdir(parents=True)
            Path(meta['activity_file']).write_text('\n'.join(LOG) + '\n', encoding='utf-8')
            (jd / 'result.md').write_text('Free text report.', encoding='utf-8')
            D.finalize(jd, meta, dict(state='running', started=time.time() - 60), None, 0)
            status = json.loads((jd / 'status.json').read_text())
            self.assertEqual((status['verdict'], status['flags']), (None, 3))     # make_job jobs are read-only
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                D.report(jd)
            self.assertIn('VERDICT: none given', out.getvalue())
            self.assertIn('FLAG: GUARD: installed software', out.getvalue())
            with patch.dict(D.CONFIG, guard_enabled=False):
                jd2, meta2 = make_job(td, 'job-b')
                Path(meta2['activity_file']).write_text('\n'.join(LOG) + '\n', encoding='utf-8')
                (jd2 / 'result.md').write_text('Free text report.', encoding='utf-8')
                D.finalize(jd2, meta2, dict(state='running', started=time.time() - 60), None, 0)
                self.assertEqual(json.loads((jd2 / 'status.json').read_text())['flags'], 0)


class SpendTests(unittest.TestCase):
    def test_month_to_date_and_budget_on_the_board(self):
        with temporary_directory() as td, \
             patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', AGENTS_ROOT=Path(td) / 'agents', DELEGATE_ROOT=Path(td)):
            now = time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
            last_month = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))
            entries = [dict(end=now - 3600, total_cost_usd=2.5), dict(end=now - 7200, total_cost_usd=1.25),
                       dict(end=last_month, total_cost_usd=40), dict(end=now, total_cost_usd=None)]
            (Path(td) / 'claude-api-ledger.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in entries), encoding='utf-8')
            self.assertEqual(D.api_spend(now), dict(month=3.75, budget=None))
            with patch.dict(D.CONFIG, claude_api_monthly_usd=100):
                self.assertEqual(D.spend_line(D.api_spend(now)), 'CLAUDE API: $3.75 this month of $100 (4%)')
                out = io.StringIO()
                with patch.object(D, '_claude_records', return_value=[]), patch.object(D.time, 'time', return_value=now), \
                     contextlib.redirect_stdout(out):
                    D.cmd_board(SimpleNamespace(hours=12))
                self.assertIn('CLAUDE API: $3.75 this month of $100', out.getvalue())

    def test_nothing_shown_without_spend_or_budget(self):
        with temporary_directory() as td, patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', DELEGATE_ROOT=Path(td)):
            out = io.StringIO()
            with patch.object(D, '_claude_records', return_value=[]), contextlib.redirect_stdout(out):
                D.cmd_board(SimpleNamespace(hours=12))
            self.assertNotIn('CLAUDE API', out.getvalue())


if __name__ == '__main__':
    unittest.main()
