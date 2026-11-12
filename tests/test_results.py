# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
import contextlib
import io
import json
import os
import struct
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delegate as D
import feed as F
import results as R
from tests.helpers import temporary_directory
from tests.test_phase import make_job


def png(path, width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                           + chunk(b'IEND', b''))


def report(block):
    return 'Did the work.\n\n```result\n' + json.dumps(block, indent=1) + '\n```\n'


def fake_git(answers):
    def git(repo, *args):
        return answers.get(args[0], (0, ''))
    return git


class ExtractTests(unittest.TestCase):
    def test_last_block_with_a_verdict_wins(self):
        text = '```json\n{"note": 1}\n```\ntext\n' + report({'verdict': 'fail'}) + report({'verdict': 'pass'})
        self.assertEqual(R.extract(text), ({'verdict': 'pass'}, None))

    def test_missing_and_broken_blocks(self):
        self.assertEqual(R.extract('Free text only.'), (None, 'no result block'))
        data, error = R.extract('```result\n{"verdict": "pass",}\n```')
        self.assertIsNone(data)
        self.assertIn('not valid JSON', error)

    def test_rules_show_a_block_the_parser_accepts(self):
        data, error = R.extract(R.RULES)
        self.assertIsNone(error)
        self.assertEqual(set(data), {'verdict', 'summary', *R.LISTS})


class ImageSizeTests(unittest.TestCase):
    def test_png_gif_bmp_jpeg_and_junk(self):
        with temporary_directory() as td:
            td = Path(td)
            png(td / 'a.png', 3840, 2160)
            (td / 'a.gif').write_bytes(b'GIF89a' + struct.pack('<HH', 320, 200) + b'\0' * 20)
            (td / 'a.bmp').write_bytes(b'BM' + b'\0' * 16 + struct.pack('<ii', 640, -480) + b'\0' * 10)
            (td / 'a.jpg').write_bytes(b'\xff\xd8' + b'\xff\xe0' + struct.pack('>H', 4) + b'\0\0'
                                       + b'\xff\xc0' + struct.pack('>H', 11) + b'\x08' + struct.pack('>HH', 1080, 1920) + b'\0' * 4)
            (td / 'a.txt').write_text('not an image')
            self.assertEqual(R.image_size(td / 'a.png'), (3840, 2160))
            self.assertEqual(R.image_size(td / 'a.gif'), (320, 200))
            self.assertEqual(R.image_size(td / 'a.bmp'), (640, 480))
            self.assertEqual(R.image_size(td / 'a.jpg'), (1920, 1080))
            self.assertIsNone(R.image_size(td / 'a.txt'))
            self.assertIsNone(R.image_size(td / 'missing.png'))


