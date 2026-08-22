# Workflow

**Status: sketch. Actively being figured out — expect whole sections to be wrong.**

[configuration.md](configuration.md) covers how a definition becomes a manifest. [execution.md](execution.md) covers how a manifest becomes running processes. This document is about the layer above both: what a person actually *does* with Steward, from starting a sweep to trusting its results.

## The premise, restated

Three facts drive every decision here:

- **The human is absent.** If someone were going to sit and watch, they could have run `eval_set()` themselves. Steward exists for the run you start and walk away from.
- **The agent is intermittent.** It works in sessions. It hits context limits, gets interrupted, and is not invoked again until morning.
- **The run is continuous.** Workers execute whether or not anyone is looking.

Everything below follows from the mismatch between those three clocks. The directory is the only thing present the whole time, so the directory has to carry the state, the instructions, and the record.

## The commands

| command | who calls it | what it does |
|---|---|---|
| `steward init [--type evalset\|flow] [--no-git]` | human | Scaffold the workspace: bootstrap `AGENTS.md`, a starter definition, `policy.md`, a repository and `.gitignore`. |
| `steward runbook` | agent | Emit the current mechanics — how to tend, what never to do. Ships with the package so it cannot go stale. |
| `steward launch [--smoke] [--max-spend N]` | agent | Claim the run, capture the manifest, spawn the first workers, **return**. `--smoke` runs a bounded rehearsal first — see *Smoke first*. |
| `steward tend` | agent, ~q10m | One turn of the loop: reconcile, spawn, reap, requeue, rewrite `status.md`, append to the journal. Never blocks. |
| `steward status` | either | `tend --dry-run` — current state plus a preview of what the next tend would do. Read-only. |
| `steward notify` | agent | Send the human a message that carries judgement, through Inspect's notification channel. |
| `steward signoff` | **human only** | Attest that the results are accepted. Terminal journal entry; records who, when, and the exceptions accepted. |
| `steward pause` / `steward stop` | either | Stop scheduling new work, or end the run. Neither is "leaving a view". |

Two things the table is meant to make obvious. **Almost everything is agent-facing**: the human's own surface is `init`, `signoff`, and asking the agent questions in prose. And **`signoff` is the one command an agent must never run** — it is a human attestation, and the runbook says so plainly.

Deliberately absent: `steward journal` (precedent travels with the anomaly instead), `steward tui` (see *Do we need a TUI?*), `steward note` (unproven), and `steward unclaim` (unnecessary — see below). `steward tasks` exists for diagnosing enumeration but is not part of the workflow: `launch` captures the manifest anyway and `status` reports what is running. The pre-flight "what would this cost" question is answered by `launch --smoke`, which measures it rather than guessing.

### What `pause` actually pauses

Two things get called pausing, and only one of them is cheap:

- **Stop scheduling.** The next tend spawns nothing; workers already running finish normally. This is entirely Steward-side — it needs no control channel at all, just a flag the reconcile honours — and it is what almost everyone means by "pause the run", because the money being spent is mostly on work not yet started.
- **Suspend work in flight.** This needs Inspect's control channel, which does have pause/resume latches at process, task, and model scope. It is implementable — the journal and discovery directory give Steward every live worker's endpoint — but it is N calls that can partially fail, and a paused worker **still holds its process, its slot, and any sandbox containers it opened**. Pausing is not free the way stopping is.

So `steward pause` means the first. The second is worth having only in the specific shape the recovery design already identifies: when a provider is down, **model-scoped pause** is the correct response, applied to live workers as a tier-2 action rather than as a user-facing verb. Process-wide suspension of in-flight work is rarely what anyone actually wants.

## The shape, end to end

```
  human                    agent                     steward                run
  ─────                    ─────                     ───────                ───

  steward init  ──────────────────────────────────►  scaffold dir
       │
       │ "run the sonnet/haiku sweep overnight"
       └──────────────────►  reads AGENTS.md
                             steward runbook  ─────► mechanics
                             reads policy.md  ─────► this human's standing rules
                             steward launch --smoke ► 2 samples/task, 15m cap ► ●
                                  │                  (.steward/smoke/, local)
                             steward launch  ───────► claim, manifest,
                                                      spawn first workers ──► ●●●
                             schedules tend q10m
                                  │
       ┌──────────────────────────┤ steward tend ──► reconcile, spawn, reap ──► ●●●
       │                          │      ...                                     │
       │  ◄── steward notify ─────┤ "47 samples hit the same rate limit"          │
       │                          │                                              │
       └── "yes, invalidate" ────►│ records ruling, re-runs ────────────────────► ●●
                                  │      ...
                                  │ steward tend ──► all tasks done, scan drains
                                  │      ...        scan finds a bad grader ──► anomaly
       │  ◄── steward notify ─────┤ "resolved: 998/1000, 2 accepted-as-errored"
       │
       └── steward signoff ──────────────────────► attested, terminal journal entry

  steward status / inspect view
```

## `steward init` — the deliverable is a directory

The output of `init` is a workspace that a human and an agent co-inhabit, and that a *third* party can pick up cold. That framing is doing real work: it means everything important has to be written down rather than held in someone's session.

```
my-sweep/
  AGENTS.md          # authored — bootstrap: "you are tending a run; read the runbook"
  CLAUDE.md          # authored — symlink to AGENTS.md
  policy.md          # authored — this human's standing rules
  evalset.py         # authored — scaffolded by `init --type evalset` (or flow)

  journal.jsonl      # DURABLE — append-only event log; the source of truth
  status.md          # rendered by every tend
  logs/              # the flat eval-set log directory

  .steward/          # DISPOSABLE — claim, manifest, inflight.jsonl, caches
```

`init --type evalset|flow` scaffolds a starter definition, so a new directory is runnable rather than a set of empty conventions. It should still accept an existing definition and merely wrap it — the contract is deliberately "any program culminating in one `eval_set()` call", and `init` should not imply otherwise.

### Three categories, and the one that matters

The obvious split — "human-readable top level, machine-owned `.steward/`" — conflates *who writes a file* with *whether it can be recovered*, and rulings are the case that breaks it. A ruling and its reasoning exist nowhere else: not in the logs, not in the manifest, not derivable from anything. So the categories are:

