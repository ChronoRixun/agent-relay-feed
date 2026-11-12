# Internals

[Back to the README](../README.md)

How the bridge works under the commands. None of this is needed to use it.

## Where jobs are kept

`~/.claude/delegate/agents/<profile>/` contains `progress/<job_id>.md`, `activity/<job_id>.log`,
`reports/<job_id>.md` and `latest-report.md`. `~/.claude/delegate/jobs/<job_id>/` contains the prompt, metadata,
command, events, stderr, result and status, plus `journal.jsonl` (stages, questions, answers and the final state),
`result.json` (the result block and its checked claims) and, for `bg` jobs, `output.log`. The high-effort Sol relay uses
the `gpt-sol-med` folder because that is its delegate profile. `DELEGATE_JOBS_DIR` and `DELEGATE_AGENTS_DIR` can
override the two storage roots.

Next to them in `~/.claude/delegate/`: `config.json` (yours, optional), `leases.json` (slots, under a lock file),
`baselines/` (saved shared state), `reviewed.json` (the reviewed marks), `inbox/` (briefs written by relay agents),
`agent-overrides.json` (descriptions kept by the installer) and the spend ledgers `claude-api-ledger.jsonl` and
`nous-ledger.jsonl`. The source filenames and this storage layout stay unchanged for compatibility.

## The detached runner

Every job runs in a runner process of its own, so a job outlives the call that started it, and the caller's session
ending does not stop it.

On Windows the runner must not stay inside the caller's Windows job object, or tearing down the caller's process tree
would kill it; Claude Code's tool processes run in a job that refuses breakaway. So `run` first tries breakaway, then
uses WMI (`Win32_Process.Create`) to start the small `detach.py`, which WMI creates hidden in your desktop session but
outside the job. It connects back to `run` over an authenticated named pipe, receives the runner's command and the
caller's full environment in memory (nothing is written to disk), and starts the runner as its own child, so the runner
sees exactly the environment it would have had. `detach_via_wmi: false` turns that off. Only if both fail does the
runner start inside the job, and the report warns about it. `detach.txt` in the job folder records which way was used.
On macOS and Linux the runner starts a new session.

## How stages and commands are read

The worker-side commands (`phase`, `ask`, `check`, `lease`, `release`) need no write access. The command itself does
little: the runner reads the stage name, the question or the slot out of the worker's own command stream (the events
its CLI writes), so it works in a read-only sandbox and even when the command fails. For Claude Code subagents the feed
reads the same commands out of their transcripts. A sandboxed Codex worker cannot write the lease file, so its runner
takes and returns slots for it and leaves the reason for a refusal where the worker's own `lease` command can read it.

## Where alerts and releases come from

A relay job's runner sends its alerts, releases its slots, puts back shared state if it was the last holder, and
releases queued jobs when it ends. Claude Code subagents have no runner, so those duties fall to whatever is running:
`wait --any` and the feed server's watcher thread release queued jobs and restore shared state, and the feed server
sends subagent alerts.