class VerifyTests(unittest.TestCase):
    def flags(self, data, **kw):
        return [f['text'] for f in R.verify(data, **kw) if not f['ok']]

    def test_clean_result_has_no_flags(self):
        with temporary_directory() as td:
            shot = Path(td) / 'shot.png'
            png(shot, 3840, 2160)
            data = dict(verdict='pass', commits=[dict(repo=td, branch='fix/x', head='abc1234def')],
                        checks=[dict(name='unit', result='pass')], files=[str(shot)],
                        evidence=[dict(path=str(shot), shows='menu', width=3840, height=2160)])
            git = fake_git({'rev-parse': (0, 'abc1234def5678')})
            found = R.verify(data, started=time.time() - 30, git=git)
            self.assertEqual([f['text'] for f in found if not f['ok']], [])
            self.assertGreaterEqual(len(found), 5)

    def test_wrong_size_image_is_flagged(self):
        with temporary_directory() as td:
            shot = Path(td) / 'shot.png'
            png(shot, 1920, 1080)
            flags = self.flags(dict(verdict='pass', evidence=[dict(path=str(shot), width=3840, height=2160)]))
            self.assertEqual(len(flags), 1)
            self.assertIn('1920x1080, NOT the claimed 3840x2160', flags[0])

    def test_missing_empty_and_stale_files(self):
        with temporary_directory() as td:
            empty, old = Path(td) / 'empty.log', Path(td) / 'old.log'
            empty.write_bytes(b'')
            old.write_text('x')
            os.utime(old, (1000, 1000))
            flags = self.flags(dict(verdict='partial', files=[str(Path(td) / 'gone.txt'), str(empty)],
                                    evidence=[dict(path=str(old))]), started=time.time())
            self.assertEqual(len(flags), 3)
            self.assertIn('not found', flags[0])
            self.assertIn('is empty', flags[1])
            self.assertIn('older than this job', flags[2])

    def test_expected_fail_checks_do_not_contradict_a_pass(self):
        checks = [dict(name='unit', result='pass'), dict(name='red test on old code', result='expected-fail'),
                  dict(name='mutation', result='XFail'), dict(name='old build', result='red test')]
        self.assertEqual(self.flags(dict(verdict='pass', checks=checks)), [])
        lines = R.summary_lines(R.digest(report(dict(verdict='pass', checks=checks))))
        self.assertIn('mutation=expected-fail', lines[-1] if 'CHECKS' in lines[-1] else ' '.join(lines))
        # a real failure still contradicts a pass
        self.assertTrue(self.flags(dict(verdict='pass', checks=checks + [dict(name='gate 2', result='fail')])))

    def test_a_folder_as_evidence_is_noted_not_flagged(self):
        with temporary_directory() as td:
            os.utime(td, (1000, 1000))
            found = R.verify(dict(verdict='pass', evidence=[dict(path=td, shows='the folder')]), started=time.time())
            self.assertEqual([f['ok'] for f in found], [True])
            self.assertIn('is a folder', found[0]['text'])

    def test_commit_claims(self):
        with temporary_directory() as td:
            data = dict(verdict='pass', commits=[dict(repo=td, branch='fix/x', head='abc1234def')])
            self.assertIn('does NOT exist', self.flags(data, git=fake_git({'cat-file': (1, '')}))[0])
            moved = self.flags(data, git=fake_git({'rev-parse': (0, 'ffff000011112222')}))
            self.assertIn('is NOT at abc1234def (it is at ffff000011)', moved[0])
            self.assertIn('does not exist', self.flags(data, git=fake_git({'rev-parse': (1, '')}))[0])
            dirty = self.flags(data, git=fake_git({'rev-parse': (0, 'abc1234def'), 'status': (0, ' M a.c\nA  b.c')}))
            self.assertIn('2 uncommitted change(s)', dirty[0])
            self.assertIn('repo not found', self.flags(dict(verdict='pass', commits=[dict(repo=td + '/nope', head='a')]))[0])

    def test_pass_contradicted_by_its_own_details(self):
        flags = self.flags(dict(verdict='pass', checks=[dict(name='live 4K', result='fail'), dict(name='odd')],
                                open=['offline install not tested']))
        self.assertTrue(any('these checks failed: live 4K' in f for f in flags))
        self.assertTrue(any('listed as open' in f for f in flags))
        self.assertTrue(any('no pass/fail/skip' in f for f in flags))
        self.assertIn('is not one of', self.flags(dict(verdict='great'))[0])

    def test_real_git_repository(self):
        with temporary_directory() as td:
            def run(*args):
                return R._git(td, *args)
            if run('init', '-q')[0] != 0:
                self.skipTest('git is not available')
            run('config', 'user.email', 'test@example.invalid')
            run('config', 'user.name', 'Test')
            run('checkout', '-q', '-b', 'fix/x')
            (Path(td) / 'a.txt').write_text('one')
            run('add', 'a.txt')
            if run('commit', '-q', '-m', 'one')[0] != 0:
                self.skipTest('git could not commit here')
            head = run('rev-parse', 'HEAD')[1]
            data = dict(verdict='pass', commits=[dict(repo=td, branch='fix/x', head=head)])
            self.assertEqual(self.flags(data), [])
            (Path(td) / 'a.txt').write_text('two')
            self.assertIn('1 uncommitted change(s)', self.flags(data)[0])
            data['commits'][0]['head'] = '0' * 40
            self.assertIn('does NOT exist', self.flags(data)[0])
            for folder, _dirs, names in os.walk(Path(td) / '.git'):     # git objects are read-only on Windows
                for name in names:
                    os.chmod(os.path.join(folder, name), 0o666)