| category | examples | if you delete it |
|---|---|---|
| **authored** | `policy.md`, `AGENTS.md`, the definition | the human's own work is gone |
| **durable machine state** | `journal.jsonl` | the audit trail is gone |
| **disposable machine state** | everything in `.steward/`, `status.md` | rebuilt on the next tend |

`journal.jsonl` therefore sits at the top level, beside the authored files, where a file nobody can regenerate belongs. Nothing in `.steward/` is irreplaceable, which makes it safe to delete — a property worth more than a tidy listing.

**Losing `.steward/` fails in the safe direction.** Anomalies re-derive from the log directory, since the errored samples are right there; the manifest is re-captured from the definition; in-flight records are rebuilt from the logs. Nothing is lost but time, because the rulings — the part that could not be recovered — are not in there.

The usual reason to delete a state directory — a stuck claim — does not arise here, because the claim is held only for the seconds a tend runs and one older than a generous tend timeout is reaped as stale (execution.md, *The reconcile core and its drivers*). There is deliberately no `steward unclaim`: it would be a command for a failure mode the short-lived claim already removed.

### Why there is no `journal.md`

The obvious companion to the log is a rendered markdown version, committed so the record is browsable. It was considered and dropped, because what it buys is narrow and what it costs is not.

What it buys is **readability for someone who cannot run a command** — a reviewer in a pull request or browsing the repo. That case is real but speculative; nothing yet says anyone will review an eval journal in a browser. Everything else is already covered: `jq -r '.reasoning' journal.jsonl` serves a technical reader with no Steward installed, and append-only JSONL **diffs cleanly** — every commit is pure additions with no context churn, so a diff shows exactly what happened between two points even though each line reads badly.

What it costs is a standing obligation: a generated-file header, a never-hand-edit rule, a gitignore decision, "which of these two do I read", and the render code itself — grouping, evidence summarization, formatting — which is real implementation surface and a real source of bugs.

**The decisive argument is reversibility.** Adding the file later is nearly free; withdrawing an artifact people have linked to is not. The trigger for revisiting is concrete rather than a guess: someone actually asking to read a journal in a browser.

In git, `.steward/` is ignored and `journal.jsonl` is committed, so cloning the directory carries the account of what happened without dragging along one machine's claim and in-flight state.

There is also no `steward journal` command, for a related reason. What an agent needs from the record is not a rendering but a **query** — "have we ruled on this class before?" — and that should not be something it must remember to ask. See *Precedent travels with the anomaly*.

### The alternative that looked best and is not

A single markdown file with YAML front-matter per entry — structured fields above, narrative below — is the most attractive rejected option, and it wins on two counts worth naming. Drift becomes structurally impossible rather than merely unlikely, and prose sits **with** the data it describes instead of at a distance from it. It would also make the journal co-writable: prose is inert to the fold, so a human could annotate an entry with no risk of breaking anything.

It fails on one property, and the failure is not recoverable by care: **line-delimited formats fail locally; block-delimited formats fail globally.** A corrupt JSONL line costs one entry, is detectable, and can be reported. A missing or mistyped `---` does not cost one entry — it merges two, or swallows the remainder of the file, and a crash mid-append leaves an unterminated block that absorbs everything written after it. For a file whose fold decides whether Steward re-asks a human or calls a run finished, a failure that cascades past its own record is disqualifying in a way a local one is not.

