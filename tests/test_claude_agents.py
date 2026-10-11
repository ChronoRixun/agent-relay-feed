# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Native Claude Code subagents: transcript reader, feed cards and watch events, on synthetic transcripts."""
import argparse
import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import claude_agents as CA
import delegate as D
import delegate_config as C
import feed as F
from tests.helpers import temporary_directory

NOW = 1_800_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


class Fixture:
    """Writes synthetic transcripts in Claude Code's layout: <root>/<project>/<session>/subagents/agent-<id>.jsonl."""

    def __init__(self, root, project='-work-shop-api', session='0000-session'):
        self.root = Path(root)
        self.dir = self.root / project / session / 'subagents'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def agent(self, agent_id, meta=None, lines=(), t=NOW - 600, mtime=None, tail=b''):
        path = self.dir / f'agent-{agent_id}.jsonl'
        if meta is not None:
            (self.dir / f'agent-{agent_id}.meta.json').write_text(json.dumps(meta), encoding='utf-8')
        data = b''.join(json.dumps(l).encode() + b'\n' for l in lines) + tail
        path.write_bytes(data)
        self.touch(path, mtime if mtime is not None else NOW - 60)
        return path

    def append(self, path, lines, mtime=None, tail=b''):
        with open(path, 'ab') as f:
            f.write(b''.join(json.dumps(l).encode() + b'\n' for l in lines) + tail)
        self.touch(path, mtime if mtime is not None else NOW)

    @staticmethod
    def touch(path, t):
        os.utime(path, (t, t))


def line(kind, t, cwd='/work/shop-api', **kw):
    return dict(type=kind, timestamp=iso(t), cwd=cwd, sessionId='0000-session', agentId='x', **kw)


def prompt(t, text='Do the task.', **kw):
    return line('user', t, message=dict(role='user', content=text), **kw)


def say(t, text, mid='m1', stop=None, model='claude-opus-5-5', out=10):
    return line('assistant', t, message=dict(id=mid, model=model, role='assistant', stop_reason=stop,
                content=[dict(type='text', text=text)],
                usage=dict(input_tokens=5, output_tokens=out, cache_read_input_tokens=100, cache_creation_input_tokens=0)))


def tool(t, name, inp, tid, mid='m2', model='claude-opus-5-5', out=20):
    return line('assistant', t, message=dict(id=mid, model=model, role='assistant', stop_reason='tool_use',
                content=[dict(type='tool_use', id=tid, name=name, input=inp)],
                usage=dict(input_tokens=5, output_tokens=out, cache_read_input_tokens=200, cache_creation_input_tokens=0)))


def result(t, tid, error=False):
    return line('user', t, message=dict(role='user', content=[dict(type='tool_result', tool_use_id=tid,
                content='private output', is_error=error)]))


def reader(root, **kw):
    kw.setdefault('window_hours', 24)
    kw.setdefault('projects', [])
    kw.setdefault('hide_types', ['relay-agent'])
    kw.setdefault('stall_seconds', 480)
    return CA.ClaudeAgents(root=str(root), humanize=F.humanize_claude, **kw)


def finished_agent(t=NOW - 600):
    return [prompt(t), tool(t + 1, 'Bash', dict(command='git status'), 't1'), result(t + 2, 't1'),
            say(t + 3, '## Summary\nFixed the parser.\nAll tests pass.', mid='m3', stop='end_turn')]


class ModelLabelTests(unittest.TestCase):
    def test_labels(self):
        for model, label in [('claude-fable-5-1', 'Fable 5.1'), ('claude-opus-5-5', 'Opus 5.5'),
                             ('claude-sonnet-4-5-20250929', 'Sonnet 4.5'), ('claude-opus-4-20250514', 'Opus 4'),
                             ('claude-haiku-6', 'Haiku 6'), ('claude-opus-5-5[1m]', 'Opus 5.5'),
                             ('claude-3-5-sonnet-20241022', 'Sonnet 3.5'), ('other-model', 'other-model'), (None, '')]:
            self.assertEqual(CA.model_label(model), label)


