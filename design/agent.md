# The agent

**Status: draft. The responsibilities, the relationship to the timer, the reporting discipline, and the bounds are settled. The tend summary's exact schema and the runbook's final text are not.**

Every other document in this set describes machinery. This one describes the only thing that operates it.

[execution.md](execution.md) establishes that the reconcile core is a pure function driven from outside, and that a timer guarantees the mechanical half of that. [workflow.md](workflow.md) establishes that the human's own surface is three commands and a conversation. Everything between those two facts is the agent, and it has accumulated more responsibility than any single document has acknowledged.

## 1. Four jobs, all of them judgement

The design's division of labour is consistent — mechanical work stays in `tend`, judgement goes to the agent — and applying it repeatedly has produced an agent that does four distinct things. Driving is deliberately **not** among them: an earlier draft had the agent scheduling `tend`, and that job now belongs to a timer for reasons the next section gives.

| | job | what happens without it |
|---|---|---|
| **tuner** | hold the ramp when climbing is unwise, and relay `tuning_proposal`s | `tend` climbs into trouble its gates cannot see — someone else's workload on a shared key, an arm whose anomalies argue against speed — and capacity against a pinned setpoint goes unrelayed ([scheduling.md](scheduling.md), *The signal is mechanical, and inside the envelope so is the decision*) |
| **grouper** | collapse computed error classes into proposals a human can answer | twelve questions where there were two causes ([workflow.md](workflow.md), *Three levels*) |
| **investigator** | judge scan results, which carry no verdict of their own | the most valuable anomaly source produces rows nobody reads |
| **author** | write `analysis.md` | the run leaves logs and a journal, and nothing that says what happened |

One of the four has since acquired a floor, and it is worth saying which. A **boolean** scan finding is now an anomaly window rather than a row on a shortlist ([workflow.md](workflow.md) §12.6), so the investigator's absence no longer ends in *rows nobody reads*: the window escalates to the person like any other unattended agent item, and the signoff gate refuses while it is open. The run stops rather than quietly passing. Judging what the flag *means* is still nobody's job but the agent's — the floor buys a refusal, not an answer — and a non-boolean result is a reading still, with no floor at all.

All four fail the same way: **with no agent in session none of them happens.** Each was individually argued as an acceptable cost, and their sum is what makes the separation below matter — because they are all judgement, an absent agent leaves a run that is *converging but undecided*, which is a tolerable overnight state. It stops being tolerable the moment the mechanical half depends on the agent too.

## 2. The agent does not guarantee the cadence — a timer does

An earlier draft made the agent the scheduler, and the reason it cannot be is structural rather than a matter of discipline:

> A supervising agent is turn-based. It acts when the human speaks, when a background job finishes, or when a scheduled wake-up fires — and never in between. So an agent-scheduled run is silent by construction whenever no agent is in session, and **silence is indistinguishable from a healthy run**.

Combine that with the four jobs above and the failure compounds: an absent agent would stop the fleet *and* leave nothing to say so. So the mechanical half is guaranteed by a timer — a system scheduler on a host, Hawk's in-pod loop in a pod — armed by `launch` rather than remembered ([execution.md](execution.md), *Drivers, one core*).

**What that buys is a clean separation of what breaks.** With a timer running and no agent in session, the fleet converges, logs land, scans drain, `status.md` stays current, and mechanical notifications fire. What accumulates is judgement: anomalies unruled, scan results unprobed, `analysis.md` unwritten. That is a run waiting for a decision, which is a fine thing to be at 4am — as opposed to a stalled run that looks exactly like a healthy one.

**One condition breaks that separation, and it is not the agent's absence but the human's.** A worker parked on an approval or an `ask_user` holds its slot while making no progress ([execution.md](execution.md), *The parked worker*), and only a person can release it — not the timer, and not the agent, which is barred from answering (§6). So a definition with human approvers is the one case where the fleet does *not* keep converging: throughput degrades as workers park, and reaches zero when the ceiling is full of them. The compensation is that it is loud rather than silent — parked workers are reported in every summary and in `status.md`, and a park is a notification kind — but the claim above holds for every other condition and not for this one.

### 2.1 Three postures, all of them supported

Because the floor is guaranteed, the agent's relationship to the run is a choice rather than an obligation:

