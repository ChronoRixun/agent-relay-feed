# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Models from Nous Portal (an OpenAI-compatible API, billed to Nous credits) run by the Kimi CLI.

The Kimi CLI gets a separate home folder (KIMI_CODE_HOME) whose config.toml names Nous Portal as
an OpenAI-compatible provider that reads its key from the NOUS_API_KEY environment variable. The
runner reads the key from `nous_api_key_file` and gives it to the CLI process only; it is never
written into the CLI's config, the job folder or a log. The user's own Kimi setup is not touched.

Spend is measured, not estimated from output: the Kimi CLI records the token usage of every model
call in its session files (usage.record events). Those counts are priced with the Portal's live
per-token prices, read from its public model list when the job starts.
"""
import json
import math
import os
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://inference-api.nousresearch.com/v1"
KEY_ENV = "NOUS_API_KEY"
LEDGER = "nous-ledger.jsonl"

# Kimi model alias -> Nous model id, reasoning efforts it accepts, and fallback prices in USD per
# token (prompt, cached prompt read, completion) used only if the live price list is unreachable.
MODELS = {
    "nous/deepseek-v4-pro": dict(id="deepseek/deepseek-v4-pro", efforts=["high", "xhigh"],
                                 prices=(0.94e-6, 0.08e-6, 1.89e-6)),
    "nous/deepseek-v4.1-flash": dict(id="deepseek/deepseek-v4.1-flash", efforts=["low", "high", "xhigh", "max"],
                                     prices=(0.106e-6, 0.004e-6, 0.513e-6)),
    "nous/mimo-v2.6-pro": dict(id="xiaomi/mimo-v2.6-pro", efforts=[],
                               prices=(0.435e-6, 0.0036e-6, 0.87e-6)),
    "nous/glm-5.3": dict(id="z-ai/glm-5.3", efforts=["low", "high", "max"],
                         prices=(0.91e-6, 0.169e-6, 2.86e-6)),
    "nous/glm-5.3-flash": dict(id="z-ai/glm-5.3-flash", efforts=["low", "high", "max"],
                               prices=(0.12e-6, 0.024e-6, 0.40e-6)),
    "nous/gemini-4-argon": dict(id="google/gemini-4-argon", efforts=["low", "medium", "high"],
                                prices=(2.0e-6, 0.1e-6, 10.0e-6)),
}


def key_path(config):
    return Path(os.path.expandvars(config["nous_api_key_file"])).expanduser()


def read_key(config):
    try:
        key = key_path(config).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise RuntimeError(f"Nous Portal key file missing or unreadable: put your API key in {key_path(config)} "
                           "(config key nous_api_key_file)") from None
    if not key or any(c.isspace() for c in key):
        raise RuntimeError(f"Nous Portal key file {key_path(config)} is empty or holds more than one word")
    return key


def config_toml():
    lines = ['# Written by agent-relay-feed (nous.py) for Nous Portal jobs. Rewritten before every job:',
             '# edits here are lost. The API key is read from the NOUS_API_KEY environment variable.',
             f'default_model = "{next(iter(MODELS))}"', 'default_permission_mode = "yolo"', '',
             '[providers.nous]', 'type = "openai"', f'base_url = "{BASE_URL}"', f'api_key_env = "{KEY_ENV}"', '']
    for alias, m in MODELS.items():
        lines += [f'[models."{alias}"]', 'provider = "nous"', f'model = "{m["id"]}"', 'max_context_size = 1048576',
                  'capabilities = [ ' + ", ".join(f'"{c}"' for c in (["thinking", "tool_use"] if m["efforts"] else ["tool_use"])) + ' ]']
        if m["efforts"]:
            lines += ['support_efforts = [ ' + ", ".join(f'"{e}"' for e in m["efforts"]) + ' ]',
                      f'default_effort = "{m["efforts"][min(1, len(m["efforts"]) - 1)]}"']
        lines.append('')
    return "\n".join(lines)


def prepare_home(folder):
    """Create a job's Kimi home and (re)write its config. Each job (and its follow-ups) has its own
    home, so every session recorded there is that job's and its spend can be counted exactly."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.toml").write_text(config_toml(), encoding="utf-8")
    return folder


