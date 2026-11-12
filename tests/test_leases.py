# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D
import delegate_config as C
import leases as L
from tests.helpers import temporary_directory
from tests.test_messaging import codex_command
from tests.test_phase import make_job

RESOURCES = {
    'build': {'slots': 1, 'rule': 'any compiler run'},
    'test-copy': {'slots': 2, 'values': ['/work/copy-1', '/work/copy-2'], 'yield_to': ['TheApp.exe']},
}


class LeaseStateTests(unittest.TestCase):
    def test_slots_values_and_refusal(self):
        with temporary_directory() as td:
            a = L.acquire(td, RESOURCES, 'test-copy', 'job-a')
            b = L.acquire(td, RESOURCES, 'test-copy', 'job-b')
            c = L.acquire(td, RESOURCES, 'test-copy', 'job-c')
            self.assertEqual((a['ok'], a['slot'], a['value']), (True, 1, '/work/copy-1'))
            self.assertEqual((b['slot'], b['value']), (2, '/work/copy-2'))
            self.assertFalse(c['ok'])
            self.assertIn('slot 1: job-a', c['reason'])
            # asking again returns the slot already held
            self.assertEqual(L.acquire(td, RESOURCES, 'test-copy', 'job-a')['slot'], 1)
            self.assertEqual(L.release(td, 'job-a', 'test-copy'), ['test-copy'])
            self.assertEqual(L.acquire(td, RESOURCES, 'test-copy', 'job-c')['value'], '/work/copy-1')

    def test_unknown_resource(self):
        with temporary_directory() as td:
            got = L.acquire(td, RESOURCES, 'gpu', 'job-a')
            self.assertTrue(got['unknown'])
            self.assertIn('build, test-copy', got['reason'])

    def test_owner_program_closes_the_resource_but_not_for_a_current_holder(self):
        with temporary_directory() as td:
            self.assertTrue(L.acquire(td, RESOURCES, 'test-copy', 'job-a')['ok'])
            busy = lambda: ['TheApp.exe (the owner)']
            refused = L.acquire(td, RESOURCES, 'test-copy', 'job-b', blocked=busy)
            self.assertFalse(refused['ok'])
            self.assertIn('TheApp.exe', refused['reason'])
            self.assertTrue(L.acquire(td, RESOURCES, 'test-copy', 'job-a', blocked=busy)['ok'])

    def test_dead_jobs_and_expired_names_lose_their_slots(self):
        with temporary_directory() as td:
            L.acquire(td, RESOURCES, 'build', 'job-dead', now=1000)
            alive = lambda job: job != 'job-dead'
            self.assertTrue(L.acquire(td, RESOURCES, 'build', 'job-b', is_job_alive=alive, now=1001)['ok'])
            L.release(td, 'job-b')
            L.acquire(td, RESOURCES, 'build', 'reviewer', job=False, ttl=60, now=2000)
            self.assertFalse(L.acquire(td, RESOURCES, 'build', 'job-c', now=2030)['ok'])
            self.assertTrue(L.acquire(td, RESOURCES, 'build', 'job-c', now=2061)['ok'])

    def test_release_everything_and_snapshot(self):
        with temporary_directory() as td:
            L.acquire(td, RESOURCES, 'build', 'job-a', now=100)
            L.acquire(td, RESOURCES, 'test-copy', 'job-a', now=100)
            snap = {r['name']: r for r in L.snapshot(td, RESOURCES, now=160)}
            self.assertEqual(snap['build']['held'][0]['holder'], 'job-a')
            self.assertEqual((snap['test-copy']['slots'], len(snap['test-copy']['held'])), (2, 1))
            self.assertEqual(sorted(L.release(td, 'job-a')), ['build', 'test-copy'])
            self.assertEqual(L.load(td), {'build': [], 'test-copy': []})

    def test_config_validation(self):
        self.assertTrue(C._valid('resources', RESOURCES))
        self.assertFalse(C._valid('resources', {'x': {'slots': 2, 'values': ['only-one']}}))
        self.assertFalse(C._valid('resources', {'bad name': {'slots': 1}}))
        self.assertFalse(C._valid('resources', {'x': {'slots': 0}}))

    def test_worker_command_shapes(self):
        self.assertEqual(C.worker_command('python "D:/x/delegate.py" lease --job j-1 test-copy'), ('lease', 'test-copy'))
        self.assertEqual(C.worker_command("powershell -Command 'python \"D:/x/delegate.py\" release --job j-1 build'"),
                         ('release', 'build'))
        self.assertEqual(C.worker_command('python delegate.py lease --as reviewer --wait 5 build; ls'), ('lease', 'build'))