| posture | how it learns something happened | judgement latency |
|---|---|---|
| **attached, reactive** | a monitor on the tend output or the journal wakes it | seconds — the best case, and worth setting up for a run being watched closely |
| **attached, periodic** | its own wake-ups; may call `tend` directly to force a turn early | its own interval |
| **transient** | reads accumulated state when it attaches, as runbook policy | until someone opens a session |

**The transient posture is the common one**, and it is the case the old arrangement served worst. Somebody opens a session in the morning; the agent reads what the night produced and works through it. This is exactly why cold pickup below is a specified procedure and why it runs several times a night — it is not the exceptional path, it is the normal one.

**Calling `tend` is still available and still useful**, but it now means *give me a turn now* rather than *keep this run alive*. An attached agent that has just ruled on an anomaly should tend immediately rather than wait out the interval, because an approved re-run is work the fleet could already be doing.

**Reactive beats periodic where the harness supports it.** A monitor watching for a new anomaly or a landed scan costs nothing while nothing happens, where a periodic check pays context on every quiet interval. Sixty tends a night read in full is a real cost; sixty tends a night that only wake an agent when something changed is not.

### 2.2 What the agent drains, and what it merely reads

Two things accumulate between sessions, and they have **opposite shapes**. Getting that wrong is the mistake this section exists to prevent.

- **Items** — open decisions — are a *set with a per-item lifecycle*. An item leaves the agent's view because the agent **acted** on it, and never because the agent read it.
- **History** — what each tend saw — is a *stream with a cursor*. It leaves the agent's view because the agent **read** it, and reading is the only thing that could.

An earlier draft governed both with the cursor, and the failure is worth recording because it is not obvious from the outside. A human-owned item — a definition drift, a stalled task — would sit in the agent's queue at *every* collection all night. The agent surfaced it at 1am and did its job; only the human can close it; so it never leaves. Sixty collections, the same item, and no way out. **Reading is the wrong verb for something the reader cannot dispose of.**

So an item has three agent-visible states, and the middle one is the one an item-as-stream design has no room for:

| state | in `collect` | in the summary's decisions | leaves by |
|---|---|---|---|
| **needs the agent** | yes | yes | the agent acting |
| **raised** — the agent did its part, the owner has not decided | no | yes | the owner deciding |
| **closed** | no | no — it moves to *what happened* | — |

Acting means different things by owner, which is what the two verbs are for. An **agent-owned** item is disposed of by acting, so `ack --by agent` closes it. A **human-owned** item is the agent's to *surface*, so `raise` records that it is now with its owner — it closes nothing, and the item stays in the summary's decisions until a person rules.

**Re-entry needs no expiry rule**, because an item's id already encodes the *instance* of a condition rather than the condition. A task that stalls again at attempt 3 has a different id from the one that stalled at attempt 2, so it arrives as new work; an unchanged condition stays raised and stays quiet.

**Reading is not disposing, and that is now structural rather than procedural.** Only an explicit act removes an item from the agent's queue, so an agent that dies mid-investigation finds its work waiting. An earlier draft asked for the same guarantee as a *discipline* — read, act, then acknowledge a position, in that order — which is the kind of rule an agent forgets. Nothing has to be remembered now, because there is no way to consume an item by looking at it.

**The cursor governs history alone, and consumes nothing.** The journal is append-only and a collection can name any earlier position, so advancing is a bookmark rather than a pop. `collected` therefore carries a position and *nothing else* — no note, no claim about action. An event that asserts two things at once is one that will eventually be read as the wrong one; the record of what was **done** is the `acknowledged`, `raised`, `ruling`, and `action` events, each written at the moment the thing was done.

**The queue is what a snapshot cannot replace, and that is the crux.** A summary says what is true *now*; it cannot say that an anomaly class grew from three instances to forty overnight, that a task died at 1am and was respawned, or that a rate-limit episode came and went at 3. That series is exactly what tuning and grouping decisions need, and a fresh session has no memory to reconstruct it from. Without a mark, an arriving agent has no familiar ground to read backwards to — it either re-reads everything or guesses where to start.

Two quantities fall out, and the second one is not the one an earlier draft named:

> **Open items owned by the human measure the human's backlog. Open items owned by the agent measure the agent's.**

That is a sharper instrument than *uncollected tends*, which was the best available before step 14 routed items by owner. Uncollected tends still measure something worth reporting — whether anyone has **looked** — but looking is not acting, and conflating them hides an attentive agent that is getting nothing done.

