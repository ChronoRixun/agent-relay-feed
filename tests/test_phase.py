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
import feed as F
from tests.helpers import temporary_directory


class PhaseParserTests(unittest.TestCase):
    def test_command_shapes(self):
        samples = [
            ('command (exit 0): python "D:/x/delegate.py" phase "live test 2/5"', 'live test 2/5'),
            # PowerShell wrapper around a single-quoted name, and a failed command still counts
            ('command (exit 1): "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '
             '"python \\"D:/x/delegate.py\\" phase \'build gates\'"', 'build gates'),
            # JSON-escaped tool input (Kimi and Claude API event streams)
            ('tool Bash: {"command": "python \\"D:/x/delegate.py\\" phase \\"reading code\\""}', 'reading code'),
            ('python delegate.py phase triage; ls', 'triage'),
            ('python3 /opt/relay/delegate.py   phase   "spaced   out"', 'spaced out'),
            ('python delegate.py wait some-job', None),
            ('python delegate.py phase ""', None),
            ('message: I will report the phase soon', None),
            (None, None),
        ]
        for text, expected in samples:
            with self.subTest(text=text):
                self.assertEqual(C.phase_from_command(text), expected)

    def test_long_names_are_cut(self):
        self.assertEqual(len(C.phase_from_command('delegate.py phase "' + 'x' * 500 + '"')), 120)


