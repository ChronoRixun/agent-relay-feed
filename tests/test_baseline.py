# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Shared state of a resource (saved for the first holder, put back after the last), who is
waiting for a slot, several slots for one holder, and outcomes left to a foreground waiter."""
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import baseline as B
import delegate as D
import delegate_config as C
import leases as L
from tests.helpers import temporary_directory
from tests.test_messaging import codex_command
from tests.test_phase import make_job

KEY = 'HKCU\\Software\\Vendor\\TheApp'
SZ, DWORD, BINARY = 1, 4, 3


class FakeRegistry:
    """The five calls baseline.py makes, over a dict {full key path: {value name: (type, data)}}."""

    def __init__(self, keys=None):
        self.keys = {k: dict(v) for k, v in (keys or {}).items()}
        self.writes = []

    def _full(self, path, rel):
        return path + ('\\' + rel if rel else '')

    def read(self, path):
        if path not in self.keys:
            return None
        return {k[len(path):].lstrip('\\'): dict(v) for k, v in self.keys.items()
                if k == path or k.startswith(path + '\\')}

    def ensure(self, path, rel):
        self.keys.setdefault(self._full(path, rel), {})
        self.writes.append(('ensure', rel))

    def set(self, path, rel, name, kind, data):
        self.keys[self._full(path, rel)][name] = (kind, data)
        self.writes.append(('set', rel, name))

    def delete_value(self, path, rel, name):
        del self.keys[self._full(path, rel)][name]
        self.writes.append(('delete_value', rel, name))

    def delete_key(self, path, rel):
        full = self._full(path, rel)
        assert not any(k.startswith(full + '\\') for k in self.keys), 'subkeys must go first'
        del self.keys[full]
        self.writes.append(('delete_key', rel))


def app_keys():
    return {KEY: {},
            KEY + '\\Controls': {'Jump': (SZ, 'Space'), 'Speed': (DWORD, 3)},
            KEY + '\\Controls\\Gamepads': {'Name1': (SZ, 'Some Pad'), 'Map': (BINARY, b'\x01\x02')}}


class BaselineTests(unittest.TestCase):
    def test_only_what_changed_is_written_back_and_the_result_is_checked(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            self.assertIsNone(B.save(td, 'app', dict(registry=[KEY]), 100, reg))
            self.assertTrue(B.is_open(td, 'app'))
            reg.keys[KEY + '\\Controls\\Gamepads']['Name1'] = (SZ, '')          # changed
            del reg.keys[KEY + '\\Controls']['Jump']                             # removed
            reg.keys[KEY + '\\Controls']['Extra'] = (DWORD, 1)                   # added
            reg.keys[KEY + '\\Online'] = {'Last': (SZ, 'room')}                  # added key ...
            reg.keys[KEY + '\\Online\\Peers'] = {}                               # ... with a subkey
            reg.writes.clear()
            rec = B.restore(td, 'app', 160, reg)
            self.assertTrue(rec['ok'], rec)
            self.assertEqual(reg.keys, app_keys())
            self.assertEqual(rec['changes'], 5)
            self.assertIn(KEY + '\\Controls\\Gamepads: value Name1 put back', rec['changed'])
            self.assertEqual(reg.writes[:2], [('delete_key', 'Online\\Peers'), ('delete_key', 'Online')])
            self.assertNotIn(('set', 'Controls', 'Speed'), reg.writes)          # untouched values are not rewritten
            self.assertFalse(B.is_open(td, 'app'))
            self.assertEqual(B.info(td, 'app')[1]['changes'], 5)

    def test_a_key_that_was_absent_is_removed_again_and_a_removed_key_comes_back(self):
        with temporary_directory() as td:
            reg = FakeRegistry()
            B.save(td, 'app', dict(registry=[KEY]), 1, reg)
            reg.keys[KEY] = {}
            reg.keys[KEY + '\\Controls'] = {'Jump': (SZ, 'Space')}
            self.assertTrue(B.restore(td, 'app', 2, reg)['ok'])
            self.assertEqual(reg.keys, {})
            reg = FakeRegistry(app_keys())
            B.save(td, 'app', dict(registry=[KEY]), 3, reg)
            reg.keys.clear()
            self.assertTrue(B.restore(td, 'app', 4, reg)['ok'])
            self.assertEqual(reg.keys, app_keys())

    def test_nothing_changed_means_nothing_written(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            B.save(td, 'app', dict(registry=[KEY]), 1, reg)
            rec = B.restore(td, 'app', 2, reg)
            self.assertEqual((rec['ok'], rec['changes'], reg.writes), (True, 0, []))

    def test_files_changed_created_and_deleted(self):
        with temporary_directory() as td:
            kept, absent = Path(td) / 'last.txt', Path(td) / 'new.txt'
            kept.write_bytes(b'room 1\r\n')
            self.assertIsNone(B.save(td, 'app', dict(files=[str(kept), str(absent)]), 1))
            kept.write_bytes(b'room 2')
            absent.write_text('made by a holder')
            rec = B.restore(td, 'app', 2)
            self.assertTrue(rec['ok'], rec)
            self.assertEqual(kept.read_bytes(), b'room 1\r\n')
            self.assertFalse(absent.exists())
            B.save(td, 'app', dict(files=[str(kept)]), 3)
            kept.unlink()
            self.assertTrue(B.restore(td, 'app', 4)['ok'])
            self.assertEqual(kept.read_bytes(), b'room 1\r\n')

    def test_a_failed_restore_keeps_the_saved_copy_for_the_next_try(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            B.save(td, 'app', dict(registry=[KEY]), 1, reg)
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'X')
            with patch.object(reg, 'set', side_effect=PermissionError('denied')):
                rec = B.restore(td, 'app', 2, reg)
            self.assertFalse(rec['ok'])
            self.assertIn('denied', rec['error'])
            self.assertTrue(B.is_open(td, 'app'))
            self.assertTrue(B.restore(td, 'app', 3, reg)['ok'])
            self.assertEqual(reg.keys, app_keys())

    def test_a_second_save_never_replaces_the_first(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            B.save(td, 'app', dict(registry=[KEY]), 1, reg)
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'changed by a holder')
            B.save(td, 'app', dict(registry=[KEY]), 2, reg)
            B.restore(td, 'app', 3, reg)
            self.assertEqual(reg.keys, app_keys())

    def test_config_validation(self):
        ok = lambda spec: C._valid('resources', {'app': {'slots': 2, 'baseline': spec}})
        self.assertTrue(ok({'registry': [KEY], 'files': ['%LOCALAPPDATA%/TheApp/last.txt']}))
        self.assertTrue(ok({'files': ['~/x.txt']}))
        for bad in ({}, {'registry': ['HKCU\\Software']}, {'registry': ['HKCU']}, {'registry': ['HKU\\a\\b\\c']},
                    {'files': ['']}, {'commands': ['x']}, 'HKCU\\Software\\Vendor\\TheApp'):
            with self.subTest(bad=bad):
                self.assertFalse(ok(bad))
        self.assertFalse(C._valid('resources', {'_waiting': {'slots': 1}}))

    @unittest.skipUnless(os.name == 'nt', 'needs the Windows registry')
    def test_the_real_registry_under_a_scratch_key(self):
        import winreg
        scratch = f'Software\\AgentRelayFeedTest\\run-{os.getpid()}'
        key = 'HKCU\\' + scratch

        def wipe():
            reg = B.WinRegistry()
            for rel in sorted(reg.read(key) or {}, key=B._depth, reverse=True):
                reg.delete_key(key, rel)
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, 'Software\\AgentRelayFeedTest')

        self.addCleanup(wipe)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scratch + '\\Controls', 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, 'Name1', 0, winreg.REG_SZ, 'Some Pad')
            winreg.SetValueEx(k, 'Speed', 0, winreg.REG_DWORD, 3)
            winreg.SetValueEx(k, 'Map', 0, winreg.REG_BINARY, b'\x00\x01\xff')
            winreg.SetValueEx(k, 'List', 0, winreg.REG_MULTI_SZ, ['a', 'b'])
        with temporary_directory() as td:
            self.assertIsNone(B.save(td, 'app', dict(registry=[key]), 1))
            before = B.WinRegistry().read(key)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scratch + '\\Controls', 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, 'Name1', 0, winreg.REG_SZ, '')
                winreg.DeleteValue(k, 'Speed')
                winreg.SetValueEx(k, 'Extra', 0, winreg.REG_QWORD, 2 ** 40)
            winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scratch + '\\Online\\Peers', 0, winreg.KEY_WRITE).Close()
            rec = B.restore(td, 'app', 2)
            self.assertTrue(rec['ok'], rec)
            self.assertEqual(rec['changes'], 5)
            self.assertEqual(B.WinRegistry().read(key), before)


class IsolationTests(unittest.TestCase):
    def test_the_suite_starts_away_from_this_machines_real_folders(self):
        import tests
        real = Path.home() / '.claude' / 'delegate'
        self.assertNotIn(real, [tests._ROOT, *tests._ROOT.parents])
        self.assertEqual((D.DELEGATE_ROOT, D.JOBS_ROOT, D.AGENTS_ROOT),
                         (tests._ROOT, tests._ROOT / 'jobs', tests._ROOT / 'agents'))


RESOURCES = {
    'build': {'slots': 1},
    'app': {'slots': 3, 'values': ['/work/copy-1', '/work/copy-2', '/work/copy-3'],
            'baseline': {'registry': [KEY]}, 'rule': 'running a copy of the app'},
}


class SharedStateOfASlotTests(unittest.TestCase):
    def test_saved_for_the_first_holder_and_put_back_after_the_last(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            take = lambda who, **kw: L.acquire(td, RESOURCES, 'app', who, registry=reg, **kw)
            self.assertTrue(take('job-a', now=100)['ok'])
            reg.keys[KEY + '\\Controls\\Gamepads']['Name1'] = (SZ, '')      # job-a's copy starts and writes
            self.assertTrue(take('job-b', now=110)['ok'])                   # joins: what is there now is NOT saved
            reg.keys[KEY + '\\Controls']['Speed'] = (DWORD, 9)
            L.release(td, 'job-a')
            self.assertEqual(L.settle(td, now=120, registry=reg), [])       # job-b still holds a slot
            self.assertEqual(reg.keys[KEY + '\\Controls']['Speed'], (DWORD, 9))
            snap = {r['name']: r for r in L.snapshot(td, RESOURCES, now=125)}
            self.assertEqual((snap['app']['baseline'], snap['build']['baseline']), (100, None))
            L.release(td, 'job-b')
            done = L.settle(td, now=130, registry=reg)
            self.assertEqual([(d['resource'], d['ok'], d['changes']) for d in done], [('app', True, 2)])
            self.assertEqual(reg.keys, app_keys())
            self.assertEqual(L.settle(td, now=131, registry=reg), [])       # nothing saved, nothing to do
            snap = {r['name']: r for r in L.snapshot(td, RESOURCES, now=140)}
            self.assertEqual((snap['app']['baseline'], snap['app']['restored']['changes']), (None, 2))

    def test_holders_that_died_do_not_keep_the_state_from_going_back(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            L.acquire(td, RESOURCES, 'app', 'job-dead', registry=reg, now=100)
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'X')
            self.assertEqual(L.settle(td, now=101, registry=reg), [])
            alive = lambda job: job != 'job-dead'
            done = L.settle(td, alive, now=102, registry=reg)
            self.assertTrue(done[0]['ok'])
            self.assertEqual(reg.keys, app_keys())
            # a named holder is gone once its time-to-live has run out
            L.acquire(td, RESOURCES, 'app', 'reviewer', job=False, ttl=60, registry=reg, is_job_alive=alive, now=200)
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'Y')
            self.assertEqual(L.settle(td, alive, now=230, registry=reg), [])
            self.assertTrue(L.settle(td, alive, now=261, registry=reg)[0]['ok'])
            self.assertEqual(reg.keys, app_keys())

    def test_a_new_holder_after_dead_ones_joins_the_clean_copy(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            L.acquire(td, RESOURCES, 'app', 'job-dead', registry=reg, now=100)
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'left behind')
            alive = lambda job: job != 'job-dead'
            self.assertTrue(L.acquire(td, RESOURCES, 'app', 'job-b', registry=reg, is_job_alive=alive, now=200)['ok'])
            L.release(td, 'job-b')
            L.settle(td, alive, now=210, registry=reg)
            self.assertEqual(reg.keys, app_keys())

    def test_holders_from_before_it_was_configured_must_finish_first(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            plain = {'app': {'slots': 3}}
            L.acquire(td, plain, 'app', 'job-old', now=100)                 # no baseline configured yet
            reg.keys[KEY + '\\Controls']['Jump'] = (SZ, 'mid-run')
            late = L.acquire(td, RESOURCES, 'app', 'job-new', registry=reg, now=110)
            self.assertFalse(late['ok'])
            self.assertIn('have finished (job-old)', late['reason'])
            self.assertFalse(B.is_open(td, 'app'))                          # the changed state was not saved
            self.assertTrue(L.acquire(td, RESOURCES, 'app', 'job-old', registry=reg, now=111)['ok'])   # it keeps its slot
            L.release(td, 'job-old')
            reg.keys = app_keys()                                           # it cleaned up after itself, as before
            self.assertTrue(L.acquire(td, RESOURCES, 'app', 'job-new', registry=reg, now=120)['ok'])
            self.assertTrue(B.is_open(td, 'app'))

    def test_no_slot_when_the_state_cannot_be_saved(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            with patch.object(reg, 'read', side_effect=PermissionError('denied')):
                got = L.acquire(td, RESOURCES, 'app', 'job-a', registry=reg)
            self.assertFalse(got['ok'])
            self.assertIn('could not be saved', got['reason'])
            self.assertIsNone(L.held(td, 'app', 'job-a'))
            self.assertTrue(L.acquire(td, RESOURCES, 'build', 'job-a')['ok'])       # other resources are unaffected


class WaitingTests(unittest.TestCase):
    def test_refused_holders_are_listed_until_they_get_a_slot_or_go_quiet(self):
        with temporary_directory() as td:
            L.acquire(td, RESOURCES, 'build', 'job-a', now=100)
            L.acquire(td, RESOURCES, 'build', 'job-b', now=110)
            L.acquire(td, RESOURCES, 'build', 'reviewer', job=False, now=150)
            L.acquire(td, RESOURCES, 'build', 'job-b', now=400)                 # still asking
            waiting = lambda **kw: [(w['holder'], w['since']) for w in
                                    {r['name']: r for r in L.snapshot(td, RESOURCES, **kw)}['build']['waiting']]
            self.assertEqual(waiting(now=410), [('job-b', 110), ('reviewer', 150)])
            self.assertEqual(waiting(now=150 + L.WAITING_FRESH_S + 1), [('job-b', 110)])        # reviewer went quiet
            self.assertEqual(waiting(now=410, is_job_alive=lambda job: job != 'job-b'), [('reviewer', 150)])
            L.release(td, 'job-a')
            self.assertTrue(L.acquire(td, RESOURCES, 'build', 'job-b', now=420)['ok'])
            self.assertEqual(waiting(now=421), [('reviewer', 150)])

    def test_repeated_refusals_do_not_rewrite_the_file_every_time(self):
        with temporary_directory() as td:
            L.acquire(td, RESOURCES, 'build', 'job-a', now=100)
            L.acquire(td, RESOURCES, 'build', 'job-b', now=110)
            with patch.object(L, '_save') as save:
                L.acquire(td, RESOURCES, 'build', 'job-b', now=112)
                save.assert_not_called()
                L.acquire(td, RESOURCES, 'build', 'job-b', now=110 + L.WAITING_WRITE_S + 1)
                save.assert_called_once()


class CountTests(unittest.TestCase):
    def test_several_slots_are_granted_together_or_not_at_all(self):
        with temporary_directory() as td:
            reg = FakeRegistry(app_keys())
            take = lambda who, n=1: L.acquire(td, RESOURCES, 'app', who, count=n, registry=reg)
            pair = take('job-a', 2)
            self.assertEqual((pair['slots'], pair['values'], pair['slot']), ([1, 2], ['/work/copy-1', '/work/copy-2'], 1))
            self.assertEqual(take('job-a', 2)['slots'], [1, 2])                 # asking again changes nothing
            late = take('job-b', 2)
            self.assertFalse(late['ok'])
            self.assertIn('2 free slots are needed and 1 is free', late['reason'])
            self.assertEqual(L.holding(td, 'app', 'job-b'), [])                 # no half grant
            self.assertEqual(take('job-b')['slots'], [3])
            self.assertEqual(L.release(td, 'job-a', 'app'), ['app'])
            self.assertEqual(take('job-b', 3)['slots'], [1, 2, 3])              # one more than it had: topped up
            never = take('job-c', 4)
            self.assertTrue(never['unknown'])
            self.assertIn('can never be granted', never['reason'])

    def test_worker_command_count(self):
        line = 'python "D:/x/delegate.py" lease --job j-1 --count 2 app'
        self.assertEqual(C.worker_command(line), ('lease', 'app'))
        self.assertEqual(C.lease_count(line), 2)
        self.assertEqual(C.lease_count('python delegate.py lease --job j-1 app'), 1)
        self.assertEqual(C.lease_count('python delegate.py lease --count=3 --job j-1 app; echo --count 9'), 3)
        self.assertEqual(D.lease_item('app:2'), ('app', 2))
        self.assertEqual(D.lease_item('app'), ('app', 1))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        self.spawned = []
        self.file = self.td / 'shared' / 'last-session.txt'
        self.file.parent.mkdir()
        self.file.write_text('the owner\'s session')
        resources = {'build': {'slots': 1},
                     'app': {'slots': 2, 'values': [str(self.td / 'copy-1'), str(self.td / 'copy-2')],
                             'baseline': {'files': [str(self.file)]}, 'rule': 'running a copy of the app'}}
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 runner_alive=lambda st: True, ASK_POLL_S=0.05, POLL_S=0.05, WAIT_ANY_POLL_S=0.05,
                                 owner_programs=lambda names: [],
                                 spawn_runner=lambda jd, meta: self.spawned.append(jd)),
                  patch.dict(D.CONFIG, resources=resources, claude_agents_enabled=False),
                  patch.object(D.notify, 'send', return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(SimpleNamespace(**kw))
        return out.getvalue()

    def test_two_sessions_side_by_side_and_the_state_goes_back_once(self):
        lease = lambda who: self.call(D.cmd_lease, job=None, holder=who, wait=0, count=1, resource='app')
        release = lambda who: self.call(D.cmd_release, job=None, holder=who, resource='app')
        self.assertIn('GRANTED: app slot 1', lease('session-a'))
        self.file.write_text('written by session a')
        self.assertIn('GRANTED: app slot 2', lease('session-b'))
        self.assertIn('shared state saved', self.call(D.cmd_slots))
        first = release('session-a')
        self.assertIn('RELEASED: app', first)
        self.assertNotIn('SHARED STATE', first)
        self.assertEqual(self.file.read_text(), 'written by session a')         # session b is still running
        last = release('session-b')
        self.assertIn('SHARED STATE of app put back and checked: 1 change undone', last)
        self.assertEqual(self.file.read_text(), 'the owner\'s session')
        self.assertIn('shared state put back', self.call(D.cmd_slots))

    def test_a_job_that_ends_puts_the_state_back(self):
        jd, meta = make_job(self.td, 'job-a')
        D.take_slot('app', 'job-a')
        self.file.write_text('left by the job')
        D.finalize(jd, meta, dict(state='running', started=1), 'failed', 1)
        self.assertEqual(self.file.read_text(), 'the owner\'s session')

    def test_a_process_looking_at_another_job_folder_never_puts_the_state_back(self):
        make_job(self.td, 'job-a')
        D.take_slot('app', 'job-a')
        self.file.write_text('written mid-run')
        with patch.object(D, 'JOBS_ROOT', self.td / 'some-other-jobs'):        # e.g. a test, or DELEGATE_JOBS_DIR
            self.assertTrue(D.job_running('job-a'))
            self.assertEqual(D.settle_slots(), [])
            self.assertIn('app 1/2: job-a', self.call(D.cmd_slots))
        self.assertEqual(self.file.read_text(), 'written mid-run')
        self.assertTrue(B.is_open(self.td, 'app'))

    def test_prompt_says_not_to_restore_it_yourself(self):
        meta = dict(mode='write', backend='codex', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
        text = D.build_prompt(None, meta, 'Do the task.', [])
        self.assertIn('running a copy of the app Its settings are shared by all holders', text)
        self.assertIn('do not save, restore or compare them yourself', text)
        self.assertIn('--count N', text)

    def test_board_and_slots_show_who_is_waiting(self):
        make_job(self.td, 'job-a')
        make_job(self.td, 'job-b')
        self.call(D.cmd_lease, job='job-a', holder=None, wait=0, count=1, resource='build')
        self.assertIn('NOT YET', self.call(D.cmd_lease, job='job-b', holder=None, wait=0, count=1, resource='build'))
        self.call(D.cmd_lease, job=None, holder='reviewer', wait=0, count=1, resource='build')
        slots = self.call(D.cmd_slots)
        self.assertRegex(slots, r'build 1/1: job-a \(\d+s\) \| 2 WAITING: job-b \(\d+s\), reviewer \(\d+s\)')
        with patch.object(D, '_claude_records', return_value=[]):
            self.assertIn('2 WAITING: job-b', self.call(D.cmd_board, hours=12))

    def test_lease_count_from_the_command_line_and_from_a_sandboxed_worker(self):
        make_job(self.td, 'job-a')
        got = self.call(D.cmd_lease, job='job-a', holder=None, wait=0, count=2, resource='app')
        self.assertIn('GRANTED: app slots 1, 2. Use: ', got)
        self.assertIn('copy-2', got)
        self.assertIn('RELEASED: app', self.call(D.cmd_release, job='job-a', holder=None, resource='app'))
        jd, meta = make_job(self.td, 'job-s')
        tail = D.ActivityTail(meta, jd)
        line = 'python "D:/x/delegate.py" lease --job job-s --count 2 app'
        with open(jd / 'events.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(codex_command('item.started', line)) + '\n')
        tail.pump()
        self.assertEqual([e['slot'] for e in L.holding(self.td, 'app', 'job-s')], [1, 2])
        with patch.object(D, 'take_slot', side_effect=PermissionError):         # the worker's own, sandboxed call
            self.assertIn('GRANTED: app slots 1, 2', self.call(D.cmd_lease, job='job-s', holder=None, wait=0, count=2,
                                                                resource='app'))

    def bg(self, *command, **kw):
        args = dict(command=['--', *command], shell='none', cwd=str(self.td), lease=None, name=None,
                    max_minutes=5, wait=0)
        args.update(kw)
        text = self.call(D.cmd_bg, **args)
        return self.spawned[-1], text

    def test_bg_job_with_two_slots(self):
        code = 'import os, sys; print(sys.argv[1]); print(sys.argv[2]); print(os.environ["DELEGATE_SLOT_APP_2"])'
        jd, text = self.bg(sys.executable, '-c', code, '{app}', '{app2}', lease=['app:2'])
        self.assertIn('holds app:2', text)
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_runner(SimpleNamespace(jobdir=str(jd)))
        one, two = str(self.td / 'copy-1'), str(self.td / 'copy-2')
        self.assertEqual((jd / 'output.log').read_text().split(), [one, two, two])
        self.assertEqual(L.holding(self.td, 'app', jd.name), [])
        with self.assertRaises(SystemExit) as stop:
            self.bg(sys.executable, '-c', 'pass', lease=['app:3'])
        self.assertIn('at most its number of slots', str(stop.exception))

    def test_wait_any_leaves_an_outcome_to_the_agent_waiting_on_that_job(self):
        jd, _ = self.bg(sys.executable, '-c', 'print("done")')
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_runner(SimpleNamespace(jobdir=str(jd)))
        (jd / D.WAITER).write_text('1')                        # its owner called `wait <job>` moments ago
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_any(0.3)
        self.assertNotIn('OUTCOME', o.getvalue())
        self.assertIn('the agent that waited on it is reading the result', o.getvalue())
        old = time.time() - D.WAITER_FRESH_S - 5               # ... but never came back for it
        os.utime(jd / D.WAITER, (old, old))
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_any(1)
        self.assertIn(f'OUTCOME: {jd.name} done', o.getvalue())

    def test_a_foreground_wait_marks_the_job_and_collects_it(self):
        jd, _ = self.bg(sys.executable, '-c', 'print("done")')
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_runner(SimpleNamespace(jobdir=str(jd)))
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_for(jd, 1)
        self.assertTrue(D.collected_by_owner(jd))
        self.assertIn('done', o.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as o:
            D.wait_any(1)
        self.assertNotIn('OUTCOME', o.getvalue())               # already read by the one who waited
        self.assertIn('NOTHING RUNNING', o.getvalue())


if __name__ == '__main__':
    unittest.main()