A third quantity sits with the first: **parked workers measure the human's backlog in the present tense.** An open item is work already done that nobody has ruled on; a parked worker is work that has stopped until someone answers. Both wait on a person, but only the second costs throughput while it waits, which is why it belongs in the summary as blocked work rather than in `anomalies.md`.

They are orthogonal, and both matter. Nothing open with six uncollected tends means nothing has gone wrong and nobody has looked. Nothing uncollected with three open means someone looked and the decisions are with the human. Only the second is a healthy overnight state, and before collection existed the two were indistinguishable.

**Whether a growing backlog should notify is a policy question, not a mechanical one.** In the transient posture a long uncollected stretch is normal — that is what an unattended night looks like. It becomes a problem only against an expectation the human holds, so it belongs in `_steward.yaml` as a setting ("I expect collection at least every four hours") rather than in a threshold Steward invents. What Steward does unconditionally is *report* it: the summary carries both ages, and a workspace whose last tend is four minutes old and whose last collection is six hours old is describing its own situation accurately.

### 2.3 What the agent still owes

Two things the timer cannot do, both of which belong in the runbook:

- **Check on attach, before anything else.** A session that starts by answering the human's question without first reading what accumulated is answering from a stale picture. As of [plan.md](plan.md) step 19 that is one command — `steward collect` — which prints the snapshot and the stretch of history since the last collection, and marks how far it read. `status` remains the right verb for *what is true now*; `collect` is the one that also answers *what did I miss*.
- **Confirm supervision is actually running**, once, early. The timer cannot detect its own absence, so two cheap signals stand in: the journal should show tend events at roughly the expected interval, and `status.md` states its own age. A workspace whose newest tend is four hours old in a ten-minute cadence is unsupervised, and nothing else will say so.

  As of [plan.md](plan.md) step 15 the first of those is computed rather than left to the reader: an `armed` journal event plus a gap of more than two intervals since the last `observation` raises an **`unsupervised`** item, which arrives in the ordinary attention list. The agent still owes the *judgement* — an unsupervised run may be one somebody is deliberately driving by hand, which is why the item is acknowledgeable and why "ask, then `steward ack --by human`" is the right response rather than silently re-arming (§6). What the agent no longer owes is noticing.

## 3. Cold pickup is a procedure, not a property

The design repeatedly claims a third party can pick a workspace up cold. That claim is a workflow, it has never been written down, and it runs far more often than the phrase suggests — **at every session boundary, several times a night**, not only when someone new arrives.

```
AGENTS.md          →  you are tending a run; read the runbook
steward runbook    →  the mechanics, shipped with the package
_steward.yaml        →  this human's standing rules
steward collect    →  what is true right now, and what happened since last time
open anomalies     →  what is undecided, with precedent attached
analysis.md        →  what has been found so far and what it meant
```

Two properties make this work rather than merely sound plausible. Everything an agent needs is **in the workspace**, so nothing depends on conversation history that a new session does not have. And every step is a file or a command, so the procedure is testable — which matters, because a claim exercised several times a night should not be exercised for the first time in production.

The last two lines are why the journal carries observations and why `analysis.md` exists at all. Without them a fresh agent inherits a list of open items and no idea which are getting worse.

**`collect` rather than `status` at the fourth line, and the difference is the whole of §2.2.** Both print the same three sections; only one of them knows where this agent stopped reading last time, sets aside the decisions it has already handed to a person, and records that it looked. A cold pickup that ran `status` would re-read the night from the top at every session boundary — several times a night — and would arrive at the same open items with no way to tell which it had already acted on.

## 4. The tend summary

The most-executed interface in the system, and a real constraint rather than a formatting question. The audience shifted once the timer took over the cadence: most tends are read by *nobody* at the time they run, and are read later in bulk by an agent that has just attached, or by a person asking how the night went. That makes the summary a **record** as much as a report.

**Its main job is to surface what a person has to decide.** Everything else in it is context for that. One document, three sections, in this order — and the order is the product decision, because §5 requires an agent to relay the whole thing verbatim, so what is at the top is what a human reads first.

Across all three: the run's **phase**, and **how old this information is** — both ages, since the last tend and since the last collection, which is the pair that says which half of supervision is missing (§2.2).

### 4.1 What needs a decision

