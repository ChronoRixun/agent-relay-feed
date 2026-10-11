# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Invented feed fixtures. Only timestamps change between snapshots."""
import time


_PROFILES = (
    ('gpt-sol-med', 'GPT-6.1-Sol (high)', 'codex', 'write'),
    ('gpt-astra-med', 'GPT-6-Astra (medium)', 'codex', 'write'),
    ('gpt-astra-xhigh', 'GPT-6-Astra (extra high)', 'codex', 'read'),
    ('kimi-k28', 'Kimi K2.8 Preview', 'kimi', 'full'),
)
_STEPS = (
    ('read', 'Read', 'auth/session.py', 'cat auth/session.py'),
    ('search', 'Search', 'token refresh callers', 'rg refresh_token src tests'),
    ('test', 'Run tests', 'tests/test_auth.py', 'pytest tests/test_auth.py -q'),
    ('git', 'git commit', 'Extract session validation', 'git commit -m "Extract session validation"'),
    ('build', 'Build', 'shop-api', 'npm run build'),
    ('code', 'Python snippet', 'check migration row counts', 'python -c "print(1200 == 1200)"'),
)
_RUNNING = (
    ('refactor-auth-module', 0, 23 * 60, 12, 34,
     'Session validation now lives in one helper. Auth tests pass; committing the refactor before checking the build.'),
    ('write-api-tests', 1, 17 * 60, 7, 27,
     'Added coverage for pagination, missing credentials and invalid payloads. Running the API suite now.'),
    ('review-pr-482', 2, 34 * 60, 11 * 60, 20,
     'Found a possible race in refresh-token rotation. Tracing concurrent callers before writing the review.'),
    ('migrate-db-schema', 3, 41 * 60, 20, 42,
     'Backfill and rollback both passed on sample data. Comparing row counts before the final migration check.'),
)
# Native Claude Code subagents: (icon, verb, detail, raw) steps as the transcript reader humanizes them.
_CLAUDE_STEPS = (
    ('read', 'Read', 'search/index.py', '~/code/shop-api/search/index.py'),
    ('search', 'Search', '“def rank_products”', 'Grep def rank_products'),
    ('agent', 'Start agent', 'researcher: Benchmark index options', 'Agent'),
    ('write', 'Edit', 'search/ranking.py', '~/code/shop-api/search/ranking.py'),
    ('test', 'Run tests', 'test_search.py', 'python -m pytest tests/test_search.py -q'),
    ('mcp', 'tracker: create issue', '', 'mcp__tracker__create_issue'),
)
_CLAUDE = (
    # name, agent type, model, depth, parent, age, idle, steps, state, finish age, note or result
    ('Plan the search rollout', 'general-purpose', 'Opus 5.5', 1, None, 29 * 60, 40, 18, 'running', None,
     'Ranking change is in; waiting for the benchmark before choosing the index.'),
    ('Benchmark index options', 'researcher', 'Sonnet 5.5', 2, 'Plan the search rollout', 21 * 60, 0, 9, 'done', 6 * 60,
     'Trigram index is 3x faster on sample queries; recommended with a size cap.'),
)
_FINISHED = (
    ('fix-pagination', 'done', 'Fixed cursor pagination; all 28 endpoint tests pass.'),
    ('add-health-endpoint', 'done', 'Added health and readiness endpoints with smoke tests.'),
    ('document-api-errors', 'done', 'Documented error codes and example responses.'),
    ('update-dependencies', 'done', 'Updated dependencies; build and test suite pass.'),
    ('cache-product-lookups', 'done', 'Added bounded lookup cache and expiry tests.'),
    ('review-checkout-flow', 'done', 'Reviewed checkout flow; two edge cases reported.'),
    ('simplify-config-loader', 'done', 'Simplified config parsing and preserved defaults.'),
    ('test-payment-webhooks', 'failed', 'Webhook integration tests failed: sample service unavailable.'),
    ('prototype-search-index', 'cancelled', 'Cancelled after comparing index options; no changes saved.'),
)


