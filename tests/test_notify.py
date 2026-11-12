# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import delegate as D
import delegate_config as C
import feed as F
import notify as N
from tests.helpers import temporary_directory
from tests.test_messaging import codex_command
from tests.test_phase import make_job

TARGETS = [dict(kind='ntfy', url='https://ntfy.example/topic'), dict(kind='discord', url='https://discord.example/hook'),
           dict(kind='json', url='http://127.0.0.1:9/hook')]


def config(**kw):
    return dict(notify=TARGETS, notify_on=list(N.EVENTS), notify_detail=True, notify_link='http://pc:8765', **kw)


class SendTests(unittest.TestCase):
    def collect(self, fn, *args, **kw):
        sent = []
        errors = fn(*args, post=lambda url, data, headers: sent.append((url, data, headers)), **kw)
        return sent, errors

    def test_each_kind_gets_its_own_shape(self):
        sent, errors = self.collect(N.question, 'Trace the crash', 'GPT Sol', 'Patch it, or report only?', config=config())
        self.assertEqual(errors, [])
        ntfy, discord, hook = sent
        self.assertEqual(ntfy[2]['Title'], 'Trace the crash needs an answer')
        self.assertEqual((ntfy[2]['Priority'], ntfy[2]['Click']), ('high', 'http://pc:8765'))
        self.assertIn(b'Patch it, or report only?', ntfy[1])
        self.assertIn('**Trace the crash needs an answer**', json.loads(discord[1])['content'])
        self.assertEqual(json.loads(hook[1])['event'], 'question')

    def test_off_when_nothing_is_configured_or_the_event_is_not_wanted(self):
        self.assertEqual(self.collect(N.question, 'a', 'b', 'c', config=dict(notify=[]))[0], [])
        only_questions = config()
        only_questions['notify_on'] = ['question']
        self.assertEqual(self.collect(N.outcome, 'a', 'b', 'done', config=only_questions)[0], [])

    def test_detail_off_sends_no_question_text(self):
        cfg = config()
        cfg['notify_detail'] = False
        sent, _ = self.collect(N.question, 'Trace the crash', 'GPT Sol', 'SECRET DETAIL', config=cfg)
        self.assertTrue(sent)
        self.assertFalse(any(b'SECRET' in data for _url, data, _h in sent))

    def test_outcome_events(self):
        def event(*args, **kw):
            sent, _ = self.collect(N.outcome, 'job', 'GPT', *args, config=dict(config(), notify=[TARGETS[2]]), **kw)
            return json.loads(sent[0][1])
        self.assertEqual(event('done', 'pass')['event'], 'done')
        flagged = event('done', 'pass', 2, 'Fixed it.', '1h 02m')
        self.assertEqual(flagged['event'], 'flagged')
        self.assertIn('2 claim(s) flagged', flagged['title'])
        self.assertIn('Fixed it.', flagged['body'])
        self.assertEqual(event('failed')['event'], 'failed')
        self.assertEqual(event('done', 'fail')['event'], 'failed')
        self.assertEqual(event('timeout')['event'], 'failed')

    def test_a_failing_target_is_reported_not_raised(self):
        def post(url, data, headers):
            if 'discord' in url:
                raise OSError('network down')
        errors = N.send('done', 'Title — with a dash', 'body', config=config(), post=post)
        self.assertEqual(len(errors), 1)
        self.assertIn('discord', errors[0])

    def test_config_validation(self):
        self.assertTrue(C._valid('notify', TARGETS))
        self.assertFalse(C._valid('notify', [dict(kind='sms', url='https://x')]))
        self.assertFalse(C._valid('notify', [dict(kind='ntfy', url='ftp://x')]))
        self.assertTrue(C._valid('notify_on', ['question']))
        self.assertFalse(C._valid('notify_on', ['everything']))
        self.assertTrue(C._valid('feed_pin', '4821'))
        self.assertTrue(C._valid('feed_pin', ''))
        self.assertFalse(C._valid('feed_pin', '12'))


