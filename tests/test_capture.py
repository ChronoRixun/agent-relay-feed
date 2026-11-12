# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Screenshots with a record of what they are of, and the result checks that read the record."""
import contextlib
import io
import json
import struct
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
        text = D.build_prompt(None, meta, 'Do the task.', [])
        self.assertIn('capture <absolute out.png> --process', text)
        self.assertIn('It refuses the WSLg desktop display :0', text)

    def test_under_wine_the_command_hands_over_to_the_x_capture(self):
        seen = {}

        def from_wine(argv, display):
            seen.update(argv=argv, display=display)
            return 0, 'CAPTURED: /mnt/e/work/shot.png  1280x720  from Game.exe "Star Racer" (...)'

        with patch.object(C, 'under_wine', return_value=True), patch.object(C, 'from_wine', from_wine), \
             patch.object(C, 'capture', side_effect=AssertionError('the Windows capture must not run under Wine')):
            text = self.run_cmd(out='Z:/mnt/e/work/shot.png', process='Game.exe', pid=4242, display=':73')
        self.assertIn('CAPTURED: /mnt/e/work/shot.png', text)
        self.assertEqual(seen, dict(argv=['Z:/mnt/e/work/shot.png', '--process', 'Game.exe', '--pid', '4242'], display=':73'))
        with patch.object(C, 'under_wine', return_value=True), \
             patch.object(C, 'from_wine', return_value=(1, 'NOT CAPTURED: DISPLAY is :0, the WSLg desktop')), \
             self.assertRaises(SystemExit) as e:
            self.run_cmd(out='Z:/mnt/e/work/shot.png', process='Game.exe')
        self.assertEqual(e.exception.code, 1)


# What the real tools printed on a private display (captured 2026-11-11 from Xwayland :87).
TREE = '''
xwininfo: Window id: 0x24a (the root window) (has no name)

  Root window id: 0x24a (the root window) (has no name)
  Parent window id: 0x0 (none)
     3 children:
     0x40000a "Star Racer": ("game.exe" "game.exe")  1280x720+0+0  +0+0
        1 child:
        0x40000b (has no name): ()  1280x720+0+0  +1+1
     0x20000a "xeyes": ("xeyes" "XEyes")  180x120+500+300  +500+300
     0x60000c "tiny": ("tiny" "Tiny")  4x4+-1+-1  +-1+-1
     0x700001 "Hidden "quoted" tool": ("tool" "Tool")  300x200+10+10  +10+10
'''
ROOT = '''
xwininfo: Window id: 0x24a (the root window) (has no name)

  Absolute upper-left X:  0
  Absolute upper-left Y:  0
  Width: 1280
  Height: 720
  Map State: IsViewable
'''


def x_tools(pids=None, hidden=(0x700001,), active=None):
    """A stand-in for the X tools on one display."""
    pids = {0x40000a: 4242} if pids is None else pids

    def run(argv, display):
        assert display == ':73', display
        if argv[:2] == ['xwininfo', '-root']:
            return TREE if '-tree' in argv else ROOT
        if argv[:2] == ['xprop', '-root']:
            return (f'_NET_ACTIVE_WINDOW(WINDOW): window id # {hex(active)}\n' if active
                    else '_NET_ACTIVE_WINDOW:  not found.\n')
        wid = int(argv[2], 16)
        if argv[0] == 'xwininfo':
            size = {0x40000a: (1280, 720), 0x20000a: (180, 120), 0x700001: (300, 200)}[wid]
            return (f'\n  Absolute upper-left X:  {500 if wid == 0x20000a else 0}\n  Absolute upper-left Y:  0\n'
                    f'  Width: {size[0]}\n  Height: {size[1]}\n  Border width: 0\n'
                    f'  Map State: {"IsUnMapped" if wid in hidden else "IsViewable"}\n  Override Redirect State: no\n')
        if argv[0] == 'xprop':
            pid = pids.get(wid)
            return ((f'_NET_WM_PID(CARDINAL) = {pid}\n' if pid else '_NET_WM_PID:  not found.\n')
                    + '_MOTIF_WM_HINTS:  not found.\n')
        raise AssertionError(argv)
    return run


