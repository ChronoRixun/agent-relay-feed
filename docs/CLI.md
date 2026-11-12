# Command reference

[Back to the README](../README.md)

Run `delegate.py` from the checkout, or give its absolute path from anywhere else. Use `python3` if that is your
Python 3 command.

- [Starting and following jobs](#starting-and-following-jobs)
- [Stages and the board](#stages-and-the-board)
- [One wake-up per outcome](#one-wake-up-per-outcome)
- [Queues and automatic resume](#queues-and-automatic-resume)
- [Talking to a running job](#talking-to-a-running-job)
- [Time limits](#time-limits)
- [Checked results and guard flags](#checked-results-and-guard-flags)
- [Long commands: `bg`](#long-commands-bg)
- [Screenshots with a record: `capture`](#screenshots-with-a-record-capture)
- [Writing a file: `save`](#writing-a-file-save)
- [Spend and the digest](#spend-and-the-digest)
- [Watching in a terminal](#watching-in-a-terminal)

Slots (`lease`, `release`, `slots`) are in [SLOTS](SLOTS.md); the feed server is in [FEED](FEED.md).

## Starting and following jobs

```sh
python delegate.py profiles
python delegate.py run gpt-astra-high --prompt-file brief.md --cwd /path/to/project --mode read
python delegate.py run gpt-sol-med --effort high --prompt-file brief.md --cwd /path/to/project --mode write
python delegate.py wait <job_id>
python delegate.py status <job_id>
python delegate.py run gpt-sol-med --resume <job_id> --prompt-file followup.md
python delegate.py list
python delegate.py cancel <job_id>
```

- `--mode` is `read`, `write` (the default) or `full`; what each means depends on the backend
  ([BACKENDS](BACKENDS.md#access-modes)).
- On Windows, pass your project directory as `--cwd`; forward slashes are accepted.
- `run` and `wait` return after at most 540 seconds by default (`--wait SECONDS`). A `STATUS: running` job continues
  in the background: keep calling `wait` rather than starting a duplicate. When calling through Claude's Bash tool,
  give the tool a timeout of 600000 milliseconds. `run ... --wait 0` returns at once.
- Jobs have a default time limit of 180 minutes (`--max-minutes N`).
- `--name "Trace the crash"` gives a job a readable title; it is what the board, the feed and alerts show.
- `--preamble FILE` prepends a brief or role file and may be repeated. `--add-dir DIR` adds permitted write
  directories and may be repeated. `--prompt-file -` reads stdin. `--effort VALUE` overrides the profile default.
- `--resume <job_id>` needs a previous job with a session ID and the same backend; it inherits the previous working
  directory unless `--cwd` overrides it.
- Claude API jobs also take `--approved "<who / when / why>"` and `--max-usd N`; Nous jobs take `--max-usd N`
  ([BACKENDS](BACKENDS.md)).

## Stages and the board

```sh
python delegate.py board               # one line per running agent: stage, time, last activity
```

Every job's prompt asks the worker to run `delegate.py phase "<stage name>"` whenever it starts a new stage. Stages are
kept in the job's `journal.jsonl` beside `started` and the final state, and appear on the feed and in `board`. Claude
Code subagents show a stage too if their brief tells them to run the same command. (How the stage reaches the bridge
from a read-only sandbox is in [INTERNALS](INTERNALS.md#how-stages-and-commands-are-read).)

`board` is the cheap answer to "how is it going?": running relay jobs and Claude Code subagents on one line each, then
queued jobs, slots, spend and recent outcomes. It also lists subagents cut off by a connection error, with the step
that resumes them ([FEED](FEED.md#claude-code-subagents)).

## One wake-up per outcome

```sh
python delegate.py wait --any --wait 43200
```

`wait --any` returns when any job ends (done, failed, timeout, cancelled or lost), when a worker asks a question, or
when a Claude Code subagent is cut off by a connection error, and prints what happened. Each outcome is reported once:
a job whose result was already shown by `run`, `wait` or `status` is not reported again, and jobs that ended between
two calls are picked up by the next one. It also keeps waiting while Claude Code subagents are running, and returns
`NOTHING RUNNING` when nothing is left. A supervising session can therefore start jobs with `run ... --wait 0` and keep
a single background `wait --any` as its only wake-up.

While an agent is waiting on one job in the foreground with `wait <job_id>`, that outcome is its to read: `wait --any`
does not also report it, unless that agent stops coming back for 90 seconds.

## Queues and automatic resume

```sh
python delegate.py run gpt-sol-med --prompt-file next.md --cwd /work/repo --after <job_id> --wait 0
python delegate.py run gpt-terra-high --prompt-file gates.md --after <job_id> --after agent-<id> --after-ok --wait 0
```

`--after` creates the job but holds it as `queued` until every job it names has ended; it then starts on its own. A
dependency is a relay job id, or `agent-<id>` (as shown on the feed) for a Claude Code subagent. With `--after-ok` the
job starts only if they all ended successfully (done, and not a `fail` or `blocked` verdict); otherwise it is cancelled
with the reason, and an alert is sent. Queued jobs are stored on disk like any other job, so the plan survives the
supervising session losing its memory: `board` lists them with what they wait for, and `cancel <job_id>` removes one.
They are released when a relay job ends and by a running `wait --any`, and no more than `queue_max_running` relay jobs
(default 3) run at once. A job queued behind a Claude Code subagent is released only while a `wait --any` is running,
since nothing else is watching.

A relay job that fails with a connection error (a dropped stream, a certificate or network error, a 502/503/504) is
resumed once automatically, as a new job on the same session with the same name; the failed job's report says
`AUTO-RESUMED AS` and jobs queued behind it wait for the resumed one. `auto_resume` sets how many times (default 1, `0`
turns it off). Claude API jobs are never resumed this way, because each of their runs needs its own approval, and
Claude Code subagents cannot be resumed from outside.

## Talking to a running job

```sh
python delegate.py tell <job_id> "The staging database is read-only today"
python delegate.py answer <job_id> "Yes, keep the old format behind a flag"
```

`tell` queues a message for a running job. The worker reads it the next time it names a stage (the `phase` command
prints waiting messages), so delivery is at its next stage change, not instant. `board` shows how many messages are
still unread.

A worker that reaches a decision it should not make alone runs `delegate.py ask --job <id> "<question>"`, which waits
for the reply. The question appears on the feed and in `board`, and `wait --any` returns with it (once per question)
and the `answer` command to use. `answer` lets the worker continue with your reply; the job is not ended and not
restarted. One `ask` waits `ask_wait_seconds` (default 240) and then tells the worker to run it again, so the wait
survives the agent's own command time limit. Time spent waiting for an answer does not count against `--max-minutes`
(Codex and Kimi jobs). Workers can also run `delegate.py check --job <id>` to read new messages without naming a stage.

Workers in a read-only sandbox cannot record which messages they have seen, so they are shown every message each time;
elsewhere only new ones are shown. All of this applies to relay jobs: Claude Code subagents are messaged with Claude
Code's own tools. With `feed_pin` set, the same answer and message boxes are on the feed pages
([FEED](FEED.md#answering-from-the-pages)).

## Time limits

**Wrap-up warning.** `checkpoint_minutes` (default 10) before a job's `--max-minutes` limit, its runner tells the worker
to stop starting new work, save or commit what it has, release its slots and write its report. Workers read messages
when they change stage, so if the warning is still unread at the limit the job gets one more window of the same length
before it is stopped. `0` turns the warning off. (Claude API jobs keep the plain limit.)

Giving way to the machine's owner is described in [SLOTS](SLOTS.md#giving-way-to-the-owner).

## Checked results and guard flags

Every job is asked to end its final report with a fenced `result` block: a verdict (`pass`, `fail`, `partial`,
`blocked`), a one-line summary, commits (repo, branch, head), uncommitted files, checks with their results, evidence
files, open items, cleanup, and items for the owner. `python delegate.py result-format` prints the format, for briefs
of agents that are not relay jobs. When the job ends the runner reads that block and checks what a machine can check:
each commit exists and its branch points at it, the repo has no uncommitted changes, each file and evidence file
exists, is not empty and is not older than the job, an image is the pixel size it is said to be, and a `pass` verdict
is not contradicted by a failed check or an open item.

`wait`, `wait --any` and `status` print this first, before the full report:

```
VERDICT: pass  |  CLAIMS CHECKED: 6 ok, 1 FLAGGED
SUMMARY: Borderless 4K fix on fix/borderless, gates green.
  FLAG: image is 1920x1080, NOT the claimed 3840x2160: /work/shots/menu.png
  COMMIT: 3f2a9c1d07 on fix/borderless in /work/repo
  CHECKS: unit=pass (214 passed); live 4K=pass
  YOUR TURN: push the branch
```

`board` and the feed show the verdict and the number of flagged claims beside each finished job. The block and
findings are kept in the job folder as `result.json`. These checks catch missing, stale and mismatched things; they
cannot tell whether a screenshot shows the right window or whether a test was honest, so an unflagged result still
deserves a look at its evidence. A report with no block is shown as before, marked as having no verdict.
`python delegate.py reviewed <job_id>` marks a finished job as dealt with (`--undo` takes it back); see the review page
in [FEED](FEED.md#review-page).

**Guard flags.** When a job ends, its activity log is scanned for things that usually need the owner's say-so:
software installs (`pip install`, `npm install -g`, `winget install`, ...), commands that publish (`git push`,
`gh release|pr|issue create`, ...), and file changes in a read-only job. Each becomes a flag beside the verdict, with an
example command. It reports after the fact and cannot stop anything, and a brief may have allowed it, so a flag means
"check", not "wrong". `guard_patterns` adds rules; `guard_enabled: false` turns it off.

**Writes outside the job's folders (Kimi and Nous jobs).** These workers have no sandbox, so where they may write is
only an instruction. When such a job ends, its tool calls are read back and any file written or deleted outside the
job's working directory, its `--add-dir` folders, its own delegate job and agent folders, the worker program's own
folder and the system temp folder is flagged, with up to three paths and what wrote them:
`GUARD: wrote or deleted outside the job's folders (2 paths), e.g. d:/other/main.c (by the Edit tool); ...`. File tools
give exact paths. Shell commands are read by rule of thumb: absolute paths that follow a deleting, writing or moving
command, the destination of a copy, a redirect, `git -C <repo>` with a subcommand that changes the checkout, and
`delegate.py save` or `capture`. Reading elsewhere is never flagged. Relative paths, and paths a script builds while it
runs, are not seen, so no flag is not proof that nothing was touched. To allow a folder for one job, start it with
`--add-dir <folder>`; to allow one for every job (a shared save folder, say), list it in `guard_allow_dirs`.

## Long commands: `bg`

An agent that must run something for a long time (a boot loop, a bulk delete, a soak test) should not keep a
background shell and wake up to check it. `bg` runs the command as a job of its own:

```sh
python delegate.py bg --name "200 boot loops" --lease test-copy --lease build -- python boot_loop.py --n 200 --copy {test-copy}
python delegate.py bg --shell powershell -- "Get-ChildItem big | Remove-Item -Recurse"    # pipes need a shell
```

The command starts detached like any job's runner, so it outlives the agent's session. It takes the slots named with
`--lease` before it starts and holds them until it ends; `{name}` in the command is replaced by that slot's value (or
its number), and `DELEGATE_SLOT_<NAME>` and `DELEGATE_JOB` are set in its environment. `--lease test-copy:2` takes two
slots of one resource; the second is `{test-copy2}` and `DELEGATE_SLOT_TEST_COPY_2`. `--shell` is `none` (the default),
`bash`, `powershell` or `cmd`. All output goes to `output.log` in the job folder, the newest lines show as activity on
the feed, and the job ends as `done` (exit code 0) or `failed`, with the exit code and the last 40 lines of output as
its result. It is on `board`, the feed and mission control, `cancel` stops it, `--max-minutes` (default 600) limits it,
and `wait --any` reports it exactly once. An agent that needs the result runs `wait <job_id>` in the foreground; one
that does not simply ends its turn.

## Screenshots with a record: `capture`

The claim checks can tell that an image exists and has the stated size, not what it is a picture of. `capture` closes
most of that gap:

```sh
python delegate.py capture D:/work/menu.png --process TheApp.exe    # or --window "<title text>", --pid <n>, --foreground
python delegate.py capture --list                                   # the windows it can see
DISPLAY=:73 python3 delegate.py capture /work/menu.png --process TheApp.exe    # a Wine, Proton or Linux program
```

It saves the client area of that program's window and writes `menu.png.capture.json` beside it: window title,
program, real pixel size, whether the window had a border, the monitor's size and whether the window fills it, whether
it was in front, and a hash of the image. It never activates or moves the window: a window that is in front is copied
off the screen, a covered one is asked to draw itself, and if one way gives a flat colour the other is tried
(`--method screen` or `--method window` forces one). A program that ignores Windows display scaling is captured at the
size it actually renders, and the stretched on-screen size is recorded. An evidence entry may then say
`"process": "TheApp.exe"`, `"window": "<title text>"`, `"borderless": true` or `"fullscreen": true`; the result checks
compare those (and the word "borderless" in `shows`) with the record and flag a picture of another program or window, a
window that had a border, a blank capture, and an image that was cropped or edited after capture. A `process` or
`borderless` claim on an image with no record is flagged as uncheckable. Unflagged screenshots are listed in the result
summary as `SCREENSHOT: captured from ...`, and the review page shows the same line under each image. Some programs
draw nothing into a background capture; the helper says so instead of saving a wrong picture as proof.

**Wine, Proton and Linux programs.** Windows' own capture calls return a flat colour when they are made inside Wine, so
there the window is captured through X instead. Run the command where the program runs, with that program's X display
in `DISPLAY` (or `--display :73`): it finds the window with `xwininfo` and `xprop` by title, program or Linux process id,
copies that one window with `xwd -id`, converts it to PNG pixel for pixel (no crop, no scaling) and writes the same
record, with `"method": "xwd"` and the display. It needs `xwininfo`, `xprop` and `xwd` (packages `x11-utils` and
`x11-apps`) and no other package. The command may be run with the machine's own `python3` or with a Windows Python
inside Wine; the latter hands over to `python3`. Under WSL it refuses display `:0`, which is the desktop the machine's
owner is using, and with no display set it prints `NOT CAPTURED` with the reason. A path under `/mnt/<drive>/` is cited
in its Windows spelling, so the result checks on the Windows side find the image and its record, and they treat it like
any other capture. One difference: a Wine program can draw its own title bar inside its X window, so whether such a
window was borderless cannot be read from X; that claim is then reported as unchecked instead of being confirmed or
flagged.

## Writing a file: `save`

For agents whose Write tool is refused: `python delegate.py save <path>` writes whatever is piped into it (parent
folders are created; `--force` replaces an existing file, `--append` adds to it).
`save <path> --from <job_id or agent-id>` writes that finished job's or Claude Code subagent's final message, so a
report that only exists in a final message can be put on disk without any quoting.

## Spend and the digest

```sh
python delegate.py spend -n 5          # recorded Claude API spend, with the last five jobs
python delegate.py digest --hours 12   # what finished while you were away
```

`board`, the feed and mission control show the Claude API spend recorded this calendar month, against
`claude_api_monthly_usd` if you set one, and the month's Nous spend against `nous_monthly_usd`. Other providers' quotas
are not visible to this tool. Costs and caps per backend are in [BACKENDS](BACKENDS.md).

`digest` sorts what finished into what is waiting for an answer, what needs a look (failed or flagged), what is yours
to do, and what was left open; `--send` also pushes it through your alerts. Schedule it with your operating system's
task scheduler for a morning summary.

## Watching in a terminal

```sh
python delegate.py watch
python delegate.py watch <job_id> --stall 10 --loop 6 --no-color
```

`watch` follows running and newly started jobs; supply job IDs to follow only those jobs. It prints a line when a Claude
Code subagent starts, finishes, goes quiet or seems to loop, and follows subagents only when no job IDs are given.
`--stall` is in minutes and overrides `stall_seconds`; `--loop` overrides `loop_count` ([CONFIG](CONFIG.md)).
