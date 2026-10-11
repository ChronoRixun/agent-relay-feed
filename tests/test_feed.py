# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
from tests.helpers import temporary_directory, windows_path
import unittest
from pathlib import Path
from unittest.mock import patch

import feed as F
import delegate_config as C

ROOT = Path(__file__).resolve().parents[1]


class HumanizerTests(unittest.TestCase):
    def test_samples(self):
        samples = [
            ('Get-Content "notes.md" -Tail 5', 'read', 'Read', 'notes.md (end)'),
            ('powershell.exe -NoProfile -Command "Get-Content notes.md"', 'read', 'Read', 'notes.md'),
            (f'"{windows_path("Program Files/PowerShell/pwsh.exe", drive="C")}" -NoLogo -NoProfile -NonInteractive -Command "git status"', 'git', 'git status', ''),
            ('cmd.exe /d /s /c "git diff"', 'git', 'git diff', ''),
            ('python -c "import json; print(1)"', 'code', 'Python snippet', ''),
            ("python -c \"from pathlib import Path; print(Path('notes.md').read_text())\"", 'read', 'Read', 'notes.md'),
            ('git -C "repo" commit -m "Fix parser"', 'git', 'git commit', '[repo] Fix parser'),
            ('rg "needle" src', 'search', 'Search', '“needle”'),
            ('rg --files "src"', 'list', 'List', 'src'),
            ('git status; git diff ...', 'git', 'git status', ''),
            ('git diff ...', 'git', 'git diff', ''),
            ('python -m unittest', 'test', 'Run tests', ''),
            ('cmake --build build', 'build', 'Build', ''),
            ('Start-Process "editor.exe"', 'proc', 'Start process', 'editor.exe'),
            (f'"{windows_path("Tools/python.exe", drive="C")}" "tools/analyze.py" report', 'run', 'Run analyze.py', 'report'),
        ]
        for command, icon, verb, detail in samples:
            with self.subTest(command=command):
                h = F.humanize_command(command)
                self.assertEqual((h['icon'], h['verb'], h['detail']), (icon, verb, detail))

    def test_inline_scripts_and_compound_ranking(self):
        h = F.humanize_command("@'\nimport numpy\nprint(1)\n'@ | python -")
        self.assertEqual((h['verb'], h['detail']), ('Python snippet', 'uses numpy'))
        h = F.humanize_command('Get-Content notes.md; python -m unittest')
        self.assertEqual((h['verb'], h['more']), ('Run tests', 1))

    def test_kimi_tools(self):
        line = 'tool Bash: ' + json.dumps({'command': 'git status'}) + '; tool ReadFile: ' + json.dumps({'path': '/work/notes.md'})
        tools = F.humanize_kimi(line)
        self.assertEqual([t['verb'] for t in tools], ['git status', 'Read'])
        self.assertEqual(tools[1]['detail'], 'notes.md')
        self.assertEqual(F.humanize_kimi('tool Bash: {"command": "git diff ...')[0]['verb'], 'git diff')
        self.assertEqual(F.humanize_kimi('tool Read: {"path": "/work/notes.md", ...')[0]['detail'], 'notes.md')

    def test_labels(self):
        for meta, expected in [
            ({'label': 'Example (medium)', 'effort': 'high', 'backend': 'codex'}, 'Example (high)'),
            ({'label': 'Example (medium)', 'effort': 'xhigh'}, 'Example (extra high)'),
            ({'label': 'Kimi preview', 'effort': 'max', 'backend': 'kimi'}, 'Kimi preview'),
            ({'label': 'Example'}, 'Example'), ({}, 'fallback'),
        ]:
            self.assertEqual(F.effective_label(meta, 'fallback'), expected)

    def test_activity_with_reconnects_and_truncation(self):
        with temporary_directory() as td:
            path = Path(td) / 'activity.log'
            text = '10:00:00  command (exit 1): git diff ...\n10:00:01  Reconnecting...\n10:00:02  tool Read: {"path": "notes.md", ...\n'
            path.write_text(text, encoding='utf-8')
            self.assertEqual(path.read_text(), text)
            events, retries = F.parse_activity(path)
            self.assertEqual(retries, 1)
            self.assertEqual([e['verb'] for e in events], ['git diff', 'Read'])
            self.assertEqual(events[0]['rc'], 1)


