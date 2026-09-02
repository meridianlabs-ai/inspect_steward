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

- **Decide to sign off.** `steward signoff` records that a *person* accepted
  these results. Telling the human the run is ready is your job, and running
  the command once they answer is too — with their name in `--by`, which is the
  same rule `steward rule` already applies to a decision you are relaying. What
  is never yours is the decision: a signature nobody asked for is the one thing
  that would make the whole record meaningless.
- **Edit the definition.** It is the human's statement of what is being
  measured, and afterwards your edit is indistinguishable from theirs. Read it,
  run it, and raise anything that looks wrong as a *question*. This includes
  adding a comment explaining what the eval set is for: you were never told why
  it exists, and a plausible-but-invented rationale is worse than none, because
  a later reader cannot tell it from the author's.
- **Write `_steward.yaml` unasked.** Propose the exact text, and write only
  what the human approved — the settings as much as the policies. Once they
  have answered, typing it is yours the same way `steward rule --by` and
  `steward signoff --by` are. What is never yours is the decision: promoting a
  one-off ruling into a standing rule widens what they have committed to, and
  the widening is theirs to authorize. See *Writing a standing rule*.
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

## Rehearse before you commit a night to it

`steward launch --smoke` runs a couple of samples per task under a wall-clock
cap, into `.steward/smoke/`, and tells you whether the run is ready. It is the
default first step, and the artifact it leaves is `.steward/smoke/digest.md`.

It launches nothing. Two invocations: `--smoke` answers *is this ready*, and a
plain `steward launch` acts on the answer. A launch with no passing smoke for
its tasks says so and proceeds anyway — that is a warning, not a gate, because
re-launching after a fix and resuming are both legitimate.

Give it the flags the launch will get. It rehearses what the launch will run,
so `--scan-model`, `--notification` and `--max-workers` belong here too;
arguments and overrides you do not repeat are taken from the committed
manifest, exactly as a re-launch takes them. Flags that shape the *launch*
rather than the run — `--no-timer`, `--sync`, `--log-root`, `--tend-interval`,
`--samples-ramp` and the like — are refused here rather than ignored, because
`--smoke` launches nothing. Pass them to `steward launch` when you run it. The launch also warns when the last
smoke covered the same tasks at a different *shape* — a grown dataset or a
raised `epochs` keeps every task identifier while changing what the night costs
— and when it ran different scanners or scanned with a different model.

The slice is taken inside whatever your run already selects, so a run limited
to `(100, 200)` rehearses the front of that window rather than samples nobody
will run.

**What it catches that a run does not.** Most of the list is things that stop a
run: a definition that will not import, a wrong model name or key, a sandbox
image that will not start, a scorer that throws.

None of those actually stop a *task*, which is why the digest reports what the
**samples** did rather than whether the tasks finished. Workers run with
`continue_on_fail` on, so every one of them lands as errored samples inside a
log that finished `success`. Any errored sample fails the rehearsal, and unlike
the checks below it cannot be waived — read the class it names, because that is
the failure you were rehearsing to find. So does a task that finished holding
fewer samples than the slice asked it for: nothing errored, nothing is marked,
and a sample went missing anyway.

Four more are worse still, because they do not stop anything — they produce an
eval that completes, scores, and is quietly wrong:

- **the scanners ran**, and what they flagged in the rehearsal. Two flagged
  samples before the sweep are worth more than two hundred after.
- **the scanners reached every transcript.** A scan that recorded nothing looks
  exactly like a scan that found nothing, so a clean findings list is only good
  news beside a full coverage. A definition that filters what its scanners see
  will leave a real gap here — that one is yours to waive by name.
- **every model resolved to a context window.** A model that resolves to none
  runs at an assumed 128000 whatever its real window is, and stops shrinking
  oversized tool output entirely. A model with no database entry whose provider
  aliased it onto the current frontier is *fine* — the digest names which entry
  it landed on, so read that line rather than assuming.
- **reasoning is replayed to the model.** Checked in the conversation and again
  in the raw request body, because a provider can drop on the wire what Inspect
  kept.

**A failed check stops the launch.** Say what failed when you tell the human,
and fix it rather than routing around it. Three answers are not failures:
`unexercised` means there was nothing to check — a non-reasoning model has no
reasoning to replay — and `undetermined` means the check could not run here,
usually a provider package this machine does not have. `--accept CHECK` waives
one by name and records the waiver in the journal, so use it when you know why
and never to make a red line go away.