(YAML's coercion of `no` and version-like strings is a lesser hazard and a controllable one, since Steward writes the file. And a `---` immediately after a paragraph makes it an H2 in CommonMark, so the delimiter is both load-bearing and ambiguous.)

Its co-writability is worth noting but not worth building for: the need for human annotation was a property of *that* design rather than a requirement anyone stated. If it turns out to be wanted, appending a narrative event to the journal is a small addition at any time.

### The one file Steward must never write

`status.md` is generated, carries a header, and is expendable. `policy.md` is its counterexample, and the reason the line is worth drawing visibly in the directory listing: it is the human's own document, and the one thing Steward only ever *proposes* changes to.

### State is a fold over the journal

Given an append-only event log in `journal.jsonl`, the rest follows the discipline execution.md already established for the reconcile core — *the supervisor is a cache, never a source of truth*:

```
reconcile(manifest, inflight, log_dir)   -> actions, summary     # execution.md
fold(journal.jsonl)                      -> anomaly state        # this document
```

Anomaly state — what is open, what was proposed, what was ruled and how — is a **pure fold over the journal**, not a separately maintained file. Any `anomalies.json` is a cache of that fold, living in `.steward/` with the rest of the disposable state. The property this buys is the same one that made reconcile worth writing as a pure function: crash recovery for adjudication state is the normal code path, exercised on every tend rather than in a rescue routine nobody tests.

### `init` and version control

The durability story assumes version control — `journal.jsonl` survives and is reviewable because it is committed — but nothing so far makes that happen. So `init` takes care of it:

- **`git init` if, and only if, there is no repository already.** Detection walks up from the directory: a Steward workspace created inside an existing project (`evals/sweep-2026-08/` in a monorepo) belongs to that repository, and initialising a nested one there is a footgun rather than a convenience. Announce it when it happens, and allow `--no-git` for people who mean it.
- **Write a local `.gitignore` either way**, listing `.steward/` and `logs/`. Scoping the rules to a nested `.gitignore` means Steward never edits a file it does not own — the parent project's own ignore rules stay untouched. Append missing entries idempotently if one already exists.

`logs/` is ignored because `.eval` files are large archives, they are outputs rather than source, and they are shared through a log server or object store rather than through git. `.steward/` is ignored because it is disposable by construction.

**A small integrity bonus falls out.** Committing an append-only journal gives a second, independent record of when each decision was made — git's own metadata — and any edit to a past entry shows up in review as a *modification* rather than an append. It does not make the record tamper-proof, but it makes tampering visible in the course of ordinary review, which is most of the value.

### There is no `steward.yaml`

Every candidate for it turned out to belong somewhere else:

| candidate | where it actually goes |
|---|---|
| definition pointer | discovered (`evalset.py` / `flow.yaml` in the directory) |
| `log_dir` | defaults to `logs/` |
| tend interval | the runbook, and the agent's scheduling |
| notification channel | `INSPECT_EVAL_NOTIFICATION` — reference-only *by design*, so a config file is the wrong home |
| eval configuration | the definition, which configuration.md establishes as the single source of truth |

That leaves budgets, and a budget is a **launch argument** (`steward launch --max-spend 200`), recorded into run state. A config file is for settings you re-apply across many invocations; a run is launched once. Where someone genuinely repeats a launch, a shell script or Makefile does the job and they have one anyway.

The stronger reason to refuse it is drift. configuration.md spends its length establishing that the definition is the single source of truth for what an eval set *is*; a second config file beside it is precisely the place where a contradicting `log_dir` or `model` ends up. Not creating it is cheaper than policing it.

## Mechanics and policy are different documents

This is the distinction I most want to get right, because conflating them causes both of the obvious failure modes.

| | `steward runbook` (a command) | `policy.md` (a file) |
|---|---|---|
| answers | how Steward works | what this human wants |
| owner | the package | the user |
| lifetime | ships with the version | lives with the project |
| example | "call `tend` every 10 min; never block on a human" | "never spend over $200 without asking; sandbox timeouts in this eval are expected" |

Making the runbook a *command* rather than a file solves version skew: instructions and implementation ship together, so an agent can never follow last year's runbook against this year's CLI. Making policy a *file* lets it be edited, reviewed, and version-controlled by the person whose standards it encodes.

`AGENTS.md` is the thin bootstrap that points at both. It is the only thing that has to be discovered by convention.

### A ruling is not a policy

`policy.md` grows over the life of a project, but **Steward should not write to it.** The distinction that matters: a *ruling* is a human's decision about one situation, with all of that situation's context behind it. A *policy* is a standing rule for every future situation of that shape. Promoting the first into the second is itself a judgement, and it is the human's.

Auto-promotion would quietly convert "invalidate those 47 rate-limit errors from the 15:40 outage" into "always invalidate rate-limit errors" — which is a different and much larger claim, and one the human never made. For a tool whose output is evaluation results, silently widening the human's standards is close to the worst failure available.

So the division is:

- **`journal.jsonl`** — every ruling, machine-written, with reasoning and evidence. Append-only, and the only copy.
- **`policy.md`** — standing rules, human-authored. Steward *proposes*: "you have ruled the same way on this class three times; add it to policy?" The human accepts, edits, or declines.

The accumulation benefit survives, and authorship stays where it belongs. It also gives the workflow a legible success metric: **interruptions per run, trending down.**

### The journal, and a naming collision to fix

`journal.jsonl` is the append-only event log — launches, anomalies observed, proposals, rulings, resolutions, completion. It is what answers "what happened here, and who decided what" a year later, and the only file in the directory that nothing else can reconstruct.

Two files named *journal* would be one too many. execution.md currently uses the word for the append-only `intent`/`launched`/`exited` process records, which are ephemeral, machine-owned, and explicitly reconstructible from the log directory — the opposite of durable, and confusingly so given they are also append-only JSONL. **Proposal: rename that one** to say what it tracks (`.steward/inflight.jsonl`), leaving *journal* to mean the durable record, which is what the word means to everyone else.

The two then differ in what they are for, which is why they sit in different places: `inflight.jsonl` is a **performance cache** over the log directory, discardable at any moment, while `journal.jsonl` is the **record**, and the only file in the directory that cannot be rebuilt.

## `steward launch`

Takes the claim, captures the manifest, writes the initial state, spawns the first workers, **returns**.

It must not block: the caller is an agent that has to go on and schedule tends, and a blocking start would trap it. `run` is the wrong word for that — it says *you* are doing the running, and invites the reader to expect a foreground process. `launch` says the opposite, and says it without a footnote: you set something in motion and it goes on without you.

It also completes a coherent verb family. **launch → tend → status** are all words that presuppose the thing has its own life; you launch a ship and then tend it. `run → tend` would have been two different metaphors bolted together. The verbs now teach the model that matters most: *the run is a live thing you look after, not a program you are executing.*

The one thing lost is that `steward run` paired cutely with discovering `run.py`. That was never worth much — file discovery does not need to echo the verb.

### Smoke first

Standing practice before an expensive sweep is a **smoke run**: a couple of samples per task under a wall-clock cap, to find out whether the thing works at all before committing real money to it. `steward launch --smoke` makes that a flag rather than a ritual people reconstruct by hand, and the runbook names it as the default first step.

Defaults of two samples per task and a fifteen-minute cap match how this is done in practice. Neither is magic; the point is that both are *bounded*, so a broken definition costs minutes instead of a night.

**Smoke logs go to `.steward/smoke/`, on local disk.** Two-sample truncated logs written into `logs/` would be indistinguishable from real results to `samples_df`, to the viewer, and to anyone analysing the eval six months later — so they need their own directory regardless. Making it local rather than a sibling of `log_dir` matters because **`log_dir` is frequently S3**, and a rehearsal has no business writing throwaway objects to a bucket: slower, billable, and leaving junk that needs lifecycle rules to clear. Local disk is free, fast, and already where disposable state lives.

Putting it under `.steward/` also means the cleanup question answers itself. It is disposable state by construction, so it needs no special rule: each smoke clears the previous one, a failed smoke's logs stay put for as long as anyone wants to read them, and everything goes when `.steward/` goes. `inspect view --log-dir .steward/smoke` works on it like any other directory.

Not deleted the moment a smoke passes, though — reading a transcript or two after a green run is a real practice, and the point of a smoke is partly to check that the agent is doing something sensible rather than merely something that terminates.

**What it catches** is most of what actually goes wrong before anything interesting does: a definition that will not import, a model name or key that is wrong, a sandbox image that will not start, a scorer that throws, a grader container that falls over. All of them are cheap to find at two samples and expensive to find at five thousand.

**It also measures cost rather than estimating it.** Spend and tokens across the smoke extrapolate to the real run, which is a far better answer to "what will this cost" than counting tasks — and it arrives exactly when someone is deciding whether to authorize the spend.

**A smoke is valid for a manifest, not for a directory.** Whether a smoke still applies is answered by the capture manifest: if the definition changed, its task identifiers changed, and the smoke is stale. That gives `launch` a precise check to warn on — "no passing smoke for this manifest" — without needing to guess at what edits matter. Warn rather than refuse: re-launching after a fix, or resuming, are both legitimate reasons to skip it, and a hard gate would only teach people to bypass it.

Nothing about a smoke needs special machinery. It is a run with a sample limit, a time cap, and a different log directory — launched, tended, and reported like any other, then recorded in the journal as part of the same story.

**It does have one upstream dependency**, and it is the first concrete motivation for a change execution.md previously argued for only in the abstract. Redirecting a definition's `log_dir` needs the overrides channel (execution.md, *Changes required in inspect_ai*, item 4): Flow accepts `--log-dir`, but a raw `evalset.py` provides no way in, so `--smoke` against a plain script cannot send its logs anywhere but where the definition says. Until `INSPECT_EVAL_SET_OVERRIDES` exists, smoke works for Flow definitions and not for script ones.

## The tend loop

The agent schedules `steward tend` every ~10 minutes (see execution.md, *The reconcile core and its drivers*). Each tend:

1. reconciles — spawns what should be running, reaps what died, requeues within policy
2. rewrites `status.md`
3. returns a compact structured summary to the agent
4. **never blocks** — everything long-running is a detached child

The agent reads the summary and decides whether anything warrants a human. Most tends warrant nothing and should produce no output beyond the file rewrite.

## Resource allocation

Two resources gate a run, and only one of them needs Steward's attention.

**Connections are already handled.** Inspect's adaptive connections — on by default, ceiling of 100 — discover a provider's throughput on their own and back off when they reach it. Left alone, they do the right thing, and nothing in this section is about improving on them.

**Sample concurrency is the one that costs money.** A sample waiting on a model-API semaphore is not free: it holds its sandbox container, its memory, and its slot in the event loop the whole time it waits. Concurrency set far above what the provider will actually serve means hundreds of samples sitting on EC2 consuming compute and doing nothing. The right level is roughly *the concurrency the provider actually supports* — not a number anyone can look up, since it varies by tier, by model, and by time of day.

### Setting `max_samples` explicitly is what makes it a knob

Three paths produce a task's sample semaphore, and which one you land on decides whether Steward can steer at all:

| condition | limiter | retunable? |
|---|---|---|
| `max_samples` set explicitly | `ResizableLimiter` | **yes** |
| unset, adaptive connections active | `DynamicSampleLimiter` | no — tracks the model's controller |
| unset, adaptive off | `ResizableLimiter`, defaulted from `max_connections` | yes |

**Explicit `max_samples` wins over adaptive, silently and deliberately.** Leaving it unset hands sample concurrency to the model's connection controller, which grows it as the provider allows — excellent inside one process, and not something Steward can adjust.

That settles the scaffolding question with a second, stronger reason. `steward init` should write an explicit `max_samples=` into the definition not only so the author thinks about it, but because **an explicit value is the difference between a fleet Steward can coordinate and one it can only watch.**

### What Steward actually has to solve

**Per-process adaptation is per-process, and Steward runs many.** Left to adapt independently, eight workers each discover headroom that is partly headroom the others have not claimed yet — so they all climb, collectively overshoot, and get rate-limited together. Each is confidently optimizing against a resource it believes it has to itself.

Nothing inside a worker can see the total. What reaches a provider is the sum across workers, and Steward is the only party that knows how many there are. Setting explicit per-worker limits is therefore not merely a way to make the user think — it is how the budget becomes **deterministic** rather than emergent, and Steward owns both factors: how many workers run, and each one's share.

The division trades against things already weighed here. Fewer, larger workers pay less per-worker startup cost (roughly a second for Flow definitions, on every spawn); more, smaller workers give finer scheduling granularity and less slot idle between tends.

### There is no single budget — there are two, with different shapes

Tasks in one eval set often run against different models, and throughput varies enormously between them: a high-tier hosted provider and a single-GPU local vLLM are not the same resource and do not compete for the same thing. A uniform per-worker limit is then wrong in both directions at once — too high for the constrained model, whose samples pile up waiting, and too low for the fast one, which sits underused.

So the budget splits along the same line the two-signals argument already draws:

| budget | scope | who competes |
|---|---|---|
| **throughput** | one per rate-limit bucket | only tasks sharing that bucket |
| **local compute** | genuinely global | **every** task, whatever model it uses |

Throughput is partitioned; sandboxes, memory, and CPU are shared. Two workers on different providers do not contend for tokens at all, and contend fully for the host.

**The manifest already carries the grouping.** A task identifier includes its model, so Steward can partition tasks by model at enumeration time and size each group separately, without discovering the structure at runtime.

**The shape of the sweep decides which budget binds**, and the two common shapes sit at opposite extremes. A model comparison — one task across many models — has almost no throughput contention and is bound entirely by local compute. A task sweep on a single model is the reverse: local compute is usually ample and the provider is the wall. Recognizing which one is in front of it tells Steward which ceiling to manage.

Setting concurrency **per task rather than per fleet** follows naturally from the mechanism, since each worker runs one task in its own process. It does put one constraint on batching several short tasks into a single worker (see the selection protocol): **batch only tasks that share a model**, or the process's initial limit is necessarily wrong for some of them.

Two caveats keep this from being exact. A rate-limit bucket is not quite a model — several models can share an account's quota, so grouping by model over-partitions. And a task may consume more than one provider: a grader model, or an agent calling a different model for subtasks, neither of which appears in the identifier. Steward can see a task's primary model and not its full appetite.

### The levers

All of these are live-tunable through the control channel — `GET`/`PATCH /tasks/<task-id>/config` for task-scoped knobs, `GET`/`PATCH /config` for process-global ones — so tuning reaches **running** workers, not just newly spawned ones.

| lever | scope | what it bounds |
|---|---|---|
| `max_samples` | task | sample concurrency, given an explicit setpoint |
| `max_sandboxes` | process | sandbox concurrency — the lever for the *local* ceiling |
| `max_connections` | process | the connection pool, and the adaptive controllers' scaling ceiling |

`max_sandboxes` is the one that matters under Docker, because it bounds compute directly rather than by proxy.

One detail worth relying on: **semaphores are task-scoped, not attempt-scoped.** A retune survives an in-process retry rather than silently reverting to the definition's value, so a worker Steward has tuned stays tuned.

### The ratchet is asymmetric, and the mechanism says so

This is not an inference about container lifetimes — it is how the limiters behave. **Lowering a limit below the current in-use count blocks new acquires until in-flight holders drain; it never preempts. Raising one lets work start immediately.**

So climbing is instant and descending is gradual. Undershooting costs wall-clock and recovers in minutes; overshooting commits compute that only releases as samples finish, and under Docker can thrash or OOM the host first. Ramp in shrinking increments, and stop short of anything that cannot be undone quickly.

**Overshoot has a partial repair, and knowing which half is fast matters.** Having climbed into rate limits, lowering `max_connections` clamps live connection concurrency down *at once* and the backoffs stop — that half is immediate. The sample side is not: those samples already hold their containers and memory, and the only way that releases is by finishing. So the correct first move on overshoot is the connection ceiling, which buys relief in seconds, followed by letting sample concurrency drain to a lower setpoint over minutes.

The provider-side damage is undoable in seconds; the compute-side commitment is only outlastable — and on a Docker host it can take the box down before it drains. That argues for shrinking increments, **not** for timidity, and the difference matters more than it first appears.

### Over-scaling risks failure; under-scaling wastes time and often money

The asymmetry above is easy to read as "be cautious", which would be the wrong lesson. The tempting summary — over-scaling costs money, under-scaling costs wall clock — is half wrong, and the wrong half is the money.

**Token spend is invariant to concurrency.** The same samples make the same calls whether forty or a hundred and twenty run at once. Concurrency changes the *rate* of spend, never the total. Whatever over-scaling costs, it is not the model bill.

What is left is compute, and it depends on the regime:

| | over-scaling | under-scaling |
|---|---|---|
| **fixed host** (Docker, one EC2 box) | free — the box is paid for either way — until it OOMs, and then catastrophic | **pays for the box three times as long** |
| **elastic** (k8s) | rents capacity to hold blocked samples: real waste | more nodes for less time ≈ a wash, so wall clock dominates |

On a rented box the conservative choice is the *expensive* one. A job at a third of the viable concurrency runs three times as long on the same instance, for identical work — under-scaling costs both time and money there, and over-scaling costs nothing at all right up until it takes the host down.

So the two downsides are not commensurable, and pretending they are is what produces false caution: **over-scaling risks a failure; under-scaling guarantees a loss.** One is an unbounded availability risk with a low probability, the other is a certainty that compounds every hour. The genuine dollar cost of over-scaling is narrow — idle capacity on elastic infrastructure, plus re-run tokens if the overload actually breaks samples.

Set against that, **undershoot compounds**: running at 40 where 120 was available does not lose a little time, it triples the run. A job started at 10pm that could have scaled at 11pm and instead waits for a human until 8am has thrown away most of the night — and being useful during exactly those hours is the whole reason Steward exists.

(One place the *rate* matters even though the total does not: against a spend cap that someone intends to stop early on, spending fast means hitting the cap before the run has taught them anything. That is an argument about observability, not about cost.)

So delegating scaling to the agent is not a risk to be minimized, it is most of the value. Where the right action is clear — no pushback for half an hour, ample local headroom, well under the envelope — it should simply raise, and the escalation list above should be read as the **exceptions** it is rather than the default posture.

**Overnight is the best time to probe, not the worst.** Nobody is waiting on interactive latency, provider load is often lower so there is genuinely more headroom to find, and there are hours in which to recover from a bad probe. The instinct that unattended means careful is partly backwards. What should actually govern boldness is **time remaining, not supervision**: probing at 11pm with a night ahead is cheap, and the same probe at 4pm against a 5pm deadline is not.

### Authorize at 10pm, do not interrogate at 3am

The cost of a question is not constant — it decays with the human's availability. Asked at launch it costs a sentence; asked at 3am it stalls the run until morning, which is the very outcome the escalation was meant to prevent.

So the agent should **front-load** the decisions while the human is present, and the smoke run is the natural moment because it has already measured throughput. One exchange at launch — *"smoke sustains about 50 concurrent, ETA six hours; push toward 100 if headroom appears?"* — is worth more than any number of well-judged 3am escalations, because it converts the whole night into standing authority.

This is the pre-authorization idea from *Notification is the gate on autonomy*, applied to scaling, and scaling is where it pays best: the questions are predictable, they can be asked before anything has gone wrong, and the answers stay valid all night.

### Rate limits are the wrong signal for the local ceiling

Provider pushback says nothing about memory. A run can climb to 200 concurrent samples with no rate limits at all and still take the box down. Which constraint binds depends on where sandboxes run:

| sandbox | local gate | provider gate | consequence |
|---|---|---|---|
| Docker | **hard** — one host's memory and CPU | soft | the host is the ceiling; `max_sandboxes` is the lever |
| k8s | elastic | **binding** | the provider is the ceiling; let adaptive climb |
| none | slight (memory per sample) | **binding** | as k8s |

"Ramp until rate limits appear" is correct advice on k8s and a way to kill a laptop under Docker.

### The signal exists

The config view carries an `adaptive` section reporting each controller's live limit, in-flight count, scaling bounds, and **recent scale changes** — so pushback is observable rather than inferred. Scale-downs across several workers at once are the signal that the fleet has collectively overshot, which is precisely the condition no individual worker can detect.

`PATCH` also supports `dry_run`, and applies what it can while warning about knobs that do not apply rather than failing the whole request — so Steward can probe before committing and tune several things at once without brittle error handling.

### The envelope is policy; the tuning is the agent's job

The **ceiling** is a judgement call about infrastructure that only the user can make — how big the box is, whether the cluster scales, how much they are willing to have running at once. It belongs in `policy.md` or as a launch argument. Everything inside that envelope is the agent's to tune **without asking**, and doing so is one of its standing jobs rather than an exceptional intervention. The envelope exists precisely so that the agent can move freely inside it: start conservatively (40 concurrent samples is a reasonable default to scaffold), raise while pushback stays absent and local headroom holds, pull back when scale-downs cluster, rebalance across groups as workers finish.

All of that is observation and arithmetic. What it cannot settle is a short list, and the items on it are unclear for structural reasons rather than for want of data:

- **Attribution.** Scale-downs mean *someone* is at the limit — not necessarily us. Another workload on the same API key looks identical from inside a worker. So does memory pressure from another process on a shared host. Ramping into someone else's workload is worse than running slowly.
- **Risk appetite.** Faster-but-riskier against slower-but-safe has no observable right answer. Near a deadline a person may accept OOM risk they would never take overnight.
- **Scope, when the numbers come back badly.** If observed throughput implies forty hours instead of four, the useful question is not what to set concurrency to — it is whether to drop a model, cut epochs, or let it run anyway.

### Escalate in the units the human thinks in

That last item generalizes into the rule that matters most here. **A human cannot usefully rule on whether `max_samples` should be 60 or 80.** They can rule immediately on "at current throughput this finishes at 3am rather than 9pm" or "we are burning $40/hour and the cap is $200".

So the agent's tuning output is a projected completion time and spend rate, not a concurrency number, and those are what a notification carries. The smoke run makes this available before the real launch rather than three hours into it, which is the difference between a decision and a rescue.

Tuning belongs in the record like anything else: each adjustment is a journal event, so "ramped to 80 at 14:10, scale-downs at 14:25, settled at 60" is reconstructible. Where the situation is genuinely unclear it becomes an **anomaly** and takes the ordinary lifecycle — open, investigating, proposed, ruled — rather than needing a parallel mechanism for resource questions.

### Tuning precedent is the most reusable kind

Accumulated rulings pay off more here than anywhere else in the design, because **a provider's throughput is broadly stable across runs in a way that error conditions are not.** A ruling about a transient outage is worth little next month; "this model on this account sustains about 50 concurrent" is worth a great deal, and stays true. After a few runs Steward should simply know it, and start there rather than rediscovering it by ramping from 40 every time.

That is the first thing in this design that wants to persist **outside the workspace**. Throughput is a property of a model and an account, not of one sweep, and `journal.jsonl` is per-project. Where cross-project learned knowledge lives — a user-level store, something under `~/.steward`, or nowhere — is unresolved, and it carries the usual hazard of implicit state: knowledge that changes behaviour while living somewhere nobody thinks to look.

## Notification is the gate on autonomy

The entire value proposition is "don't bother me unless it matters," and both failure modes are bad in the same way: notify too much and the human stops reading; notify too little and they discover in the morning that $400 went somewhere wrong. So notification policy is *the* tuning knob for how much autonomy the human has actually granted, and it belongs in `policy.md`.

Two sources, and the distinction matters:

- **Steward notifies mechanically** — run complete, budget exceeded, no workers alive but tasks pending. Conditions with no judgement in them.
- **The agent notifies with judgement** — `steward notify`, carrying an interpretation: "the sonnet arm is failing systematically, I've paused it, here's why."

The second is the valuable one and the reason `steward notify` should exist as a command rather than being Steward-internal. It also means the agent needs to know when *not* to use it, which is a policy question, not a mechanism question.

### What Inspect already provides

`inspect_ai.util.notify(message, title=None)` — async, best-effort, backed by [Apprise](https://appriseit.com), so every channel (Slack, email, SMS, desktop, webhook) comes from Apprise's URL DSL rather than from Inspect. It never raises and never blocks more than five seconds, which suits a caller that must not stall a tend. Apprise is an optional dependency.

Three properties of it shape Steward's design:

**Configuration is reference-only, and Steward must preserve that.** Inspect deliberately refuses notification URLs as API or CLI arguments — you pass `True` (read `INSPECT_EVAL_NOTIFICATION`) or a path to an Apprise config file. The rationale is keeping credentials out of source, shell history, process listings, and eval logs. Steward inherits that discipline for free and should not break it — the channel is named by the environment, never by an argument Steward accepts. Worth writing down before someone adds a well-meaning `--notify-url` flag.

**There is no policy layer whatsoever** — no rate limiting, no dedup, no severity, no thresholds, no only-on-failure. So the notification policy this document cares about is entirely Steward's to build. Inspect's own `notify_user()` tool description already carries the concern as advice to the model ("operators get noise fatigue; batch into milestones"), which is the same problem one level down.

**There is no eval-completion notification.** Nothing fires on task or eval completion today. That makes Steward the natural owner rather than a duplicator, and appropriately so: *complete* is a Steward-level concept — tasks finished **and** the scan drained **and** adjudication resolved — that Inspect is not in a position to know.

### The gap: notifying from outside an eval

`notify()` resolves an Apprise instance from a `ContextVar` installed inside `eval_resolve_tasks`, so it is a **silent no-op anywhere outside a running eval**. Steward's tend, and therefore `steward notify`, runs in a process that is not inside an eval at all — so the function most relevant to Steward is exactly the one that does nothing when Steward calls it.

The fix is small: `build_apprise()` and `init_apprise()` already exist and do precisely what is needed, but live in the private `inspect_ai.util._notify`. Making them public (or adding a `notification_scope(config)` convenience) is the same move as *Public eval-set directory operations* in execution.md — a documented surface for external callers rather than a private one reached around. It is also not Steward-specific: any script that runs evals and wants to be told when it finished hits the identical wall.

### A distinction worth not blurring

Inspect's existing `notify()` call sites are human-in-the-loop moments — `request_input()` behind the `ask_user()` tool, and `human_approver`. Both **block the sample** until a human responds. Steward's escalations use the same channel for the opposite semantics: queue the question, notify, and *carry on*. Same pipe, blocking versus non-blocking, and conflating them would reintroduce the one thing adjudication must never do.

## Anomalies are structured state

Adjudication needs a data structure, not just a conversation. Without one, an unresolved problem is only ever a sentence in a summary — which means at a ten-minute cadence it gets re-discovered and re-reported on every tend, and nothing can tell whether it was already raised, already ruled on, or already fixed.

An **anomaly** is anything observed in the run that may need a decision: a cluster of errored samples, samples that hit a token or time limit, a task that scored uniformly zero, a worker that died repeatedly, spend trending past its cap, a scan pass that failed, or — see below — something a scan pass *found*. It is not stored directly: anomalies are **folded out of `journal.jsonl`** (see *State is a fold over the journal*), cached in `.steward/`, and surfaced through `status.md`. Each tend appends what it observed and what it decided; current state is the replay.

The fields that earn their place:

| field | why |
|---|---|
| `id` | **stable across tends** — the whole point |
| `class` | the grouping key; 47 samples become one decidable item |
| `evidence` | sample ids, error text, time window, counts |
| `state` | `open` → `investigating` → `proposed` → `ruled` → `resolved`, or `accepted` |
| `proposal` | what Steward suggests, so the human can agree in one word |
| `ruling` | the decision and its reasoning |
| `resolution` | what happened after — re-ran and passed, re-ran and failed again |
| `precedent` | prior rulings on this class, carried along rather than looked up |

**Stable identity is the hard requirement**, and it is easy to get wrong. The natural key is the class plus the window it opened in — *not* the set of affected samples, because that set grows as more samples fail into the same class. Get this wrong and either the same anomaly notifies fifty times over an overnight run, or a growing problem keeps looking like a new one and never accumulates the weight that would justify escalating it.

**This also answers a question execution.md left open.** Open question 10 asks what "resolved" means for an eval set. The answer falls out: **a run is resolved when no anomaly is open.** Not "all tasks finished" — tasks can finish with holes — but every observed problem carried to a ruling and a resolution. That is a definition Steward can actually enforce, and it is why completion is a Steward-level concept that Inspect could not compute.

*Resolved* is not the same as *done*, though. See *Signoff*.

### Scan findings are anomalies, and they arrive last

Scanners are purpose-built to notice things, which makes them the most valuable anomaly source in the system. Worth separating two cases that the word "scan error" blurs:

- a scan pass **failing** — infrastructure, handled like any dead worker;
- a scan pass **succeeding and reporting something bad** — the grader container fell over, a scorer disagreed with itself, a transcript shows the tool harness broke.

The second is a *finding*, not an error, and it is the kind most likely to need a human. Execution errors say what broke mechanically; scan findings say what broke **semantically**, and semantic damage is exactly what a person has to weigh: is this grader failure bad enough to re-run, or is the score still trustworthy?

This settles the lifecycle ordering. Scans run over logs that have already landed, and the final pass runs after cleanup and adjudication settle (execution.md, *The one real ordering constraint*), so **scan-sourced anomalies appear after everything else looks finished**. A run that read as resolved can un-resolve when its scan drains. Hence:

```
tasks complete  →  scan drains  →  anomalies settle  →  human signs off
```

Which is the concrete reason signoff sits after the scan rather than after the tasks, and another reason a run is not finished when its workers are.

### Scanning collects; investigation digs

A distinction worth importing from how scanning is used in practice: **scanning collects results across everything, investigation digs into the interesting ones.** They are different activities with different economics — one is broad, mechanical, and runs over every transcript; the other is narrow, targeted, and follows a specific question.

That maps directly onto a state the anomaly model was missing. Scanning produces *candidate* anomalies, and some of them are not yet decidable: the grader container fell over, but was that one flaky sandbox or a systematic harness break? Nobody can rule on it until someone looks. Investigation is the step between observing an anomaly and being able to propose a ruling, and it needs its own state — `investigating` — so that the next tend does not re-propose it and `status` can report that it is being worked rather than ignored.

**Investigation is mostly the agent's job, not a scanner's**, and that follows the division running through the whole design. Scanning is mechanical: it runs on everything and exercises no judgement. Investigation is judgement-adjacent — choosing what to look at, deciding what it means — and the agent already has the tools for it (`inspect log`, `samples_df`, reading the transcript). Writing a scanner to answer a one-off question is the wrong shape.

Its cost is also a different kind. A scan pass is expensive in wall-clock and tokens across many logs; an investigation is expensive in **agent context** for a few. That matters for pacing: an anomaly whose investigation means reading a five-hundred-sample transcript may need to be narrowed before it is opened, not after.

Where investigation *is* a scan — a targeted pass with more expensive scanners over a handful of logs — the mechanism already exists. The scan protocol takes a list of log locations, so a narrow pass over three logs is the same call as a broad pass over three thousand. What it does not yet have is a way to ask for *different* scanners than the definition's, which is what an investigation pass would usually want.

For an anomaly to be investigable at all, it has to carry the pointers: which logs, which samples, which time window. That is already in `evidence`, and this is the use that justifies keeping it precise.

### Precedent travels with the anomaly

An agent adjudicating an anomaly wants to know whether this class has come up before and what was decided. Making that a lookup it must remember to perform is a design failure — it will sometimes forget, and the whole point of accumulating rulings is that they get applied.

So **prior rulings for a class are attached to the anomaly** wherever it surfaces, in `tend` output and in `status`. The agent can apply precedent without a round trip, and a human deciding sees "you ruled this way twice before, on these dates, for this reason" at the moment of deciding rather than after. It is also the mechanism that makes *interruptions per run, trending down* actually happen rather than merely being hoped for.

## Signoff

Steward can compute that no anomaly is open. Only a person can say **I accept these results**. Those are different claims, and conflating them is how a run ends up looking certified because a machine ran out of things to flag.

`steward signoff` is that attestation: the terminal event in the journal, recording who, when, and what was true at the time — task counts, samples resolved, exceptions accepted.

Three properties it needs:

- **Accepting known holes must be explicit, not blocked.** Real evals ship with failures nobody intends to fix. Refusing to sign until everything is clean would just push people to fake resolutions, so signoff records accepted exceptions by name — "2 samples accepted as errored" is a signed statement, not a silent gap.
- **It is an attestation, not access control.** Nothing can stop an agent from running the command, so the design does not pretend to: it records the signer, and the runbook states plainly that the agent never signs. A forged signature is then visible rather than prevented, which is the same bargain a commit author line makes.
- **It can be invalidated.** A scan landing after signoff, or a later invalidation, re-opens anomalies. The signature stays in the journal as a record of what was believed at the time, and a fresh one is required.

The result is a lifecycle with two distinct terminal states rather than one overloaded boolean: **resolved** (computed — nothing is open) and **signed off** (attested — a person accepted it). A run can be resolved and unsigned, or signed with exceptions, and those are usefully different things to report.



## Adjudication is a conversation, and it has rules

The hardest part of the workflow, and the least designed. Some starting positions:

**Adjudication never gates execution.** If the agent needs a ruling and the human is asleep, the run keeps going. Queue the question, notify, carry on. This falls straight out of `fail_on_error=False` — everything runs to the end and anomalies are settled afterwards.

**Rule on classes, not instances.** A human cannot adjudicate 47 errored samples, but they can rule on one *class* with evidence attached: "47 samples failed `RateLimitError` against anthropic between 15:40–16:05 — invalidate and re-run?" Clustering anomalies into a handful of decidable classes is the agent's core contribution here, and it depends entirely on the error taxonomy that execution.md lists as unresolved.

**A ruling is reusable, and that is how policy grows** — but only with the human's consent (see *A ruling is not a policy*). Rulings land in `journal.jsonl` automatically; promotion into `policy.md` is proposed and never assumed. Over months the human's actual standards accumulate in place of the ones they guessed at up front, which is the most interesting property in the whole design.

**The default is conservative, because this is a scientific judgement.** Invalidating samples changes the number you report. The agent proposes; the human disposes; everything is recorded with provenance. A run where someone quietly dropped the inconvenient samples is worse than a run with known holes in it.

## Do we need a TUI?

Probably not, and the reason is the premise: **a TUI assumes a present human**, which is the case Steward is explicitly not built for.

Test it against what someone actually wants when they come back:

| they want | the answer |
|---|---|
| "how is it going" | `steward status` |
| "what happened while I was out" | `status.md` + notifications received |
| "look at this weird sample" | `inspect view` — already exists, already good |
| "why did you invalidate those" | the agent, reading `journal.jsonl` |

Nothing in that list wants a bespoke live UI. And if a run-level live view is genuinely wanted later, the right home is probably the Inspect viewer rather than a second UI in a second tool.

This deletes a component, which is the best kind of design decision. It does leave one gap: `inspect view` shows *evals*, not *runs* — pending tasks, adjudication queue, and budget have no home in it today.

## The audit trail

A thread running through all of the above: Steward's directory is an **integrity record**, not a scratch space. Someone reviewing results months later should be able to answer "which samples were re-run, which were dropped, who decided, and why" from the directory alone. Inspect already carries part of this (`EvalLog.invalidated`, per-sample `invalidation` records with provenance); the reasoning and the class-level rulings are Steward's to keep.

This is also the argument for the whole workspace being git-friendly: the record of an eval's conduct is as reviewable as the code that produced it.

## Open questions

1. **Anomaly identity.** The class-plus-window key above is a sketch. Real error text varies (ids, timestamps, hosts), so classing requires normalization, and where the window boundary falls decides whether a slow-burning problem reads as one anomaly or twenty. This is the same unresolved error taxonomy that execution.md's open question 7 names, arriving from the other direction.
2. **How does a human answer when no agent is in session?** Notifications are outbound only, and Inspect's are deliberately one-way. The reply path is "start a session and the agent reads the open anomalies" — workable, but it means a yes/no costs a terminal. The mitigation is making questions rare (pre-authorization plus accumulated policy) rather than making answers fast, but that is a bet, not a solution.
3. **What does Steward propose, and how confidently?** The `proposal` field is what lets a human agree in one word, so it carries most of the value — and most of the risk, since a plausible wrong proposal is easier to accept than to check.
4. **Who commits the journal?** `init` prepares the repository, but nothing in the workflow commits. If nobody does, the durability-through-git story quietly fails to happen. Candidates: the agent commits at milestones as a runbook instruction, `signoff` commits as its terminal act, or it stays the human's job. Auto-committing on every tend would collide with the user's own working tree, so the cadence matters as much as the owner.

5. **`status.md` staleness.** With no agent tending and no cron, it silently goes stale. It should carry its own timestamp and say how old it is rather than reading as current.
6. **Multiple runs per directory.** Policy is clearly per-project; claims, manifests, and anomalies are per-run. Whether one directory hosts a series of runs (with `logs/` accumulating) or each run gets its own is unresolved, and it determines whether `journal.jsonl` is a project history or a run record.
7. **How does a scanner result become an anomaly?** Scanners write values, not verdicts, so something has to decide which values mean trouble. Whether scanners are *declared* as anomaly-producing, whether Steward applies thresholds to their output, or whether it takes a purpose-built scanner to say so, is unresolved — and it gates the most valuable anomaly source in the design. Related: an investigation pass wants different scanners than the broad pass, and a definition supplies only one scanner configuration.

8. **How are the concurrency budgets divided?** Steward owns both factors, but the policy is unresolved, and it is now two policies: dividing each per-provider throughput budget among the tasks sharing it, and dividing the global local-compute ceiling across all of them. Equal shares is simplest and wrong when tasks differ in sample count or model speed; rebalancing as workers finish means retuning live limits every tend. Open alongside it: whether clustered scale-downs should pull back the whole group or only its newest workers, and how to handle a task whose grader model consumes a bucket the manifest never revealed.

9. **What is in a journal event?** Settling on JSONL answers the format question but not the schema. Events need enough structure for the fold to reconstruct anomaly state and for an agent to look up prior rulings by class, while staying legible when rendered. Whether reasoning is a free-text field or something more constrained is the crux — it is the part a human writes and the part an agent most needs to match against.