class SummaryTests(unittest.TestCase):
    def test_compact_view(self):
        with temporary_directory() as td:
            shot = Path(td) / 'shot.png'
            png(shot, 1920, 1080)
            d = R.digest(report(dict(verdict='pass', summary='Borderless fix.',
                                     checks=[dict(name='unit', result='pass', detail='214 passed')],
                                     evidence=[dict(path=str(shot), width=3840, height=2160)],
                                     owner=['push the branch'])))
            lines = R.summary_lines(d)
            self.assertEqual(lines[0], 'VERDICT: pass  |  CLAIMS CHECKED: 1 ok, 1 FLAGGED')
            self.assertIn('SUMMARY: Borderless fix.', lines)
            self.assertTrue(any(l.startswith('  FLAG: image is 1920x1080') for l in lines))
            self.assertIn('  CHECKS: unit=pass (214 passed)', lines)
            self.assertIn('  YOUR TURN: push the branch', lines)

    def test_no_block(self):
        d = R.digest('Free text only.')
        self.assertEqual((d['verdict'], d['flags']), (None, 0))
        self.assertIn('VERDICT: none given', R.summary_lines(d)[0])


class WiringTests(unittest.TestCase):
    def test_finalize_checks_the_result_and_report_shows_it_first(self):
        with temporary_directory() as td, \
             patch.multiple(D, JOBS_ROOT=Path(td) / 'jobs', AGENTS_ROOT=Path(td) / 'agents', DELEGATE_ROOT=Path(td)):
            jd, meta = make_job(td, 'job-a')
            (Path(td) / 'agents' / 'example' / 'reports').mkdir(parents=True)
            shot = Path(td) / 'shot.png'
            png(shot, 1920, 1080)
            (jd / 'events.jsonl').write_text('', encoding='utf-8')
            (jd / 'result.md').write_text(report(dict(verdict='pass', summary='Done.',
                                                      evidence=[dict(path=str(shot), width=3840, height=2160)])),
                                          encoding='utf-8')
            D.finalize(jd, meta, dict(state='running', started=time.time() - 60), None, 0)
            status = json.loads((jd / 'status.json').read_text())
            self.assertEqual((status['state'], status['verdict'], status['flags']), ('done', 'pass', 1))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                D.report(jd)
            text = out.getvalue()
            self.assertIn('VERDICT: pass  |  CLAIMS CHECKED: 1 ok, 1 FLAGGED', text)
            self.assertLess(text.index('VERDICT:'), text.index('----- RESULT -----'))
            with patch.object(D, '_claude_records', return_value=[]), contextlib.redirect_stdout(io.StringIO()) as board:
                D.cmd_board(SimpleNamespace(hours=12))
            self.assertIn('done / pass, 1 FLAGGED', board.getvalue())
            summary = F.job_summary(jd, time.time())
            self.assertEqual((summary['verdict'], summary['flags']), ('pass', 1))

    def test_prompt_asks_for_the_block_on_new_jobs_only(self):
        meta = dict(mode='read', backend='kimi', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
        self.assertIn('```result', D.build_prompt(None, meta, 'Do the task.', []))
        self.assertNotIn('```result', D.build_prompt(None, dict(meta, resume_session='s1'), 'More.', []))


if __name__ == '__main__':
    unittest.main()