class ReaderTests(unittest.TestCase):
    def test_partial_last_line_waits_for_completion(self):
        with temporary_directory() as td:
            fx = Fixture(td)
            whole = json.dumps(tool(NOW - 5, 'Read', dict(file_path='/work/shop-api/notes.md'), 't2', mid='m4')).encode()
            path = fx.agent('a1', dict(agentType='general-purpose', description='Partial'),
                            [prompt(NOW - 10), tool(NOW - 8, 'Bash', dict(command='git status'), 't1'), result(NOW - 7, 't1')],
                            tail=whole[:40])
            r = reader(td)
            rec = r.scan(NOW)[0]
            self.assertEqual(rec['steps'], 1)
            fx.append(path, [], tail=whole[40:] + b'\n')
            rec = r.scan(NOW)[0]
            self.assertEqual(rec['steps'], 2)
            self.assertEqual(rec['events'][-1]['verb'], 'Read')
            self.assertEqual(rec['events'][-1]['detail'], 'notes.md')

    def test_large_file_is_read_incrementally(self):
        with temporary_directory() as td:
            fx = Fixture(td)
            lines = [prompt(NOW - 3000)]
            for i in range(1500):
                lines += [tool(NOW - 3000 + i, 'Bash', dict(command=f'python tools/step.py {i}'), f't{i}', mid=f'm{i}'),
                          result(NOW - 3000 + i, f't{i}')]
                lines[-1]['message']['content'][0]['content'] = 'x' * 2000     # bulky tool output
            path = fx.agent('big', dict(agentType='general-purpose', description='Big'), lines)
            self.assertGreater(path.stat().st_size, 3_000_000)
            r = reader(td)
            self.assertEqual(r.scan(NOW)[0]['steps'], 1500)
            fx.append(path, [tool(NOW - 1, 'Grep', dict(pattern='needle'), 'tz', mid='mz')])
            fed = []
            original = CA.Transcript.feed
            with patch.object(CA.Transcript, 'feed', lambda self, ev: (fed.append(ev), original(self, ev))):
                rec = r.scan(NOW)[0]
            self.assertEqual(len(fed), 1)           # only the new line was parsed
            self.assertEqual(rec['steps'], 1501)
            self.assertEqual(rec['events'][-1]['verb'], 'Search')
            self.assertLessEqual(len(rec['events']), CA.EVENTS_KEPT)
            self.assertEqual(r.files[str(path)].offset, path.stat().st_size)

    def test_replaced_file_starts_over(self):
        with temporary_directory() as td:
            fx = Fixture(td)
            path = fx.agent('a1', {}, finished_agent())
            r = reader(td)
            self.assertEqual(r.scan(NOW)[0]['steps'], 1)
            fx.agent('a1', None, [prompt(NOW - 5)])
            rec = r.scan(NOW)[0]
            self.assertEqual((rec['steps'], rec['state']), (0, 'running'))

    def test_parent_stop_cancels(self):
        t = NOW - 600
        lines = [prompt(t), tool(t + 1, 'Bash', dict(command='make'), 't1')]

        def notice(at, task, status):
            text = (f'<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n'
                    f'<summary>Agent stopped</summary>\n</task-notification>')
            return dict(type='user', timestamp=iso(at), message=dict(role='user', content=text))

        with temporary_directory() as td:
            fx = Fixture(td)
            fx.agent('a1', dict(agentType='general-purpose', description='stopped one'), lines, mtime=t + 1)
            fx.agent('a2', dict(agentType='general-purpose', description='kept running'), lines, mtime=NOW - 5)
            parent = fx.dir.parent.with_suffix('.jsonl')
            parent.write_bytes(b''.join(json.dumps(l).encode() + b'\n' for l in [
                notice(t + 30, 'a1', 'killed'), notice(t + 30, 'a2', 'completed')]))
            states = {r['label']: r['state'] for r in reader(td).scan(NOW)}
            self.assertEqual(states, {'stopped one': 'cancelled', 'kept running': 'running'})
            # A stopped agent that is resumed later works again.
            fx.append(fx.dir / 'agent-a1.jsonl', [prompt(NOW - 20, 'Continue.')], mtime=NOW - 20)
            states = {r['label']: r['state'] for r in reader(td).scan(NOW)}
            self.assertEqual(states['stopped one'], 'running')

    def test_status_rules(self):
        t = NOW - 600
        cases = [
            ('done after grace', finished_agent(), {}, NOW - 60, 'done', False),
            ('closed turn still in grace', finished_agent(NOW - 10), {}, NOW - 2, 'running', False),
            ('tool call pending', [prompt(t), tool(t + 1, 'Bash', dict(command='make'), 't1')], {}, NOW - 30, 'running', False),
            ('quiet past stall', [prompt(t), tool(t + 1, 'Bash', dict(command='make'), 't1')], {}, NOW - 500, 'running', True),
            ('session gone', [prompt(t - 9000), tool(t - 8999, 'Bash', dict(command='make'), 't1')], {}, NOW - 7200, 'lost', False),
            ('api error', [prompt(t), dict(say(t + 1, 'API Error: overloaded', mid='e1', stop='stop_sequence', model='<synthetic>'),
                                           isApiErrorMessage=True, error='server_error')], {}, NOW - 60, 'failed', False),
            ('stopped by user', [prompt(t), tool(t + 1, 'Bash', dict(command='make'), 't1'), result(t + 2, 't1', True),
                                 prompt(t + 3, '[Request interrupted by user]')], dict(stoppedByUser=True), NOW - 60, 'cancelled', False),
            ('follow-up reopens', finished_agent() + [prompt(NOW - 100, 'One more thing.')], {}, NOW - 60, 'running', False),
        ]
        for name, lines, extra, mtime, state, stalled in cases:
            with self.subTest(name), temporary_directory() as td:
                Fixture(td).agent('a1', dict(agentType='general-purpose', description=name, **extra), lines, mtime=mtime)
                rec = reader(td).scan(NOW)[0]
                self.assertEqual((rec['state'], rec['stalled']), (state, stalled))
                if state == 'done':
                    self.assertEqual(rec['result'], '## Summary\nFixed the parser.\nAll tests pass.')
                    self.assertEqual(rec['finished'], NOW - 60)
                elif state == 'failed':
                    self.assertEqual(rec['errors'], ['API error: server_error'])
                    self.assertEqual(rec['model'], '')
                else:
                    self.assertIsNone(rec['result'])

    def test_result_keeps_first_lines_only(self):
        with temporary_directory() as td:
            text = '\n'.join(f'line {i}' for i in range(30))
            Fixture(td).agent('a1', {}, [prompt(NOW - 100), say(NOW - 90, text, stop='end_turn')])
            rec = reader(td).scan(NOW)[0]
            self.assertEqual(rec['result'].splitlines(), [f'line {i}' for i in range(CA.RESULT_LINES)] + ['…'])

    def test_usage_counts_each_message_once_and_model_is_latest(self):
        with temporary_directory() as td:
            lines = [prompt(NOW - 100),
                     say(NOW - 99, 'Looking.', mid='m1', out=3), say(NOW - 98, 'Still looking.', mid='m1', out=7),
                     tool(NOW - 97, 'Bash', dict(command='ls'), 't1', mid='m2', out=20, model='claude-fable-5-1')]
            Fixture(td).agent('a1', {}, lines)
            rec = reader(td).scan(NOW)[0]
            self.assertEqual(rec['tokens'], dict(output=27, context=205))
            self.assertEqual((rec['model_id'], rec['model']), ('claude-fable-5-1', 'Fable 5.1'))
            self.assertEqual(rec['messages'], 2)
            self.assertEqual([e['kind'] for e in rec['events']], ['message', 'message', 'command'])

    def test_nested_agents_hidden_relays_window_and_project_filter(self):
        with temporary_directory() as td:
            fx = Fixture(td)
            fx.agent('parent', dict(agentType='planner', description='Coordinate the refactor', spawnDepth=1), finished_agent())
            fx.agent('child', dict(agentType='plugin:worker', description='Refactor auth', spawnDepth=2,
                                   parentAgentId='parent'), [prompt(NOW - 50)])
            fx.agent('relay', dict(agentType='plugin:relay-agent', description='Relay wrapper'), [prompt(NOW - 50)])
            fx.agent('old', dict(agentType='planner', description='Yesterday'), finished_agent(), mtime=NOW - 30 * 3600)
            Fixture(td, project='-other-project').agent('elsewhere', dict(agentType='planner', description='Elsewhere'),
                                                        [prompt(NOW - 20, cwd='/other/project')])
            recs = {r['agent_id']: r for r in reader(td).scan(NOW)}
            self.assertEqual(set(recs), {'parent', 'child', 'elsewhere'})
            child = recs['child']
            self.assertEqual((child['depth'], child['parent_id'], child['parent']), (2, 'parent', 'Coordinate the refactor'))
            self.assertIsNone(recs['parent']['parent'])
            self.assertEqual(child['label'], 'Refactor auth')
            self.assertEqual(child['session'], '0000-session')
            only = {r['agent_id'] for r in reader(td, projects=['/OTHER/project']).scan(NOW)}
            self.assertEqual(only, {'elsewhere'})
            self.assertEqual(reader(td).scan(NOW)[0]['agent_id'], 'elsewhere')      # newest first

    def test_missing_meta_and_root(self):
        with temporary_directory() as td:
            self.assertEqual(reader(Path(td) / 'missing').scan(NOW), [])
            Fixture(td).agent('abcdef1234', None, [prompt(NOW - 10)])
            rec = reader(td).scan(NOW)[0]
            self.assertEqual((rec['label'], rec['agent_type'], rec['depth']), ('Claude subagent abcdef12', 'subagent', None))

    def test_work_folder_from_file_tools(self):
        with temporary_directory() as td:
            lines = [prompt(NOW - 100, 'x')] + [tool(NOW - 90 + i, 'Edit', dict(file_path=f'/repo/src/f{i}.py', old_string='a',
                                                new_string='b'), f't{i}', mid=f'm{i}') for i in range(3)]
            Fixture(td).agent('a1', {}, lines)
            rec = reader(td).scan(NOW)[0]
            self.assertEqual((rec['cwd'], rec['workdir'], rec['edits']), ('/work/shop-api', '/repo/src', 3))


