# The agent

**Status: draft. The responsibilities, the relationship to the timer, the reporting discipline, and the bounds are settled. The tend summary's exact schema and the runbook's final text are not.**

Every other document in this set describes machinery. This one describes the only thing that operates it.

[execution.md](execution.md) establishes that the reconcile core is a pure function driven from outside, and that a timer guarantees the mechanical half of that. [workflow.md](workflow.md) establishes that the human's own surface is three commands and a conversation. Everything between those two facts is the agent, and it has accumulated more responsibility than any single document has acknowledged.

## 1. Four jobs, all of them judgement

The design's division of labour is consistent — mechanical work stays in `tend`, judgement goes to the agent — and applying it repeatedly has produced an agent that does four distinct things. Driving is deliberately **not** among them: an earlier draft had the agent scheduling `tend`, and that job now belongs to a timer for reasons the next section gives.

| | job | what happens without it |
|---|---|---|
| **tuner** | raise `max_samples` while rate limits stay absent | the run stays at 40 concurrent all night ([scheduling.md](scheduling.md), *The signal is mechanical; the decision is not*) |
| **grouper** | collapse computed error classes into proposals a human can answer | twelve questions where there were two causes ([workflow.md](workflow.md), *Three levels*) |
| **investigator** | judge scan results, which carry no verdict of their own | the most valuable anomaly source produces rows nobody reads |
| **author** | write `scanning.md` and `analysis.md` | the run leaves logs and a journal, and nothing that says what happened |

All four fail the same way: **with no agent in session none of them happens.** Each was individually argued as an acceptable cost, and their sum is what makes the separation below matter — because they are all judgement, an absent agent leaves a run that is *converging but undecided*, which is a tolerable overnight state. It stops being tolerable the moment the mechanical half depends on the agent too.

## 2. The agent does not guarantee the cadence — a timer does

An earlier draft made the agent the scheduler, and the reason it cannot be is structural rather than a matter of discipline:

> A supervising agent is turn-based. It acts when the human speaks, when a background job finishes, or when a scheduled wake-up fires — and never in between. So an agent-scheduled run is silent by construction whenever no agent is in session, and **silence is indistinguishable from a healthy run**.

Combine that with the four jobs above and the failure compounds: an absent agent would stop the fleet *and* leave nothing to say so. So the mechanical half is guaranteed by a timer — a system scheduler where one exists, Steward's own detached ticker otherwise, Hawk's in-pod loop in a pod — armed by `launch` rather than remembered ([execution.md](execution.md), *Drivers, one core*).

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

### 2.2 Tend output is a queue the agent drains

`tend` emits a structured summary whether or not anyone is listening, and most of them run unread. So the summaries are a **queue** that an occasionally-connected agent drains: it takes the unacknowledged ones in order, acts on them, and acknowledges its position, which lands in the journal like any other event.

**The queue is what a snapshot cannot replace, and that is the crux.** `status.md` says what is true *now*; it cannot say that an anomaly class grew from three instances to forty overnight, that a task died at 1am and was respawned, or that a rate-limit episode came and went at 3. That series is exactly what tuning and grouping decisions need, and a fresh session has no memory to reconstruct it from. Without a mark, an arriving agent has no "familiar ground" to read backwards to — it either re-reads everything or guesses where to start.

Reading and acknowledging are **separate steps**, deliberately — the ordinary at-least-once shape. Reading is cheap, idempotent, and side-effect-free: any number of readers, no claim. Acknowledging asserts the work is done. An agent that dies mid-processing has acknowledged nothing, so the next one re-reads the same items, which is the right failure direction: redelivery costs context, and a dropped item costs a night.

Acknowledgment is a **position, not per-item**, because the journal is ordered and an agent works through it in order. One number, not a set.

A second thing falls out for free — a quantity nothing else measures:

> **Open anomalies measure the human's backlog. Uncollected tends measure the agent's.**

A third quantity sits with the first: **parked workers measure the human's backlog in the present tense.** An open anomaly is work already done that nobody has ruled on; a parked worker is work that has stopped until someone answers. Both wait on a person, but only the second costs throughput while it waits, which is why it belongs in the summary as blocked work rather than in `anomalies.md`.

They are orthogonal, and both matter. Zero open anomalies with six uncollected tends means nothing has gone wrong and nobody has looked. Zero uncollected with three open means someone looked and the decisions are with the human. Only the second is a healthy overnight state, and before collection existed the two were indistinguishable.

It also answers the tend summary's audience problem concretely: the delta an arriving agent needs is *since the last collection*, not since the last tend, and now that is a fact rather than an inference.

