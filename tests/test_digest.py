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
import notify as N
from tests.helpers import temporary_directory
from tests.test_phase import make_job


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        p = patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                           runner_alive=lambda st: True, _claude_records=lambda: self.records)
        p.start()
        self.addCleanup(p.stop)
        self.records = []

    def job(self, job, state, name, checked=None, **status):
        jd, meta = make_job(self.td, job, state=state, **status)
        meta['name'] = name
        (jd / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
        if checked:
            (jd / 'result.json').write_text(json.dumps(checked), encoding='utf-8')
        return jd

    def test_sorts_what_happened_into_what_matters(self):
        self.job('job-ok', 'done', 'Docs sweep', dict(verdict='pass', flags=0, findings=[dict(ok=True, text='fine')],
                                                       data=dict(owner=['push the branch'])))
        self.job('job-flag', 'done', 'Borderless fix', dict(verdict='pass', flags=1, data=dict(open=['windowed mode untested']),
                                                             findings=[dict(ok=False, text='image is 1920x1080, NOT the claimed 3840x2160')]))
        self.job('job-bad', 'failed', 'Offline install')
        self.job('job-old', 'failed', 'Ancient', finished=time.time() - 40 * 3600)
        self.job('job-resumed', 'failed', 'Cut off', resumed_by='job-ok')
        asking = self.job('job-ask', 'running', 'Trace the crash')
        D.journal(asking, 'asked', 'Patch it, or report only?')
        self.job('job-q', 'queued', 'Next thing')
        self.records = [dict(id='agent-1', label='Review flags', state='done', finished=time.time() - 60, checked=None),
                        dict(id='agent-2', label='Release prep', state='running', finished=None, checked=None)]
        lines = D.digest_lines(12)
        text = '\n'.join(lines)
        self.assertEqual(lines[0], 'DIGEST: last 12h | finished 4 (2 clean, 2 need a look) | running 2 | asking 1 | queued 1')
        self.assertIn('WAITING FOR AN ANSWER:\n  "Trace the crash" (job-ask): Patch it, or report only?', text)
        self.assertIn('"Borderless fix": done / pass', text)
        self.assertIn('FLAG: image is 1920x1080', text)
        self.assertIn('"Offline install": failed', text)
        self.assertIn('YOUR TURN:\n  "Docs sweep": push the branch', text)
        self.assertIn('STILL OPEN:\n  "Borderless fix": windowed mode untested', text)
        for absent in ('Ancient', 'Cut off', 'Next thing'):
            self.assertNotIn(absent, text)

    def test_quiet_night(self):
        self.assertEqual(D.digest_lines(12), ['DIGEST: last 12h | finished 0 (0 clean, 0 need a look) | running 0 | asking 0 | queued 0'])

    def test_send_goes_out_even_when_notify_on_excludes_everything_else(self):
        sent = []
        cfg = dict(notify=[dict(kind='json', url='http://x')], notify_on=['question'], notify_detail=True, notify_link='')
        self.assertEqual(N.send('digest', 'DIGEST: ...', 'body', config=cfg, post=lambda *a: sent.append(a)), [])
        self.assertEqual(json.loads(sent[0][1])['event'], 'digest')
        out = io.StringIO()
        with patch.dict(D.CONFIG, notify=[]), contextlib.redirect_stdout(out):
            D.cmd_digest(SimpleNamespace(hours=12, send=True))
        self.assertIn('NOT SENT: no `notify` target', out.getvalue())


if __name__ == '__main__':
    unittest.main()
