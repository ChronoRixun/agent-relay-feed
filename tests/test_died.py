# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Claude Code subagents cut off by a connection error, and the ids the board shows for subagents."""
import contextlib
import io
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_agents as CA
import delegate as D
from tests.helpers import temporary_directory

SSL = 'API Error: Unable to connect to API: SSL certificate has expired'


def died_transcript(td, text=SSL, name='agent-a88abea383c3cd1c5.jsonl'):
    tr = CA.Transcript(Path(td) / name)
    tr.feed(dict(type='assistant', timestamp='2026-11-11T06:00:00Z', message=dict(
        id='m1', model='claude-opus-5-5', stop_reason='tool_use', content=[dict(type='tool_use', id='t1', name='Bash',
                                                                              input=dict(command='ls'))])))
    tr.feed(dict(type='user', timestamp='2026-11-11T06:00:05Z', message=dict(content=[dict(type='tool_result', tool_use_id='t1')])))
    tr.feed(dict(type='assistant', timestamp='2026-11-11T06:31:45Z', isApiErrorMessage=True, error='server_error',
                 message=dict(id='m2', model='<synthetic>', stop_reason='stop_sequence', content=[dict(type='text', text=text)])))
    return tr


def rec(agent_id, agent_type='general-purpose', state='running', **kw):
    return dict(dict(id='agent-' + agent_id, agent_id=agent_id, agent_type=agent_type, label='Boot loop', state=state,
                     started=100, finished=None, died=False, resume=None), **kw)


class DetectionTests(unittest.TestCase):
    def test_ssl_error_is_a_death_with_a_resume_step(self):
        with temporary_directory() as td:
            tr = died_transcript(td)
            r = tr.record(tr.last_line_at + 60, 480, 3600)
            self.assertEqual((r['state'], r['died']), ('failed', True))
            self.assertIn('SendMessage to "a88abea383c3cd1c5"', r['resume'])
            self.assertIn('Died of a connection error', r['errors'][0])
            self.assertIn('SSL certificate has expired', r['errors'][0])

    def test_other_api_errors_are_plain_failures(self):
        with temporary_directory() as td:
            tr = died_transcript(td, text='API Error: prompt is too long')
            r = tr.record(tr.last_line_at + 60, 480, 3600)
            self.assertEqual((r['state'], r['died'], r['resume']), ('failed', False, None))

    def test_a_resumed_agent_is_no_longer_dead(self):
        with temporary_directory() as td:
            tr = died_transcript(td)
            tr.feed(dict(type='user', timestamp='2026-11-11T07:00:00Z', message=dict(content='Your run was cut off...')))
            r = tr.record(tr.last_line_at + 10, 480, 3600)
            self.assertEqual((r['state'], r['died']), ('running', False))


class ResolveTests(unittest.TestCase):
    records = [rec('a88abea383c3cd1c5'), rec('a0326e8dbaaa10397', agent_type='Explore'),
               rec('b1111111111110397', agent_type='general-purpose')]

    def test_every_form_the_board_and_feed_show(self):
        with temporary_directory() as td, patch.object(D, 'JOBS_ROOT', Path(td)):
            (Path(td) / '20261111-job-x').mkdir()
            r = lambda ref: D.resolve_ref(ref, self.records)
            self.assertEqual(r('20261111-job-x'), '20261111-job-x')
            self.assertEqual(r('agent-a88abea383c3cd1c5'), 'agent-a88abea383c3cd1c5')
            self.assertEqual(r('a88abea383c3cd1c5'), 'agent-a88abea383c3cd1c5')
            self.assertEqual(r('a88abea'), 'agent-a88abea383c3cd1c5')
            self.assertEqual(r('claude:general-purpose#d1c5'), 'agent-a88abea383c3cd1c5')
            self.assertEqual(r('general-purpose#d1c5'), 'agent-a88abea383c3cd1c5')
            self.assertEqual(r('Explore#0397'), 'agent-a0326e8dbaaa10397')
            with self.assertRaisesRegex(ValueError, 'matches 2'):
                r('#0397')
            with self.assertRaisesRegex(ValueError, 'no such job'):
                r('claude:Plan#d1c5')
            # a full id that has aged out of the transcript window is still accepted
            self.assertEqual(r('agent-ffffffffffffffff1'), 'agent-ffffffffffffffff1')

    def test_reviewed_accepts_the_short_label(self):
        with temporary_directory() as td, patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', DELEGATE_ROOT=Path(td)), \
             patch.object(D, '_claude_records', return_value=self.records), contextlib.redirect_stdout(io.StringIO()) as o:
            D.cmd_reviewed(SimpleNamespace(job='claude:general-purpose#d1c5', undo=False))
            self.assertIn('agent-a88abea383c3cd1c5', D.load_reviewed())
            self.assertIn('agent-a88abea383c3cd1c5: marked', o.getvalue())