**In full at the very top, not as a count.** An earlier arrangement put the verdict at the top and the decisions at the bottom, under the task table; running the M2 gate showed what that costs, since finding out what actually needed a person meant scrolling past fifteen rows of tasks that did not.

Ordered by **what waiting costs**, which is a better key than recency or count:

1. **The run is stopped on you.** A worker parked on a human approval, holding its slot; a stalled task in a run that is otherwise finished. Throughput is being lost right now.
2. **The run continues and is quietly wrong.** `unsupervised`, `drift`, `degraded`. Arguably worse than the first tier, because the run looks healthy and nothing else will ever say otherwise.
3. **Nothing is burning.** Open anomaly classes, agent proposals awaiting a word, a completed run ready for signoff. Real decisions with no clock on them.

**Signoff-ready is a decision item**, which narrows what all-clear can mean. A finished run with nothing else wrong is not green — it is waiting on the most consequential decision in the whole workflow. Genuine all-clear is only three states: *running, nothing needs you*; *paused, nothing needs you*; and *signed off*, which is terminal.

### 4.2 Where the run stands

**Every task, one row.** No collapsing and no elision — the manifests this is built for are dozens of tasks rather than hundreds, and a rule that hides rows past some width would be answering a problem nobody has reported.

With the table, the **concurrency settings in force and how long each has held** — tasks in flight, processes, sample concurrency per task. Tuning is a judgement about a setting that has been given time to show its effect, so the age of a setting is part of the setting.

Below the table, a small block of **live-only** figures, for tasks currently running: refusals and HTTP retries among them, and the fleet's memory and CPU. These come from the control channel and the process table, neither of which persists — inspect records neither refusals nor HTTP retries in an eval log, so these describe *what is running now* and nothing else. That has to be said in the labelling, because a total that **falls** as tasks complete reads as a problem resolving itself when it is only work finishing. When nothing is running the block is absent, and the measured startup bound from the capture takes its place — a ceiling is the useful figure before there is an actual to report, and the actual is the useful figure once there is.

### 4.3 What happened

**Complete, and deliberately not a delta.** This section says everything material since the workspace began, not everything since some mark — which keeps the summary **stateless**, and leaves the collection cursor with one job (§2.2) instead of two.

Completeness is affordable only because admission is narrow. The test:

> **It happened *to* the run rather than *as* the run.**

A task completing is the run happening. A task retried because its worker died is something that happened to it. And the corollary that bounds the rest: anything that can occur hundreds of times a night appears as a **count with its shape**, never as instances.

There is a second boundary already drawn, in [workflow.md](workflow.md) *The journal records observations, not only decisions*: a failed tend, a spawn error, a sync timeout are records of the runner working or not, and they belong to Steward's operational log. This section is the **journal**, filtered — never `steward.log`.

What qualifies:

- **Closed decisions.** Every item that left §4.1, with who decided and why. This is why `ack` **moves** an item rather than hiding it: *somebody dealt with this at 2am, and here is their reason* is precisely what a 6am reader has no other way to learn, and it used to be discarded.
- **Things done to a task** — worker crashes and respawns (distinct from a task retried because its log said `error`: a process dying under a healthy task is a different diagnosis), logs recovered from an interrupted write, and as counts, the sample retries `retry_on_error` performed and the errored samples that survived them.
- **Things done to the run** — logs archived with origin and reason, since *Steward never deletes an eval log* is a headline guarantee and this is where it shows its work; pauses and resumes; timer changes; a definition edit accepted by a relaunch.
- **Things done about load** — rate-limit episodes per model with when and how long, and connection downshifts that did not correct themselves.

Later steps add scan coverage and which results look worth a look, and the agent's own judgement calls once there is an agent. Unwritten per-task sections of `analysis.md` belong with those, and as **items owned by the agent** rather than as history — unwritten is work outstanding, not something that happened.

**Two sources, two temporal characters, and only one of them grows.** Counts derive from the current log directory, which is what observation already reads — archived work is not in it, so they can never report errors from results that no longer exist. Events come from the append-only journal and are genuinely lifetime; that is the only unbounded axis, and the admission test is what bounds it.

### 4.4 The schema

**One structured object rendered as markdown**, not prose an agent has to parse and not a table it has to reformat — for the reason in the next section. Three renderings of one model: aligned columns for a terminal, real tables for relaying, JSON for a program.