def live_prices(model_ids, fetch=None, timeout=15):
    """{model id: (prompt, cache read, completion) USD per token} from the Portal's public model list."""
    try:
        if fetch is None:
            with urllib.request.urlopen(BASE_URL + "/models", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            data = fetch()
    except Exception:
        return {}
    out = {}
    for m in (data.get("data") if isinstance(data, dict) else data) or []:
        if isinstance(m, dict) and m.get("id") in model_ids:
            p = m.get("pricing") or {}
            try:
                prompt, completion = float(p["prompt"]), float(p["completion"])
                cached = float(p.get("input_cache_read", prompt))
            except (KeyError, TypeError, ValueError):
                continue
            if all(math.isfinite(v) and v >= 0 for v in (prompt, cached, completion)):
                out[m["id"]] = (prompt, cached, completion)
    return out


def prices_for(alias, fetch=None):
    m = MODELS[alias]
    return list(live_prices({m["id"]}, fetch).get(m["id"]) or m["prices"])


def wire_files(kimi_home):
    """Every wire.jsonl in a job's Kimi home: each session's main agent and any sub-agents."""
    return sorted(Path(kimi_home).glob("sessions/*/session_*/agents/*/wire.jsonl"))


def minus(tokens, baseline):
    return {k: max(0, tokens.get(k, 0) - (baseline or {}).get(k, 0)) for k in tokens}


class Meter:
    """Running token usage of a Kimi home, reading only what was added to its session files since
    the last call, so it is cheap enough to check every few seconds on a long job."""

    def __init__(self, kimi_home):
        self.home = Path(kimi_home)
        self.offsets = {}
        self.total = dict(input=0, cached=0, cache_write=0, output=0, calls=0)

    def read(self):
        for f in wire_files(self.home):
            start = self.offsets.get(f, 0)
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    chunk = fh.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1          # a half-written last line waits for the next read
            self.offsets[f] = start + end
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                _add(self.total, line)
        return dict(self.total)


def _add(total, line):
    if '"usage.record"' not in line:
        return
    try:
        u = json.loads(line).get("usage") or {}
    except ValueError:
        return
    total["input"] += int(u.get("inputOther") or 0)
    total["cached"] += int(u.get("inputCacheRead") or 0)
    total["cache_write"] += int(u.get("inputCacheCreation") or 0)
    total["output"] += int(u.get("output") or 0)
    total["calls"] += 1


def usage(files):
    """Summed token usage of every model call recorded in the session files."""
    total = dict(input=0, cached=0, cache_write=0, output=0, calls=0)
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    _add(total, line)
        except OSError:
            continue
    return total


def cost(tokens, prices):
    """USD for summed usage at (prompt, cache read, completion) per-token prices; cache writes at the prompt price."""
    prompt, cached, completion = prices
    return round((tokens["input"] + tokens["cache_write"]) * prompt + tokens["cached"] * cached
                 + tokens["output"] * completion, 6)


def append_ledger(root, jd, meta, status):
    entry = dict(job_id=jd.name, agent=meta["profile"], model=MODELS.get(meta["model"], {}).get("id", meta["model"]),
                 start=status.get("started", meta.get("created")), end=status.get("finished"),
                 total_cost_usd=status.get("total_cost_usd"), tokens=status.get("usage"), status=status.get("state"))
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def month_spend(root, now=None):
    now = time.time() if now is None else now
    month, total = time.strftime("%Y-%m", time.localtime(now)), 0.0
    try:
        with open(Path(root) / LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                when, usd = e.get("end") or e.get("start"), e.get("total_cost_usd")
                if when and type(usd) in (int, float) and time.strftime("%Y-%m", time.localtime(when)) == month:
                    total += usd
    except OSError:
        pass
    return round(total, 4)