class RunnerAlertTests(unittest.TestCase):
    def test_question_and_outcome_alert_once_each_with_the_job_name(self):
        with temporary_directory() as td, \
             patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', AGENTS_ROOT=Path(td) / 'agents', DELEGATE_ROOT=Path(td)), \
             patch.object(D.notify, 'send', return_value=[]) as send:
            jd, meta = make_job(td, 'job-a')
            meta['name'] = 'Trace the crash'
            (jd / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
            (Path(td) / 'agents' / 'example' / 'reports').mkdir(parents=True)
            ask = 'python "D:/x/delegate.py" ask --job job-a "Patch it?"'
            (jd / 'events.jsonl').write_text(json.dumps(codex_command('item.started', ask)) + '\n' +
                                             json.dumps(codex_command('item.started', ask, 'item_2')) + '\n', encoding='utf-8')
            D.ActivityTail(meta, jd).pump()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[:2], ('question', 'Trace the crash needs an answer'))
            (jd / 'result.md').write_text('All done.', encoding='utf-8')
            D.finalize(jd, meta, dict(state='running', started=1), None, 0)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[:2], ('done', 'Trace the crash: done'))

    def test_run_name_is_the_title(self):
        self.assertEqual(D.job_title(dict(name='Release gates', cwd='/work/repo'), 'job-1'), 'Release gates')
        self.assertEqual(D.job_title(dict(cwd='/work/repo'), 'job-1'), 'repo')
        self.assertEqual(F.task_name(dict(name='Release gates', cwd='/work/repo'), 'job-1'), 'Release gates')


class FeedAnswerTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = self._td.__enter__()
        self.addCleanup(self._td.__exit__, None, None, None)
        for p in (patch.multiple(D, JOBS_ROOT=Path(self.td) / 'jobs', AGENTS_ROOT=Path(self.td) / 'agents',
                                 runner_alive=lambda st: True),
                  patch.dict(F.CONFIG, feed_pin='4821'), patch.dict(F._pin_failures, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), F.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, **body):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode('utf-8', 'replace')

    def test_answer_and_tell_with_the_pin(self):
        jd, _ = make_job(self.td, 'job-a')
        D.journal(jd, 'asked', 'Patch it?')
        code, body = self.post('/api/answer', pin='4821', job='job-a', text='Report only.')
        self.assertEqual(code, 200)
        self.assertIn('ANSWERED job-a', body['message'])
        self.assertIsNone(D.open_question(jd))
        self.assertEqual(D.read_inbox(jd)[0]['text'], 'Report only.')
        code, body = self.post('/api/tell', pin='4821', job='job-a', text='1080 spills too')
        self.assertEqual(code, 200)
        self.assertEqual(D.read_inbox(jd)[1]['kind'], 'tell')
        # nothing to answer any more
        self.assertEqual(self.post('/api/answer', pin='4821', job='job-a', text='again')[0], 409)

    def test_wrong_pin_changes_nothing_and_locks_out_after_five(self):
        jd, _ = make_job(self.td, 'job-a')
        D.journal(jd, 'asked', 'Patch it?')
        for _ in range(5):
            self.assertEqual(self.post('/api/answer', pin='0000', job='job-a', text='yes')[0], 401)
        self.assertEqual(self.post('/api/answer', pin='4821', job='job-a', text='yes')[0], 429)
        self.assertIsNotNone(D.open_question(jd))
        self.assertEqual(D.read_inbox(jd), [])

    def test_bad_jobs_paths_and_bodies(self):
        self.assertEqual(self.post('/api/answer', pin='4821', job='../x', text='yes')[0], 404)
        self.assertEqual(self.post('/api/answer', pin='4821', job='nope', text='yes')[0], 404)
        self.assertEqual(self.post('/api/cancel', pin='4821', job='job-a')[0], 404)

    def test_read_only_without_a_pin(self):
        make_job(self.td, 'job-a')
        with patch.dict(F.CONFIG, feed_pin=''):
            code, body = self.post('/api/tell', pin='', job='job-a', text='hello')
            self.assertEqual(code, 403)
            self.assertIn('feed_pin', body['error'])
            self.assertFalse(F.snapshot(1)['can_answer'])
        self.assertTrue(F.snapshot(1)['can_answer'])


if __name__ == '__main__':
    unittest.main()
