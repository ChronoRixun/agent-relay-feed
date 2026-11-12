# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import delegate as D
import feed as F
import results as R
from tests.helpers import temporary_directory
from tests.test_phase import make_job
from tests.test_results import png


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        p = patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(F, 'claude_reader', return_value=None)      # never this machine's real subagents
        p.start()
        self.addCleanup(p.stop)
        self.shot, self.log, self.secret = self.td / 'menu.png', self.td / 'run.log', self.td / 'secret.txt'
        png(self.shot, 1920, 1080)
        self.log.write_text('line one\nline two\n', encoding='utf-8')
        self.secret.write_text('not cited by any job', encoding='utf-8')
        self.jd, _ = make_job(self.td, 'job-a', state='done')
        block = dict(verdict='pass', summary='Borderless fix.', evidence=[
            dict(path=str(self.shot), shows='4K borderless menu', width=3840, height=2160),
            dict(path=str(self.log), shows='gate log'),
            dict(path=str(self.td / 'gone.png'), shows='missing shot')])
        checked = R.digest('```result\n' + json.dumps(block) + '\n```', started=time.time() - 600)
        (self.jd / 'result.json').write_text(json.dumps(checked), encoding='utf-8')

    def test_listing_reads_the_facts_off_the_files(self):
        listing = F.evidence(12)
        job = listing['jobs'][0]
        self.assertEqual((job['id'], job['verdict'], job['summary']), ('job-a', 'pass', 'Borderless fix.'))
        self.assertGreaterEqual(job['flags'], 2)
        shot, log, gone = job['items']
        self.assertEqual((shot['image'], shot['width'], shot['height'], shot['claimed_width']), (True, 1920, 1080, 3840))
        self.assertEqual((log['image'], log['exists'], log['shows']), (False, True, 'gate log'))
        self.assertFalse(gone['exists'])

    def test_old_jobs_and_jobs_without_evidence_are_left_out(self):
        make_job(self.td, 'job-plain', state='done')
        self.assertEqual([j['id'] for j in F.evidence(12)['jobs']], ['job-a'])
        st = json.loads((self.jd / 'status.json').read_text())
        st['finished'] = time.time() - 100 * 3600
        (self.jd / 'status.json').write_text(json.dumps(st), encoding='utf-8')
        self.assertEqual(F.evidence(12)['jobs'], [])

    def test_stale_file_is_marked(self):
        os.utime(self.shot, (1000, 1000))
        self.assertTrue(F.evidence(12)['jobs'][0]['items'][0]['stale'])

    def test_only_cited_files_are_served(self):
        kind, body = F.evidence_file('job-a', 0)
        self.assertEqual(kind, 'image/png')
        self.assertEqual(body, self.shot.read_bytes())
        kind, body = F.evidence_file('job-a', 1)
        self.assertTrue(kind.startswith('text/plain'))
        self.assertIn(b'line two', body)
        for job, index in (('job-a', 2), ('job-a', 3), ('job-a', -1), ('nope', 0), ('../jobs/job-a', 0), ('', 0)):
            with self.subTest(job=job, index=index):
                self.assertIsNone(F.evidence_file(job, index))

    def test_a_renamed_non_image_is_not_served_as_an_image(self):
        fake = self.td / 'fake.png'
        fake.write_text('<script>alert(1)</script>', encoding='utf-8')
        checked = R.digest('```result\n' + json.dumps(dict(verdict='pass', evidence=[dict(path=str(fake))])) + '\n```')
        (self.jd / 'result.json').write_text(json.dumps(checked), encoding='utf-8')
        self.assertTrue(F.evidence_file('job-a', 0)[0].startswith('text/plain'))

    def test_over_http(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), F.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    return r.status, r.headers.get('Content-Type'), r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, None, e.read()

        self.assertIn(b'<h1>Review</h1>', get('/evidence')[2])
        self.assertEqual(json.loads(get('/api/evidence')[2])['jobs'][0]['id'], 'job-a')
        code, kind, body = get('/api/evidence/file?job=job-a&i=0')
        self.assertEqual((code, kind, body), (200, 'image/png', self.shot.read_bytes()))
        self.assertEqual(get('/api/evidence/file?job=job-a&i=9')[0], 404)
        self.assertEqual(get('/api/evidence/file?job=job-a&i=x')[0], 404)
        self.assertEqual(get('/api/evidence/file?path=' + str(self.secret))[0], 404)


if __name__ == '__main__':
    unittest.main()
