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

## Trust the artifact, not the exit code

Every gate has an artifact that says what happened — the manifest delta, the
smoke digest, the log itself, the anomaly count. A clean exit means a process
ended, which is not the same as the work having succeeded. Steward runs evals
with `fail_on_error=False`, so a task that completed with every sample errored
exits zero. **Completion is not success.** Read the artifact.

## Render the snapshot; do not replace it

"How is it going", "what's the latest", "any update" are requests for the
snapshot, not for your reading of it.

> Run `steward status` and render what it printed — every section, in its
> order, in full, with nothing above it. As markdown, not inside a code fence,
> because it is a document with tables meant to be read rendered.

Brevity is not the failure; substitution is. The detail *is* the answer, and a
summary replaces the reader's judgement with yours at the moment they were
trying to form their own. Hold analysis by default, and put it *below* the
snapshot, marked as yours, only when it is both important and not obvious from
the snapshot: an arm that has stopped, a climbing retry count, a scan finding,
anything in the anomaly list that is growing.

This applies to a wake-up at 3am exactly as it applies to a question asked
directly.

## Context is the real budget

- **Never read a full eval log.** Use `header_only=True` for status and counts;
  `read_eval_log_sample_summaries` or `samples_df` for per-sample data. A full
  read pulls the whole archive for what the header already has.
- **Transcript analysis goes through a scan**, never a raw log read.
- **Narrow an anomaly before opening it.**

## The cadence, and how it is guaranteed

*Not yet written.* A timer, not you, guarantees the mechanical tend.

## Cold pickup

*Not yet written.* The procedure for attaching to a run you did not start.

## Tuning inside the envelope

*Not yet written.* `_steward.md` sets bounds — `max_workers` in the front
matter is the hard ceiling Steward enforces, and the prose says what room you
have beneath it. The signal is rate limits rather than saturation.

## When to notify

*Not yet written.* Four kinds; two of them are Steward's alone. A question you
are blocked on is the most important thing you will send and the easiest to
leave sitting in the conversation, where nobody is reading it.

## Hard stops

*Not yet written.* Conditions to stop and ask on rather than work around. A
stop is not a teardown: healthy work keeps running.
