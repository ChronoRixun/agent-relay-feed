# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""No network/provider calls: all credentials and CLI events below are synthetic."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_api as A
import delegate as D
import delegate_config as C
import feed as F
import install_agents as I
from tests.helpers import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = 'synthetic-key-only-NEVER-a-provider-key'
FAKE_CLI = r'''
import json, os, sys, time
from pathlib import Path
def emit(data):
    print(json.dumps(data), flush=True)
prompt = sys.stdin.read()
case = os.environ.get('FAKE_CASE', 'success')
key = os.environ['ANTHROPIC_API_KEY']
assert key == 'synthetic-key-only-NEVER-a-provider-key'
assert os.environ['CLAUDE_CONFIG_DIR'].endswith('claude-api-home')
assert all(n not in os.environ for n in ['ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_PROFILE',
    'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR'])
assert '--strict-mcp-config' in sys.argv
assert sys.argv[sys.argv.index('--setting-sources') + 1] == ''
assert json.loads(sys.argv[sys.argv.index('--settings') + 1])['apiKeyHelper'] == ''
assert 'task body' in prompt
if case != 'no-init':
    event = dict(type='system', subtype='init', session_id='fake-session', apiKeySource='ANTHROPIC_API_KEY')
    if case == 'wrong-init': event['apiKeySource'] = 'oauth'
    if case == 'missing-source': del event['apiKeySource']
    emit(event)
if case in ('wrong-init', 'missing-source', 'cancel', 'timeout'):
    time.sleep(4)
    Path('should-not-exist').write_text('billing guard was too slow')
if case == 'running-cost':
    emit(dict(type='system', subtype='cost', total_cost_usd=3.5))
    time.sleep(4)
    Path('should-not-exist').write_text('budget guard was too slow')
emit(dict(type='assistant', message=dict(content=[dict(type='text', text='hello ' + key)])))
sys.stderr.write('synthetic stderr ' + key[:20]); sys.stderr.flush()
time.sleep(.03)
sys.stderr.write(key[20:] + '\n'); sys.stderr.flush()
cost = 3.5 if case == 'final-cost' else .125
emit(dict(type='result', subtype='error_max_budget_usd' if case == 'budget-result' else 'success',
    is_error=case == 'error', result='report ' + key + ' argv=' + json.dumps(sys.argv),
    session_id='fake-session', total_cost_usd=cost,
    usage=dict(input_tokens=10, output_tokens=20, cache_read_input_tokens=2)))
'''


class ClaudeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.root = Path(self.tmp.__enter__())
        self.storage = self.root / 'storage'
        self.key = self.root / 'fake-key.txt'
        self.key.write_text(FAKE_KEY, encoding='utf-8')
        self.cli = self.root / 'fake_claude.py'
        self.cli.write_text(FAKE_CLI, encoding='utf-8')
        self.config = dict(C.DEFAULTS, claude_api_key_file=str(self.key))
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(D, 'DELEGATE_ROOT', self.storage))
        self.patches.enter_context(patch.object(D, 'JOBS_ROOT', self.storage / 'jobs'))
        self.patches.enter_context(patch.object(D, 'AGENTS_ROOT', self.storage / 'agents'))
        self.patches.enter_context(patch.dict(D.CONFIG, self.config))
        self.patches.enter_context(patch.dict(os.environ, DELEGATE_CLAUDE=str(self.cli),
            ANTHROPIC_AUTH_TOKEN='fake-token', ANTHROPIC_PROFILE='fake-profile',
            CLAUDE_CODE_OAUTH_TOKEN='fake-oauth', CLAUDE_CODE_USE_BEDROCK='1',
            CLAUDE_CODE_USE_VERTEX='1', CLAUDE_CODE_USE_FOUNDRY='1',
            ANTHROPIC_BASE_URL='http://invalid.test', CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR='9'))

    def tearDown(self):
        self.patches.close()
        self.tmp.__exit__(None, None, None)

    def job(self, name='fake-job', **overrides):
        jd = D.JOBS_ROOT / name
        jd.mkdir(parents=True)
        ap = D.agent_paths('claude-api-sonnet', name)
        for key in ('activity', 'report', 'progress'):
            ap[key].parent.mkdir(parents=True, exist_ok=True)
        ap['activity'].write_text('', encoding='utf-8')
        meta = dict(profile='claude-api-sonnet', **D.PROFILES['claude-api-sonnet'],
                    mode='write', cwd=str(self.root), add_dirs=[], created=1,
                    approved='Owner / now / test only', max_usd=None, max_minutes=1,
                    activity_file=str(ap['activity']), progress_file=str(ap['progress']))
        meta.update(overrides)
        D.save_json(jd / 'meta.json', meta)
        (jd / 'prompt.md').write_text('task body', encoding='utf-8')
        return jd, meta

    def run_job(self, case='success', **overrides):
        jd, meta = self.job(**overrides)
        with patch.dict(os.environ, FAKE_CASE=case):
            D.cmd_runner(argparse.Namespace(jobdir=str(jd)))
        return jd, meta, D.load_json(jd / 'status.json')

    def assert_no_key(self):
        for path in self.storage.rglob('*'):
            if path.is_file():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding='utf-8'), str(path))

    def test_success_redacted_cost_ledger_feed_and_spend(self):
        jd, meta, st = self.run_job()
        self.assertEqual(st['state'], 'done', st)
        self.assertTrue(st['billing_verified'])
        self.assertEqual(st['session_id'], 'fake-session')
        self.assertEqual(st['total_cost_usd'], .125)
        self.assertEqual(st['usage']['output_tokens'], 20)
        ledger = list(D.iter_events(self.storage / 'claude-api-ledger.jsonl'))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['approved'], meta['approved'])
        self.assertEqual(ledger[0]['tokens'], st['usage'])
        summary = F.job_summary(jd, 100)
        self.assertEqual(summary['total_cost_usd'], .125)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            D.report(jd)
            D.cmd_spend(argparse.Namespace(n=5))
        self.assertIn('0.125000', out.getvalue())
        self.assertNotIn(FAKE_KEY, out.getvalue())
        self.assertIn('[REDACTED]', D.read_text(jd / 'stderr.log'))
        self.assert_no_key()

    def test_wrong_init_kills_immediately(self):
        jd, _, st = self.run_job('wrong-init')
        self.assertEqual(st['state'], 'failed')
        self.assertIn('not billed to API credits', ' '.join(st['errors']))
        self.assertFalse((self.root / 'should-not-exist').exists())
        self.assertIsNone(st.get('total_cost_usd'))
        self.assert_no_key()

    def test_missing_init_source_kills(self):
        _, _, st = self.run_job('missing-source')
        self.assertEqual(st['state'], 'failed')
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_missing_init_fails_closed(self):
        _, _, st = self.run_job('no-init')
        self.assertEqual(st['state'], 'failed')
        self.assertIn('not billed to API credits', ' '.join(st['errors']))
        self.assert_no_key()

    def test_running_cost_kills_before_work(self):
        _, _, st = self.run_job('running-cost', max_usd=1)
        self.assertEqual(st['state'], 'failed')
        self.assertEqual(st['total_cost_usd'], 3.5)
        self.assertIn('max-usd exceeded', ' '.join(st['errors']))
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_final_cost_cap_is_checked(self):
        _, _, st = self.run_job('final-cost', max_usd=1)
        self.assertEqual(st['state'], 'failed')
        self.assertEqual(st['total_cost_usd'], 3.5)

    def test_cli_budget_result_is_failure_even_if_is_error_false(self):
        _, _, st = self.run_job('budget-result', max_usd=1)
        self.assertEqual(st['state'], 'failed')
        self.assertIn('error_max_budget_usd', ' '.join(st['errors']))

    def test_error_result_is_failure(self):
        _, _, st = self.run_job('error')
        self.assertEqual(st['state'], 'failed')
        self.assert_no_key()

    def test_missing_and_empty_key_never_launch(self):
        for value in (None, '', '  ', 'invalid\nkey'):
            with self.subTest(value=value):
                if value is None:
                    self.key.unlink(missing_ok=True)
                else:
                    self.key.write_text(value, encoding='utf-8')
                with patch.object(A.subprocess, 'Popen') as popen:
                    with self.assertRaisesRegex(RuntimeError, 'No subscription fallback'):
                        A.read_key(self.config)
                    with self.assertRaisesRegex(SystemExit, 'No subscription fallback'):
                        D.cmd_run(self.args())
                    popen.assert_not_called()
        self.assertFalse(D.JOBS_ROOT.exists())

    def test_runner_failure_redacts_exception(self):
        jd, _ = self.job()
        with patch.object(A, 'build_cmd', side_effect=RuntimeError('synthetic error ' + FAKE_KEY)):
            D.cmd_runner(argparse.Namespace(jobdir=str(jd)))
        st = D.load_json(jd / 'status.json')
        self.assertEqual(st['state'], 'failed')
        self.assertIn('[REDACTED]', ' '.join(st['errors']))
        self.assert_no_key()

    def test_concurrent_ledger_appends_keep_every_job(self):
        script = '''import sys
from pathlib import Path
import claude_api
root = Path(sys.argv[1])
for i in range(20):
    claude_api.append_ledger(root, Path(sys.argv[2] + str(i)),
        dict(profile='fake', model='fake-model', approved='unit test', created=1),
        dict(state='done', started=1, finished=2, total_cost_usd=.01, usage={}))
'''
        procs = [subprocess.Popen([sys.executable, '-c', script, str(self.storage), f'worker-{i}-'],
                                 cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(4)]
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, stderr.decode())
        entries = list(D.iter_events(self.storage / 'claude-api-ledger.jsonl'))
        self.assertEqual(len(entries), 80)
        self.assertEqual(len({e['job_id'] for e in entries}), 80)

    def test_prelaunch_cancel_is_ledgered(self):
        jd, _ = self.job()
        (jd / 'cancel.flag').touch()
        with patch.object(A.subprocess, 'Popen') as popen:
            D.cmd_runner(argparse.Namespace(jobdir=str(jd)))
            popen.assert_not_called()
        self.assertEqual(list(D.iter_events(self.storage / 'claude-api-ledger.jsonl'))[0]['status'], 'cancelled')

    def test_live_cancel_and_timeout(self):
        for case, status in [('cancel', 'cancelled'), ('timeout', 'timeout')]:
            jd, meta = self.job(name=case, max_minutes=.002 if case == 'timeout' else 1)
            with patch.dict(os.environ, FAKE_CASE=case), \
                    patch.object(D, 'cancel_requested', side_effect=lambda p: case == 'cancel' and (p / 'events.jsonl').exists()):
                D.cmd_runner(argparse.Namespace(jobdir=str(jd)))
            self.assertEqual(D.load_json(jd / 'status.json')['state'], status)
            self.assertFalse((self.root / 'should-not-exist').exists())

    def args(self, **changes):
        brief = self.root / 'brief.md'
        brief.write_text('task body', encoding='utf-8')
        values = dict(profile='claude-api-sonnet', approved='Owner / now / unit test', max_usd=1,
                      prompt_file=str(brief), preamble=[], cwd=None, mode='write', effort=None,
                      add_dir=[], resume=None, wait=0, max_minutes=1)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_run_requires_new_approval_and_valid_cap(self):
        for changes in [dict(approved=None), dict(approved=' '), dict(max_usd=float('nan')),
                        dict(max_usd=float('inf')), dict(max_usd=0), dict(max_usd=-1)]:
            with self.subTest(changes=changes), patch.object(D.subprocess, 'Popen') as popen:
                with self.assertRaises(SystemExit):
                    D.cmd_run(self.args(**changes))
                popen.assert_not_called()
        self.assertFalse(D.JOBS_ROOT.exists())

    def test_resume_inherits_cwd_and_passes_session(self):
        jd, _, _ = self.run_job()
        with patch.object(D.subprocess, 'Popen'), patch.object(D, 'wait_for') as wait, \
                contextlib.redirect_stdout(io.StringIO()):
            D.cmd_run(self.args(resume=jd.name))
        new_jd = wait.call_args.args[0]
        meta = D.load_json(new_jd / 'meta.json')
        self.assertEqual(meta['cwd'], str(self.root))
        self.assertEqual(meta['resume_session'], 'fake-session')
        D.cmd_runner(argparse.Namespace(jobdir=str(new_jd)))
        cmd = D.load_json(new_jd / 'command.json')
        self.assertEqual(cmd[cmd.index('--resume') + 1], 'fake-session')
        self.assertEqual(D.load_json(new_jd / 'status.json')['state'], 'done')
        self.assertEqual(len(list(D.iter_events(self.storage / 'claude-api-ledger.jsonl'))), 2)
        with self.assertRaises(SystemExit):
            D.cmd_run(self.args(resume=jd.name, approved=None))

    def test_modes_add_dirs_and_mcp_allowlist(self):
        for mode, expected in [('read', 'plan'), ('write', 'acceptEdits'), ('full', 'bypassPermissions')]:
            jd, meta = self.job(name=mode, mode=mode, add_dirs=[str(self.root / 'extra')])
            config = dict(self.config, claude_api_mcp_allow=['rea'], claude_api_mcp_servers={
                'rea': {'command': 'fake-rea'}, 'unwanted': {'command': 'do-not-run'}})
            cmd = A.build_cmd(meta, jd, config)
            self.assertEqual(cmd[cmd.index('--permission-mode') + 1], expected)
            self.assertEqual(cmd[cmd.index('--add-dir') + 1], meta['add_dirs'][0])
            self.assertEqual(D.load_json(jd / 'claude-mcp.json')['mcpServers'], {'rea': {'command': 'fake-rea'}})
        with self.assertRaisesRegex(RuntimeError, 'Missing'):
            A.build_cmd(meta, jd, dict(config, claude_api_mcp_allow=['missing']))

    def test_definitions_and_config_validation(self):
        for spec in I.SPECS:
            if spec['profile'].startswith('claude-api'):
                text = I.generate(spec, plugin=True)
                self.assertIn('Use ONLY when the owner has approved this specific run; pass --approved', text)
                self.assertIn('--approved "<owner approval:', text)
                self.assertTrue(I.is_generated(text))
        self.assertTrue(C._valid('claude_api_key_file', str(self.key)))
        self.assertFalse(C._valid('claude_api_key_file', ''))
        self.assertFalse(C._valid('claude_api_mcp_servers', []))
        self.assertFalse(C._valid('claude_api_mcp_allow', 'rea'))

    def test_cli_end_to_end_uses_only_temporary_home(self):
        home = self.root / 'home'
        relay = home / '.claude' / 'delegate'
        relay.mkdir(parents=True)
        (relay / 'config.json').write_text(json.dumps({'claude_api_key_file': str(self.key)}), encoding='utf-8')
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home),
                   DELEGATE_JOBS_DIR=str(relay / 'jobs'), DELEGATE_AGENTS_DIR=str(relay / 'agents'))
        brief = self.args().prompt_file
        result = subprocess.run([sys.executable, str(ROOT / 'delegate.py'), 'run', 'claude-api-haiku',
            '--prompt-file', brief, '--cwd', str(self.root), '--approved', 'Owner / now / fake CLI',
            '--max-usd', '1', '--wait', '10'], env=env, capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STATUS: done', result.stdout)
        self.assertNotIn(FAKE_KEY, result.stdout + result.stderr)
        entries = list(D.iter_events(relay / 'claude-api-ledger.jsonl'))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['total_cost_usd'], .125)
        for path in relay.rglob('*'):
            if path.is_file():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding='utf-8'), str(path))
