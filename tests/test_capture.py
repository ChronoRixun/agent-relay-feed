# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Screenshots with a record of what they are of, and the result checks that read the record."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import capture as C
import delegate as D
import feed as F
import results as R


def window(process='Game.exe', title='Star Racer II', client=(3840, 2160), borderless=True, foreground=True,
           monitor=(3840, 2160), hwnd=1, pid=10):
    return dict(hwnd=hwnd, title=title, pid=pid, exe='D:/Games/' + process, process=process, client=list(client),
                origin=[0, 0], borderless=borderless, monitor=list(monitor), monitor_origin=[0, 0], foreground=foreground)


def pixels(w, h, blank=False):
    if blank:
        return bytes(w * h * 4)
    row = b''.join(bytes((x % 251, (x * 7) % 251, 30, 255)) for x in range(w))
    return b''.join(row[y % 7:] + row[:y % 7] for y in range(h))


def grabber(size=None, blank=False, calls=None):
    def grab(win, method):
        if calls is not None:
            calls.append(method)
        w, h = size or win['client']
        return w, h, pixels(w, h, blank)
    return grab


class CaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.td = Path(tmp.name)

    def shot(self, name='shot.png', windows=None, size=(64, 36), **kw):
        windows = [window(client=size, monitor=size)] if windows is None else windows
        kw.setdefault('process', 'game')
        return C.capture(str(self.td / 'out' / name), windows=windows, grabber=kw.pop('grabber', grabber()), **kw)

    def test_capture_writes_a_png_and_a_record(self):
        got = self.shot()
        self.assertTrue(got['ok'])
        self.assertEqual(R.image_size(got['path']), (64, 36))
        rec = C.load_record(got['path'])
        self.assertEqual((rec['process'], rec['title'], rec['width'], rec['height']), ('Game.exe', 'Star Racer II', 64, 36))
        self.assertEqual((rec['borderless'], rec['fills_monitor'], rec['foreground'], rec['method'], rec['blank'], rec['scaled']),
                         (True, True, True, 'screen', False, False))
        self.assertEqual(rec['sha256'], C.file_hash(got['path']))
        self.assertIn('Game.exe "Star Racer II" (64x36, borderless, fills the 64x36 monitor)', C.describe(rec))

    def test_picking_the_window(self):
        browser = window('firefox.exe', 'Star Racer II wiki - Mozilla Firefox', (2560, 1392), False, True, hwnd=2)
        game = window(foreground=False, hwnd=3)
        small = window(client=(640, 480), foreground=False, hwnd=4)
        self.assertEqual([w['hwnd'] for w in C.pick([browser, small, game], process='Game.exe')], [3, 4])
        self.assertEqual([w['hwnd'] for w in C.pick([browser, small, game], title='star racer')], [2, 3, 4])
        self.assertEqual([w['hwnd'] for w in C.pick([browser, game], foreground=True)], [2])
        missing = self.shot(windows=[browser], process='Game.exe')
        self.assertEqual((missing['ok'], len(missing['windows'])), (False, 1))
        self.assertFalse(self.shot(windows=[browser], process=None)['ok'])     # nothing named: refuse to guess
        calls = []
        got = self.shot(windows=[browser, game], grabber=grabber((64, 36), calls=calls))
        self.assertEqual((calls, got['method'], got['other_matches']), (['window'], 'window', 0))   # covered: not off the screen

    def test_blank_capture_tries_the_other_way_then_says_so(self):
        calls = []

        def grab(win, method):
            calls.append(method)
            return 8, 8, pixels(8, 8, blank=method == 'screen')

        got = self.shot(grabber=grab)
        self.assertEqual((calls, got['method'], got['blank']), (['screen', 'window'], 'window', False))
        got = self.shot(grabber=grabber((8, 8), blank=True))
        self.assertTrue(got['blank'])

    def test_program_that_ignores_display_scaling(self):
        win = window(client=(800, 500), borderless=False, foreground=False, monitor=(2560, 1440))
        got = self.shot(windows=[win], grabber=grabber((640, 400)))
        self.assertEqual((got['width'], got['height'], got['displayed'], got['scaled']), (640, 400, [800, 500], True))
        self.assertIn('stretched to 800x500', C.describe(got))


class CheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.td = Path(tmp.name)

    def capture(self, win, name='shot.png', blank=False):
        return C.capture(str(self.td / name), process=win['process'], windows=[win], grabber=grabber(blank=blank))['path']

    def flags(self, entry):
        found = R.verify(dict(verdict='pass', evidence=[entry]))
        return [f['text'] for f in found if not f['ok']], found

    def test_the_right_window_is_confirmed(self):
        path = self.capture(window(client=(64, 36), monitor=(64, 36)))
        flags, found = self.flags(dict(path=path, shows='4K borderless main menu', width=64, height=36, process='game.exe'))
        self.assertEqual(flags, [])
        shot = [f for f in found if f.get('capture')]
        self.assertIn('captured from Game.exe "Star Racer II"', shot[0]['text'])
        lines = R.summary_lines(dict(data=dict(verdict='pass'), findings=found, flags=0))
        self.assertTrue(any(l.startswith('  SCREENSHOT: captured from Game.exe') for l in lines))

    def test_a_browser_passed_off_as_the_game_is_flagged(self):
        path = self.capture(window('firefox.exe', 'Star Racer release notes - Mozilla Firefox', (64, 36), borderless=False))
        flags, _ = self.flags(dict(path=path, shows='4K borderless gameplay', width=64, height=36, process='Game.exe'))
        self.assertEqual(len(flags), 2)
        self.assertIn('it is of firefox.exe, NOT the claimed Game.exe', flags[0])
        self.assertIn('NOT borderless', flags[1])

    def test_borderless_word_in_the_claim_is_checked_without_a_process(self):
        path = self.capture(window(client=(64, 36), borderless=False))
        self.assertIn('NOT borderless', self.flags(dict(path=path, shows='Borderless at 4K'))[0][0])
        self.assertEqual(self.flags(dict(path=path, shows='windowed, not borderless'))[0], [])
        self.assertIn('does not contain the claimed', self.flags(dict(path=path, window='Options menu'))[0][0])
        self.assertIn('did not fill its monitor', self.flags(dict(path=path, fullscreen=True))[0][0])

    def test_cropped_or_blank_captures_are_flagged(self):
        path = self.capture(window(client=(64, 36)))
        from tests.test_results import png
        png(path, 32, 18)                                   # cropped after the capture
        flags, _ = self.flags(dict(path=path, shows='menu', process='Game.exe'))
        self.assertEqual(len(flags), 1)
        self.assertIn('changed after it was captured', flags[0])
        blank = self.capture(window(client=(8, 8)), 'blank.png', blank=True)
        self.assertIn('one flat colour', self.flags(dict(path=blank, shows='menu'))[0][0])

    def test_no_record(self):
        from tests.test_results import png
        plain = self.td / 'plain.png'
        png(plain, 10, 10)
        self.assertEqual(self.flags(dict(path=str(plain), shows='a chart', width=10, height=10))[0], [])
        flags, _ = self.flags(dict(path=str(plain), shows='menu', process='Game.exe'))
        self.assertIn('no capture record, so the claimed process cannot be checked', flags[0])
        (self.td / 'plain.png.capture.json').write_text(json.dumps(dict(tool='something else', process='Game.exe')))
        self.assertIsNone(C.load_record(str(plain)))        # only the helper's own records count

    def test_review_page_shows_what_was_captured(self):
        path = self.capture(window(client=(64, 36)))
        items = F._evidence_items('job-1', [dict(path=path, shows='menu', process='Game.exe')], None)
        self.assertIn('Game.exe', items[0]['captured'])
        self.assertEqual(items[0]['claimed_process'], 'Game.exe')


class CommandTests(unittest.TestCase):
    def run_cmd(self, **kw):
        args = dict(out=None, process=None, window=None, pid=None, foreground=False, method='auto', list=False)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            D.cmd_capture(SimpleNamespace(**args))
        return out.getvalue()

    def test_command_output(self):
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / 'shot.png')
            rec = dict(ok=True, path=target, width=3840, height=2160, process='Game.exe', title='Star Racer II',
                       borderless=True, foreground=True, monitor=[3840, 2160], fills_monitor=True, blank=False, other_matches=0)
            with patch.object(C, 'capture', return_value=rec):
                text = self.run_cmd(out=target, process='Game.exe')
            self.assertIn('CAPTURED:', text)
            self.assertIn('"process": "Game.exe"', text)
            with patch.object(C, 'capture', return_value=dict(ok=False, reason='no visible window matches', windows=[window('firefox.exe', 'Wiki')])), \
                 self.assertRaises(SystemExit) as e:
                self.run_cmd(out=target, process='Game.exe')
            self.assertEqual(e.exception.code, 1)
            with self.assertRaises(SystemExit):
                self.run_cmd(out=str(Path(td) / 'shot.jpg'), process='x')

    def test_new_jobs_are_told_about_the_capture_command(self):
        meta = dict(mode='read', backend='kimi', cwd='/work', add_dirs=[], progress_file='/p.md', job_id='job-7')
        with patch.object(D.os, 'name', 'nt'):
            self.assertIn('capture <absolute out.png> --process', D.build_prompt(None, meta, 'Do the task.', []))


if __name__ == '__main__':
    unittest.main()
