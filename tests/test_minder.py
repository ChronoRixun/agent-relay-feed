# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import unittest
from pathlib import Path
from unittest.mock import patch

import delegate as D
import leases as L
from tests.helpers import temporary_directory
from tests.test_phase import make_job

RESOURCES = {'test-copy': {'slots': 1, 'yield_to': ['TheApp.exe']}, 'build': {'slots': 1}}


class MinderTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        self.busy = []
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 owner_programs=lambda names: list(self.busy) if names else []),
                  patch.dict(D.CONFIG, checkpoint_minutes=10, resources=RESOURCES)):
            p.start()
            self.addCleanup(p.stop)
        self.jd, self.meta = make_job(self.td, 'job-a')
        self.meta['max_minutes'] = 60

    def kinds(self):
        return [e['kind'] for e in D.read_journal(self.jd)]

    def test_warning_then_stop(self):
        m = D.Minder(self.jd, self.meta, now=0)
        self.assertIsNone(m.tick(now=49 * 60))
        self.assertEqual(D.read_inbox(self.jd), [])
        self.assertIsNone(m.tick(now=50 * 60))
        self.assertIsNone(m.tick(now=51 * 60))                  # sent once
        warning = D.read_inbox(self.jd)
        self.assertEqual(len(warning), 1)
        self.assertIn('stopped in about 10 minutes', warning[0]['text'])
        self.assertIn('result block', warning[0]['text'])
        D.journal(self.jd, 'phase', 'wrapping up')              # the worker changed stage, so it read the warning
        self.assertEqual(m.tick(now=60 * 60 + 1), 'timeout')

    def test_unread_warning_earns_one_extension(self):
        m = D.Minder(self.jd, self.meta, now=0)
        with patch.object(D.time, 'time', return_value=50 * 60):
            m.tick(now=50 * 60)
        self.assertIsNone(m.tick(now=60 * 60 + 1))
        self.assertIn('extended', self.kinds())
        self.assertIsNone(m.tick(now=69 * 60))
        self.assertEqual(m.tick(now=70 * 60 + 2), 'timeout')    # only one extension

    def test_waiting_time_does_not_count(self):
        m = D.Minder(self.jd, self.meta, now=0)
        for _ in range(100):
            m.tick(waiting=True, now=10)
        self.assertEqual(m.deadline, 60 * 60 + 100 * D.POLL_S)

    def test_short_jobs_and_switched_off(self):
        self.meta['max_minutes'] = 8
        self.assertEqual(D.Minder(self.jd, self.meta, now=0).window, 120)
        with patch.dict(D.CONFIG, checkpoint_minutes=0):
            m = D.Minder(self.jd, self.meta, now=0)
            self.assertEqual(m.tick(now=8 * 60 + 1), 'timeout')
            self.assertEqual(D.read_inbox(self.jd), [])

    def test_yield_notice_once_per_episode_and_only_for_held_slots(self):
        m = D.Minder(self.jd, self.meta, now=0)
        self.busy = ['TheApp.exe (the owner)']
        m.tick(now=1)
        self.assertEqual(D.read_inbox(self.jd), [])             # holds nothing
        L.acquire(self.td, RESOURCES, 'test-copy', 'job-a')
        L.acquire(self.td, RESOURCES, 'build', 'job-a')
        m.tick(now=20)
        m.tick(now=40)
        notices = D.read_inbox(self.jd)
        self.assertEqual(len(notices), 1)
        self.assertIn('YIELD NOW', notices[0]['text'])
        self.assertIn('test-copy', notices[0]['text'])
        self.assertIn('TheApp.exe', notices[0]['text'])
        self.busy = []
        m.tick(now=60)
        self.busy = ['TheApp.exe (the owner)']
        m.tick(now=80)
        self.assertEqual(len(D.read_inbox(self.jd)), 2)         # a new episode, a new notice
        self.assertEqual(self.kinds().count('yield'), 2)


if __name__ == '__main__':
    unittest.main()