class LeaseCommandTests(unittest.TestCase):
    def patched(self, root):
        return contextlib.ExitStack(), patch.multiple(
            D, JOBS_ROOT=Path(root) / 'jobs', AGENTS_ROOT=Path(root) / 'agents', DELEGATE_ROOT=Path(root),
            runner_alive=lambda st: True, ASK_POLL_S=0.05, owner_programs=lambda names: [])

    def setUp(self):
        self._td = temporary_directory()
        self.td = self._td.__enter__()
        self.addCleanup(self._td.__exit__, None, None, None)
        for p in (patch.multiple(D, JOBS_ROOT=Path(self.td) / 'jobs', AGENTS_ROOT=Path(self.td) / 'agents',
                                 DELEGATE_ROOT=Path(self.td), runner_alive=lambda st: True, ASK_POLL_S=0.05,
                                 owner_programs=lambda names: []),
                  patch.dict(D.CONFIG, resources=RESOURCES)):
            p.start()
            self.addCleanup(p.stop)

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(SimpleNamespace(**kw))
        return out.getvalue()

    def test_unsandboxed_worker_takes_and_returns_a_slot_itself(self):
        make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        got = self.call(D.cmd_lease, job='job-a', holder=None, wait=0, resource='build')
        self.assertIn('GRANTED: build slot 1', got)
        late = self.call(D.cmd_lease, job='job-b', holder=None, wait=0.1, resource='build')
        self.assertIn('NOT YET', late)
        self.assertIn('job-a', late)
        self.assertIn('Do not use build', late)
        self.assertIn('RELEASED: build', self.call(D.cmd_release, job='job-a', holder=None, resource='build'))
        self.assertIn('GRANTED', self.call(D.cmd_lease, job='job-b', holder=None, wait=0, resource='build'))

    def test_grant_hands_over_the_value(self):
        make_job(self.td, 'job-a')
        got = self.call(D.cmd_lease, job='job-a', holder=None, wait=0, resource='test-copy')
        self.assertIn('Use: /work/copy-1', got)

    def test_named_holder_outside_a_job(self):
        self.assertIn('GRANTED', self.call(D.cmd_lease, job=None, holder='reviewer', wait=0, resource='build'))
        self.assertIn('RELEASED', self.call(D.cmd_release, job=None, holder='reviewer', resource=None))

    def test_no_holder_or_unknown_job_is_never_granted(self):
        for kw in (dict(job=None, holder=None), dict(job='nope', holder=None), dict(job=None, holder='../x')):
            with self.subTest(kw=kw):
                self.assertIn('NOT GRANTED', self.call(D.cmd_lease, wait=0, resource='build', **kw))
        self.assertEqual(L.load(self.td), {})

    def test_owner_program_blocks_and_is_named(self):
        make_job(self.td, 'job-a')
        with patch.object(D, 'owner_programs', lambda names: ['TheApp.exe (the owner)'] if names else []):
            self.assertIn('TheApp.exe', self.call(D.cmd_lease, job='job-a', holder=None, wait=0, resource='test-copy'))
            self.assertIn('GRANTED', self.call(D.cmd_lease, job='job-a', holder=None, wait=0, resource='build'))
            self.assertIn('closed while the owner runs', self.call(D.cmd_slots))

    def test_sandboxed_worker_is_granted_by_its_runner(self):
        jd, meta = make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        L.acquire(self.td, RESOURCES, 'build', 'job-b')
        tail = D.ActivityTail(meta, jd)
        lease = 'python "D:/x/delegate.py" lease --job job-a build'

        def feed(*events):
            with open(jd / 'events.jsonl', 'a', encoding='utf-8') as f:
                f.writelines(json.dumps(e) + '\n' for e in events)
            tail.pump()

        feed(codex_command('item.started', lease))
        self.assertEqual(tail.wanted, {'build'})
        self.assertIn('job-b', json.loads((jd / 'lease.wait').read_text())['build'])
        # the worker's own command cannot write the lease file: it reports what the runner recorded
        with patch.object(D, 'take_slot', side_effect=PermissionError):
            waiting = self.call(D.cmd_lease, job='job-a', holder=None, wait=0.1, resource='build')
        self.assertIn('NOT YET', waiting)
        self.assertIn('job-b', waiting)
        L.release(self.td, 'job-b')
        tail.grant()                                    # the runner retries on every tick
        self.assertEqual(tail.wanted, set())
        with patch.object(D, 'take_slot', side_effect=PermissionError):
            self.assertIn('GRANTED: build slot 1', self.call(D.cmd_lease, job='job-a', holder=None, wait=0, resource='build'))
        feed(codex_command('item.completed', 'python "D:/x/delegate.py" release --job job-a build', 'item_9', 0))
        self.assertIsNone(L.held(self.td, 'build', 'job-a'))

    def test_a_worker_that_stops_waiting_is_not_granted_later(self):
        jd, meta = make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        L.acquire(self.td, RESOURCES, 'build', 'job-b')
        tail = D.ActivityTail(meta, jd)
        lease = 'python "D:/x/delegate.py" lease --job job-a build'
        (jd / 'events.jsonl').write_text(json.dumps(codex_command('item.started', lease)) + '\n' +
                                         json.dumps(codex_command('item.completed', lease, exit_code=0)) + '\n',
                                         encoding='utf-8')
        tail.pump()
        self.assertEqual(tail.wanted, set())

    def test_finished_job_holds_nothing(self):
        jd, meta = make_job(self.td, 'job-a')
        L.acquire(self.td, RESOURCES, 'build', 'job-a')
        D.finalize(jd, meta, dict(state='running', started=1), 'failed', 1)
        self.assertIsNone(L.held(self.td, 'build', 'job-a'))

    def test_prompt_and_board_list_the_resources(self):
        jd, _ = make_job(self.td, 'job-a')
        L.acquire(self.td, RESOURCES, 'build', 'job-a')
        meta = dict(mode='write', backend='codex', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
        text = D.build_prompt(None, meta, 'Do the task.', [])
        self.assertIn('- build (1 slot): any compiler run', text)
        self.assertIn('lease --job job-7 <name>', text)
        with patch.object(D, '_claude_records', return_value=[]):
            board = self.call(D.cmd_board, hours=12)
        self.assertIn('build 1/1: job-a', board)
        self.assertIn('test-copy 0/2: free', board)

    def test_no_resources_means_no_prompt_section(self):
        with patch.dict(D.CONFIG, resources={}):
            meta = dict(mode='write', backend='codex', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
            self.assertNotIn('Shared machine', D.build_prompt(None, meta, 'Do the task.', []))


if __name__ == '__main__':
    unittest.main()