**Whether a growing backlog should notify is a policy question, not a mechanical one.** In the transient posture a long uncollected stretch is normal — that is what an unattended night looks like. It becomes a problem only against an expectation the human holds, so it belongs in `_steward.md`'s front matter ("I expect collection at least every four hours") rather than in a threshold Steward invents. What Steward does unconditionally is *report* it: `status.md` carries both ages, and a workspace whose last tend is four minutes old and whose last collection is six hours old is describing its own situation accurately.

### 2.3 What the agent still owes

Two things the timer cannot do, both of which belong in the runbook:

- **Check on attach, before anything else.** A session that starts by answering the human's question without first reading what accumulated is answering from a stale picture.
- **Confirm supervision is actually running**, once, early. The timer cannot detect its own absence, so two cheap signals stand in: the journal should show tend events at roughly the expected interval, and `status.md` states its own age. A workspace whose newest tend is four hours old in a ten-minute cadence is unsupervised, and nothing else will say so.

## 3. Cold pickup is a procedure, not a property

The design repeatedly claims a third party can pick a workspace up cold. That claim is a workflow, it has never been written down, and it runs far more often than the phrase suggests — **at every session boundary, several times a night**, not only when someone new arrives.

```
AGENTS.md          →  you are tending a run; read the runbook
steward runbook    →  the mechanics, shipped with the package
_steward.md        →  this human's standing rules
steward status     →  what is true right now
open anomalies     →  what is undecided, with precedent attached
analysis.md        →  what has been found so far and what it meant
```

Two properties make this work rather than merely sound plausible. Everything an agent needs is **in the workspace**, so nothing depends on conversation history that a new session does not have. And every step is a file or a command, so the procedure is testable — which matters, because a claim exercised several times a night should not be exercised for the first time in production.

The last two lines are why the journal carries observations and why `analysis.md` exists at all. Without them a fresh agent inherits a list of open items and no idea which are getting worse.

## 4. The tend summary

The most-executed interface in the system, and a real constraint rather than a formatting question. Note the audience shifted once the timer took over the cadence: most tends are now read by *nobody* at the time they run, and are read later in bulk by an agent that has just attached. That makes the summary a **record** as much as a report, and it is the reason the last row below exists — an agent arriving after six unattended tends needs the delta since anyone last looked, not since the last one ran.

What it must carry, given what the five jobs need:

| for | content |
|---|---|
| driving | tasks by state, what was spawned and reaped this tend, anything unaccounted for |
| tuning | per-model rate-limit episodes since the last tend, current `max_samples` per worker, how long each has held |
| grouping | open anomaly classes with instance counts and their trend since last tend |
| investigating | which scan passes landed, and which results look worth a look |
| authoring | which per-task sections of `scanning.md` and `analysis.md` are unwritten |
| the human | overall phase, and how old this information is |

The schema itself is unsettled (open question 1). What is settled is that it is **one structured object rendered as markdown**, not prose the agent has to parse and not a table the agent has to reformat — for the reason in the next section.

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
- raise `max_samples` inside the envelope while rate limits are absent, and pull back when they are not
- group classes into proposals, and investigate anything
- write `scanning.md` and `analysis.md`
- notify

**Never, under any circumstances:**

- **`steward signoff`.** It is a human attestation; an agent running it is the one thing that would make the record meaningless.
- **Edit the definition.** It is the human's statement of what is being measured, and an agent's edit to it is afterwards indistinguishable from theirs. Read it, run it, and raise anything that looks wrong as a *question* rather than as a change. This includes adding a comment explaining what the eval set is for — an agent handed a definition was never told why it exists, and a plausible rationale in the file is worse than none, because a later reader cannot tell it from the author's.
- **Write `_steward.md`.** Steward proposes; the human writes.
- **Move or delete a log.** Not even an empty cancelled one, and not into a folder named for discards. Resume matches logs where they are.
- **Answer a parked approval or `ask_user`.** A worker that has stopped for a human decision ([execution.md](execution.md), *The parked worker*) is asking whether the eval may do something, and answering is authority over what is being measured — the same authority that puts editing the definition on this list. The distinction from the ruling below is real and worth holding: a ruling decides whether to *re-run* work that already happened, while an approval decides what happens *next*, inside a sample, and leaves no record anyone reviews afterwards. Surface it, name the worker, print the command that attaches to it, notify — and wait.

**Ask first, then do:** `steward ack`. Acknowledging an item takes it out of the tend summary, the verdict, `status.md`, and the channel — so an agent that may do it unasked can silence its own attention list, which is the gate on its own autonomy rather than a chore to tidy. Ask, then run it with `--by human` and the person's reason. The exception is narrow and worth naming: an item the agent **investigated and resolved itself** — a file that turned out to be a partial upload, say — is the agent's own disposal, recorded `--by agent`, and needs nobody's permission. The test is whether anyone but the agent would have to know.