class HumanizeToolTests(unittest.TestCase):
    def test_tools(self):
        samples = [
            ('Bash', dict(command='cd /repo && python -m unittest', description='Run the tests'), 'test', 'Run tests', ''),
            ('PowerShell', dict(command='Get-Content notes.md -Tail 5'), 'read', 'Read', 'notes.md (end)'),
            ('Read', dict(file_path='/repo/src/app.py', offset=10), 'read', 'Read', 'app.py'),
            ('Edit', dict(file_path='/repo/src/app.py', old_string='SECRET', new_string='SECRET2'), 'write', 'Edit', 'app.py'),
            ('Write', dict(file_path='/repo/new.py', content='SECRET'), 'write', 'Write', 'new.py'),
            ('Grep', dict(pattern='def main', path='/repo'), 'search', 'Search', '“def main”'),
            ('Glob', dict(pattern='**/*.py'), 'list', 'Find files', '**/*.py'),
            ('Agent', dict(subagent_type='worker', description='Fix the parser', prompt='SECRET'), 'agent', 'Start agent', 'worker: Fix the parser'),
            ('TodoWrite', dict(todos=[dict(content='SECRET', status='completed'), dict(content='b', status='pending')]), 'todo', 'Update todos', '1/2 done'),
            ('WebFetch', dict(url='https://example.org/docs/page', prompt='SECRET'), 'web', 'Fetch', 'example.org'),
            ('mcp__tracker__create_issue', dict(title='SECRET'), 'mcp', 'tracker: create issue', ''),
            ('SubagentHandback', dict(message='SECRET'), 'message', 'Hand back to caller', ''),
            ('NewTool', dict(x='SECRET'), 'run', 'NewTool', ''),
        ]
        for name, inp, icon, verb, detail in samples:
            with self.subTest(name):
                h = F.humanize_claude(name, inp)
                self.assertEqual((h['icon'], h['verb'], h['detail']), (icon, verb, detail))
                self.assertNotIn('SECRET', json.dumps(h))
                self.assertTrue({'icon', 'verb', 'detail', 'more', 'raw'} <= h.keys())

    def test_bash_control_flow(self):
        for command, verb in [('for f in a b; do python tools/x.py $f; done', 'Run x.py'),
                              ('(cmake -S . -B build && cmake --build build)', 'Build'),
                              ('until grep -q ready log.txt; do sleep 5; done', 'Search'),
                              ('timeout 600 python -m unittest', 'Run tests')]:
            self.assertEqual(F.humanize_command(command)['verb'], verb)