**Do not project the run's spend from it, and do not ask the digest to.** A
rehearsal is a couple of samples off the front of each dataset, which is not a
sample of the run in any sense that supports multiplying. The digest says how
many samples the run will produce; that count is a fact and everything past it
would be a guess wearing a number.

**Stop and ask if a smoke fails twice.** A rehearsal that keeps failing is a
problem to understand, not to retry. And notify: before the first worker of the
real run starts there is no tend, no `status.md` and nothing posting, so a
launch blocked here is silent unless somebody says so.

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

**A parked worker refuses an ack.** A sample stopped on a tool approval or an
`ask_user` is waiting for authority over what the eval does, which is the
human's alone — and it holds its slot, its sandbox and its model connections
the whole time. So `ack` refuses it whatever reason you give. Raise it, pass on
the attach command the item carries (`inspect acp`, whose picker floats the
samples waiting on a person to the top), and leave it open: it clears when
somebody answers it, and nothing else clears it.

**An anomaly refuses an ack too — it closes through a ruling.** See the next
section; the dismissal that says *looked, nothing here* is
`steward rule <class> --disposition dismiss`, recorded with a reason, never a
wave-past.

An item comes back if the condition **changes**, because its id encodes the
instance rather than the condition: a task that stalls again at attempt 3 has a
different id from the one that stalled at attempt 2, so it arrives as new work,
while an unchanged condition stays raised and stays quiet.

**Nothing `collect` sets aside is dropped silently.** A shortened section says
how much it left out and how to see it. Take those counts literally: `1 raised,
awaiting a person` under an otherwise empty decisions section means there *is*
an open decision, and it is not yours.

## A sample that has stopped moving

A **stuck** item names samples that are alive but idle past `stuck_after` —
nothing failed, nothing is waiting on a person, the task's clock just keeps
running. One item per task, and it is not an anomaly: nothing is broken and
there is nothing to rule on. It clears on its own the moment the sample moves.

The remedy is a ladder, cheapest rung first, and the item carries the
applicable command ready to run:

- **Cancel the pending tool call** (`inspect ctl sample cancel-tool-call ...`)
  — the call fails inside the sample, which continues and works with the
  failure. This rung is yours **only when** the human granted it in advance
  (`stuck_cancel:` in `_steward.yaml`), and the item says so by arriving
  agent-owned. Run the command it carries, then record what you did:
  `steward ack <id> --by agent --reason "cancelled bash in <task>"`. This is
  the narrow `--by agent` exception — something you did yourself — and the
  recorded reason is what keeps the next session from asking again.
