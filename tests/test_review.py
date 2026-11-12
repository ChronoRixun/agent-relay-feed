# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""The shared 'reviewed' mark: set from the review page or the CLI, honoured by every view."""
import contextlib
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D
import feed as F
from tests.helpers import temporary_directory
from tests.test_phase import make_job


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 runner_alive=lambda st: True, _claude_records=lambda: []),
                  patch.object(F, 'claude_reader', return_value=None),
                  patch.dict(F.CONFIG, feed_pin='4821'), patch.dict(F._pin_failures, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        # one clean job, one flagged job, one failed job with no result block or evidence
        self.clean = self.job('job-clean', 'done', dict(verdict='pass', flags=0, findings=[], data=dict(verdict='pass')))
        self.flagged = self.job('job-flag', 'done', dict(verdict='pass', flags=1, data=dict(verdict='pass', summary='Fixed.'),
                                                          findings=[dict(ok=False, text='image is 1920x1080, NOT the claimed 3840x2160')]))
        self.failed = self.job('job-fail', 'failed', None, errors=['stream disconnected'])

    def job(self, job, state, checked, **status):
        jd, meta = make_job(self.td, job, state=state, **status)
        if checked:
            (jd / 'result.json').write_text(json.dumps(checked), encoding='utf-8')
        return jd

    def listing(self):
        return {j['id']: j for j in F.evidence(12)['jobs']}

    def test_review_page_lists_what_needs_a_look_even_without_evidence(self):
        jobs = self.listing()
        self.assertEqual(set(jobs), {'job-flag', 'job-fail'})       # the clean job cites nothing and needs nothing
        self.assertEqual(jobs['job-flag']['problems'], ['image is 1920x1080, NOT the claimed 3840x2160'])
        self.assertEqual(jobs['job-fail']['problems'], ['stream disconnected'])
        self.assertTrue(all(j['needs_look'] and not j['reviewed'] for j in jobs.values()))

    def test_mark_reviewed_is_seen_by_every_view(self):
        D.set_reviewed('job-flag')
        self.assertTrue(self.listing()['job-flag']['reviewed'])
        snap = {j['id']: j for j in F.snapshot(12)['jobs']}
        self.assertTrue(snap['job-flag']['reviewed'])
        self.assertFalse(snap['job-fail']['reviewed'])
        digest = D.digest_lines(12)
        self.assertIn('finished 3 (1 clean, 1 need a look, 1 reviewed)', digest[0])
        self.assertNotIn('job-flag', '\n'.join(digest))
        D.set_reviewed('job-flag', False)
        self.assertIn('2 need a look)', D.digest_lines(12)[0])

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.cmd_reviewed(SimpleNamespace(job='job-fail', undo=False))
            D.cmd_reviewed(SimpleNamespace(job='agent-abc123', undo=False))
        self.assertEqual(set(D.load_reviewed()), {'job-fail', 'agent-abc123'})
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_reviewed(SimpleNamespace(job='job-fail', undo=True))
        self.assertEqual(set(D.load_reviewed()), {'agent-abc123'})
        for bad in ('nope', '../job-fail'):
            with self.assertRaises(SystemExit):
                D.cmd_reviewed(SimpleNamespace(job=bad, undo=False))

    def test_over_http_needs_the_pin(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), F.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(**body):
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/review', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())

        self.assertEqual(post(pin='0000', job='job-flag')[0], 401)
        self.assertEqual(D.load_reviewed(), {})
        self.assertEqual(post(pin='4821', job='job-flag'), (200, dict(ok=True, reviewed=True)))
        self.assertIn('job-flag', D.load_reviewed())
        self.assertEqual(post(pin='4821', job='job-flag', undo=True), (200, dict(ok=True, reviewed=False)))
        self.assertEqual(post(pin='4821', job='../x')[0], 404)
        self.assertTrue(F.evidence(12)['can_review'])


if __name__ == '__main__':
    unittest.main()
