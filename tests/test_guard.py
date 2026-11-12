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
                 '10:00:01  tool Shell: {"command":"cd \\"F:/work\\" && git -C repo stash push -m wip && python t.py"}',
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


def call(name, **args):
    """One line of a Kimi event stream: an assistant message with one tool call."""
    return json.dumps(dict(role='assistant', tool_calls=[dict(type='function', function=dict(name=name, arguments=json.dumps(args)))]))


CWD = 'F:/work/task'
HOME = 'C:/Users/sam'


class OutsideWritesTests(unittest.TestCase):
    def check(self, *events, allowed=('C:/Users/sam/.claude/delegate/jobs/job-a',), cwd=CWD):
        with patch.object(R.os, 'name', 'nt'), patch.object(R.tempfile, 'gettempdir', lambda: 'C:/Users/sam/AppData/Local/Temp'):
            return [f['text'] for f in R.outside_writes(events, cwd=cwd, allowed=allowed, home=HOME)]

    def test_file_tools_inside_the_job_are_fine(self):
        self.assertEqual(self.check(
            call('Edit', path='F:/work/task/src/a.c', old_string='x', new_string='y'),
            call('Edit', path='F:\\work\\task\\src\\b.c', old_string='x', new_string='y'),
            call('Write', path='notes/todo.md', content='x'),                         # relative: inside the working directory
            call('Write', path='C:/Users/sam/.claude/delegate/jobs/job-a/progress.md', content='x'),
            call('Write', path='C:\\Users\\sam\\AppData\\Local\\Temp\\scratch.txt', content='x'),
            call('Read', path='E:/shared/other/secret.txt'),                         # reading is not writing
            call('Grep', path='E:/shared/other', pattern='x')), [])

    def test_file_tools_outside_are_flagged_with_the_path(self):
        found = self.check(
            call('Edit', new_string='y', old_string='x', path='E:\\shared\\other\\main.c'),
            call('Write', path='../sibling/out.txt', content='x'),                    # climbs out of the working directory
            call('Edit', path='E:/shared/other/main.c', old_string='a', new_string='b'))
        self.assertEqual(len(found), 1)
        self.assertIn("wrote or deleted outside the job's folders (2 paths)", found[0])
        self.assertIn('e:/shared/other/main.c (by the Edit tool)', found[0])
        self.assertIn('f:/work/sibling/out.txt', found[0])
        self.assertIn('--add-dir', found[0])

    def test_add_dir_allows_a_folder(self):
        event = call('Edit', path='E:/shared/other/main.c', old_string='a', new_string='b')
        self.assertEqual(len(self.check(event)), 1)
        self.assertEqual(self.check(event, allowed=('E:\\shared\\other',)), [])
        self.assertEqual(len(self.check(event, allowed=('E:/shared/oth',))), 1)   # a name that only starts the same

    def test_shell_commands_that_delete_or_write_elsewhere(self):
        cases = [
            'rm -rf "E:/shared/other/build"',
            'del /q E:\\shared\\other\\old.log',
            'Remove-Item -Recurse -Force "E:\\shared\\other\\cache"',
            'python -c "import shutil; shutil.rmtree(\'E:/shared/other/tmp\')"',
            'echo done > E:/shared/other/flag.txt',
            'git status && python tool.py >> "E:/shared/other/log.txt" 2>&1',
            'mv out.bin E:/shared/other/out.bin',
            'cp build/app.exe /mnt/e/shared/other/app.exe',
            'Copy-Item -Path build\\app.exe -Destination E:\\shared\\other\\app.exe -Force',
            'git -C E:/shared/other commit -am wip',
            'git -C "E:/shared/other" worktree add ../wt fix/x',
            'git -C E:/shared/other branch -D old',
            'python -c "import shutil; shutil.copyfile(\'out.bin\', \'E:/shared/other/out.bin\')"',
            'python "E:/tools/relay/delegate.py" save "E:/shared/other/REPORT.md"',
            'cd /e/shared/other && rm /e/shared/other/x.txt',
            'touch ~/.bashrc',
        ]
        for command in cases:
            with self.subTest(command=command):
                found = self.check(call('Bash', command=command))
                self.assertEqual(len(found), 1, found)
                self.assertIn("outside the job's folders (1 path)", found[0])

    def test_shell_commands_that_only_read_elsewhere_or_write_here(self):
        cases = [
            'cat E:/shared/other/README.md',
            'type E:\\shared\\other\\a.txt > copy.txt',
            'cp E:/shared/other/a.txt ./local/',
            'cp -r "E:/shared/other/assets" assets',
            'git clone E:/shared/other repo',
            'git -C E:/shared/other log --oneline -5',
            'git -C /e/shared/other branch -a && git -C E:/shared/other stash list',
            'python -c "import shutil; shutil.copyfile(\'E:/shared/other/a.dll\', p/\'a.dll\')"',
            'python -c "print(open(\'E:/shared/other/a.txt\', \'rb\').read())"',
            'git -C F:/work/task/repo commit -am wip',
            'python build.py --src E:/shared/other --out F:/work/task/out 2>&1',
            'rm -rf F:/work/task/build && mkdir F:/work/task/build',
            'python tool.py > /dev/null 2>&1; echo x > $null; echo y > NUL',
            'rm -f /tmp/scratch.txt; echo x > "$TEMP/y.txt"',
            'python "E:/tools/relay/delegate.py" phase --job job-a "build > test"',
            'rg -n "open\\(" E:/shared/other/src',
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertEqual(self.check(call('Bash', command=command)), [])

    def test_the_owners_extra_folders_and_the_workers_own_folder(self):
        events = [call('Bash', command='mkdir -p "/c/Users/sam/Documents/TheApp/copy-1/save"'),
                  call('Bash', command='rm -rf C:/Users/sam/.kimi-code/sessions/old')]
        self.assertEqual(len(self.check(*events)), 1)
        with temporary_directory() as td, patch.dict(D.CONFIG, guard_allow_dirs=['C:/Users/sam/Documents/TheApp']), \
             patch.dict(D.os.environ, KIMI_CODE_HOME='C:/Users/sam/.kimi-code'):
            roots = D.write_roots(Path(td) / 'jobs' / 'job-a', dict(backend='kimi', profile='example', add_dirs=['D:/extra']))
        self.assertIn('D:/extra', roots)
        self.assertEqual(self.check(*events, allowed=roots), [])
        self.assertTrue(C._valid('guard_allow_dirs', ['C:/x']))
        self.assertFalse(C._valid('guard_allow_dirs', 'C:/x'))

    def test_unreadable_lines_and_other_tools_are_skipped(self):
        self.assertEqual(self.check('not json', json.dumps(dict(role='tool', content='rm -rf D:/x')),
                                    json.dumps(dict(role='assistant', content='I will not rm -rf E:/shared/other')),
                                    json.dumps(dict(role='assistant', tool_calls=[dict(function=dict(name='Edit', arguments='{"path": "D:/tru'))])),
                                    call('WaitFor', task_id='bash-1', timeout=600)), [])

    def test_only_unsandboxed_backends_are_checked_when_a_job_ends(self):
        event = call('Edit', path=str(Path.home() / 'somewhere-else' / 'main.c'), old_string='a', new_string='b')
        for backend, flags in (('kimi', 1), ('codex', 0)):
            with self.subTest(backend=backend), temporary_directory() as td, \
                 patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', AGENTS_ROOT=Path(td) / 'agents', DELEGATE_ROOT=Path(td)), \
                 patch.object(D.notify, 'send', return_value=[]):
                jd, meta = make_job(td, 'job-a', backend=backend)
                meta.update(mode='write', cwd=str(Path(td) / 'work'), add_dirs=[])
                (Path(td) / 'agents' / 'example' / 'reports').mkdir(parents=True)
                (jd / 'events.jsonl').write_text(event + '\n', encoding='utf-8')
                (jd / 'result.md').write_text('Free text report.', encoding='utf-8')
                D.finalize(jd, meta, dict(state='running', started=time.time() - 60), None, 0)
                status = json.loads((jd / 'status.json').read_text())
                self.assertEqual(status['flags'], flags)
                if flags:
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        D.report(jd)
                    self.assertIn("FLAG: GUARD: wrote or deleted outside the job's folders", out.getvalue())
                    meta.update(add_dirs=[str(Path.home() / 'somewhere-else')])
                    jd2, meta2 = make_job(td, 'job-b', backend=backend)
                    meta2.update(mode='write', cwd=str(Path(td) / 'work'), add_dirs=[str(Path.home() / 'somewhere-else')])
                    (jd2 / 'events.jsonl').write_text(event + '\n', encoding='utf-8')
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