class ProcessTests(unittest.TestCase):
    def test_disabled_panel_does_not_query_processes(self):
        with patch.dict(C.CONFIG, watched_processes=[]), patch.object(F, 'read_processes') as read:
            self.assertEqual(F.watched_processes([]), [])
            read.assert_not_called()

    def test_owner_rules_and_longest_job_path(self):
        spec = dict(name='editor', owners=[dict(path_patterns=['/personal/'], owner='you')])
        jobs = [('parent', '/work'), ('child', '/work/task')]
        self.assertEqual(F.process_owner('editor', '/work/task/bin/editor', jobs, spec), ('child', 'agent'))
        self.assertEqual(F.process_owner('editor', '/personal/editor', jobs, spec), ('you', 'owner'))
        self.assertEqual(F.process_owner('editor', '', jobs, spec), ('unknown', 'unknown'))

    def test_configured_shared_roots_and_fallback(self):
        with patch.dict(C.CONFIG, job_path_roots=[windows_path('Work/tasks')], activity_ignore_paths=[windows_path('Work/tasks')]):
            jobs = [('job-a', windows_path('Work/tasks/task-a')), ('shared', windows_path('Work/tasks'))]
            spec = dict(name='editor', job_cwd_patterns=['task-a'])
            self.assertEqual(F.process_owner('editor', windows_path('Work/tasks/task-a/bin/editor.exe'), jobs, spec), ('job-a', 'agent'))
            self.assertEqual(F.process_owner('editor', windows_path('Other/editor.exe'), jobs, spec), ('job-a', 'agent'))

    def test_filter_and_snapshot_panel_flag(self):
        specs = [dict(name='editor', owner='you')]
        with patch.dict(C.CONFIG, watched_processes=specs), patch.object(F, '_processes', dict(at=0, list=[])), patch.object(F, 'read_processes', return_value=[['1', 'editor', '/bin/editor'], ['2', 'other', '/bin/other']]):
            result = F.watched_processes([])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['who'], 'you')
        with temporary_directory() as td, patch.object(F.D, 'JOBS_ROOT', Path(td)), patch.dict(C.CONFIG, watched_processes=[]):
            snap = F.snapshot(1)
            self.assertFalse(snap['process_panel'])
            self.assertEqual(snap['processes'], [])


class JobSummaryTests(unittest.TestCase):
    def test_loop_and_polling_in_job_summary(self):
        with temporary_directory() as td:
            jd = Path(td)
            activity = jd / 'activity.log'
            meta = dict(profile='example', label='Example (medium)', backend='codex',
                        effort='high', activity_file=str(activity), created=1)
            text = json.dumps(meta)
            (jd / 'meta.json').write_text(text, encoding='utf-8')
            self.assertEqual((jd / 'meta.json').read_text(), text)
            for interval, expected in [(1, True), (30, False)]:
                lines = ''.join(f'10:{i * interval // 60:02d}:{i * interval % 60:02d}  command (exit 0): git status\n'
                                for i in range(6))
                activity.write_text(lines, encoding='utf-8')
                self.assertEqual(activity.read_text(), lines)
                with patch.object(F.D, 'effective_status', return_value=dict(state='running', started=1)):
                    result = F.job_summary(jd, 100)
                self.assertEqual(result['loop'], expected)
                self.assertEqual(result['label'], 'Example (high)')
                self.assertEqual(result['counts']['commands'], 6)

    def test_recent_working_file_suppresses_stall(self):
        with temporary_directory() as td:
            jd = Path(td)
            with patch.object(F.D, 'load_json', return_value=dict(profile='example', created=1, cwd=str(jd))), \
                 patch.object(F.D, 'effective_status', return_value=dict(state='running', started=1)), \
                 patch.object(F, 'newest_write', return_value=999):
                result = F.job_summary(jd, 1000)
            self.assertFalse(result['stalled'])
            self.assertEqual(result['idle'], 1)


class FeedCliTests(unittest.TestCase):
    def test_dump_never_binds_or_opens_browser(self):
        with patch.object(F, 'snapshot', return_value={'jobs': []}), \
             patch.object(F, 'ThreadingHTTPServer') as server, \
             patch.object(F.webbrowser, 'open') as browser, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            F.main(['--dump', '--open'])
        self.assertEqual(json.loads(out.getvalue()), {'jobs': []})
        server.assert_not_called()
        browser.assert_not_called()

    def test_server_uses_loopback_and_actual_port_and_closes(self):
        for args, should_open in [([], False), (['--open'], True)]:
            with self.subTest(args=args), patch.object(F, 'ThreadingHTTPServer') as server, \
                 patch.object(F.webbrowser, 'open', return_value=True) as browser, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                server.return_value.server_port = 9876
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                F.main(args + ['--port', '0'])
                server.assert_called_once_with(('127.0.0.1', 0), F.Handler)
                server.return_value.server_close.assert_called_once()
                self.assertIn('http://127.0.0.1:9876', out.getvalue())
                if should_open:
                    browser.assert_called_once_with('http://127.0.0.1:9876')
                else:
                    browser.assert_not_called()

    def test_browser_failure_keeps_feed_running_and_reports_url(self):
        for failure in (False, F.webbrowser.Error('unavailable')):
            with self.subTest(failure=failure), patch.object(F, 'ThreadingHTTPServer') as server, \
                 patch.object(F.webbrowser, 'open') as browser, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                server.return_value.server_port = 8765
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                if isinstance(failure, Exception):
                    browser.side_effect = failure
                else:
                    browser.return_value = failure
                F.main(['--open'])
                server.return_value.serve_forever.assert_called_once()
                self.assertIn('manually', out.getvalue())
                server.return_value.server_close.assert_called_once()
