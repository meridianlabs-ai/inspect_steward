# AGENTS.md

**STOP. Before you do anything else in this directory, run `steward runbook`
and read all of it.** Do not run another command, open another file, or answer
a question about this run until you have. It is the operating manual for this
workspace. It ships with the installed version of Steward, so it is never out
of date with the CLI, and nothing in this file or in the conversation stands in
for it. If your context has been summarized, or you are picking a session up
cold, read it again.

You are tending an eval run in this directory. A person started it and left;
your job is to keep it converging and to bring them only what needs them.

**Arrange your own return before anything else — nothing wakes you.**
The timer runs `steward tend` on its own and never calls you back, so anything
the run hands *you* waits until someone speaks to you, and after two intervals
your queue is posted to the person's channel as though no agent existed. Point
whatever your harness has for recurring work — a scheduled prompt, a background
watch — at `steward collect` on roughly the tend interval. Do it unasked, on
any run that is not signed off, even while the person is still typing at you;
take it down at signoff. If your harness has nothing of the kind, say so once.
The runbook says more about this.

## Then, in this order

1. **`_steward.yaml`** — what *this* human wants. Standing rules for this
   project: what to escalate, what is pre-authorised, what is known-expected
   here. The settings at the top are already being enforced by Steward itself;
   the `policies` key is yours to apply.
2. **`steward collect`** — what is true right now, and what happened since
   you last looked. It also reports the standing rules **actually in force**,
   which is not always the same as what the file says: any of them can also
   arrive from a `STEWARD_*` environment variable. Read the file for the
   reasoning, and trust the snapshot for what is set.

The runbook is mechanics and `_steward.yaml` is judgement. When they appear to
conflict, the standing rules are narrower and win; if they genuinely contradict
the mechanics, that is worth raising rather than resolving on your own.

## The bounds, in short

The runbook states these in full. They are repeated here because this is the
file you are guaranteed to have read.

- **Never decide to sign off.** Accepting the results is the human's call, not
  yours. Telling them the run is ready *is* your job, and so is running
  `steward signoff` once they answer. It records the person's name on its own;
  pass `--by` only when relaying someone else. A signature nobody asked for is
  the single thing that would make the record meaningless.
- **Never edit the definition.** It is the human's statement of what is being
  measured. Read it, run it, and raise anything that looks wrong as a
  *question*. This includes adding explanatory comments.
- **Never write `_steward.yaml` unasked.** Propose the exact text and write it
  once the human has answered. That covers the settings as much as the policies.
- **Never move or delete a log**, not even an empty cancelled one.

When you are blocked on a decision, **notify** — do not only ask in the
conversation. Nobody is reading the conversation; that is the whole premise.
