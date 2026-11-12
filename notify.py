# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Alerts to the owner's phone when a job needs an answer or ends.

Off unless the `notify` config key lists at least one place to send to:

    "notify": [{"kind": "ntfy", "url": "https://ntfy.sh/<your-private-topic>"},
               {"kind": "discord", "url": "https://discord.com/api/webhooks/..."},
               {"kind": "json", "url": "http://127.0.0.1:9000/hook"}],
    "notify_on": ["question", "failed", "flagged", "done"],
    "notify_detail": true,
    "notify_link": "http://192.168.1.20:8765"

`ntfy` posts to an ntfy topic, `discord` to a Discord webhook, `json` posts
{"event", "title", "body", "link"} to any URL. With `notify_detail` false only the kind of event
and the job's name are sent, never a question's text or a result. Sending is best effort: a
failure is returned as text and never raised, because an alert must not break a job.
"""
import json
import urllib.request

from delegate_config import CONFIG

EVENTS = ("question", "failed", "flagged", "done")
KINDS = ("ntfy", "discord", "json")
TIMEOUT_S = 6


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        resp.read(200)


def _header(text):
    """HTTP headers are Latin-1: anything else in a title is replaced, never dropped silently."""
    return " ".join(str(text).split()).encode("latin-1", "replace").decode("latin-1")[:200]


def send(event, title, body="", config=None, post=_post):
    """Send one alert to every configured place. Returns a list of error strings (empty = all sent)."""
    cfg = CONFIG if config is None else config
    targets = cfg.get("notify") or []
    # a digest is sent only when asked for, so it is not subject to notify_on
    if not targets or (event != "digest" and event not in (cfg.get("notify_on") or EVENTS)):
        return []
    if not cfg.get("notify_detail", True):
        body = ""
    body = str(body or "")[:900]
    link = (cfg.get("notify_link") or "").rstrip("/")
    errors = []
    for target in targets:
        kind, url = target.get("kind"), target.get("url")
        try:
            if kind == "ntfy":
                headers = {"Title": _header(title), "Tags": {"question": "question", "failed": "x", "flagged": "warning",
                                                             "done": "white_check_mark", "digest": "clipboard"}[event],
                           "Priority": "high" if event == "question" else "default"}
                if link:
                    headers["Click"] = link
                post(url, (body or title).encode("utf-8"), headers)
            elif kind == "discord":
                text = f"**{title}**" + (f"\n{body}" if body else "") + (f"\n{link}" if link else "")
                post(url, json.dumps({"content": text[:1900]}).encode("utf-8"),
                     {"Content-Type": "application/json", "User-Agent": "agent-relay-feed"})
            else:
                post(url, json.dumps(dict(event=event, title=title, body=body, link=link)).encode("utf-8"),
                     {"Content-Type": "application/json", "User-Agent": "agent-relay-feed"})
        except Exception as e:      # never let an alert break the caller
            errors.append(f"{kind}: {e!r}")
    return errors


def question(job_name, label, text, **kw):
    return send("question", f"{job_name} needs an answer", f"{label}: {text}", **kw)


def outcome(job_name, label, state, verdict=None, flags=0, summary="", elapsed="", **kw):
    """One alert for a finished job: failed, flagged (done, but claims did not check out) or done."""
    bad = state != "done" or verdict in ("fail", "blocked")
    event = "failed" if bad else "flagged" if flags else "done"
    what = verdict or state
    title = f"{job_name}: {what}" + (f", {flags} claim(s) flagged" if flags else "")
    body = f"{label}" + (f" after {elapsed}" if elapsed else "") + (f"\n{summary}" if summary else "")
    return send(event, title, body, **kw)