class FeedIntegrationTests(unittest.TestCase):
    def test_snapshot_merges_claude_cards_with_relay_jobs(self):
        with temporary_directory() as td:
            root, jobs = Path(td) / 'projects', Path(td) / 'jobs'
            jobs.mkdir()
            fx = Fixture(root)
            loop = [tool(NOW - 20 + i, 'Bash', dict(command='git status'), f't{i}', mid=f'm{i}') for i in range(6)]
            fx.agent('a1', dict(agentType='worker', description='Refactor auth', spawnDepth=1),
                     [prompt(NOW - 30), say(NOW - 25, 'Starting with the session helper.')] + loop, mtime=NOW - 1)
            fx.agent('a2', dict(agentType='worker', description='Old review'), finished_agent(NOW - 5000), mtime=NOW - 4000)
            r = reader(root)
            with patch.object(F, 'claude_reader', return_value=r), patch.object(F.D, 'JOBS_ROOT', jobs), \
                 patch.object(F, 'newest_write', return_value=0), patch.dict(C.CONFIG, watched_processes=[]), \
                 patch.object(F.time, 'time', return_value=NOW):
                snap = F.snapshot(1)
                wide = F.snapshot(2)
            self.assertEqual([j['id'] for j in snap['jobs']], ['agent-a1'])
            self.assertEqual([j['id'] for j in wide['jobs']], ['agent-a1', 'agent-a2'])
            card = snap['jobs'][0]
            self.assertEqual((card['source'], card['backend'], card['label'], card['profile'], card['name']),
                             ('claude', 'claude', 'Opus 5.5', 'worker', 'Refactor auth'))
            self.assertTrue(card['loop'])
            self.assertFalse(card['stalled'])
            self.assertEqual(card['now_msg'], 'Starting with the session helper.')
            self.assertEqual(card['current']['verb'], 'git status')
            self.assertEqual(card['counts']['commands'], 6)
            done = wide['jobs'][1]
            self.assertEqual((done['state'], done['result'].splitlines()[1]), ('done', 'Fixed the parser.'))
            json.dumps(snap, allow_nan=False)
            page = F.PAGE.read_text(encoding='utf-8')
            self.assertIn("j.source === 'claude'", page)

    def test_card_shape_matches_relay_jobs(self):
        with temporary_directory() as td:
            Fixture(td).agent('a1', {}, finished_agent())
            rec = reader(td).scan(NOW)[0]
            with patch.object(F.D, 'load_json', return_value={}), patch.object(F.D, 'effective_status', return_value={}), \
                 patch.object(F, 'parse_activity', return_value=([], 0)), patch.object(F, 'tail_lines', return_value=[]), \
                 patch.object(F.D, 'read_text', return_value=''):
                relay = F.job_summary(Path(td), NOW)
            self.assertEqual(set(F.claude_job_summary(rec, NOW)), set(relay))

    def test_switched_off(self):
        with patch.dict(C.CONFIG, claude_agents_enabled=False), patch.object(F, '_claude', dict(reader=None)):
            self.assertIsNone(F.claude_reader())
            self.assertEqual(F.claude_jobs(NOW), [])

    def test_unreadable_transcripts_do_not_blank_the_page(self):
        with temporary_directory() as td, patch.object(F, 'claude_jobs', side_effect=OSError('boom')), \
             patch.object(F.D, 'JOBS_ROOT', Path(td)), patch.dict(C.CONFIG, watched_processes=[]):
            snap = F.snapshot(1)
        self.assertEqual(snap['jobs'][0]['state'], '?')


