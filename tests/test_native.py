# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Claude Code subagents: result blocks, evidence, alerts and queue release without a runner."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_agents as CA
import delegate as D
import feed as F
from tests.helpers import temporary_directory
from tests.test_results import png


def finished_transcript(td, text, name='agent-abc.jsonl'):
    tr = CA.Transcript(Path(td) / name)
    tr.feed(dict(type='assistant', timestamp='2026-11-11T10:00:00Z', message=dict(
        id='m1', model='claude-opus-5-5', stop_reason='end_turn', content=[dict(type='text', text=text)])))
    return tr


class FakeReader:
    def __init__(self, records):
        self.records = records

    def scan(self, now=None):
        return self.records


class NativeResultTests(unittest.TestCase):
    def test_final_message_with_a_block_is_checked_once(self):
        with temporary_directory() as td:
            shot = Path(td) / 'shot.png'
            png(shot, 1920, 1080)
            block = dict(verdict='pass', summary='Fixed.', evidence=[dict(path=str(shot), shows='menu', width=3840, height=2160)])
            tr = finished_transcript(td, 'Done.\n\n```result\n' + json.dumps(block) + '\n```')
            with patch.object(CA.results, 'digest', wraps=CA.results.digest) as digest:
                rec = tr.record(tr.last_line_at + 60, 480, 3600)
                tr.record(tr.last_line_at + 90, 480, 3600)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual((rec['state'], rec['verdict']), ('done', 'pass'))
            self.assertGreaterEqual(rec['flags'], 1)
            summary = F.claude_job_summary(dict(rec, parent=None), tr.last_line_at + 60)
            self.assertEqual((summary['verdict'], summary['flags']), ('pass', rec['flags']))

    def test_plain_final_message_has_no_verdict(self):
        with temporary_directory() as td:
            rec = finished_transcript(td, 'All done, nothing structured.').record(1e12, 480, 1e12)
            self.assertEqual((rec['checked'], rec['verdict'], rec['flags']), (None, None, 0))

    def test_evidence_is_listed_and_served_for_a_subagent(self):
        with temporary_directory() as td:
            shot = Path(td) / 'shot.png'
            png(shot, 640, 480)
            block = dict(verdict='pass', evidence=[dict(path=str(shot), shows='menu', width=640, height=480)])
            tr = finished_transcript(td, '```result\n' + json.dumps(block) + '\n```')
            rec = tr.record(tr.last_line_at + 60, 480, 3600)
            rec.update(parent=None, finished=F.time.time() - 30)
            with patch.object(F, 'claude_reader', return_value=FakeReader([rec])), \
                 patch.object(D, 'JOBS_ROOT', Path(td) / 'jobs'):
                listing = F.evidence(12)
                self.assertEqual([(j['id'], j['items'][0]['width']) for j in listing['jobs']], [('agent-abc', 640)])
                self.assertEqual(F.evidence_file('agent-abc', 0), ('image/png', shot.read_bytes()))
                self.assertIsNone(F.evidence_file('agent-abc', 1))
                self.assertIsNone(F.evidence_file('agent-other', 0))


class WatcherTests(unittest.TestCase):
    def rec(self, state, **kw):
        return dict(dict(id='agent-abc', label='Trace the pool', model='Opus 5.5', state=state, started=100, finished=400,
                         verdict=None, flags=0, checked=None), **kw)

    def test_alert_when_a_watched_subagent_finishes_and_queue_is_released(self):
        reader = FakeReader([self.rec('running')])
        w = F.Watcher()
        with patch.object(F, 'claude_reader', return_value=reader), patch.dict(F.CONFIG, notify=[dict(kind='json', url='http://x')]), \
             patch.object(D, 'start_ready') as start, patch.object(D.notify, 'send', return_value=[]) as send:
            w.step()
            send.assert_not_called()
            reader.records = [self.rec('done', verdict='pass', flags=1, checked=dict(data=dict(summary='Found it.')))]
            w.step()
            w.step()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], 'flagged')
            self.assertIn('Trace the pool', send.call_args.args[1])
            self.assertIn('Found it.', send.call_args.args[2])
            self.assertEqual(start.call_count, 3)

    def test_no_alert_for_agents_that_were_already_finished_or_when_alerts_are_off(self):
        reader = FakeReader([self.rec('done')])
        with patch.object(F, 'claude_reader', return_value=reader), patch.object(D, 'start_ready'), \
             patch.object(D.notify, 'send', return_value=[]) as send:
            with patch.dict(F.CONFIG, notify=[dict(kind='json', url='http://x')]):
                F.Watcher().step()
            w = F.Watcher()
            with patch.dict(F.CONFIG, notify=[]):
                reader.records = [self.rec('running')]
                w.step()
                reader.records = [self.rec('failed')]
                w.step()
            send.assert_not_called()

    def test_result_format_command_prints_the_rules(self):
        self.assertIn('```result', D.results.RULES)


if __name__ == '__main__':
    unittest.main()
