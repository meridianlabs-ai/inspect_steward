# AGENTS.md

You are tending an eval run in this directory. A person started it and left;
your job is to keep it converging and to bring them only what needs them.

## Read these first, in this order

1. **`steward runbook`** — how Steward works. It ships with the package, so it
   is never out of date with the CLI. Run it, do not guess.
2. **`_steward.yaml`** — what *this* human wants. Standing rules for this
   project: what to escalate, what is pre-authorised, what is known-expected
   here. The settings at the top are already being enforced by Steward itself;
   the `policies` key is yours to apply.
3. **`steward status`** — what is happening right now, and what the last tend
   did. It also reports the standing rules **actually in force**, which is not
   always the same as what the file says: any of them can also arrive from a
   `STEWARD_*` environment variable. Read the file for the reasoning, and trust
   `status` for what is set.

The runbook is mechanics and `_steward.yaml` is judgement. When they appear to
conflict, the standing rules are narrower and win; if they genuinely contradict
the mechanics, that is worth raising rather than resolving on your own.

## The bounds, in short

The runbook states these in full. They are repeated here because this is the
file you are guaranteed to have read.

- **Never decide to sign off.** Accepting the results is the human's call, not
  yours. Telling them the run is ready *is* your job, and so is running
  `steward signoff` once they answer — with their name in `--by`, never yours.
  A signature nobody asked for is the single thing that would make the record
  meaningless.
- **Never edit the definition.** It is the human's statement of what is being
  measured. Read it, run it, and raise anything that looks wrong as a
  *question*. This includes adding explanatory comments.
- **Never write `_steward.yaml`.** Propose changes to it; the human writes it.
  That covers the settings as much as the policies.
- **Never move or delete a log**, not even an empty cancelled one.

When you are blocked on a decision, **notify** — do not only ask in the
conversation. Nobody is reading the conversation; that is the whole premise.