**The envelope is fixed now; the fields arrive with their producers.** Sections gain content at the steps that produce it — blocked work at the parked-worker step, rate-limit episodes and connection history at the tuning step, anomaly classes at the anomaly step, scans later still. Declaring the whole shape up front would ship sections that are permanently empty, and Steward's rule elsewhere is that a key which parses and does nothing is a lie about what exists. The rule is weaker here than it is for `_steward.yaml` — that is *input*, where a dead key misleads about what you control, and this is *output*, where an empty section is merely noise — but the conclusion is the same, and it costs nothing to fix the envelope while letting the contents grow. Versioned, with unrecognised keys read as data rather than as errors, exactly as the journal treats an unrecognised event type.

## 5. Render the summary; do not replace it

*"How is it going", "what's the latest", "any update"* are requests for **the snapshot**, not for the agent's reading of it. The rule is worth stating as flatly as possible because the temptation is constant and the failure is invisible:

> Run `steward status --format md` and render what it printed — every section, in its order, in full, with nothing above it. As markdown, not inside a code fence, because it is a document with tables meant to be read rendered.

**`--format md` rather than plain `status`**, because there are three renderings of one model and only one of them survives relaying. The default is aligned monospace columns for a terminal, which collapse into a line of words when rendered as markdown; `--format md` is the same content as real tables. Reading `status.md` instead is the wrong move for the same question: `status` is read-only and never writes that file, so it carries the last *tend*'s snapshot and can be a full interval stale while claiming to be current.

**Brevity is not the failure; substitution is.** The detail *is* the answer, and a summary replaces the reader's judgement with the agent's at the exact moment they were trying to form their own. They asked to see the run.

Analysis is **held by default** and goes below the snapshot, marked as the agent's, only when it is both important and not obvious from the snapshot itself: an arm that has stopped, a climbing retry count, a connection downshift that is not self-correcting, a scan finding, anything in the anomaly list that is growing. Everything else — a comparison against a baseline, a trend across two snapshots, a caveat about a gauge — is real, useful, and waits to be asked for.

This applies to a wake-up at 3am exactly as it applies to a question asked directly.

## 6. What the agent may do without asking

The bounds are already set by decisions in other documents; collected here because an agent needs them in one place.

**Freely, as standing work:**

- call `tend`, spawn, reap, and converge toward the manifest
- hold the ramp (`steward ramp hold`) on its own judgement and resume it, and retune a worker *downward* through `inspect ctl` when containing an incident — the climb itself is `tend`'s ([scheduling.md](scheduling.md) §3.5)
- group classes into proposals (`steward propose`), and investigate anything — `steward investigate --note` records that a class is being worked, which is what stops the next session re-proposing what this one was mid-way through
- write `analysis.md`
- notify

**Never, under any circumstances:**

- **`steward signoff`.** It is a human attestation; an agent running it is the one thing that would make the record meaningless.
- **Edit the definition.** It is the human's statement of what is being measured, and an agent's edit to it is afterwards indistinguishable from theirs. Read it, run it, and raise anything that looks wrong as a *question* rather than as a change. This includes adding a comment explaining what the eval set is for — an agent handed a definition was never told why it exists, and a plausible rationale in the file is worse than none, because a later reader cannot tell it from the author's.
- **Move or delete a log.** Not even an empty cancelled one, and not into a folder named for discards. Resume matches logs where they are.
- **Answer a parked approval or `ask_user`.** A worker that has stopped for a human decision ([execution.md](execution.md), *The parked worker*) is asking whether the eval may do something, and answering is authority over what is being measured — the same authority that puts editing the definition on this list. The distinction from the ruling below is real and worth holding: a ruling decides whether to *re-run* work that already happened, while an approval decides what happens *next*, inside a sample, and leaves no record anyone reviews afterwards. Surface it, name the worker, print the command that attaches to it, notify — and wait.

**Freely, and this is the one addition autonomy does not have to be argued for:** `steward raise`. Putting a human-owned item in front of the human is the agent doing its job, not deciding anything — it closes nothing, the item stays in the summary's decisions until a person rules, and all it changes is that the agent stops being shown work it has already handed off (§2.2). An agent that could not do this unasked would either re-surface the same drift at every collection all night or start acking things that are not its to ack.

