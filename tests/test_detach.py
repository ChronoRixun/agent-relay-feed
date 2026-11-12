# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Runners must leave the caller's Windows job object: breakaway, else WMI + detach.py, else in-job."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import delegate as D
from tests.helpers import temporary_directory
from tests.test_phase import make_job


class SpawnOrderTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        self.jd, self.meta = make_job(self.td, 'job-a')
        self.popen = []

    def spawn(self, breakaway_ok, wmi_pid, wmi_on=True):
        def popen(cmd, creationflags=0, **kw):
            if creationflags & D.CREATE_BREAKAWAY_FROM_JOB and not breakaway_ok:
                raise PermissionError(5, 'Access is denied')
            self.popen.append(creationflags)
        with patch.object(D.os, 'name', 'nt'), patch.object(D.subprocess, 'Popen', side_effect=popen), \
             patch.object(D, 'spawn_via_wmi', return_value=wmi_pid) as wmi, patch.dict(D.CONFIG, detach_via_wmi=wmi_on):
            D.spawn_runner(self.jd, self.meta)
        return (self.jd / 'detach.txt').read_text(), wmi

    def test_breakaway_when_the_job_allows_it(self):
        how, wmi = self.spawn(True, None)
        self.assertEqual(how, 'breakaway')
        wmi.assert_not_called()

    def test_wmi_when_breakaway_is_refused(self):
        how, wmi = self.spawn(False, 4242)
        self.assertEqual(how, 'wmi')
        self.assertEqual(wmi.call_args.args[0][-2:], ['_runner', str(self.jd)])
        self.assertEqual(self.popen, [])                    # nothing was started inside the job

    def test_in_job_only_when_both_fail_or_wmi_is_off(self):
        for wmi_pid, wmi_on in ((None, True), (4242, False)):
            with self.subTest(wmi_on=wmi_on):
                self.popen.clear()
                how, _wmi = self.spawn(False, wmi_pid, wmi_on)
                self.assertEqual(how, 'in-job')
                self.assertEqual(len(self.popen), 1)


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class LiveWmiTests(unittest.TestCase):
    def test_child_is_outside_the_job_with_the_callers_environment(self):
        with temporary_directory() as td:
            out = Path(td) / 'probe.json'
            code = ("import json,os,sys,ctypes; from ctypes import wintypes; k=ctypes.WinDLL('kernel32');"
                    "k.GetCurrentProcess.restype=wintypes.HANDLE;"
                    "k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)];"
                    "b=wintypes.BOOL(); k.IsProcessInJob(k.GetCurrentProcess(),None,ctypes.byref(b));"
                    "json.dump(dict(in_job=bool(b), mark=os.environ.get('DELEGATE_TEST_MARK'), cwd=os.getcwd()), open(sys.argv[1],'w'))")
            with patch.dict(os.environ, DELEGATE_TEST_MARK='only-the-caller-has-this'):
                pid = D.spawn_via_wmi([sys.executable, '-c', code, str(out)], td)
            if pid is None:
                self.skipTest('WMI is not available here')
            for _ in range(50):
                if out.exists() and out.stat().st_size:
                    break
                time.sleep(0.1)
            got = json.loads(out.read_text())
            self.assertFalse(got['in_job'])
            self.assertEqual(got['mark'], 'only-the-caller-has-this')
            self.assertEqual(Path(got['cwd']).resolve(), Path(td).resolve())

    def test_failed_wmi_leaves_nothing_behind(self):
        with patch.object(D, '_wmi_create', return_value=None):
            self.assertIsNone(D.spawn_via_wmi([sys.executable, '-c', 'pass'], '.'))


if __name__ == '__main__':
    unittest.main()
