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
from tests.test_phase import make_job


def codex_command(kind, command, item_id='item_1', exit_code=None):
    return dict(type=kind, item=dict(id=item_id, type='command_execution', command=command, exit_code=exit_code))


class MessagingParserTests(unittest.TestCase):
    def test_ask_and_check_shapes(self):
        samples = [
            ('python "D:/x/delegate.py" ask --job 20261111-1-gpt-a1b2 "Private desktop cannot start D3D. Use the visible one?"',
             ('ask', 'Private desktop cannot start D3D. Use the visible one?')),
            # PowerShell wrapper; the question has quotes of its own
            ('"powershell.exe" -Command \'python "D:/x/delegate.py" ask --job j-1 "Is "windowed" ok, or 1080?"\'',
             ('ask', 'Is "windowed" ok, or 1080?')),
            # JSON-escaped tool input (Kimi and Claude API event streams)
            ('tool Bash: {"command": "python \\"D:/x/delegate.py\\" ask --job j-1 --wait 60 \\"Push now?\\""}',
             ('ask', 'Push now?')),
            ('python delegate.py check --job j-1', ('check', None)),
            ('python "D:/x/delegate.py" phase --job j-1 "build gates"', ('phase', 'build gates')),
            ('python delegate.py tell j-1 "not a worker command"', (None, None)),
        ]
        for text, expected in samples:
            with self.subTest(text=text):
                self.assertEqual(C.worker_command(text), expected)