**As far as a pre-authorization goes, and no further:** intervening in a sample that is still running. A sample stuck on a tool call that will not return ([execution.md](execution.md), *The stuck sample*) has a ladder behind it — cancel the call, cancel the sample, requeue it — and each rung decides more of what the eval measured than the one below. `_steward.yaml` may admit the first rung as a class, and the agent then acts on it the way it acts on any pre-authorized re-run: it is executing a decision the human already made and recorded, not making one. The rungs above it are the human's unless the file names them too, and nothing is pre-authorized by default. Two habits go with this. **Never send the same directive twice** — a cancel that was delivered and not heeded says so, and the answer to *it did not stop* is a person, not a repeat. And **journal what was done and why**, because an intervention that changes a live sample is precisely the autonomy a reader will want to audit afterwards, and it leaves no other trace: unlike a ruling, there is no landed log recording that anything happened.

**Ask first, then do:** `steward ack`. Acknowledging **disposes** of an item — it leaves the decisions section, the verdict, and the agent's queue, and reappears under *what happened* carrying who decided and why. That last part is deliberate and is what makes the act safe to record: the item leaves the surface without leaving the record, so *somebody dealt with this at 2am, and here is their reason* survives for the reader who arrives at six. But an agent that may dispose of items unasked can silence its own attention list, which is the gate on its own autonomy rather than a chore to tidy. Ask, then run it with `--by human` and the person's reason. The exception is narrow and worth naming: an item the agent **investigated and resolved itself** — a file that turned out to be a partial upload, say — is the agent's own disposal, recorded `--by agent`, and needs nobody's permission. The test is whether anyone but the agent would have to know. An anomaly is never `ack`ed at all — its window closes through `steward rule`, and the dismissal that says *looked, nothing here* is a ruling with a record (`--disposition dismiss`), never a wave-past.

**Ask first, then do, and the same shape:** writing `_steward.yaml`. The file is the human's standing rules, and what the earlier rule protected — that reading it tells you what a *person* decided — is protected by the approval rather than by the keystroke ([workflow.md](workflow.md) §5.4). So propose the exact text, and write it once they have answered. A proposal is a wording rather than a question: *shall I add a policy about timeouts?* cannot be answered without the human composing the rule themselves, which is the work the proposal was meant to save. Two things follow. `preauthorized:` needs a yes to the **pattern as written**, because it is the one key that widens what happens with nobody present — agreeing that timeouts are usually worth re-running is not agreeing to `error:*Timeout*`. And say what was written and leave it visible: the file is tracked in the workspace's repository, and unlike a ruling it carries no `--by`, so `git diff` is the whole of the provenance.

**Only with a ruling** — every re-run past the automatic tier, at sample or task level ([execution.md](execution.md), *Two tiers, not three*). A ruling is recorded with `steward rule`, and its `--by` names the person who decided, never a role: the agent relaying a decision is the ordinary path, and the record still says whose decision it was. `_steward.yaml` may grant a ruling in advance, in which case acting on it is executing a decision already made.

**Stop and ask** on: a smoke gate that still fails after two attempts; identity or resume errors, where the manifest and the logs disagree about what the eval *is*; numbers that fail sanity; anything requiring a destructive or irreversible action; persistent credential failures; or a non-converging loop — the same task failing across three launches, or logs accumulating for one task. A stop is not a teardown: healthy work keeps running, and the journal gets a final entry with state and a hypothesis.

## 7. Notification is the only channel that reaches an absent human

Two rules, and the first is the one most likely to be skipped.

**A question the agent is blocked on is the most important thing it will send, and the easiest to leave in the conversation.** Asking in the conversation reaches whoever is reading the conversation, and the entire premise of an overnight run is that nobody is. So a blocked question notifies *at the moment it is asked*, naming the decision needed rather than describing the situation — otherwise a pending question is indistinguishable from work in progress, for as long as the human stays away.

This is the answer to [workflow.md](workflow.md) open question 2 from the other side. The reply path is still "start a session and the agent reads the open anomalies", which is unchanged and still costs a terminal; what changes is that the human finds out there is something to reply to.

**Before the first worker starts, silence is total.** The mechanical notifications all describe a running fleet, so a launch blocked at the smoke gate has no tend, no `status.md`, and nothing posting at all — a failed gate does not announce itself. That is the one window where a stop is completely silent, and it is precisely when someone has launched and walked away. Anything that stops here notifies explicitly.