def demo_snapshot(hours=12, now=None):
    """Return the live snapshot shape using generic, reproducible example data."""
    now = time.time() if now is None else now

    def stamp(age):
        return time.strftime('%H:%M:%S', time.localtime(now - age))

    def job(name, profile_index, age, idle, steps, state='running', finish_age=None, result=None):
        profile, label, backend, mode = _PROFILES[profile_index]
        events = []
        for i in range(steps):
            # Spread commands evenly through the job; the last matches its idle age.
            event_age = age - (age - idle) * (i + 1) / steps
            icon, verb, detail, raw = _STEPS[i % len(_STEPS)]
            events.append(dict(t=stamp(event_age), kind='command', rc=0,
                               icon=icon, verb=verb, detail=detail, raw=raw, more=0))
        finished = None if finish_age is None else now - finish_age
        return dict(
            id='demo-' + name, name=name, profile=profile, label=label, backend=backend,
            mode=mode, cwd='~/code/shop-api', state=state, started=now - age,
            finished=finished, elapsed=age - (finish_age or 0),
            idle=idle if state == 'running' else None,
            stalled=state == 'running' and idle > 480, loop=False, retries=0,
            now_msg=None, now_msg_t=None, current=events[-1],
            counts=dict(commands=steps, failed=0, files=steps // 4, messages=0, errors=0),
            events=events, progress=[], result=result, errors=[],
            source='relay', agent_type=None, depth=None, parent=None, tokens=None,
        )

    def claude(name, kind, model, depth, parent, age, idle, steps, state, finish_age, text):
        events = []
        for i in range(steps):
            event_age = age - (age - max(idle, finish_age or 0)) * (i + 1) / steps
            icon, verb, detail, raw = _CLAUDE_STEPS[i % len(_CLAUDE_STEPS)]
            events.append(dict(t=stamp(event_age), kind='command', rc=0,
                               icon=icon, verb=verb, detail=detail, raw=raw, more=0))
        running = state == 'running'
        events.insert(-1, dict(t=stamp((finish_age or idle) + 30), kind='message', text=text))
        return dict(
            id='demo-claude-' + kind, name=name, profile=kind, label=model, backend='claude',
            mode=None, cwd='~/code/shop-api', state=state, started=now - age,
            finished=None if running else now - finish_age, elapsed=age - (finish_age or 0),
            idle=idle if running else None, stalled=False, loop=False, retries=0,
            now_msg=text, now_msg_t=stamp((finish_age or idle) + 30), current=events[-1],
            counts=dict(commands=steps, failed=0, files=steps // 6, messages=1, errors=0),
            events=events, progress=[], result=None if running else text, errors=[],
            source='claude', agent_type=kind, depth=depth, parent=parent,
            tokens=dict(output=steps * 1400, context=60000 + steps * 2500),
        )

    jobs = []
    for name, profile, age, idle, steps, note in _RUNNING:
        entry = job(name, profile, age, idle, steps)
        entry['now_msg'] = note
        entry['now_msg_t'] = stamp(idle + 45)
        entry['counts']['messages'] = 4
        entry['progress'] = [f'{stamp(idle + 45)} {note}']
        entry['events'].insert(-1, dict(t=stamp(idle + 45), kind='message', text=note))
        entry['retries'] = 3 if profile == 1 else 0
        jobs.append(entry)
    # The running Claude subagent sits second so it is visible beside the relay cards.
    for spec in _CLAUDE:
        if spec[8] == 'running':
            jobs.insert(1, claude(*spec))
        elif spec[9] < hours * 3600:
            jobs.append(claude(*spec))
    for i, (name, state, result) in enumerate(_FINISHED):
        finish_age = (i + 1) * 14 * 60
        if finish_age >= hours * 3600:
            continue
        entry = job(name, i % len(_PROFILES), finish_age + (12 + i * 3) * 60,
                    finish_age, 12 + i, state, finish_age, result)
        if state == 'failed':
            entry['counts']['failed'] = 1
            entry['current']['rc'] = 1
            entry['errors'] = [result]
        jobs.append(entry)
    return dict(now=now, jobs=jobs, process_panel=True, processes=[
        dict(pid='4201', name='node.exe', path='~/code/shop-api/tools/node.exe',
             who='dev server (you)', owner_kind='owner'),
        dict(pid='4202', name='pytest', path='~/code/shop-api/.venv/bin/pytest',
             who='write-api-tests', owner_kind='agent'),
    ])