class MessagingTests(unittest.TestCase):
    def patched(self, root):
        return patch.multiple(D, JOBS_ROOT=Path(root) / 'jobs', AGENTS_ROOT=Path(root) / 'agents',
                              runner_alive=lambda st: True, WAIT_ANY_POLL_S=0.05, ASK_POLL_S=0.05)

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(SimpleNamespace(**kw))
        return out.getvalue()

    def test_codex_question_is_journalled_once_and_cleared_by_the_answer(self):
        with temporary_directory() as td, self.patched(td):
            jd, meta = make_job(td, 'job-a')
            ask = 'python "D:/x/delegate.py" ask --job job-a "Use the visible desktop?"'
            tail = D.ActivityTail(meta, jd)

            def feed(*events):
                with open(jd / 'events.jsonl', 'a', encoding='utf-8') as f:
                    f.writelines(json.dumps(e) + '\n' for e in events)
                tail.pump()

            feed(codex_command('item.started', ask))
            self.assertEqual(D.open_question(jd)['text'], 'Use the visible desktop?')
            # the wait ran out and the worker asked again: still one question
            feed(codex_command('item.completed', ask, exit_code=0), codex_command('item.started', ask, 'item_2'))
            self.assertEqual([e['kind'] for e in D.read_journal(jd)], ['asked'])
            self.call(D.cmd_answer, job='job-a', text=['Yes,', 'visible is fine'])
            feed(codex_command('item.completed', ask, 'item_2', exit_code=0))
            self.assertIsNone(D.open_question(jd))
            self.assertEqual([e['kind'] for e in D.read_journal(jd)], ['asked', 'answered'])
            self.assertEqual(D.read_inbox(jd)[0]['question'], 'Use the visible desktop?')

    def test_other_backends_ask_through_the_tool_call_line(self):
        with temporary_directory() as td, self.patched(td):
            jd, meta = make_job(td, 'job-k', backend='kimi')
            call = dict(tool_calls=[dict(function=dict(name='Shell', arguments=json.dumps(
                dict(command='python "D:/x/delegate.py" ask --job job-k "Push now?"'))))])
            (jd / 'events.jsonl').write_text(json.dumps(call) + '\n', encoding='utf-8')
            D.ActivityTail(meta, jd).pump()
            self.assertEqual(D.open_question(jd)['text'], 'Push now?')

    def test_ask_prints_the_answer_or_asks_to_be_run_again(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            D.journal(jd, 'asked', 'Use the visible desktop?')
            late = self.call(D.cmd_ask, job='job-a', wait=0.1, text=['Use the visible desktop?'])
            self.assertIn('NO ANSWER YET', late)
            self.assertIn('same ask command', late)
            self.call(D.cmd_answer, job='job-a', text=['Yes'])
            time.sleep(1.1)
            # the answer arrived between two waits: the repeated command still gets it
            again = self.call(D.cmd_ask, job='job-a', wait=0.1, text=['Use  the visible desktop?'])
            self.assertIn('ANSWER FROM YOUR SUPERVISOR: Yes', again)
            other = self.call(D.cmd_ask, job='job-a', wait=0.1, text=['A different question?'])
            self.assertIn('NO ANSWER YET', other)

    def test_ask_returns_as_soon_as_the_answer_lands(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            D.journal(jd, 'asked', 'Push now?')

            def answer(_secs):
                if not D.read_inbox(jd):
                    D.send(jd, 'answer', 'No, wait for Sam', question='Push now?')

            with patch.object(D.time, 'sleep', side_effect=answer):
                text = self.call(D.cmd_ask, job='job-a', wait=30, text=['Push now?'])
            self.assertIn('ANSWER FROM YOUR SUPERVISOR: No, wait for Sam', text)

    def test_worker_commands_never_fail_on_a_bad_job(self):
        with temporary_directory() as td, self.patched(td):
            self.assertIn('best judgement', self.call(D.cmd_ask, job='nope', wait=0, text=['Q?']))
            self.assertIn('No messages', self.call(D.cmd_check, job='../x'))
            self.assertEqual(self.call(D.cmd_phase, job=None, text=['triage']).strip(), 'ok')

    def test_tell_is_queued_then_delivered_once_at_the_next_stage(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            self.assertIn('No messages', self.call(D.cmd_phase, job='job-a', text=['triage']))
            self.assertIn('QUEUED', self.call(D.cmd_tell, job='job-a', text=['1080', 'spills', 'too']))
            self.assertEqual(D.phase_info(jd)['queued'], 1)
            first = self.call(D.cmd_phase, job='job-a', text=['live', 'test'])
            self.assertIn('NEW MESSAGES FROM YOUR SUPERVISOR', first)
            self.assertIn('1080 spills too', first)
            D.journal(jd, 'phase', 'live test')          # what the runner records for that command
            self.assertEqual(D.phase_info(jd)['queued'], 0)
            self.assertIn('No new messages', self.call(D.cmd_check, job='job-a'))

    def test_read_only_worker_gets_every_message_each_time(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            D.send(jd, 'tell', 'Fullscreen works')
            with patch.object(Path, 'write_text', side_effect=PermissionError):
                for _ in range(2):
                    text = self.call(D.cmd_check, job='job-a')
                    self.assertIn('all so far', text)
                    self.assertIn('Fullscreen works', text)

    def test_tell_and_answer_refuse_when_they_cannot_land(self):
        with temporary_directory() as td, self.patched(td):
            make_job(td, 'job-done', state='done')
            make_job(td, 'job-run')
            with self.assertRaises(SystemExit) as stop:
                self.call(D.cmd_tell, job='job-done', text=['hello'])
            self.assertIn('--resume', str(stop.exception))
            with self.assertRaises(SystemExit) as stop:
                self.call(D.cmd_answer, job='job-run', text=['yes'])
            self.assertIn('no open question', str(stop.exception))

    def test_wait_any_wakes_once_per_question(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            D.journal(jd, 'asked', 'Use the visible desktop?')
            first = self.call(D.cmd_wait, job=None, any=True, wait=60)
            self.assertIn('QUESTION: job-a', first)
            self.assertIn('Use the visible desktop?', first)
            self.assertIn('answer job-a', first)
            self.assertNotIn('QUESTION', self.call(D.cmd_wait, job=None, any=True, wait=0))
            self.call(D.cmd_answer, job='job-a', text=['Yes'])
            time.sleep(0.01)
            D.journal(jd, 'asked', 'And push afterwards?')
            self.assertIn('And push afterwards?', self.call(D.cmd_wait, job=None, any=True, wait=60))

    def test_board_and_feed_show_the_question(self):
        with temporary_directory() as td, self.patched(td):
            jd, _ = make_job(td, 'job-a')
            D.journal(jd, 'asked', 'Use the visible desktop?')
            D.send(jd, 'tell', 'Fullscreen works')
            with patch.object(D, '_claude_records', return_value=[]):
                board = self.call(D.cmd_board, hours=12)
            self.assertIn('ASKING', board)
            self.assertIn('Use the visible desktop?', board)
            self.assertIn('1 queued', board)
            self.assertNotIn('QUIET', board)
            summary = F.job_summary(jd, time.time() + 9999)
            self.assertEqual(summary['question'], 'Use the visible desktop?')
            self.assertFalse(summary['stalled'])
            self.assertEqual(summary['queued'], 1)


if __name__ == '__main__':
    unittest.main()