**Four of the six notification kinds are not the agent's to send.** `attention` and `stopped` carry judgement and are its own; `progress`, `clear`, `gate`, and `signed_off` are latched, terminal, or read off state the agent does not own, and are Steward's alone ([workflow.md](workflow.md), *Six kinds*). A blocked question is always `stopped` — that is the kind's entire reason for existing.

**Post freely otherwise.** The cost of an unnecessary `attention` is a line in a channel; the cost of a skipped `stopped` is a night spent waiting for an answer nobody knew was wanted. An unconfigured channel makes `steward notify` *fail* rather than succeed quietly, which is the one place this differs from Steward's own posts: an agent that believed it had escalated and had not is worse off than one told it could not. Say so in the conversation and in the writeup; the remedy is a `notification` setting and it is the human's to add.

## 8. Context is the real budget

Roughly sixty tends a night, plus evidence on every anomaly opened, plus whatever investigation costs. Three rules keep it bounded:

- **Never read a full eval log.** `header_only=True` for status and counts; `read_eval_log_sample_summaries` or `samples_df` for per-sample data. A full read pulls the whole archive for what the header already has. Steward's own detection already went as deep as classification needs — one summaries read per log, one capped read per errored sample, under `instances.py`'s stated discipline — and the evidence travels in the journal and the status, so an investigation starts from the evidence rather than by re-reading logs.
- **Transcript analysis goes through a scan**, never a raw log read. Live diagnosis of a *running* sample over the control channel is different and is fine — that is diagnosis, not analysis.
- **Narrow an anomaly before opening it.** An investigation that means reading a five-hundred-sample transcript needs scoping first; the cost of a scan is wall-clock and tokens across many logs, and the cost of an investigation is context on a few ([workflow.md](workflow.md), *Scanning collects; investigation digs*).

## 9. What `steward runbook` says

The runbook ships with the package and is mechanics, not policy — [workflow.md](workflow.md) draws that line and this document does not move it. It is closer to a prompt than to documentation, and its content is this document reduced to instructions: the cadence and how to arm it, cold pickup, the rendering discipline, the bounds above, when to notify, and the hard stops.

One rule belongs there that appears nowhere else, because it is about how an agent reads tool output rather than about Steward: **trust the artifact, not the exit code.** Every gate here has an artifact that says what happened — the manifest delta, the smoke digest, the log itself, the anomaly count. A clean exit means a process ended, which is not the same as the work having succeeded, and under `fail_on_error=False` the gap between those two is the normal case rather than an edge one.

## 10. Open questions

1. **~~The tend summary schema.~~** *Settled in §4.* Three sections ordered by what a person has to decide, every task shown, a live-only block for what is running, and a *what happened* section that is complete rather than a delta. The encoding question dissolved rather than being answered: the **envelope** is fixed and versioned now, unrecognised keys are data, and fields arrive at the steps that produce them — so there is no single moment at which the whole shape has to be got right.

2. **~~Which timer, and what `launch` does when it cannot arm one.~~** *Settled by [plan.md](plan.md) step 15, and then re-settled the other way.* Three backends in preference order — launchd, systemd `--user`, cron — and **arming fails when none of them can run the entry**. The step first shipped a fourth, a detached process of Steward's own that was always usable, on the reasoning that it dissolved the harder half of the question: with it, there is no *none can be armed* state on a machine that can start a process. Review found the cost of that dissolution, and it was too high — an always-usable backend made the failure branch unreachable, so a bare container was handed a timer that died with its terminal instead of being told what it lacked, and it needed a snapshot of the arming environment to have credentials at all, which is a second and permanently shadowing answer to a question every later step has to ask once. **No timer at all is still a choice somebody makes rather than an outcome a bare container arrives at** — it is just made by a person reading a refusal rather than by a fallback nobody chose. Deliberately hand-driving a short run stays available (`--no-timer` at step 16, `steward timer disarm` at any point) and is not silent: it raises an `unsupervised` item that the operator acknowledges once.

3. **What surfaces an agent's mistake.** The agent groups classes into proposals and a human agrees in one word. Per-class ruling records make a bad grouping *recoverable* — that was deliberate — but nothing brings it to anyone's attention, and a wrong grouping re-runs samples that did not earn it. There is no review path.

4. **How the runbook is tested.** The runbook plus the tend loop is a prompt artifact whose quality is most of the product, and nothing in the design says how it is exercised. Belongs with [testing.md](testing.md) when that exists.