class BoardAndWaitTests(unittest.TestCase):
    def dead(self, finished):
        return rec('a88abea383c3cd1c5', state='failed', finished=finished, died=True,
                   resume=CA.resume_hint('a88abea383c3cd1c5'))

    def test_board_shows_full_ids_and_died_agents(self):
        now = time.time()
        records = [rec('c22222222222abcd', started=now - 60, last_activity=now - 5, workdir=None), self.dead(now - 120)]
        with temporary_directory() as td, patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', DELEGATE_ROOT=Path(td)), \
             patch.object(D, '_claude_records', return_value=records), contextlib.redirect_stdout(io.StringIO()) as o:
            D.cmd_board(SimpleNamespace(hours=12))
            text = o.getvalue()
            self.assertIn('agent-c22222222222abcd (general-purpose)', text)
            self.assertIn('DIED OF A CONNECTION ERROR', text)
            self.assertIn('SendMessage to "a88abea383c3cd1c5"', text)
            D.set_reviewed('agent-a88abea383c3cd1c5')
            with contextlib.redirect_stdout(io.StringIO()) as o2:
                D.cmd_board(SimpleNamespace(hours=12))
            self.assertNotIn('DIED', o2.getvalue())

    def test_wait_any_reports_a_death_once_and_history_is_not_news(self):
        now = time.time()
        old, new = self.dead(now - 600), rec('d3333333333333333', state='failed', finished=now - 5, died=True,
                                             resume=CA.resume_hint('d3333333333333333'))
        reader = SimpleNamespace(scan=lambda: records)
        with temporary_directory() as td, patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', DELEGATE_ROOT=Path(td)), \
             patch.object(CA, 'ClaudeAgents', return_value=reader):
            (Path(td) / 'jobs').mkdir()
            records = [old]
            with contextlib.redirect_stdout(io.StringIO()) as o:
                D.wait_any(5)       # first run on this machine: the old death is history
            self.assertNotIn('DIED', o.getvalue())
            self.assertIn('NOTHING RUNNING', o.getvalue())
            records = [old, new]
            with contextlib.redirect_stdout(io.StringIO()) as o:
                D.wait_any(5)
            self.assertIn('DIED: agent-d3333333333333333', o.getvalue())
            self.assertIn('SendMessage to "d3333333333333333"', o.getvalue())
            self.assertNotIn('a88abea', o.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as o:
                D.wait_any(5)
            self.assertNotIn('DIED', o.getvalue())

    def test_wait_any_keeps_waiting_while_subagents_run(self):
        records = [rec('e4444444444444444')]
        reader = SimpleNamespace(scan=lambda: records)
        with temporary_directory() as td, patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', DELEGATE_ROOT=Path(td), WAIT_ANY_POLL_S=0.01), \
             patch.object(CA, 'ClaudeAgents', return_value=reader), contextlib.redirect_stdout(io.StringIO()) as o:
            (Path(td) / 'jobs').mkdir()
            started = time.time()
            D.wait_any(0.3)
            self.assertGreaterEqual(time.time() - started, 0.25)
            self.assertIn('STILL RUNNING: 1 (agent-e4444444444444444)', o.getvalue())


if __name__ == '__main__':
    unittest.main()
