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
import feed as F
from tests.helpers import temporary_directory
from tests.test_phase import make_job

PROF = dict(backend='codex', model='m', effort='high', label='Example (high)')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        self.spawned = []
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 runner_alive=lambda st: True, WAIT_ANY_POLL_S=0.05,
                                 spawn_runner=lambda jd, meta: self.spawned.append(jd.name)),
                  patch.dict(D.PROFILES, example=PROF), patch.dict(D.CONFIG, queue_max_running=3, auto_resume=1),
                  patch.object(D.notify, 'send', return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.brief = self.td / 'brief.md'
        self.brief.write_text('Do the next thing.', encoding='utf-8')

    def run_job(self, **kw):
        args = dict(profile='example', prompt_file=str(self.brief), cwd=str(self.td), mode='read', preamble=[],
                    effort=None, add_dir=[], resume=None, wait=0, max_minutes=30, approved=None, max_usd=None,
                    name=None, after=None, after_ok=False)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.cmd_run(SimpleNamespace(**args))
        return out.getvalue()

    def state(self, job):
        return json.loads((self.td / 'jobs' / job / 'status.json').read_text())['state']

    def queued_id(self, text):
        return text.split('QUEUED: ')[1].split()[0]

    def end(self, job, state='done', **status):
        jd = self.td / 'jobs' / job
        st = json.loads((jd / 'status.json').read_text())
        st.update(state=state, finished=time.time(), **status)
        (jd / 'status.json').write_text(json.dumps(st), encoding='utf-8')

    def test_plain_run_starts_at_once(self):
        text = self.run_job(name='First')
        self.assertIn('STARTED:', text)
        self.assertEqual(len(self.spawned), 1)

    def test_job_waits_for_its_dependency_then_starts(self):
        make_job(self.td, 'job-a')
        text = self.run_job(after=['job-a'], name='Second')
        job = self.queued_id(text)
        self.assertIn('starts when job-a has ended', text)
        self.assertIn('QUEUED BEHIND: job-a', text)
        self.assertEqual((self.state(job), self.spawned), ('queued', []))
        self.assertEqual(D.start_ready(), [])
        self.end('job-a', 'failed')                     # without --after-ok any ending releases it
        self.assertEqual(D.start_ready(), [job])
        self.assertEqual((self.state(job), self.spawned), ('starting', [job]))
        self.assertEqual(D.start_ready(), [])           # and only once

    def test_already_finished_dependency_starts_immediately(self):
        make_job(self.td, 'job-a', state='done')
        text = self.run_job(after=['job-a'])
        self.assertEqual(self.spawned, [self.queued_id(text)])

    def test_several_dependencies_and_comma_lists(self):
        make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        job = self.queued_id(self.run_job(after=['job-a,job-b']))
        self.end('job-a')
        self.assertEqual(D.start_ready(), [])
        self.end('job-b')
        self.assertEqual(D.start_ready(), [job])

    def test_after_ok_cancels_when_the_dependency_failed(self):
        make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        failed = self.queued_id(self.run_job(after=['job-a'], after_ok=True))
        flagged = self.queued_id(self.run_job(after=['job-b'], after_ok=True))
        self.end('job-a', 'failed')
        self.end('job-b', 'done', verdict='fail')
        self.assertEqual(D.start_ready(), [])
        for job in (failed, flagged):
            self.assertEqual(self.state(job), 'cancelled')
        self.assertIn('did not end successfully', json.loads((self.td / 'jobs' / failed / 'status.json').read_text())['errors'][0])
        self.assertEqual(self.spawned, [])

    def test_a_resumed_dependency_is_followed_to_its_last_attempt(self):
        make_job(self.td, 'job-a')
        make_job(self.td, 'job-a2')
        job = self.queued_id(self.run_job(after=['job-a'], after_ok=True))
        self.end('job-a', 'failed', resumed_by='job-a2')
        self.assertEqual(D.start_ready(), [])           # the resumed attempt is still running
        self.assertEqual(self.state(job), 'queued')
        self.end('job-a2', 'done')
        self.assertEqual(D.start_ready(), [job])

    def test_unknown_dependency_is_refused(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_job(after=['no-such-job'])
        self.assertIn('no such job', str(stop.exception))
        self.assertFalse(any((self.td / 'jobs').glob('*example*')))

    def test_claude_subagent_as_a_dependency(self):
        records = [dict(id='agent-abc', state='running')]
        with patch.object(D, '_claude_records', lambda: records), patch.dict(D._claude_states, at=0, states={}):
            job = self.queued_id(self.run_job(after=['agent-abc']))
            self.assertEqual(D.start_ready(), [])
            records[0]['state'] = 'done'
            D._claude_states['at'] = 0
            self.assertEqual(D.start_ready(), [job])

    def test_running_limit_holds_jobs_back(self):
        make_job(self.td, 'job-a', state='done')
        make_job(self.td, 'job-run-1')
        make_job(self.td, 'job-run-2')
        with patch.dict(D.CONFIG, queue_max_running=2):
            text = self.run_job(after=['job-a'])
            self.assertEqual(self.spawned, [])
            self.end('job-run-1')
            self.assertEqual(D.start_ready(), [self.queued_id(text)])

    def test_cancel_and_board_and_wait_any(self):
        make_job(self.td, 'job-a')
        job = self.queued_id(self.run_job(after=['job-a'], name='Second'))
        out = io.StringIO()
        with patch.object(D, '_claude_records', return_value=[]), contextlib.redirect_stdout(out):
            D.cmd_board(SimpleNamespace(hours=12))
            D.cmd_wait(SimpleNamespace(job=None, any=True, wait=0))
            D.cmd_cancel(SimpleNamespace(job=job))
        text = out.getvalue()
        self.assertIn('QUEUED:', text)
        self.assertIn('Second', text)
        self.assertIn('behind job-a', text)
        self.assertIn('STILL RUNNING: 2', text)
        self.assertIn('never started', text)
        self.assertEqual(self.state(job), 'cancelled')
        self.end('job-a')
        self.assertEqual(D.start_ready(), [])

    def test_a_finishing_job_releases_what_waited_on_it(self):
        jd, meta = make_job(self.td, 'job-a')
        (self.td / 'agents' / 'example' / 'reports').mkdir(parents=True, exist_ok=True)
        job = self.queued_id(self.run_job(after=['job-a']))
        (jd / 'result.md').write_text('Done.', encoding='utf-8')
        D.finalize(jd, meta, dict(state='running', started=time.time() - 5), None, 0)
        self.assertEqual(self.spawned, [job])

    def test_feed_lists_queued_jobs(self):
        make_job(self.td, 'job-a')
        job = self.queued_id(self.run_job(after=['job-a'], name='Second'))
        with patch.dict(F.CONFIG, claude_agents_enabled=False):
            summary = next(j for j in F.snapshot(12)['jobs'] if j['id'] == job)
        self.assertEqual((summary['state'], summary['after'], summary['name']), ('queued', ['job-a'], 'Second'))


class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        self.spawned = []
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 spawn_runner=lambda jd, meta: self.spawned.append(jd.name)),
                  patch.dict(D.CONFIG, auto_resume=1), patch.object(D.notify, 'send', return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        (self.td / 'agents' / 'example' / 'reports').mkdir(parents=True)

    def finish(self, job='job-a', errors=('stream disconnected before completion',), session='s1', state=None, rc=1, **meta_extra):
        jd, meta = make_job(self.td, job)
        meta.update(name='Trace the crash', **meta_extra)
        (jd / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
        status = dict(state='running', started=time.time() - 60)
        if session:
            status['session_id'] = session
        D.finalize(jd, meta, status, state, rc, list(errors))
        return jd, json.loads((jd / 'status.json').read_text())

    def test_connection_error_is_resumed_once_on_the_same_session(self):
        jd, status = self.finish()
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(self.spawned, [status['resumed_by']])
        new_meta = json.loads((self.td / 'jobs' / status['resumed_by'] / 'meta.json').read_text())
        self.assertEqual((new_meta['resume_job'], new_meta['resume_session'], new_meta['auto_resumes'], new_meta['name']),
                         ('job-a', 's1', 1, 'Trace the crash'))
        self.assertIn('cut off by a connection error', (self.td / 'jobs' / status['resumed_by'] / 'prompt.md').read_text())
        D.notify.send.assert_not_called()               # the resumed job reports the outcome, not this one
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.report(jd)
        self.assertIn('AUTO-RESUMED AS: ' + status['resumed_by'], out.getvalue())

    def test_not_resumed_twice(self):
        _jd, status = self.finish(auto_resumes=1)
        self.assertNotIn('resumed_by', status)
        self.assertEqual(self.spawned, [])
        D.notify.send.assert_called_once()

    def test_not_resumed_for_other_failures(self):
        cases = [dict(errors=('assertion failed in test_x',)), dict(session=None), dict(state='cancelled'),
                 dict(state='timeout'), dict(backend='claude-api', approved='Sam', max_usd=None)]
        for n, case in enumerate(cases):
            with self.subTest(case=case):
                extra = {k: case.pop(k) for k in list(case) if k in ('backend', 'approved', 'max_usd')}
                with patch.object(D.claude_api, 'append_ledger'):
                    _jd, status = self.finish(job=f'job-{n}', **case, **extra)
                self.assertNotIn('resumed_by', status)
        self.assertEqual(self.spawned, [])

    def test_switched_off(self):
        with patch.dict(D.CONFIG, auto_resume=0):
            _jd, status = self.finish()
        self.assertNotIn('resumed_by', status)


if __name__ == '__main__':
    unittest.main()
