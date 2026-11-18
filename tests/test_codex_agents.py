# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import os
import unittest
from pathlib import Path

from tests.helpers import temporary_directory

MODULE = Path(__file__).resolve().parents[1] / 'codex_agents.py'
if MODULE.exists():
    import codex_agents as C
else:
    C = None

SID = '019aaa00-0000-7000-8000-000000000001'
CHILD = '019aaa00-0000-7000-8000-000000000002'
OTHER = '019aaa00-0000-7000-8000-000000000003'
STAMP = '2026-11-15T12:00:00Z'
NOW = 1794744000.0


def event(kind, payload, stamp=STAMP):
    return dict(timestamp=stamp, type=kind, payload=payload)


def meta(sid=SID, source='vscode', **kw):
    return event('session_meta', dict(id=sid, source=source, cwd='/work/example', **kw))


def msg(kind, **kw):
    return event('event_msg', dict(type=kind, **kw))


def write(root, rows, name='rollout.jsonl'):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return path


class CodexReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(C, 'Native Codex rollout reader is missing')
        self.tmp = temporary_directory()
        self.root = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)

    def reader(self, **kw):
        return C.CodexAgents(root=self.root, **kw)

    def test_lifecycle_does_not_treat_open_chat_as_running(self):
        p = write(self.root, [meta()])
        reader = self.reader()
        self.assertEqual(reader.scan(NOW + 2)[0]['state'], 'idle')
        with p.open('a') as f: f.write(json.dumps(msg('task_started', turn_id='t1')) + '\n')
        self.assertEqual(reader.scan(NOW + 3)[0]['state'], 'running')
        with p.open('a') as f: f.write(json.dumps(msg('task_complete', turn_id='t1', last_agent_message='SECRET')) + '\n')
        r = reader.scan(NOW + 4)[0]
        self.assertEqual(r['state'], 'done')
        self.assertNotIn('SECRET', json.dumps(r))
        with p.open('a') as f: f.write(json.dumps(msg('task_started', turn_id='t2')) + '\n')
        self.assertEqual(reader.scan(NOW + 5)[0]['state'], 'running')
        self.assertEqual(reader.scan(NOW + 4000)[0]['state'], 'lost')

    def test_old_turn_completion_does_not_close_new_turn(self):
        write(self.root, [meta(), msg('task_started', turn_id='a'), msg('task_started', turn_id='b'), msg('task_complete', turn_id='a')])
        self.assertEqual(self.reader().scan(NOW + 1)[0]['state'], 'running')

    def test_helper_exclusion_relay_resumes_and_child_parent(self):
        write(self.root, [meta(), msg('task_started')])
        source = dict(subagent=dict(thread_spawn=dict(parent_thread_id=SID, depth=1, agent_path='/root/reader', agent_nickname='Reader', agent_role='worker')))
        write(self.root, [meta(CHILD, source), msg('task_started')], 'child.jsonl')
        write(self.root, [meta(OTHER, dict(subagent=dict(other='guardian')))], 'helper.jsonl')
        reader = self.reader(relay_sessions=lambda: {SID: [dict(id='relay-old', name='Earlier'), dict(id='relay-new', name='Current')]})
        rows = reader.scan(NOW + 1)
        self.assertEqual([r['id'] for r in rows], ['codex-' + CHILD])
        self.assertIn('relay', rows[0]['parent'].lower())
        self.assertEqual(rows[0]['parent_id'], 'codex-' + SID)
        self.assertEqual(rows[0]['depth'], 1)

    def test_missing_parent_does_not_hide_child_and_duplicate_files_deduplicate(self):
        source = dict(subagent=dict(thread_spawn=dict(parent_thread_id=OTHER, depth=1)))
        write(self.root, [meta(CHILD, source), msg('task_started')])
        write(self.root, [meta(CHILD, source), msg('task_started'), msg('task_complete')], 'copy.jsonl')
        rows = self.reader().scan(NOW + 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'done')
        self.assertTrue(rows[0]['parent'])

    def test_partial_long_header_and_truncation(self):
        p = Path(self.root) / 'rollout.jsonl'
        header = json.dumps(meta(instructions='SECRET' * 20000))
        p.write_text(header[:500], encoding='utf8')
        reader = self.reader()
        self.assertEqual(reader.scan(NOW + 1), [])
        with p.open('a') as f: f.write(header[500:] + '\n' + json.dumps(msg('task_started')) + '\n')
        self.assertEqual(reader.scan(NOW + 2)[0]['state'], 'running')
        write(self.root, [meta(), msg('task_complete')])
        self.assertEqual(reader.scan(NOW + 3)[0]['state'], 'done')
        self.assertNotIn('SECRET', json.dumps(reader.scan(NOW + 4)))

    def test_modern_events_and_cumulative_tokens_are_not_double_counted(self):
        item = dict(type='CommandExecution', id='cmd1', command='python -c SECRET', parsed_cmd=[], aggregated_output='SECRET', exit_code=0)
        total = dict(input_tokens=100, cached_input_tokens=40, output_tokens=20, reasoning_output_tokens=5, total_tokens=120)
        rows = [meta(), msg('task_started'), event('turn_context', dict(model='gpt-example', effort='high', cwd='/work/example')),
                event('response_item', dict(type='custom_tool_call', call_id='outer', name='exec', input='SECRET')),
                msg('item_completed', item=item), msg('item_completed', item=item),
                msg('token_count', info=dict(total_token_usage=total, last_token_usage=total)),
                event('token_usage_record', dict(thread_token_usage=total)),
                msg('item_completed', item=dict(type='UserMessage', id='user', content='SECRET')),
                msg('item_completed', item=dict(type='Reasoning', id='reason', raw_content='SECRET'))]
        write(self.root, rows)
        r = self.reader().scan(NOW + 1)[0]
        self.assertEqual(r['steps'], 1)
        self.assertEqual(r['tokens']['output'], 20)
        self.assertEqual(r['tokens']['input'], 100)
        self.assertIn('high', r['model'])
        self.assertNotIn('SECRET', json.dumps(r))

    def test_legacy_calls_are_safe_and_completion_requires_event(self):
        write(self.root, [meta(), msg('task_started'), event('response_item', dict(type='function_call', name='exec_command', call_id='c', arguments=json.dumps(dict(cmd='echo SECRET')))),
                         event('response_item', dict(type='message', role='assistant', channel='final', content=[dict(type='output_text', text='SECRET')]))])
        r = self.reader().scan(NOW + 1)[0]
        self.assertEqual((r['steps'], r['state']), (1, 'running'))
        self.assertNotIn('SECRET', json.dumps(r))

    def test_abort_and_transient_error_have_distinct_states(self):
        write(self.root, [meta(), msg('task_started'), msg('turn_aborted', turn_id='t')])
        r = self.reader().scan(NOW + 1)[0]
        self.assertEqual(r['state'], 'cancelled')
        self.assertFalse(r['died'])
        write(self.root, [meta(), msg('task_started'), msg('error', message='stream disconnected SECRET')])
        r = self.reader().scan(NOW + 1)[0]
        self.assertEqual(r['state'], 'failed')
        self.assertTrue(r['died'])
        self.assertNotIn('SECRET', json.dumps(r))

    def test_project_filter_and_disabled_reader(self):
        write(self.root, [meta(), msg('task_started')])
        self.assertEqual(self.reader(config=dict(codex_agents_enabled=False)).scan(NOW), [])
        self.assertEqual(self.reader(config=dict(codex_agents_projects=['not-this-project'])).scan(NOW), [])

    def test_files_only_show_names_not_patch_contents(self):
        write(self.root, [meta(), msg('task_started'), msg('item_completed', item=dict(type='FileChange', id='f', status='completed', changes={'/work/example/foo.py': {'diff':'SECRET'}}))])
        r = self.reader().scan(NOW + 1)[0]
        self.assertEqual(r['edits'], 1)
        self.assertIn('foo.py', json.dumps(r['events']))
        self.assertNotIn('SECRET', json.dumps(r))

    def test_distinct_private_commands_do_not_look_like_a_loop(self):
        from delegate_config import repetition
        rows = [meta(), msg('task_started')]
        for i in range(8):
            rows.append(msg('item_completed', item=dict(type='CommandExecution', id=str(i), command='python task' + str(i), parsed_cmd=[], exit_code=0)))
        write(self.root, rows)
        self.assertFalse(repetition(self.reader().scan(NOW + 1)[0]['recent'])[0])

    def test_context_reports_last_prompt_not_window_capacity(self):
        write(self.root, [meta(), msg('task_started', model_context_window=1000000), msg('token_count', info=dict(total_token_usage=dict(input_tokens=500, output_tokens=30), last_token_usage=dict(input_tokens=100, output_tokens=5)))])
        self.assertEqual(self.reader().scan(NOW + 1)[0]['tokens']['context'], 100)

    def test_retryable_error_keeps_turn_running(self):
        write(self.root, [meta(), msg('task_started'), msg('error', message='stream disconnected', will_retry=True)])
        self.assertEqual(self.reader().scan(NOW + 1)[0]['state'], 'running')

    def test_mixed_format_resume_preserves_prior_turn_tools(self):
        p=write(self.root, [meta(), msg('task_started', turn_id='a'),
            event('response_item', dict(type='function_call', name='exec_command', call_id='old1', arguments='{}')),
            event('response_item', dict(type='function_call', name='exec_command', call_id='old2', arguments='{}')),
            msg('task_complete', turn_id='a')])
        reader=self.reader()
        self.assertEqual(reader.scan(NOW+1)[0]['steps'], 2)
        with p.open('a') as f:
            for row in [msg('task_started', turn_id='b'), msg('item_completed', item=dict(type='UserMessage', id='u', content='SECRET')),
                        msg('item_completed', item=dict(type='CommandExecution', id='new', command='echo hello', exit_code=0))]:
                f.write(json.dumps(row)+'\n')
        self.assertEqual(reader.scan(NOW+2)[0]['steps'], 3)

    def test_new_turn_resets_elapsed_time_without_resetting_usage(self):
        write(self.root, [meta(), msg('task_started',turn_id='a'), msg('task_complete',turn_id='a'),
                         event('event_msg',dict(type='task_started',turn_id='b'), '2026-11-15T12:30:00Z')])
        r=self.reader().scan(NOW+1801)[0]
        self.assertEqual(r['started'], NOW+1800)

    def test_list_shaped_file_changes_only_show_filenames(self):
        write(self.root, [meta(), msg('task_started'), msg('item_completed',item=dict(type='file_change',id='f',changes=[dict(path='/work/a.py',kind='update',diff='SECRET')]))])
        r=self.reader().scan(NOW+1)[0]
        self.assertIn('a.py',json.dumps(r['events']))
        self.assertNotIn('SECRET',json.dumps(r))

    def test_usage_and_reasoning_activity_prevent_false_lost_state(self):
        for row in [event('token_usage_record',dict(thread_token_usage=dict(output_tokens=40)), '2026-11-15T13:00:00Z'),
                    event('event_msg',dict(type='token_count',info=dict(total_token_usage=dict(output_tokens=40))), '2026-11-15T13:00:00Z'),
                    event('response_item',dict(type='reasoning',summary='SECRET'), '2026-11-15T13:00:00Z')]:
            write(self.root,[meta(),msg('task_started'),row])
            rec=self.reader().scan(NOW+3700)[0]
            self.assertEqual(rec['state'],'running')
            self.assertEqual(rec['quiet'],100)
            self.assertNotIn('SECRET',json.dumps(rec))
