# Supervising many agents from one session

[Back to the README](../README.md)

This is a way of working, not a feature list: how one Claude Code session (the supervisor) runs several agents at
once without reading their logs, polling, or losing track after its own context is compacted. Every command it uses is
described in [CLI](CLI.md).

## 1. Start jobs directly

A supervisor can call the bridge itself instead of going through a relay agent. That saves a relay turn per job and
keeps the job id in the supervisor's own hands.

```sh
python delegate.py run gpt-sol-med --name "Port settings screen" --prompt-file brief.md --cwd ~/code/app --mode write --wait 0
```

- Give every job a `--name`: it is what the board, the pages and alerts show.
- Write a self-contained brief: goal, working directory, relevant paths and what is already known, what may be changed,
  what must not be touched, the checks to run and the report wanted. Keep the user's limits word for word.
- The bridge adds the shared rules itself (stages, messages, result block, slots, the capture command), so briefs can
  shrink to the task.
- When several workers share a codebase, give each its own files and tell them about each other.
- Pick the backend for the work and the bill: [BACKENDS](BACKENDS.md). Claude API runs need the owner's approval for each
  run, Nous runs have a per-job cap.

## 2. One background wait is the only wake-up

```sh
python delegate.py wait --any --wait 43200
```

Run it once in the background and do other work. It returns once for each outcome, each new question and each Claude
Code subagent cut off by a connection error, then prints the exact `NEXT:` command to start the next wait. Do not poll,
and do not start a second job to find out how the first is doing.

## 3. Status costs one command

```sh
python delegate.py board
```

One line per running agent (stage, time, last activity, open question, slots, unread messages), then queued jobs,
slots, spend and recent outcomes. The feed pages show the same thing to the owner ([FEED](FEED.md)).

## 4. Answer questions quickly

A worker that asks is blocked until it gets an answer, and its waiting time does not count against its limit. Answer
from the wake-up:

```sh
python delegate.py answer <job_id> "<reply>"
```

If the question is the owner's to decide, ask the owner and pass their words on. With `feed_pin` set the owner can
answer from the feed page or a phone ([FEED](FEED.md#answering-from-the-pages)).

## 5. Send new facts with `tell`

When something changes mid-run (a brief error, a new constraint, another job's finding):

```sh
python delegate.py tell <job_id> "<what changed>"
```

The worker reads it at its next stage change. Prefer this to cancelling and restarting.

## 6. Plans that survive compaction

Put order on disk, not in the supervisor's memory:

```sh
python delegate.py run gpt-terra-high --name "Run the gates" --prompt-file gates.md --after <job_id> --after-ok --wait 0
```

Queued jobs start on their own when what they wait for ends, and `board` lists them. Long commands go to `bg`, which
holds its slots and reports once ([CLI](CLI.md#long-commands-bg)).

## 7. Shared resources are leased

Builds, test copies, emulators and the like are configured as `resources` ([SLOTS](SLOTS.md)). Briefs name the slots a
task needs instead of describing locks in prose; the worker leases and releases them itself. A subagent that is not a
relay job leases with `--as <its work folder name>`.

## 8. Read the verdict, then the evidence

Each outcome starts with `VERDICT` and the claim checks. A `FLAG` line is a claim that did not hold, or a command that
usually needs the owner's say-so. An unflagged result still deserves a look at its evidence: the review page shows each
cited file beside its claim ([FEED](FEED.md#review-page)). Mark a job handled with `delegate.py reviewed <job_id>`.

## 9. Claude Code subagents

Native subagents appear on the board and the pages beside relay jobs, and `wait --any` waits for them too. Give their
briefs the same stage command and result block (`delegate.py result-format`). If one is cut off by a connection error,
`wait --any` reports it once with the step that resumes it: resume it once from the session that started it; if it dies
again, tell the owner. `delegate.py reviewed agent-<id>` clears it afterwards. If a subagent's report exists only in its
final message, `delegate.py save <path> --from agent-<id>` puts it on disk.

## What still needs judgement

The bridge reports and checks; it does not decide. Whether a worker's approach is right, whether its evidence shows the
right thing, and whether to push, publish or send anything stay with the supervisor and the owner.