class WatchTests(unittest.TestCase):
    def test_start_loop_stall_and_finish_events(self):
        with temporary_directory() as td:
            fx = Fixture(td)
            t0 = time.time()
            path = fx.agent('a1', dict(agentType='plugin:worker', description='Refactor auth'), [prompt(t0 - 30)], mtime=t0 - 1)
            r = CA.ClaudeAgents(root=td, projects=[], hide_types=[], stall_seconds=60)
            args = argparse.Namespace(stall=1, loop=6)
            lines, followed = [], {}

            def emit(tag, colour, text):
                lines.append((tag, colour, text))

            D._watch_claude(r, followed, t0, args, emit)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0][0], 'claude/worker#a1')
            self.assertIn('▶ Refactor auth', lines[0][2])
            fx.append(path, [tool(t0 + i, 'Bash', dict(command='git status'), f't{i}', mid=f'm{i}') for i in range(6)], mtime=t0 + 6)
            D._watch_claude(r, followed, t0 + 7, args, emit)
            self.assertIn('possible loop', lines[-1][2])
            with patch.object(D, '_newest_write', return_value=0):
                D._watch_claude(r, followed, t0 + 200, args, emit)
                D._watch_claude(r, followed, t0 + 260, args, emit)       # warned once only
            self.assertEqual(sum('no activity' in l[2] for l in lines), 1)
            fx.append(path, [result(t0 + 300, f't{i}') for i in range(6)] + [say(t0 + 301, 'Done: auth refactored.', mid='z', stop='end_turn')], mtime=t0 + 301)
            D._watch_claude(r, followed, t0 + 320, args, emit)
            self.assertEqual(lines[-1][1], 'green')
            self.assertIn('■ done', lines[-1][2])
            self.assertIn('» Done: auth refactored.', lines[-1][2])
            count = len(lines)
            D._watch_claude(r, followed, t0 + 330, args, emit)
            self.assertEqual(len(lines), count)

    def test_finished_agents_are_not_announced(self):
        with temporary_directory() as td:
            Fixture(td).agent('a1', {}, finished_agent(time.time() - 600), mtime=time.time() - 300)
            lines = []
            D._watch_claude(CA.ClaudeAgents(root=td, projects=[], hide_types=[]), {}, time.time(),
                            argparse.Namespace(stall=8, loop=6), lambda *a: lines.append(a))
            self.assertEqual(lines, [])


class ConfigKeyTests(unittest.TestCase):
    def test_claude_keys(self):
        with temporary_directory() as td:
            path = Path(td) / 'config.json'
            good = dict(claude_agents_enabled=False, claude_projects_root='/elsewhere/projects',
                        claude_agents_window_hours=6, claude_agents_projects=['shop-api'])
            path.write_text(json.dumps(good), encoding='utf-8')
            self.assertEqual({k: C.load_config(path)[k] for k in good}, good)
            bad = dict(claude_agents_enabled='yes', claude_projects_root=' ', claude_agents_window_hours=0,
                       claude_agents_projects='shop-api')
            path.write_text(json.dumps(bad), encoding='utf-8')
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                config = C.load_config(path)
            self.assertEqual({k: config[k] for k in bad}, {k: C.DEFAULTS[k] for k in bad})
