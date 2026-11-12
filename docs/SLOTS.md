# Slots for shared resources

[Back to the README](../README.md)

On a machine that several agents and its owner share, the `resources` config key names what must not be used at the
same time:

```json
"resources": {
  "build":     {"slots": 1, "rule": "any compiler or full test-suite run"},
  "test-copy": {"slots": 2, "values": ["/work/copy-1", "/work/copy-2"],
                "yield_to": ["TheApp.exe"], "rule": "launching the application under test",
                "baseline": {"registry": ["HKCU\\Software\\Vendor\\TheApp"],
                             "files": ["%LOCALAPPDATA%/TheApp/last-session.txt"]}}
}
```

| Field | Meaning |
|---|---|
| `slots` | How many holders at once (1 to 64) |
| `values` | Optional, one per slot: what a holder is handed, such as its own folder |
| `yield_to` | Optional program names of the machine's owner; see [Giving way to the owner](#giving-way-to-the-owner) |
| `rule` | What a worker needs the slot for, in the words its prompt will show |
| `baseline` | Optional state all holders share; see [Shared state](#shared-state-baseline) |

## Leasing

Every job's prompt then lists these with their rules and tells the worker to run `delegate.py lease --job <id> <name>`
before such work and `release` afterwards. `lease` waits for a free slot and prints `GRANTED` (with the slot's value,
such as the folder to use) or `NOT YET` with the reason. Slots a job still holds are freed when it ends, and a holder
that died loses its slots. Anything outside a relay job, such as a Claude Code subagent, takes part with
`lease --as <name> <resource>`; those slots expire after three hours unless leased again. A task that needs several
slots of one resource at once (a host and a guest copy) asks with `lease --count 2`: it gets all of them or none. A
`bg` job takes its slots with `--lease <name>` or `--lease <name>:2` ([CLI](CLI.md#long-commands-bg)).

```sh
python delegate.py slots          # who holds what, for how long, and who is waiting; also on `board` and the pages
python delegate.py release --job <job_id> build     # take a slot back by hand
```

`slots` and `board` show how long each holder has had its slot and who was refused and is still asking
(`build 1/1: job-a (35m) | 2 WAITING: job-b (12m), reviewer (3m)`), so a long holder is easy to spot.

## Giving way to the owner

While a program listed in `yield_to` is running and is not an agent's own (as the feed's process watcher judges it),
nobody new is granted that resource, and `slots` says so. While a worker holds a slot on such a resource and the owner
starts one of those programs, the runner tells the worker once to stop using it and release it. Like every message, it
lands at the worker's next stage change. A slot that is already held is not taken away.

## Shared state (`baseline`)

Some programs keep settings in one place for the whole machine, such as a registry key or a file in the user's
profile, so every copy under test reads and writes the same ones. A holder that saves those settings, runs, restores
them and compares can then be broken by any other holder, and the only cure is to run one at a time. A resource's
`baseline` moves that job to the slots instead:

1. The named registry keys (Windows; at least two levels below `HKCU` or `HKLM`) and files are saved when the first
   holder is granted a slot.
2. Later holders simply join. Holders run side by side and never save, restore or compare that state themselves; their
   prompt says so.
3. Once the last holder has gone (released, ended, or died) the state is put back, read again and compared. Only what
   differs is written.

`slots` shows `shared state saved 35m ago` while it is held and `shared state put back 2m ago (1 change undone)`
afterwards; a restore that fails is retried at every later check and shown as `NOT RESTORED`. The saved copy is
`baselines/<resource>.json` in the delegate folder and the outcome of the last restore `baselines/<resource>.last.json`.

Limits:

- If nobody runs `slots`, `board`, `wait --any`, a release or the feed after the last holder died, the restore waits
  until someone does.
- Holders that were already running when `baseline` was configured keep their slots, and nobody new is granted one
  until they have finished, so that a half-changed state is never saved. If one of them keeps its slot for a long time
  without using the resource, release that slot by hand once you are sure nothing is running.
- Whatever the owner's own program writes to that state while agents hold slots is put back too.

## What slots cannot do

This is cooperative: it tells workers when they may go ahead and gives each its own slot, but it cannot stop a worker
that ignores its instructions.