**Only with a ruling** — every re-run past the automatic tier, at sample or task level ([execution.md](execution.md), *Two tiers, not three*). `_steward.md` may grant that ruling in advance, in which case acting on it is executing a decision already made.

**Stop and ask** on: a smoke gate that still fails after two attempts; identity or resume errors, where the manifest and the logs disagree about what the eval *is*; numbers that fail sanity; anything requiring a destructive or irreversible action; persistent credential failures; or a non-converging loop — the same task failing across three launches, or logs accumulating for one task. A stop is not a teardown: healthy work keeps running, and the journal gets a final entry with state and a hypothesis.

## 7. Notification is the only channel that reaches an absent human

Two rules, and the first is the one most likely to be skipped.

**A question the agent is blocked on is the most important thing it will send, and the easiest to leave in the conversation.** Asking in the conversation reaches whoever is reading the conversation, and the entire premise of an overnight run is that nobody is. So a blocked question notifies *at the moment it is asked*, naming the decision needed rather than describing the situation — otherwise a pending question is indistinguishable from work in progress, for as long as the human stays away.

This is the answer to [workflow.md](workflow.md) open question 2 from the other side. The reply path is still "start a session and the agent reads the open anomalies", which is unchanged and still costs a terminal; what changes is that the human finds out there is something to reply to.

**Before the first worker starts, silence is total.** The mechanical notifications all describe a running fleet, so a launch blocked at the smoke gate has no tend, no `status.md`, and nothing posting at all — a failed gate does not announce itself. That is the one window where a stop is completely silent, and it is precisely when someone has launched and walked away. Anything that stops here notifies explicitly.

**Two of the four notification kinds are not the agent's to send.** `attention` and `stopped` carry judgement and are its own; `gate` and `complete` are terminal, made once, and Steward's alone ([workflow.md](workflow.md), *Four kinds*). A blocked question is always `stopped` — that is the kind's entire reason for existing.

**Post freely otherwise.** An unconfigured channel makes `notify()` a silent no-op, so there is nothing to check and nothing accumulates. The cost of an unnecessary `attention` is a line in a channel; the cost of a skipped `stopped` is a night spent waiting for an answer nobody knew was wanted.

## 8. Context is the real budget

Roughly sixty tends a night, plus evidence on every anomaly opened, plus whatever investigation costs. Three rules keep it bounded:

- **Never read a full eval log.** `header_only=True` for status and counts; `read_eval_log_sample_summaries` or `samples_df` for per-sample data. A full read pulls the whole archive for what the header already has.
- **Transcript analysis goes through a scan**, never a raw log read. Live diagnosis of a *running* sample over the control channel is different and is fine — that is diagnosis, not analysis.
- **Narrow an anomaly before opening it.** An investigation that means reading a five-hundred-sample transcript needs scoping first; the cost of a scan is wall-clock and tokens across many logs, and the cost of an investigation is context on a few ([workflow.md](workflow.md), *Scanning collects; investigation digs*).

## 9. What `steward runbook` says

The runbook ships with the package and is mechanics, not policy — [workflow.md](workflow.md) draws that line and this document does not move it. It is closer to a prompt than to documentation, and its content is this document reduced to instructions: the cadence and how to arm it, cold pickup, the rendering discipline, the bounds above, when to notify, and the hard stops.

One rule belongs there that appears nowhere else, because it is about how an agent reads tool output rather than about Steward: **trust the artifact, not the exit code.** Every gate here has an artifact that says what happened — the manifest delta, the smoke digest, the log itself, the anomaly count. A clean exit means a process ended, which is not the same as the work having succeeded, and under `fail_on_error=False` the gap between those two is the normal case rather than an edge one.

## 10. Open questions

1. **The tend summary schema.** The contents are settled above; the encoding is not. It is read sixty times a night, so its size is a real cost, and the balance between a compact structured form and one legible when rendered directly to a human is unresolved.

2. **Which timer, and what `launch` does when it cannot arm one.** [execution.md](execution.md) settles that `launch` arms a timer and names the mechanism. What it does when no system scheduler is usable *and* the ticker cannot be spawned is unresolved: proceed unsupervised with a loud warning, or refuse. Refusing is more attractive than it sounds, since an unsupervised run is the failure the arrangement exists to prevent — but it would also block the case of someone deliberately driving a short run by hand.

3. **What surfaces an agent's mistake.** The agent groups classes into proposals and a human agrees in one word. Per-class ruling records make a bad grouping *recoverable* — that was deliberate — but nothing brings it to anyone's attention, and a wrong grouping re-runs samples that did not earn it. There is no review path.

4. **How the runbook is tested.** The runbook plus the tend loop is a prompt artifact whose quality is most of the product, and nothing in the design says how it is exercised. Belongs with [testing.md](testing.md) when that exists.
