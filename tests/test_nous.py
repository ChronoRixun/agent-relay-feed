# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Nous Portal profiles: key handling, the Kimi home, measured spend, the cap and the budget."""
import contextlib
import io
import json
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D
import delegate_config as C
import install_agents as I
import nous as N
from tests.helpers import temporary_directory

FAKE_KEY = 'synthetic-nous-key-NEVER-real'


def usage_line(inp, out, cached=0, write=0):
    return json.dumps(dict(type='usage.record', agentId='main', model='nous/x',
                           usage=dict(inputOther=inp, output=out, inputCacheRead=cached, inputCacheCreation=write))) + '\n'


def write_session(home, session, agent, lines):
    f = Path(home) / 'sessions' / 'wd_x_1' / f'session_{session}' / 'agents' / agent / 'wire.jsonl'
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, 'a', encoding='utf-8') as fh:
        fh.writelines(lines)
    return f


class NousUnitTests(unittest.TestCase):
    def test_kimi_config_is_valid_and_holds_no_key(self):
        with temporary_directory() as td:
            home = N.prepare_home(Path(td) / 'kh')
            text = (home / 'config.toml').read_text(encoding='utf-8')
            cfg = tomllib.loads(text)
            self.assertEqual(cfg['providers']['nous'], dict(type='openai', base_url=N.BASE_URL, api_key_env='NOUS_API_KEY'))
            self.assertEqual(set(cfg['models']), set(N.MODELS))
            self.assertEqual(cfg['models']['nous/glm-5.3']['model'], 'z-ai/glm-5.3')
            self.assertNotIn('support_efforts', cfg['models']['nous/mimo-v2.6-pro'])
            self.assertNotIn('api_key =', text)

    def test_every_profile_names_a_known_model(self):
        for name, prof in D.PROFILES.items():
            if prof.get('provider') == 'nous':
                with self.subTest(name):
                    self.assertIn(prof['model'], N.MODELS)
                    effort = prof['effort']
                    self.assertTrue(effort == 'default' or effort in N.MODELS[prof['model']]['efforts'])

    def test_usage_is_summed_across_agents_and_priced(self):
        with temporary_directory() as td:
            write_session(td, 's1', 'main', [usage_line(1000, 200, cached=9000), 'not json\n',
                                             json.dumps(dict(type='other', usage=dict(inputOther=5))) + '\n'])
            write_session(td, 's1', 'sub-1', [usage_line(500, 100, write=300)])
            tokens = N.usage(N.wire_files(td))
            self.assertEqual(tokens, dict(input=1500, cached=9000, cache_write=300, output=300, calls=2))
            self.assertEqual(N.cost(tokens, (1e-6, 0.1e-6, 2e-6)), round(1800e-6 + 900e-6 + 600e-6, 6))
            self.assertEqual(N.minus(tokens, dict(input=1000, cached=9000, output=200, calls=1)),
                             dict(input=500, cached=0, cache_write=300, output=100, calls=1))

    def test_meter_reads_only_new_complete_lines(self):
        with temporary_directory() as td:
            meter = N.Meter(td)
            self.assertEqual(meter.read()['calls'], 0)                  # no sessions yet
            f = write_session(td, 's1', 'main', [usage_line(100, 10)])
            self.assertEqual(meter.read()['input'], 100)
            with open(f, 'a', encoding='utf-8') as fh:
                fh.write(usage_line(50, 5)[:20])                         # half-written line
            self.assertEqual(meter.read()['input'], 100)
            with open(f, 'a', encoding='utf-8') as fh:
                fh.write(usage_line(50, 5)[20:])
            write_session(td, 's1', 'sub-1', [usage_line(7, 1)])
            self.assertEqual(meter.read(), dict(input=157, cached=0, cache_write=0, output=16, calls=3))
            self.assertEqual(meter.read()['calls'], 3)                  # nothing new, nothing counted twice

    def test_live_prices_and_fallback(self):
        listing = dict(data=[dict(id='z-ai/glm-5.3', pricing=dict(prompt='0.000001', completion='0.000003',
                                                                    input_cache_read='0.0000002')),
                             dict(id='z-ai/other', pricing=dict(prompt='1', completion='1'))])
        self.assertEqual(N.prices_for('nous/glm-5.3', fetch=lambda: listing), [1e-6, 2e-7, 3e-6])
        def down():
            raise OSError('offline')
        self.assertEqual(N.prices_for('nous/glm-5.3', fetch=down), list(N.MODELS['nous/glm-5.3']['prices']))

    def test_key_file_checks(self):
        with temporary_directory() as td:
            cfg = dict(nous_api_key_file=str(Path(td) / 'key.txt'))
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                N.read_key(cfg)
            (Path(td) / 'key.txt').write_text('two words', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'more than one word'):
                N.read_key(cfg)
            (Path(td) / 'key.txt').write_text(FAKE_KEY + '\n', encoding='utf-8')
            self.assertEqual(N.read_key(cfg), FAKE_KEY)

    def test_month_spend_and_config(self):
        with temporary_directory() as td:
            now = time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
            entries = [dict(end=now - 60, total_cost_usd=1.25), dict(end=now - 120, total_cost_usd=0.5),
                       dict(end=now - 40 * 86400, total_cost_usd=9), dict(end=now, total_cost_usd=None)]
            (Path(td) / N.LEDGER).write_text(''.join(json.dumps(e) + '\n' for e in entries), encoding='utf-8')
            self.assertEqual(N.month_spend(td, now), 1.75)
        self.assertTrue(C._valid('nous_monthly_usd', 25))
        self.assertFalse(C._valid('nous_max_usd_per_job', -1))

    def test_relay_definitions_describe_the_model_and_billing(self):
        spec = next(s for s in I.SPECS if s['name'] == 'nous-gemini-argon')
        text = I.generate(spec, home=Path('/home/x'), delegate=Path('/x/delegate.py'), python=Path('/usr/bin/python'))
        self.assertIn('Gemini 4 Argon', text)
        self.assertIn('Billed to Nous Portal credits', text)
        self.assertIn('run nous-gemini-argon', text)