- **Everything above that is the human's.** Cancelling the sample records an
  outcome (`--action score|error|cancel` is a judgement about the eval's data),
  and a requeue discards everything the sample did. Raise the item and pass on
  the command it carries.

**Ask once.** A cancel is a request, and the feedback is explicit: if it was
asked and the call has not stopped, the item comes back with `:asked` in its
id and human-owned — *a cancel was asked and it did not stop*. That is the
signal to climb a rung, never to repeat the ask.

## Anomalies: from failure to ruling

Failures that mean the same thing share a **class** — an exception type at a
raising frame (`error:TimeoutError@openai/_client.py:post`), a task that died a
particular way (`task:no-log-exit:...`), an operator kill (`limit:operator`), a
task whose every score is zero. A class absorbs instances until somebody rules
on it; `status` lists what is open with counts, an example message, and any
prior rulings attached.

Your verbs, in the order a night usually uses them:

```
steward investigate <class> --note "..."   # mark it worked, so the next session does not redo you
steward propose <class>... --action <d> --reason "..."   # these classes are one decision
steward rule <class> --disposition <d> --reason "..." --by <person>   # record the decision
```

- **Investigate freely.** The note is a hand-off to the next session, not a
  diary. Prefixes work everywhere a class key does.
- **Propose freely.** One action per proposal; classes wanting different
  answers are different proposals. The proposal becomes one consolidated
  question for the human, answerable whole or in part
  (`steward rule --proposal <id> [<class>...]`).
- **A ruling is never yours.** `--by` names the person who decided — you
  relaying their answer is the ordinary path, and the record still says whose
  decision it was. The dispositions: `rerun`, `exclude`, `zero`, `score` for
  errored samples; `accept` (with `--effect`, the sentence the report carries)
  and `dismiss` for anomaly-level closure. `accept` is refused for `error:`
  classes — accepting errored samples as-is is exclusion wearing a decision's
  clothes.
- **A substrate-flagged class gets no re-run proposal.** Credentials, disk,
  storage: re-running into broken machinery burns the work twice. Verify first;
  a person ruling `rerun` directly is that verification.

After a `rerun` ruling the tend carries it out itself: samples in a
still-running task are requeued in place, and a landed log has exactly the
ruled samples invalidated so the next turn re-launches it, reusing everything
else. **There is nothing for you to run** — the window stays open while the
re-run is in flight (the status says so), resolves itself when its tasks come
home clean, and the same samples failing again comes back to the human as *the
ruling's premise did not hold*. A ruling can also arrive from a standing
`preauthorized:` pattern in `_steward.yaml`; the record then says `by: policy`,
and it is the same decision made earlier, not yours to make. Recurrence after
any ruling opens a new generation carrying every prior ruling as precedent — so
read the precedent lines before proposing; the 11pm decision usually answers
the 2am question.

Two classes deliberately make less noise than the rest. A `task:` window
resolves itself once Steward's own respawn brings the task home — leave it
alone unless it is not healing, in which case the `stalled` item is the real
question. And a `limit:operator` window (samples an operator killed) asks
nothing inline at all: the operator knows what they did, so it waits in the
anomalies block for the signoff conversation. Both still take a ruling any time
someone wants to give one.

### Scan findings: the bar is threatened integrity

A `scan:` class is samples a scanner flagged — `scan:scoring_integrity:reward_hacking`,
`scan:scoring_integrity:internet_egress`, one class per scanner and label. The
window opens when the rows land, not when you decide something is wrong, so
**every flag needs a ruling before the run can be signed**. That is deliberate:
a flag nobody looked at is a hole in the record, and it should read like one.

Your judgement is the whole product here, because the scanner already did the
only part that could be automated. It read one transcript; you can read the
population, the score, and the rest of the run.

- **The bar is whether the score can still be trusted**, not whether the
  behaviour was interesting. Escalate only where you can name the mechanism,
  cite the decisive messages, and say what the number would be if the concern
  is real. If you cannot do all three, the honest ruling is `dismiss`.
- **A failed attempt is not a finding.** The model tried to read the grader and
  could not; it tried to reach the network and the sandbox refused it. Nothing
  was earned and nothing was contaminated, so the score stands. Dismiss it —
  with the reason written out, because the person signing is told at signoff
  that these happened and the reason is what they read.
- **A *successful* escape or a returned egress response is a finding at n=1.**
  One sample that actually got data from outside the sandbox is worth raising
  immediately; forty that tried and failed are worth one dismissal.
- **Rarity is not the signal.** 197 of 200 flagged is the alarming case, not
  the noisy one — an exploit that works gets used everywhere, so a class
  covering most of a task is the one to read first, not the one to write off as
  a chatty scanner.
- **A confirmed validity finding is sample-shaped**, so `exclude`, `zero` and
  `score` are all available beside `accept` — unlike an `error:` class. Use
  them: a score you have established is wrong should not be silently averaged
  in.
- **Dismiss on the record, never by silence.**
  `steward rule <class> --disposition dismiss --reason "..."`. The reason is
  the entire artifact; a window left open blocks the signature and tells the
  next session nothing.

Only boolean scanners open windows. A scanner returning a number or a string is
recorded and readable and never escalated on its own — reading one is your job,
through the results rather than through the queue.

## Writing a standing rule

When you notice yourself asking the same question twice, or the human answering
it the same way twice, the answer belongs in `_steward.yaml` rather than in a
conversation nobody can read afterwards. Two keys take one:

- **`policies:`** — free-form text you apply when you are in session. Steward
  carries it and never interprets it, so write what a person would need to
  hear.
- **`preauthorized:`** — an anomaly class pattern mapped to the disposition it
  may receive. Steward acts on this one itself, on a timer, with nobody
  watching.

Propose, get an answer, then write it. Three things go with that.

**Propose the wording, not the idea.** *Shall I add a policy about timeouts?*
cannot be answered without the human writing it themselves, which is the work
you were trying to save them. The two lines you intend to add can be answered
yes or no.

**A `preauthorized:` pattern needs a yes to the pattern.** It is the one key
that widens what happens unattended, and the gap between them is where the
damage lives: a human who agrees that provider timeouts are usually worth
re-running has not thereby agreed that everything matching
`error:*Timeout*` may be re-run at 3am without them.

**Leave the edit visible.** The workspace is a git repository and this file is
tracked, so say plainly what you wrote and let `git diff` show it. Unlike a
ruling, a YAML edit carries no `--by` and the file cannot say afterwards whose
decision it was.

The rest of the file is unchanged by this: the definition is still never yours
to edit, and a setting the human has not approved is still not yours to write.

## Preparing a signoff

When the verdict turns 🏁, the run is finished and nobody has accepted the
results. That is a `signoff_ready` item, and it is the one item you cannot
acknowledge — the only thing that closes it is `steward signoff`.

**Tell the human.** Notify; do not only say it in the conversation. Then get
the run into a state where the answer is one command, because everything the
gate refuses over is something you can prepare in advance:

- **Every anomaly window closed by a ruling**, `limit:operator` included. Those
  raise no inline item on purpose, so they are the ones most easily left open —
  read the anomalies block, not the item list.
- **Say out loud what the scanners flagged and you dismissed.** The readiness
  item counts them; you name them. A dismissal leaves no caveat and appears in
  `anomalies.md` nowhere — correctly, since the whole content of it is *this
  does not change the numbers* — but "the model tried to read the grader on
  four samples and failed every time" is something the person signing wants to
  have heard from you rather than to find later. The reasons are in the journal
  and in `analysis.md`; put them in the message.
- **Every errored sample covered by a ruling.** `status` splits the errored
  cell; anything reading `undecided` refuses the signature.
- **Every task settled** — complete, short with the hole accepted by a ruling,
  or stalled and acknowledged. An accepted hole is what the signature is *for*;
  an unnamed one is what it refuses. Acknowledging a `stalled` item settles its
  task: the guard fires on attempt history rather than on an anomaly, so there
  is often no class to rule and the ack is the decision.
- **Every log in the directory readable, or its absence named.** A file that
  will not read is the one hole nobody can size, so signoff refuses until
  `steward ack unreadable:NAME --by NAME --reason ...` records why the results
  stand without it — and the signature then carries it as a caveat.
- **Every transcript scanned, or the gap ruled.** A scanner that threw leaves a
  sample with no verdict either way, and that is indistinguishable in the
  findings from a sample that came back clean — so a run whose scans errored
  would otherwise be signed as *nothing was flagged*. Those samples are their
  own anomaly class, `scanerror:SCANNER:TYPE@FRAME`, and it refuses the
  signature until it is ruled like any other. `accept` and `dismiss` are the
  answers; `rerun`, `exclude` and `zero` are refused, because the samples are
  fine and only the reading of them failed. The class already says how many and
  out of what: one grader timeout in five hundred is a class of one, and a
  scanner that threw on every transcript is a class of five hundred. A scanner
  that **never ran at all** raises no class — it wrote no rows for one to be
  built from — and what shows it is the `scanned` column below, which is why
  reading that column is its own bullet.
- **Read the `scanned` column, because nothing refuses over it.** It counts
  transcripts *every* scanner answered for against samples landed, and a
  shortfall means transcripts nobody looked at — a worker that died between
  logging and scanning, or a scanner added at a re-launch that will never
  revisit what already landed. Signoff does **not** block on it, deliberately:
  nothing Steward can run closes that gap, so a refusal would wedge the run
  rather than route it. Say the number in the message when you tell them the
  run is ready. `48 of 50 scanned` is a different thing to sign than `50 of
  50`. The signature carries the shortfall by name among its exceptions, so it
  is durable whether or not anybody said it out loud — but the point of saying
  it is that they hear it *before* they answer, not after.
- **No worker still running a task the definition no longer names.**
- **Something to sign for at all.** A capture that enumerated no tasks, or
  tasks that finished and produced no samples, is refused: a signature over no
  results is not a caveated attestation, it is a statement about nothing. This
  is the one refusal with no act that answers it — go and find out why the
  capture is empty.

And one thing to ask about that is not a blocker at all:

- **Whether to publish.** If the readiness item names a log store, there is a
  second decision waiting beside the first. Publishing puts these logs into an
  index other projects read, so a task somebody else has already run is copied
  in rather than run again — and a row in it is a claim that a result is good
  enough to reuse sight-unseen, which is the same claim the signature makes.
  **Ask; never assume.** There is no setting that turns this on, deliberately:
  the store may be shared with people who are not in this conversation, and
  results are not exported by default. Add `--publish` if they say yes. A task
  signed with an accepted exception publishes like any other — the caveat is in
  this project's `anomalies.md` and does not travel with the log, and whoever
  answers should know that is the trade.

One more refuses and is not something to prepare: an action the turn **could
not carry out** — an acceptance whose log amendment hit a read-only mount, say.
Run it again; the next turn retries what failed, and one that keeps failing is
a defect to look at rather than to sign around.

What the signature names as its exceptions is the caveat list, not the ruling
list: an acknowledged `stalled` task or `unreadable` log
left a mark on the results and is named there too. Acknowledging one is a decision about the data,
so give it the reason you would give a ruling — it ends up quoted in
`anomalies.md` under the numbers.

Then run it when they answer:

```
steward signoff --by NAME [--note TEXT] [--publish]
```

`--by` is the name of whoever decided, never yours and never a role — the same
rule as `steward rule --by`. The command runs a final turn, refuses with every
blocker at once if anything is still unnamed, moves superseded attempts into
`logs-archive/`, records who signed and what they signed over, and takes the
timer down. After it returns, nothing tends this run.

**It does not commit the journal**, and that is deliberate — the workspace is
the human's repository. What it guarantees instead is that the record is
complete and quiescent when it returns, so a commit taken any time afterwards
captures the same thing. Say so; do not commit on their behalf.

## Writing `analysis.md`

`status.md` and `anomalies.md` are Steward's; `AGENTS.md` and `_steward.yaml`
are the human's. `analysis.md` is the one file you and Steward share, and the
line between you is a pair of HTML comments:

```markdown
## cybench@openai/gpt-5

<!-- steward:begin cybench_0a1b2c3d -->
- scanned 48 of 50 transcripts — the rest carry no verdict either way
- 2 samples flagged for scoring integrity — scan:scoring_integrity:reward_hacking; no ruling yet
<!-- steward:end -->

Both flagged samples tried to read the grader file and failed. Dismissed:
the attempt is in the transcript, the score is honest, and the same two
samples pass on a re-run.
```

Every turn rewrites what is **between** the markers and nothing else. Your
prose outside them comes back byte-identical, turn after turn, so you can write
into a section while the run is still going and it will still be there at
signoff.

- **One section per task**, appended with an empty placeholder the first time
  Steward sees the task. A section whose prose is empty raises an item — one
  per task, yours, not acknowledgeable. There is no way to wave it past,
  because *looked, nothing here* is the entry: a task whose numbers turned out
  to be unremarkable is a finding, and the person reading this in a month needs
  to know you looked.
- **Do not move or delete the markers.** A section whose `steward:begin` and
  `steward:end` do not pair is left completely alone — Steward will not guess
  at a boundary in a file whose other half is your work — so its facts silently
  stop being updated and the damage goes to `.steward/steward.log`. If you have
  broken one, put it back.
- **Write outside them, anywhere**: above the block, below it, in sub-headings
  of your own. The only reserved text in the file is the marker pair.
- **Quote the decision, not just the outcome.** The facts block already carries
  the disposition and the ruling's own words. What it cannot carry is why you
  believe them — which samples you opened, what the transcript actually showed,
  what would change your mind.
- **A section for a task the definition no longer names is left alone.** The
  file is durable and a removed task's investigation is still what happened.

It reaches the log directory through the ordinary sync, so a remote reader gets
it beside `status.md` with no step of your own.

## Context is the real budget

- **Take the log directory from the summary, never from the workspace.** The
  `Logs` line in `status.md` is where this run's results are, and it is often
  not `logs/` here — a definition can name its own, and a machine can put every
  run under a shared root. Anything you point at `logs/` on the assumption that
  it exists will find nothing rather than fail.
- **Never read a full eval log.** Use `header_only=True` for status and counts;
  `read_eval_log_sample_summaries` or `samples_df` for per-sample data. A full
  read pulls the whole archive for what the header already has. Steward's own
  detection already read each errored sample once and put the evidence in the
  anomaly — start an investigation from the evidence, not by re-reading logs.
- **Transcript analysis goes through a scan**, never a raw log read.
- **Narrow an anomaly before opening it.**

## The cadence, and how it is guaranteed

*Not yet written.* A timer, not you, guarantees the mechanical tend.

## Cold pickup

Attaching to a run you did not start: `steward runbook`, then `_steward.yaml`
for this human's standing rules, then **`steward collect`** for what is true and
what you missed — the anomaly list arrives with it, investigation notes and
precedent included, so a class the last session was working says so. Then read
**`analysis.md`**, which is where the last session's reading of the numbers is:
the facts blocks are regenerated and tell you nothing you cannot get from
`status`, and the prose around them is the part no command reproduces.
Everything you need is in the workspace, and nothing depends on a conversation
this session did not have.

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
