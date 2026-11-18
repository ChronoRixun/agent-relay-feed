# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import codex_agents as C
import delegate as D
import delegate_config as CFG
import feed as F
from tests.helpers import temporary_directory
from tests.test_codex_agents import SID, CHILD, NOW, meta, msg, write


class CodexIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.root = Path(self.tmp.__enter__())
        self.addCleanup(self.tmp.__exit__, None, None, None)
        for p in [patch.multiple(D, DELEGATE_ROOT=self.root, JOBS_ROOT=self.root/'jobs'),
                  patch.dict(D.CONFIG, codex_agents_enabled=True, codex_sessions_root=str(self.root/'sessions'))]:
            p.start(); self.addCleanup(p.stop)

    def record(self, state='running'):
        rows = [meta(), msg('task_started')]
        if state == 'done': rows.append(msg('task_complete'))
        write(self.root/'sessions', rows)
        return C.CodexAgents(root=self.root/'sessions').scan(NOW + 1)[0]

    def test_config_validates_codex_fields(self):
        for key, good, bad in [('codex_agents_enabled', True, 'yes'), ('codex_sessions_root', '/tmp/sessions', 123),
                               ('codex_agents_window_hours', 24, -1), ('codex_agents_projects', ['example'], 'example')]:
            self.assertTrue(CFG._valid(key, good), key)
            self.assertFalse(CFG._valid(key, bad), key)

    def test_codex_refs_are_distinct_and_uuid_prefixes_are_not_guessed(self):
        r = self.record()
        self.assertEqual(D.resolve_ref('codex-' + SID, [r]), r['id'])
        self.assertEqual(D.resolve_ref(SID, [r]), r['id'])
        self.assertEqual(D.resolve_ref('codex:session#00000001', [r]), r['id'])
        with self.assertRaises(ValueError): D.resolve_ref('codex-' + SID[:8], [r])
        with self.assertRaises(ValueError): D.resolve_ref('claude:session#00000001', [r])

    def test_relay_map_preserves_all_resumed_jobs(self):
        self.assertTrue(hasattr(D, 'codex_relay_sessions'), 'Codex relay session map missing')
        for job in ['old', 'new']:
            path = self.root/'jobs'/job; path.mkdir(parents=True)
            (path/'status.json').write_text(json.dumps(dict(session_id=SID, state='done')))
            (path/'meta.json').write_text(json.dumps(dict(backend='codex', name=job)))
        self.assertEqual({j['id'] for j in D.codex_relay_sessions()[SID]}, {'old', 'new'})

    def test_native_card_has_same_keys_and_correct_source(self):
        self.assertTrue(hasattr(F, 'codex_job_summary'), 'Codex card builder missing')
        rec = self.record()
        job = self.root/'jobs'/'example'; job.mkdir(parents=True)
        card = F.codex_job_summary(rec, NOW + 1)
        self.assertEqual(set(card), set(F.job_summary(job, NOW + 1)))
        self.assertEqual((card['source'], card['backend']), ('codex', 'codex'))
        self.assertIsNone(card['question'])
        self.assertIsNone(card['total_cost_usd'])

    def test_wait_idle_not_running_and_active_counts(self):
        write(self.root/'sessions', [meta()])
        with contextlib.redirect_stdout(io.StringIO()) as out: D.wait_any(0)
        self.assertIn('NOTHING RUNNING', out.getvalue())
        now = time.time()
        active = dict(self.record(), last_activity=now)
        with patch.object(C, 'CodexAgents') as reader:
            reader.return_value.scan.return_value = [active]
            with contextlib.redirect_stdout(io.StringIO()) as out: D.wait_any(0)
        self.assertIn('codex-' + SID, out.getvalue())
        self.assertIn('STILL RUNNING', out.getvalue())

    def test_native_dependency_and_reviewed(self):
        self.assertTrue(hasattr(D, '_native_records'), 'Combined native records missing')
        rec = self.record('done')
        with patch.object(D, '_native_records', return_value=[rec]), patch.dict(D._claude_states, at=0, states={}):
            self.assertEqual(D.dep_state(rec['id']), 'done')
            self.assertFalse(D.dep_failed(rec['id']))
            with contextlib.redirect_stdout(io.StringIO()): D.cmd_reviewed(SimpleNamespace(job=rec['id'], undo=False))
        self.assertIn(rec['id'], D.load_reviewed())

    def test_codex_death_history_is_separate_and_once(self):
        self.assertTrue(hasattr(D, 'CODEX_NATIVE_SEEN'), 'Separate Codex seen history missing')
        now=time.time()
        old=dict(self.record(), died=True, finished=now-30, state='failed')
        # Existing Claude history must not turn old Codex deaths into fresh alerts.
        (self.root/D.NATIVE_SEEN).write_text('{}')
        self.assertEqual(D.native_deaths([old], now), [])
        new=dict(old, id='codex-'+CHILD, finished=now)
        self.assertEqual([r['id'] for r in D.native_deaths([old,new],now)], [new['id']])
        D.mark_native_seen([new])
        self.assertEqual(D.native_deaths([old,new],now), [])

    def test_pages_do_not_offer_messages_to_native_codex(self):
        for file in ['feed.html','mission.html','orbit.html']:
            text=Path(F.__file__).with_name(file).read_text(encoding='utf8')
            self.assertNotIn("j.source !== 'claude' ? `<form", text)
            self.assertNotIn("j.source !== 'claude' && j.backend", text)
            self.assertIn("j.source === 'relay'", text)

    def test_watch_label_is_codex(self):
        self.assertTrue(D._claude_tag(self.record()).startswith('codex:'))

    def test_demo_contains_codex_parent_and_child(self):
        from demo_data import demo_snapshot
        rows=[j for j in demo_snapshot()['jobs'] if j['source']=='codex']
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(any(j['parent'] for j in rows))

    def test_feed_snapshot_discovers_native_without_process_ownership(self):
        rec = self.record()
        self.assertTrue(hasattr(F, 'codex_reader'))
        reader=SimpleNamespace(scan=lambda now=None: [rec])
        with patch.object(F, 'codex_reader', return_value=reader), patch.object(F, 'claude_jobs', return_value=[]), patch.object(F, 'watched_processes', return_value=[]) as watched:
            snap=F.snapshot(12)
        self.assertEqual([j['source'] for j in snap['jobs']], ['codex'])
        watched.assert_called_once_with([])

    def test_codex_failed_turn_is_visible_on_review_page(self):
        rec=dict(self.record(),state='failed',finished=time.time(),errors=['Turn failed'])
        reader=SimpleNamespace(scan=lambda now=None: [rec])
        with patch.object(F, 'codex_reader', return_value=reader), patch.object(F, '_claude_finished_records', return_value=[]):
            jobs=F.evidence(12)['jobs']
        self.assertEqual([j['id'] for j in jobs], ['codex-'+SID])
        self.assertTrue(jobs[0]['needs_look'])
        self.assertIsNone(F.evidence_file('codex-'+SID, 0))

    def test_wait_does_not_wait_on_its_own_codex_caller(self):
        rec=self.record()
        reader=SimpleNamespace(scan=lambda: [rec])
        with patch.object(C,'CodexAgents',return_value=reader), patch.dict(D.os.environ,CODEX_THREAD_ID=SID):
            with contextlib.redirect_stdout(io.StringIO()) as out: D.wait_any(0)
        self.assertIn('NOTHING RUNNING',out.getvalue())

    def test_wait_keeps_independent_child_while_excluding_caller_and_ancestors(self):
        parent=self.record()
        child=dict(parent,id='codex-'+CHILD,parent_id=parent['id'])
        other=dict(parent,id='codex-019aaa00-0000-7000-8000-000000000099',parent_id=parent['id'])
        reader=SimpleNamespace(scan=lambda: [parent,child,other])
        with patch.object(C,'CodexAgents',return_value=reader), patch.dict(D.os.environ,CODEX_THREAD_ID=CHILD):
            with contextlib.redirect_stdout(io.StringIO()) as out: D.wait_any(0)
        self.assertIn('STILL RUNNING: 1',out.getvalue())
        self.assertIn(other['id'],out.getvalue())