def make_job(root, job_id, state='running', backend='codex', **status):
    jd = Path(root) / 'jobs' / job_id
    jd.mkdir(parents=True)
    activity = jd / 'activity.log'
    activity.write_text('', encoding='utf-8')
    meta = dict(profile='example', label='Example (high)', backend=backend, model='m', effort='high',
                mode='read', cwd=str(jd), created=time.time() - 600, activity_file=str(activity),
                progress_file=str(jd / 'progress.md'))
    (jd / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
    st = dict(state=state, started=time.time() - 600, **status)
    if state in D.TERMINAL:
        st.setdefault('finished', time.time() - 5)
    (jd / 'status.json').write_text(json.dumps(st), encoding='utf-8')
    return jd, meta


class JournalTests(unittest.TestCase):
    def test_activity_tail_records_phases_once_each(self):
        with temporary_directory() as td:
            jd, meta = make_job(td, 'job-a')
            events = [
                dict(type='thread.started', thread_id='s1'),
                dict(type='item.completed', item=dict(type='command_execution', exit_code=0,
                                                       command='python "D:/x/delegate.py" phase "triage"')),
                dict(type='item.completed', item=dict(type='agent_message',
                                                       text='next I run delegate.py phase "not a command"')),
                dict(type='item.completed', item=dict(type='command_execution', exit_code=0, command='git status')),
                dict(type='item.completed', item=dict(type='command_execution', exit_code=1,
                                                       command='python "D:/x/delegate.py" phase "live test 1 of 3"')),
            ]
            (jd / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
            tail = D.ActivityTail(meta, jd)
            tail.pump()
            tail.pump()
            journal = D.read_journal(jd)
            self.assertEqual([(e['kind'], e['text']) for e in journal],
                             [('phase', 'triage'), ('phase', 'live test 1 of 3')])
            info = D.phase_info(jd)
            self.assertEqual((info['phase'], info['phases']), ('live test 1 of 3', 2))
            self.assertAlmostEqual(info['phase_since'], time.time(), delta=5)

    def test_no_journal_means_no_phase(self):
        with temporary_directory() as td:
            jd, _meta = make_job(td, 'job-a')
            self.assertEqual(D.phase_info(jd), dict(phase=None, phase_since=None, phases=0,
                                                    question=None, question_since=None, queued=0))

    def test_prompt_tells_the_worker_how_to_report(self):
        meta = dict(mode='read', backend='codex', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
        text = D.build_prompt(None, meta, 'Do the task.', [])
        self.assertIn('delegate.py" phase --job job-7 "', text)
        self.assertIn('delegate.py" ask --job job-7 "', text)
        self.assertEqual(C.phase_from_command(text.split('\n')[[i for i, l in enumerate(text.split('\n'))
                                                               if 'delegate.py" phase' in l][0]]),
                         '<3-8 words naming the stage>')

    def test_feed_summary_carries_the_phase(self):
        with temporary_directory() as td:
            jd, _meta = make_job(td, 'job-a')
            D.journal(jd, 'phase', 'build gates')
            result = F.job_summary(jd, time.time())
            self.assertEqual(result['phase'], 'build gates')
            self.assertEqual(result['phases'], 1)
            # the mission-control timeline draws from the stage history
            self.assertEqual([(s['kind'], s['text']) for s in result['stages']], [('phase', 'build gates')])

    def test_mission_control_page_is_served(self):
        sent = []
        handler = SimpleNamespace(path='/mission?demo', _send=lambda code, body, ctype: sent.append((code, body, ctype)))
        F.Handler.do_GET(handler)
        self.assertEqual(sent[0][0], 200)
        self.assertIn('MISSION CONTROL', sent[0][1])


class ClaudeSubagentPhaseTests(unittest.TestCase):
    def test_transcript_phase_from_bash_command(self):
        import claude_agents as CA
        with temporary_directory() as td:
            tr = CA.Transcript(Path(td) / 'agent-abc.jsonl')

            def tool(stamp, name, inp, tid):
                tr.feed(dict(type='assistant', timestamp=stamp, message=dict(
                    id=tid, model='claude-opus-5-5', stop_reason='tool_use',
                    content=[dict(type='tool_use', id=tid, name=name, input=inp)])))

            tool('2026-11-11T10:00:00Z', 'Bash', dict(command='python "D:/x/delegate.py" phase "trace the pool"'), 't1')
            tool('2026-11-11T10:05:00Z', 'Write', dict(file_path='/n.md', content='delegate.py phase "not a command"'), 't2')
            tool('2026-11-11T10:09:00Z', 'Bash', dict(command='git status'), 't3')
            rec = tr.record(tr.last_line_at + 1, 480, 3600)
            self.assertEqual((rec['phase'], rec['phases']), ('trace the pool', 1))
            self.assertEqual(rec['phase_since'], tr.started)


class ClaudeSubagentHandbackTests(unittest.TestCase):
    def test_handing_the_report_back_ends_the_agent(self):
        import claude_agents as CA
        with temporary_directory() as td:
            tr = CA.Transcript(Path(td) / 'agent-abc.jsonl')
            tr.feed(dict(type='assistant', timestamp='2026-11-11T10:00:00Z', message=dict(
                id='m1', model='claude-opus-5-5', stop_reason='tool_use',
                content=[dict(type='tool_use', id='t1', name='SubagentHandback', input=dict(message='## Report\nFound it.'))])))
            tr.feed(dict(type='user', timestamp='2026-11-11T10:00:01Z', message=dict(
                content=[dict(type='tool_result', tool_use_id='t1', content='ok')])))
            rec = tr.record(tr.last_line_at + 60, 480, 3600)
            self.assertEqual(rec['state'], 'done')
            self.assertIn('Found it.', rec['result'])
            # a follow-up message puts it back to work
            tr.feed(dict(type='user', timestamp='2026-11-11T10:05:00Z', message=dict(content='One more thing')))
            self.assertEqual(tr.record(tr.last_line_at + 60, 480, 3600)['state'], 'running')


class WaitAnyTests(unittest.TestCase):
    def run_wait(self, root, wait=0):
        out = io.StringIO()
        with patch.object(D, 'JOBS_ROOT', Path(root) / 'jobs'), \
             patch.object(D, 'AGENTS_ROOT', Path(root) / 'agents'), \
             patch.object(D, 'runner_alive', return_value=True), \
             patch.object(D, 'WAIT_ANY_POLL_S', 0.05), \
             contextlib.redirect_stdout(out):
            D.cmd_wait(SimpleNamespace(job=None, any=True, wait=wait))
        return out.getvalue()

    def test_nothing_running(self):
        with temporary_directory() as td:
            self.assertIn('NOTHING RUNNING', self.run_wait(td))

    def test_reports_each_outcome_exactly_once(self):
        with temporary_directory() as td:
            make_job(td, 'job-old', state='done')                     # finished before the journal existed
            done, _ = make_job(td, 'job-done', state='done')
            D.journal(done, 'done')
            (done / 'result.md').write_text('All gates pass.', encoding='utf-8')
            make_job(td, 'job-run')
            first = self.run_wait(td)
            self.assertIn('OUTCOME: job-done done', first)
            self.assertIn('All gates pass.', first)
            self.assertNotIn('job-old', first)
            self.assertIn('STILL RUNNING: 1', first)
            second = self.run_wait(td)
            self.assertNotIn('OUTCOME', second)
            self.assertIn('STILL RUNNING: 1', second)

    def test_wakes_when_a_running_job_ends(self):
        with temporary_directory() as td:
            jd, _ = make_job(td, 'job-run')
            calls = []

            def finish(_secs):
                calls.append(1)
                st = json.loads((jd / 'status.json').read_text())
                st.update(state='failed', finished=time.time(), errors=['boom'])
                (jd / 'status.json').write_text(json.dumps(st), encoding='utf-8')
                D.journal(jd, 'failed')

            with patch.object(D.time, 'sleep', side_effect=finish):
                text = self.run_wait(td, wait=60)
            self.assertEqual(len(calls), 1)
            self.assertIn('OUTCOME: job-run failed', text)
            self.assertIn('boom', text)
            self.assertIn('NOTHING RUNNING', text)

    def test_plain_wait_marks_the_outcome_as_seen(self):
        with temporary_directory() as td:
            done, _ = make_job(td, 'job-done', state='done')
            D.journal(done, 'done')
            with patch.object(D, 'JOBS_ROOT', Path(td) / 'jobs'), patch.object(D, 'AGENTS_ROOT', Path(td) / 'agents'), \
                 contextlib.redirect_stdout(io.StringIO()):
                D.cmd_wait(SimpleNamespace(job='job-done', any=False, wait=0))
            self.assertNotIn('OUTCOME', self.run_wait(td))


class BoardTests(unittest.TestCase):
    def test_one_line_per_running_agent(self):
        with temporary_directory() as td:
            jd, _ = make_job(td, 'job-run')
            D.journal(jd, 'phase', 'live test 2 of 5')
            failed, _ = make_job(td, 'job-bad', state='failed')
            claude = [dict(id='agent-abc', label='Trace the pool', model='Opus 5.5', agent_type='engineer',
                           state='running', started=time.time() - 120, last_activity=time.time() - 1200,
                           workdir=None, stalled=True, phase=None, phase_since=None, finished=None)]
            out = io.StringIO()
            with patch.object(D, 'JOBS_ROOT', Path(td) / 'jobs'), patch.object(D, 'AGENTS_ROOT', Path(td) / 'agents'), \
                 patch.object(D, 'runner_alive', return_value=True), \
                 patch.object(D, '_claude_records', return_value=claude), \
                 contextlib.redirect_stdout(out):
                D.cmd_board(SimpleNamespace(hours=12))
            lines = out.getvalue().splitlines()
            self.assertIn('RUNNING: 2', lines[0])
            run = next(l for l in lines if 'job-run' in l)
            self.assertIn('phase: live test 2 of 5', run)
            native = next(l for l in lines if 'Trace the pool' in l)
            self.assertIn('no phase reported', native)
            self.assertIn('QUIET', native)
            self.assertTrue(any('job-bad' in l and 'failed' in l for l in lines))


class FeedHostTests(unittest.TestCase):
    def test_host_flag_and_lan_url(self):
        with patch.object(F, 'ThreadingHTTPServer') as server, patch.object(F, 'lan_address', return_value='192.0.2.7'), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            server.return_value.server_port = 9876
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            F.main(['--host', '0.0.0.0', '--port', '0'])
        server.assert_called_once_with(('0.0.0.0', 0), F.Handler)
        self.assertIn('http://192.0.2.7:9876', out.getvalue())
        self.assertIn('http://127.0.0.1:9876', out.getvalue())

    def test_host_config_key(self):
        self.assertTrue(C._valid('feed_host', '0.0.0.0'))
        self.assertFalse(C._valid('feed_host', ''))
        self.assertEqual(C.DEFAULTS['feed_host'], '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
