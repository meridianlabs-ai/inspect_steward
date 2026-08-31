# Steward runbook

How Steward works. This ships with the package, so it can never be out of date
with the CLI you are running. It is mechanics; `_steward.yaml` is what this
particular human wants — settings Steward already enforces on its own, and a
`policies` key that you are the one who applies.

> **Status: skeletal.** The prohibitions and the reading disciplines below are
> settled and binding now. The sections marked *not yet written* describe
> machinery that does not exist yet — there is nothing to follow there, and
> nothing to infer from the silence.

## What you must never do

No situation makes any of these correct.

- **`steward signoff`.** It is a human attestation that the results are
  accepted. An agent running it is the one thing that would make the whole
  record meaningless.
- **Edit the definition.** It is the human's statement of what is being
  measured, and afterwards your edit is indistinguishable from theirs. Read it,
  run it, and raise anything that looks wrong as a *question*. This includes
  adding a comment explaining what the eval set is for: you were never told why
  it exists, and a plausible-but-invented rationale is worse than none, because
  a later reader cannot tell it from the author's.
- **Write `_steward.yaml`.** Propose; the human writes. The settings as much as
  the policies.
- **Move or delete a log.** Not even an empty cancelled one, and not into a
  folder named for discards. Resume matches logs where they are.
- **Pass `steward launch --accept-archive` on your own judgement.** A launch
  that would move results out of `logs/` prints what it would move and refuses.
  Adding work is different: if the delta is purely additive, `launch` commits it
  and you needed nobody's permission — the human asked for the work, and
  checking whether they meant it is the interruption Steward exists to remove.
  But a one-character change to a task argument reads exactly like a deliberate
  removal, and only one of the two quietly buys a re-run of everything. So show
  the human the delta and let them answer. **The refusal is the mechanism, not
  an obstacle to it**: a flag reached for reflexively is the same as no gate.

## Trust the artifact, not the exit code

Every gate has an artifact that says what happened — the manifest delta, the
smoke digest, the log itself, the anomaly count. A clean exit means a process
ended, which is not the same as the work having succeeded. Steward runs evals
with `fail_on_error=False`, so a task that completed with every sample errored
exits zero. **Completion is not success.** Read the artifact.

## Render the snapshot; do not replace it

"How is it going", "what's the latest", "any update" are requests for the
snapshot, not for your reading of it.

> Run `steward status --format md` and render what it printed — every section,
> in its order, in full, with nothing above it. As markdown, not inside a code
> fence, because it is a document with tables meant to be read rendered.

`--format md` rather than plain `status`: the default is aligned monospace
columns for a terminal, which collapse into a line of words once rendered as
markdown. Do not read `status.md` instead — `status` never writes that file, so
it holds the last *tend*'s snapshot and can be a full interval stale while
claiming to be current.

Brevity is not the failure; substitution is. The detail *is* the answer, and a
summary replaces the reader's judgement with yours at the moment they were
trying to form their own. Hold analysis by default, and put it *below* the
snapshot, marked as yours, only when it is both important and not obvious from
the snapshot: an arm that has stopped, a climbing retry count, a scan finding,
anything in the anomaly list that is growing.

This applies to a wake-up at 3am exactly as it applies to a question asked
directly.

## Your queue, and what takes something out of it

```bash
steward collect              # what is true now, and what happened since you last looked
steward collect --peek       # the same, without marking it read
steward collect --since 0    # the whole history, however far back
```

`collect` is the verb to start a session with. It prints the same three
sections `status` does — what needs a decision, where the run stands, what
happened — and adds the one thing a snapshot cannot give you: the stretch of
history since your last collection. A snapshot says what is true now. It cannot
say that a task died at 1am and was respawned, or that a class grew from three
instances to forty, and that series is what most judgement calls need.

**Reading consumes nothing.** The cursor governs history alone, and an open
decision leaves your queue only because you *acted* on it. So a session that
dies mid-investigation finds its work waiting, and there is no ordering
discipline for you to remember.

Two acts take an item out of the queue, and which one is right is decided by
who owns the item — `collect` prints that beside each one.

- **`steward ack <id> --reason "..."`** closes it. Ask first: acking removes an
  item from every surface including the verdict, so an agent free to ack unasked
  can silence its own attention list. Record the human's answer as
  `--by human`. The narrow exception is something you investigated and resolved
  yourself — a file that turned out to be a partial upload — which is
  `--by agent`. The test is whether anyone but you would need to know.

- **`steward raise <id> [--note "..."]`** hands it to the person who can decide
  it, and **closes nothing**. Do this freely and without asking: putting a
  human-owned item in front of the human is you doing your job, not deciding
  anything. The item stays in the summary's decisions and the verdict still
  counts it; what changes is that `collect` stops offering it back to you every
  time you look. The note is optional, and is for what you actually did to
  surface it — where you asked, and of whom.

  **Only a human-owned item can be raised**, and the command refuses the rest.
  Raising takes something out of your queue without closing it, which is safe
  only because somebody else is going to close it — do that to your own item
  and it is stranded: open forever, and gone from the one list that would have
  brought it back to you. If you are stuck on an agent-owned item, that is a
  question to ask in the conversation, not a hand-off to record.

**A parked worker is the one item that refuses an ack.** A sample stopped on a
tool approval or an `ask_user` is waiting for authority over what the eval does,
which is the human's alone — and it holds its slot, its sandbox and its model
connections the whole time. So `ack` refuses it whatever reason you give. Raise
it, pass on the attach command the item carries (`inspect acp`, whose picker
floats the samples waiting on a person to the top), and leave it open: it clears
when somebody answers it, and nothing else clears it.