def xwd(width, height, pixel, bpp=32, lsb=True, pad=0, name=b'Star Racer\0'):
    """An xwd dump whose pixel (x, y) has the colour pixel(x, y) = (r, g, b)."""
    n = bpp // 8
    stride = width * n + pad
    header = 100 + len(name)
    fields = [header, 7, 2, 24, width, height, 0, 0 if lsb else 1, 32, 0, 32, bpp, stride, 4,
              0xFF0000, 0xFF00, 0xFF, 8, 256, 2, width, height, 0, 0, 0]
    body = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b = pixel(x, y)
            value = ((r << 16 | g << 8 | b) & (1 << bpp) - 1).to_bytes(n, 'little' if lsb else 'big')
            body += value
        body += b'\xEE' * pad
    return struct.pack('>25I', *fields) + name + bytes(12 * 2) + bytes(body)


class XCaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.td = Path(tmp.name)

    def test_only_a_private_display_will_do(self):
        self.assertEqual(C.x_display(None, {'DISPLAY': ':73'}, wsl=True), (':73', None))
        self.assertEqual(C.x_display(':74', {'DISPLAY': ':0'}, wsl=True), (':74', None))        # --display wins
        for display in (':0', ':0.0', 'localhost:0'):
            got, why = C.x_display(None, {'DISPLAY': display}, wsl=True)
            self.assertIsNone(got)
            self.assertIn('WSLg desktop', why)
        got, why = C.x_display(None, {}, wsl=True)
        self.assertIsNone(got)
        self.assertIn('no X display is set', why)
        self.assertEqual(C.x_display(None, {'DISPLAY': ':0'}, wsl=False), (':0', None))        # a plain Linux desktop
        self.assertTrue(C.in_wsl({'WSL_DISTRO_NAME': 'Ubuntu'}))

    def test_windows_are_found_with_their_program(self):
        wins = C.x_list_windows(':73', run=x_tools(), process_of=lambda pid: ('Game.exe', 'Z:\\games\\copy\\Game.exe'))
        self.assertEqual([w['title'] for w in wins], ['Star Racer', 'xeyes'])       # unmapped and tiny ones are left out
        game, eyes = wins
        self.assertEqual((game['process'], game['pid'], game['client'], game['monitor'], game['display']),
                         ('Game.exe', 4242, [1280, 720], [1280, 720], ':73'))
        self.assertIsNone(game['borderless'])               # a Wine program: X cannot tell
        self.assertEqual((eyes['process'], eyes['pid'], eyes['borderless'], eyes['origin']), ('xeyes', 0, True, [500, 0]))
        self.assertEqual([w['title'] for w in C.pick(wins, process='game.exe')], ['Star Racer'])
        self.assertEqual([w['title'] for w in C.pick(wins, pid=4242)], ['Star Racer'])
        quoted = [w for w in C.parse_tree(TREE) if 'quoted' in w['title']]
        self.assertEqual(quoted[0]['title'], 'Hidden "quoted" tool')

    def test_the_window_in_front_is_known_when_the_display_says_so(self):
        wins = C.x_list_windows(':73', run=x_tools(active=0x20000a), process_of=lambda pid: ('Game.exe', ''))
        self.assertEqual({w['title']: w['foreground'] for w in wins}, {'Star Racer': False, 'xeyes': True})

    def test_program_name_from_proc(self):
        with patch('builtins.open', lambda name, mode='r', **kw: io.BytesIO(b'Z:\\games\\copy\\Game.exe\0-windowed\0')):
            self.assertEqual(C.process_of(4242), ('Game.exe', 'Z:\\games\\copy\\Game.exe'))
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertEqual(C.process_of(4242), ('', ''))

    def test_xwd_images_are_read_pixel_for_pixel(self):
        colour = lambda x, y: (10 + x, 100 + y, 200 - x)
        for kw in (dict(bpp=32, lsb=True), dict(bpp=32, lsb=False), dict(bpp=24, lsb=True, pad=3), dict(bpp=24, lsb=False, pad=1)):
            with self.subTest(**kw):
                w, h, bgra = C.parse_xwd(xwd(5, 3, colour, **kw))
                self.assertEqual((w, h, len(bgra)), (5, 3, 60))
                for x, y in ((0, 0), (4, 0), (2, 1), (4, 2)):
                    at = (y * 5 + x) * 4
                    self.assertEqual(tuple(bgra[at:at + 3]), colour(x, y)[::-1])
        for bad in (b'', xwd(5, 3, colour)[:150], xwd(5, 3, colour, bpp=16)):
            with self.assertRaises(OSError):
                C.parse_xwd(bad)

    def capture(self, **kw):
        dump = xwd(64, 36, kw.pop('colour', lambda x, y: (x * 3, y * 5, 90)))
        ran = []

        def run(argv, display):
            ran.append(argv)
            if argv[0] == 'xwd':
                Path(argv[argv.index('-out') + 1]).write_bytes(dump)
                return ''
            return x_tools()(argv, display)

        with patch.object(C, 'run_x', run), patch.object(C.os, 'name', 'posix'), \
             patch.object(C, 'process_of', lambda pid: ('Game.exe', 'Z:\\games\\copy\\Game.exe')):
            got = C.capture(str(self.td / 'shot.png'), display=':73', **kw)
        return got, ran

    def test_a_wine_window_is_captured_through_x_with_the_same_record(self):
        got, ran = self.capture(process='Game.exe')
        self.assertTrue(got['ok'], got)
        self.assertEqual((got['method'], got['display'], got['platform'], got['width'], got['height'], got['blank']),
                         ('xwd', ':73', 'x11', 64, 36, False))
        self.assertEqual((got['process'], got['title'], got['pid'], got['borderless']), ('Game.exe', 'Star Racer', 4242, None))
        grabs = [argv for argv in ran if argv[0] == 'xwd']
        self.assertEqual(len(grabs), 1)
        self.assertEqual(grabs[0][:5], ['xwd', '-silent', '-nobdrs', '-id', '0x40000a'])     # that one window, by its id
        self.assertEqual(R.image_size(got['path']), (64, 36))            # no crop, no scaling
        rec = C.load_record(got['path'])
        self.assertEqual(rec['sha256'], C.file_hash(got['path']))
        self.assertIn('X display :73, copied with xwd', C.describe(rec))
        self.assertNotIn('not in front', C.describe(rec))

    def test_a_flat_colour_capture_is_still_called_blank(self):
        got, _ = self.capture(process='Game.exe', colour=lambda x, y: (0, 0, 0))
        self.assertTrue(got['blank'])
        self.assertEqual(got['method'], 'xwd')

    def test_no_private_display_means_not_captured(self):
        with patch.object(C.os, 'name', 'posix'), patch.dict(C.os.environ, {'DISPLAY': ':0', 'WSL_DISTRO_NAME': 'Ubuntu'}), \
             patch.object(C, 'run_x', side_effect=AssertionError('nothing may touch display :0')):
            got = C.capture(str(self.td / 'never.png'), process='Game.exe')
        self.assertFalse(got['ok'])
        self.assertIn('WSLg desktop', got['reason'])
        self.assertFalse((self.td / 'never.png').exists())

    def test_result_checks_treat_it_like_a_windows_capture(self):
        got, _ = self.capture(process='Game.exe')
        path = got['path']
        entry = dict(path=path, shows='the menu', width=64, height=36, process='Game.exe', window='Star Racer')
        found = R.verify(dict(verdict='pass', evidence=[entry]), None)
        shot = [f for f in found if f.get('capture')]
        self.assertTrue(all(f['ok'] for f in found), found)
        self.assertIn('captured from Game.exe "Star Racer"', shot[0]['text'])
        wrong = R.verify(dict(verdict='pass', evidence=[dict(entry, process='Other.exe')]), None)
        self.assertTrue(any(not f['ok'] and 'NOT the claimed Other.exe' in f['text'] for f in wrong))
        unsure = R.verify(dict(verdict='pass', evidence=[dict(entry, borderless=True)]), None)
        self.assertTrue(all(f['ok'] for f in unsure))
        self.assertTrue(any('cannot be read from an X capture of a Wine program' in f['text'] for f in unsure))

    def test_a_wsl_path_is_cited_the_way_windows_opens_it(self):
        self.assertEqual(C.host_path('/mnt/e/work/shot.png'), 'E:/work/shot.png')
        self.assertEqual(C.host_path('/home/user/shot.png'), '/home/user/shot.png')
        self.assertEqual(C.host_path('E:/work/shot.png'), 'E:/work/shot.png')


if __name__ == '__main__':
    unittest.main()
