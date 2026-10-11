#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: generate portable plugin agents from the installer template."""
import argparse
from pathlib import Path

from install_agents import SPECS, generate

ROOT = Path(__file__).resolve().parent


def main(argv=None, *, root=ROOT):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', help='check generated agents without writing')
    args = ap.parse_args(argv)
    agents = Path(root) / 'agents'
    failed = False
    expected = {spec['name'] + '.md' for spec in SPECS}
    unexpected = {p.name for p in agents.glob('*.md')} - expected
    for name in sorted(unexpected):
        print('unexpected agent:', name)
        failed = True
    for spec in SPECS:
        path = agents / (spec['name'] + '.md')
        text = generate(spec, plugin=True)
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != text:
                print('missing or stale:', path)
                failed = True
        else:
            agents.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')
            if path.read_text(encoding='utf-8') != text:
                raise OSError(f'write verification failed: {path}')
            print('verified:', path)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
