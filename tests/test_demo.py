# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import feed as F
from demo_data import demo_snapshot
from tests.helpers import temporary_directory


class DemoTests(unittest.TestCase):
    def test_snapshot_matches_live_and_page_fields(self):
        snap = demo_snapshot(now=1800000000)
        with temporary_directory() as td, \
             patch.object(F.D, 'load_json', return_value={}), \
             patch.object(F.D, 'effective_status', return_value={}), \
             patch.object(F, 'parse_activity', return_value=([], 0)), \
             patch.object(F, 'tail_lines', return_value=[]), \
             patch.object(F.D, 'read_text', return_value=''):
            live = F.job_summary(Path(td), snap['now'])
        page = F.PAGE.read_text(encoding='utf-8')
        page_fields = set(re.findall(r'\bj\.([a-z_]+)', page))
        for job in snap['jobs']:
            self.assertEqual(set(job), set(live))
            self.assertTrue(page_fields <= job.keys())
            self.assertTrue(set(re.findall(r'\bk\.([a-z_]+)', page)) <= job['counts'].keys())
            self.assertIsInstance(job['elapsed'], (int, float))
            self.assertGreater(job['elapsed'], 0)
            self.assertEqual(job['counts']['commands'],
                             sum(e['kind'] == 'command' for e in job['events']))
            for event in job['events']:
                self.assertRegex(event['t'], r'^\d{2}:\d{2}:\d{2}$')
                if event['kind'] == 'command':
                    self.assertTrue({'t', 'kind', 'rc', 'icon', 'verb', 'detail', 'more', 'raw'} <= event.keys())
                else:
                    self.assertIsInstance(event['text'], str)
        self.assertEqual(set(snap), {'now', 'jobs', 'processes', 'process_panel'})
        process_renderer = page.split('function pc(')[1].split('function render(')[0]
        for process in snap['processes']:
            self.assertTrue(set(re.findall(r'\bp\.([a-z_]+)', process_renderer)) <= process.keys())
        json.dumps(snap, allow_nan=False)

    def test_demo_content_and_privacy(self):
        snap = demo_snapshot()
        running = [j for j in snap['jobs'] if j['state'] == 'running']
        finished = [j for j in snap['jobs'] if j['state'] != 'running']
        self.assertEqual(len(running), 5)
        self.assertEqual(len(finished), 10)
        self.assertEqual(sum(j['stalled'] for j in running), 1)
        self.assertEqual(sum(bool(j['retries']) for j in running), 1)
        relays = [j for j in running if j['source'] == 'relay']
        self.assertEqual({j['mode'] for j in relays}, {'read', 'write', 'full'})
        self.assertEqual({j['state'] for j in finished}, {'done', 'failed', 'cancelled'})
        self.assertTrue(all(j['now_msg'] for j in running) and all(j['progress'] for j in relays))
        claude = [j for j in snap['jobs'] if j['source'] == 'claude']
        self.assertEqual([(j['state'], j['depth'], j['backend']) for j in claude],
                         [('running', 1, 'claude'), ('done', 2, 'claude')])
        self.assertEqual(claude[1]['parent'], claude[0]['name'])
        self.assertTrue(all(j['tokens']['output'] > 0 and j['label'] for j in claude))
        self.assertTrue(all(j['result'] and '\n' not in j['result'] for j in finished))
        verbs = {e['verb'] for j in running for e in j['events'] if e['kind'] == 'command'}
        self.assertTrue({'Read', 'Search', 'Run tests', 'git commit', 'Build', 'Python snippet', 'Start agent', 'Edit'} <= verbs)
        self.assertTrue(snap['process_panel'])
        self.assertEqual(len(snap['processes']), 2)
        self.assertTrue(all(j['cwd'] == '~/code/shop-api' for j in snap['jobs']))
        self.assertTrue(all(p['path'].startswith('~/code/shop-api/') for p in snap['processes']))
        serialized = json.dumps(snap)
        self.assertNotIn(str(Path.home()), serialized)
        self.assertNotRegex(serialized, r'(?i)\b[a-z]:[/\\]|[\w.+-]+@[\w.-]+\.[a-z]{2,}')

    def test_relative_timestamps_and_finished_filter(self):
        first = demo_snapshot(now=1800000000)
        second = demo_snapshot(now=1800000060)
        self.assertEqual(second['now'] - first['now'], 60)
        for a, b in zip(first['jobs'], second['jobs']):
            self.assertEqual(b['started'] - a['started'], 60)
            self.assertEqual(a['elapsed'], b['elapsed'])
            self.assertEqual(a['idle'], b['idle'])
            self.assertEqual(a['name'], b['name'])
            if a['finished'] is not None:
                self.assertEqual(b['finished'] - a['finished'], 60)
        self.assertEqual(len(demo_snapshot(hours=0)['jobs']), 5)
        self.assertEqual(len(demo_snapshot(hours=1)['jobs']), 10)
        first['jobs'][0]['name'] = 'changed'
        self.assertEqual(demo_snapshot(now=1800000000)['jobs'][0]['name'], 'refactor-auth-module')

    def test_demo_dump_skips_live_sources_and_browser(self):
        forbidden = Mock(side_effect=AssertionError('live source accessed'))
        with patch.object(F.D, 'JOBS_ROOT', forbidden), \
             patch.object(F, 'job_summary', forbidden), \
             patch.object(F, 'watched_processes', forbidden), \
             patch.object(F, 'ThreadingHTTPServer') as server, \
             patch.object(F.webbrowser, 'open') as browser, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            F.main(['--demo', '--dump', '--open'])
        self.assertEqual(len(json.loads(out.getvalue())['jobs']), 15)
        forbidden.assert_not_called()
        server.assert_not_called()
        browser.assert_not_called()

    def test_cli_selects_demo_for_http_then_resets_for_live(self):
        original = F.Handler.demo
        try:
            for args, expected in [(['--demo'], True), ([], False)]:
                with patch.object(F, 'ThreadingHTTPServer') as server, \
                     contextlib.redirect_stdout(io.StringIO()):
                    server.return_value.server_port = 9000
                    server.return_value.serve_forever.side_effect = KeyboardInterrupt
                    F.main(args + ['--port', '9000'])
                self.assertEqual(F.Handler.demo, expected)
                server.assert_called_once_with(('127.0.0.1', 9000), F.Handler)
        finally:
            F.Handler.demo = original