class NousRunTests(unittest.TestCase):
    def setUp(self):
        self._td = temporary_directory()
        self.td = Path(self._td.__enter__())
        self.addCleanup(self._td.__exit__, None, None, None)
        (self.td / 'key.txt').write_text(FAKE_KEY, encoding='utf-8')
        self.spawned = []
        for p in (patch.multiple(D, JOBS_ROOT=self.td / 'jobs', AGENTS_ROOT=self.td / 'agents', DELEGATE_ROOT=self.td,
                                 spawn_runner=lambda jd, meta: self.spawned.append(jd)),
                  patch.dict(D.CONFIG, nous_api_key_file=str(self.td / 'key.txt'), nous_monthly_usd=25,
                             nous_max_usd_per_job=3.0),
                  patch.object(N, 'live_prices', return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.brief = self.td / 'brief.md'
        self.brief.write_text('Do the thing.', encoding='utf-8')

    def run_job(self, **kw):
        args = dict(profile='nous-glm', prompt_file=str(self.brief), cwd=str(self.td), mode='read', preamble=[],
                    effort=None, add_dir=[], resume=None, wait=0, max_minutes=30, approved=None, max_usd=None,
                    name='Second opinion', after=None, after_ok=False)
        args.update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            D.cmd_run(SimpleNamespace(**args))
        jd = self.spawned[-1]
        return jd, json.loads((jd / 'meta.json').read_text())

    def test_meta_has_its_own_kimi_home_prices_and_default_cap(self):
        jd, meta = self.run_job()
        self.assertEqual(meta['provider'], 'nous')
        self.assertEqual(Path(meta['kimi_home']), jd / 'kimi-home')
        self.assertEqual(meta['max_usd'], 3.0)
        self.assertEqual(meta['prices'], list(N.MODELS['nous/glm-5.3']['prices']))
        self.assertNotIn(FAKE_KEY, json.dumps(meta) + (jd / 'prompt.md').read_text())
        jd2, meta2 = self.run_job(max_usd=0.5)
        self.assertEqual(meta2['max_usd'], 0.5)

    def test_follow_up_reuses_the_kimi_home(self):
        jd, meta = self.run_job()
        (jd / 'status.json').write_text(json.dumps(dict(state='done', session_id='s1', finished=time.time())), encoding='utf-8')
        _jd2, meta2 = self.run_job(resume=jd.name)
        self.assertEqual(meta2['kimi_home'], meta['kimi_home'])
        self.assertEqual(meta2['resume_session'], 's1')

    def test_refusals(self):
        with patch.dict(D.CONFIG, nous_api_key_file=str(self.td / 'missing.txt')), self.assertRaises(SystemExit) as stop:
            self.run_job()
        self.assertIn('key file missing', str(stop.exception))
        (self.td / N.LEDGER).write_text(json.dumps(dict(end=time.time(), total_cost_usd=25.5)) + '\n', encoding='utf-8')
        with self.assertRaises(SystemExit) as stop:
            self.run_job()
        self.assertIn('budget', str(stop.exception))
        with self.assertRaises(SystemExit):
            self.run_job(approved='Sam')
        self.assertEqual(self.spawned, [])

    def test_spend_is_measured_against_the_baseline_and_recorded(self):
        jd, meta = self.run_job()
        home = meta['kimi_home']
        write_session(home, 's0', 'main', [usage_line(10_000, 1_000)])          # an earlier run in this home
        status = dict(state='running', started=time.time(), nous_baseline=N.usage(N.wire_files(home)))
        write_session(home, 's0', 'main', [usage_line(100_000, 20_000, cached=900_000)])
        usd = D.nous_spend(meta, status, jd)
        prompt, cached, completion = meta['prices']
        self.assertAlmostEqual(usd, 100_000 * prompt + 900_000 * cached + 20_000 * completion, places=6)
        saved = json.loads((jd / 'status.json').read_text())
        self.assertEqual(saved['usage']['cached_input_tokens'], 900_000)
        (self.td / 'agents' / 'nous-glm' / 'reports').mkdir(parents=True, exist_ok=True)
        (jd / 'events.jsonl').write_text('', encoding='utf-8')
        (jd / 'result.md').write_text('Looks right.', encoding='utf-8')
        with patch.object(D.notify, 'send', return_value=[]):
            D.finalize(jd, meta, status, None, 0)
        entry = json.loads((self.td / N.LEDGER).read_text().splitlines()[-1])
        self.assertEqual((entry['agent'], entry['model']), ('nous-glm', 'z-ai/glm-5.3'))
        self.assertAlmostEqual(entry['total_cost_usd'], usd, places=6)
        self.assertEqual(N.month_spend(self.td), round(usd, 4))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.report(jd)
        self.assertIn('NOUS COST (USD)', out.getvalue())
        self.assertIn('(cap $3)', out.getvalue())
        self.assertTrue(D.nous_line().startswith('NOUS PORTAL: $'))


if __name__ == '__main__':
    unittest.main()
