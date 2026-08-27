# Steward runbook

How Steward works. This ships with the package, so it can never be out of date
with the CLI you are running. It is mechanics; `_steward.md` is what this
particular human wants — YAML front matter Steward already enforces on its own,
and prose beneath it that you are the one who applies.

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
- **Write `_steward.md`.** Propose; the human writes. Both halves of it.
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

An item comes back if the condition **changes**, because its id encodes the
instance rather than the condition: a task that stalls again at attempt 3 has a
different id from the one that stalled at attempt 2, so it arrives as new work,
while an unchanged condition stays raised and stays quiet.

**Nothing `collect` sets aside is dropped silently.** A shortened section says
how much it left out and how to see it. Take those counts literally: `1 raised,
awaiting a person` under an otherwise empty decisions section means there *is*
an open decision, and it is not yours.

## Context is the real budget

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
`_steward.md` for this human's standing rules, then **`steward collect`** for
what is true and what you missed. Everything you need is in the workspace, so
nothing depends on a conversation this session did not have.

## Tuning inside the envelope

*Not yet written.* `_steward.md` sets bounds — `max_tasks` in the front matter
is the hard ceiling Steward enforces, and the prose says what room you have
beneath it. The signal is rate limits rather than saturation.

## When to notify

*Not yet written.* Four kinds; two of them are Steward's alone. A question you
are blocked on is the most important thing you will send and the easiest to
leave sitting in the conversation, where nobody is reading it.

## Hard stops

*Not yet written.* Conditions to stop and ask on rather than work around. A
stop is not a teardown: healthy work keeps running.