An item comes back if the condition **changes**, because its id encodes the
instance rather than the condition: a task that stalls again at attempt 3 has a
different id from the one that stalled at attempt 2, so it arrives as new work,
while an unchanged condition stays raised and stays quiet.

**Nothing `collect` sets aside is dropped silently.** A shortened section says
how much it left out and how to see it. Take those counts literally: `1 raised,
awaiting a person` under an otherwise empty decisions section means there *is*
an open decision, and it is not yours.

## Context is the real budget

- **Take the log directory from the summary, never from the workspace.** The
  `Logs` line in `status.md` is where this run's results are, and it is often
  not `logs/` here — a definition can name its own, and a machine can put every
  run under a shared root. Anything you point at `logs/` on the assumption that
  it exists will find nothing rather than fail.
- **Never read a full eval log.** Use `header_only=True` for status and counts;
  `read_eval_log_sample_summaries` or `samples_df` for per-sample data. A full
  read pulls the whole archive for what the header already has.
- **Transcript analysis goes through a scan**, never a raw log read.
- **Narrow an anomaly before opening it.**

## The cadence, and how it is guaranteed

*Not yet written.* A timer, not you, guarantees the mechanical tend.

## Cold pickup

*Partly written.* The full procedure for attaching to a run you did not start
depends on machinery that does not exist yet — the anomaly list and
`analysis.md`. What is settled is where it begins: `steward runbook`, then
`_steward.yaml` for this human's standing rules, then **`steward collect`** for
what is true and what you missed. Everything you need is in the workspace, so
nothing depends on a conversation this session did not have.

One caveat on the second of those: every setting, `policies` included, can also
arrive from a `STEWARD_*` environment variable, which outranks the file and is
not in the workspace at all. `steward status` reports the standing rules
actually in force, so read the file for the reasoning behind them and `status`
for what is set.

## Tuning inside the envelope

Tend ramps sample concurrency on its own; your part is oversight, not the
arithmetic. Unless a `max_samples` is pinned somewhere, every task starts at
the ramp's floor and climbs one step per clean window — limiter saturated, no
rate-limit pushback, no new errors, retries not surging, CPU with headroom — and
on sustained pushback tend cuts the connection ceiling at once and steps sample
concurrency back down. Every move is a journal action, so your next collect
shows it as history (`ramped <task> 60→80 — ...`), and the tuning block under
the status table shows each task's level and whatever gates its next step.

**A Hawk config always pins it**, because Hawk's infra config sets `max_samples`
itself. So a Hawk run shows no ramp actions and no tuning block, and that is the
loop obeying the config rather than a loop that has stopped working. Do not
retune around it: the number is the run owner's to change, in the Hawk config.

- **When a ramp action coincides with something you dislike** — errors or
  anomalies rising, pushback the cut is not containing, CPU climbing — run
  `steward ramp hold --reason "..."` (add a task identifier to hold one arm).
  Levels stay where they are and the defensive cut stays active; only the
  climb stops. `steward ramp resume` re-arms it. Holding is yours to do on
  your own judgement; explain it in the reason, because the reason is the
  record.
- **A `tuning_proposal` is capacity tend has no authority to take** — a pinned
  `max_samples` showing a clean, saturated window, or a ramp at the top of its
  range with pushback still absent. Only the human can move that bound: raise
  it to them, and when they rule, record the ruling for them with
  `steward ack` ("seen, happy at 60"). The ack is narrow — a different level
  is a different item — so it never silences the next proposal.
- **Never lower a pinned setpoint and never edit `samples_ramp` yourself** —
  both are the human's numbers. What you may do freely is hold, resume, and
  retune *downward* through `inspect ctl config` when you are containing an
  incident; tend will not climb past your hold.

## When to notify

**`steward notify "..."` reaches the human when nobody is reading the
conversation**, which is most of the night. It is the single most valuable
thing you do that no trigger could do for you: Steward's own posts are
arithmetic over what a turn saw, and yours carry an interpretation — *"the
sonnet arm is failing systematically, I've paused it, here's why"*.

    steward notify "the grader is failing on every sample since 01:40" \
      --kind stopped \
      --detail "8 tasks affected, all against the same grader model" \
      --detail "paused the run; nothing new is being scheduled"

Two kinds are yours:

- **`--kind attention`** (the default) — worth knowing, and work continues.
- **`--kind stopped`** — nothing progresses until a person answers.

**A question you are blocked on is `stopped`, always.** It is the most
important thing you will send and the easiest to leave sitting in the
conversation where nobody is reading it. That is the kind's entire reason for
existing.

The other four kinds — `progress`, `clear`, `gate`, `signed_off` — are
Steward's and the command refuses them. Each is either latched, terminal, or
read off state you do not own, and a hand-sent one is a claim about the run
that nobody computed.

**Post rather than agonise, and do not batch.** Steward already batches its own
posts to one per turn, so the noise budget you are spending from is not tight.
The cost of an unnecessary `attention` is one line in a channel; the cost of a
skipped `stopped` is a run that waits all night for an answer nobody knew was
wanted.

**Notifying is not raising, and one does not imply the other.** `steward raise`
records that an item is with the person who can decide it; `notify` is how you
actually tell them. Do both when you hand something over — the item stays in
the summary either way, and the post is what makes somebody look at it.

**With no channel configured the command fails rather than succeeding
quietly**, so an escalation you thought you made is never one you did not.
If that happens, say so in the conversation and put it in your writeup; the
remedy is a `notification` setting and it is the human's to add.

## Hard stops

*Not yet written.* Conditions to stop and ask on rather than work around. A
stop is not a teardown: healthy work keeps running.
