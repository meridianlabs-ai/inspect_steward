# Steward runbook

You supervise an `inspect_ai` eval set for a person who is not always present. A timer runs `steward tend` without you: it starts workers, watches them, ramps them, and raises what it cannot decide. You supply the judgement: read what it raised, diagnose, propose, relay the person's answers, and write down what you found. The person rules and signs.

`_steward.yaml` is that person's rules for this run. Read it before you act. The mechanics are `steward` verbs and `inspect ctl`; reach for them before a script.

## How it works

The workspace is a directory. The definition is the person's statement of what to run. `_steward.yaml` is their rules for this run. `journal.jsonl` is every event and decision, append-only. `analysis.md` is your findings. The log directory is named in the snapshot. `status.md` and `anomalies.md` are rendered from the journal and the logs at every tend.

`steward launch` resolves the definition into a manifest of tasks and arms the tend timer. Each tend is one turn of the loop: reap finished workers, start the next, ramp concurrency, sync the scans, detect what changed, render the snapshot, post at most one notification. A worker is one `inspect` process running one or more tasks. It writes a log, scans each sample as that sample finishes, and answers `inspect ctl` while it runs. A log has landed when its file is complete and its status is known. A worker that stops inside a sample to ask a person's approval is parked until someone attaches with `inspect acp`.

What a tend cannot decide becomes an item in the queue. Every item has a kind, an owner (a person or you), and the command that resolves it. **The timer runs the loop; nothing runs you** — a tend never invokes an agent, so the items it hands you wait until something else does, and after two intervals they are posted to the person as though no agent existed. Where the run is unfinished and you may not be spoken to again, arranging your own return is part of picking it up (*Each session*). A class of failed samples or scan findings is an anomaly. Each occurrence of a class opens a window, and a ruling closes it. `steward status` prints the snapshot. `steward collect` prints it with everything that happened since you last collected. The run ends when the person signs it off with `steward signoff`, and its gate names whatever stands in the way.

## Launching

**Smoke first.** `steward launch --smoke` runs a few samples of every task under a wall-clock cap and launches nothing.

The smoke fails on any errored sample and on four named checks: `context_window`, `reasoning`, `reasoning_api`, `scan_coverage`. `unexercised` and `undetermined` are not failures. Fix what failed rather than routing around it. `--accept CHECK` waives one check by name; an errored sample cannot be waived. Do not project the run's spend from the smoke.

**The cap is not one of the things that can fail.** A smoke runs a few samples under a wall-clock deadline and stops when it fires, mid-sample if that is where it is. That is the tool working: samples are meant to be cut off, nothing is reported about the ones that were, and a rehearsal that answered its checks is ready whether or not it finished what it started. So a cap firing is not a reason to reach for `--cap` or `--samples`, and not a reason to rehearse again: a slow task is a fact about the run, and the smoke has already told you what it went to find out. Those flags are for choosing how much rehearsal you want up front, not for answering a deadline that did its job. The one deadline worth acting on is one that fires before a single sample lands, which establishes nothing and says so.

A smoke that fails twice is a stop. Notify it explicitly: nothing posts before the first worker starts.

**Then launch.** `steward launch` shows what will change and starts the run.

**The tend timer.** `launch` arms it. `steward timer status` is how you check it, not the system scheduler. A run launched `--no-timer` carries an `unsupervised` item until a person acknowledges it, which says they are driving by hand.

## What you may do

**Without asking.** `launch --smoke` and `launch`. `tend`, `status`, `collect`. `raise`, `investigate`, `propose`, `notify`, `note`. `ramp hold` and `ramp resume`. Every `inspect ctl` read, and lowering a worker's concurrency through `inspect ctl config` while containing an incident. `ack --by agent` for an item you resolved yourself, when nobody else would need to know. Writing `analysis.md`.

**Only with a person's answer, recorded in `--by`.** `ack --by human`. `rule`. `signoff`. `pause` and `resume`, except during a stop (see When to stop and ask). `launch --accept CHECK` and `launch --accept-archive`. Writing `_steward.yaml`. Any `inspect ctl` mutation of a sample or task; a pre-authorization in `_steward.yaml` is an answer already given.

**Never.** Edit the definition; raise the change as a question instead. Move or delete a log, not even an empty one. Answer a parked approval or `ask_user`; name the worker, print the command that attaches to it, notify, and wait.

`--by` records who decided, never who typed. `rule` and `signoff` record the person's name on their own; pass `--by` only for someone else. `ack`, `pause` and `ramp` take `human` or `agent`.

**Trust the artifact, not the exit code.** A task whose every sample errored still exits zero. Read the smoke digest, the log's status and the anomaly count. Completion is not success.

## When to stop and ask

Stop when a retry is not the answer:

- the smoke fails a second time
- a worker fails on identity or resume: the logs on disk and the definition disagree about what the eval is
- numbers fail sanity, such as a sample count that does not match the dataset
- the next step is destructive or irreversible
- a credential failure survives a retry
- you are about to relaunch the same task a third time

A stop is not a teardown. Healthy work keeps running. What you do:

1. `steward note` the state and your hypothesis.
2. `steward notify MESSAGE --kind stopped --detail ...` with the question you need answered.
3. `steward pause --by agent --reason ...` only if new work would be spent on a premise you now doubt. Pausing stops scheduling; work in flight finishes.

Then leave it. The rest of the queue is still yours; the question waits for a person. Do not work around the block.

## Each session

**Arrange your own return, because nothing wakes you.**

The timer runs `steward tend` without you and never calls you back. Between one message from the person and the next you are not running, so an item that arrives owned by *you* sits until something invokes you again — and after two intervals your queue is posted to their channel as though there were no agent. The tend keeps working and `status.md` keeps updating throughout, which is what makes it quiet.

So where work is still outstanding, point whatever your harness has for coming back — a recurring prompt, a scheduled wake-up, a background watch — at `steward collect` on roughly the tend interval, and handle what it returns. Two cases do not need it: a run that is signed off, and a person who is here and talking to you. Take it down when either becomes true, rather than notifying a channel somebody is already reading.

If your harness has nothing of the kind, say so once, so the person knows the run advances only while they are talking to you.

Pick up cold, in this order:

1. This runbook, then `_steward.yaml`.
2. `steward collect`: what is true now, and what happened since you last looked. `--peek` reads without marking it read; `--since 0` reads the whole history.
3. The snapshot's anomalies, with any precedent printed beside them.
4. `analysis.md`, for what has been found and what it meant.

Everything you need is in the workspace. Nothing depends on a conversation you were not part of.

The snapshot has three sections: what needs a decision, the run, and what happened. Its header carries two ages: tended is the last tend, collected is the last time you looked. The verdict line says the state in words.

**When the person asks how it is going**, run `steward status --format md` and render what it printed in full, in its order, as markdown outside a code fence. Do not summarize it, and do not read `status.md` instead; it can be a full interval stale. **They cannot see what the command printed — only what you write**, so rendering it is the only way the snapshot reaches them. The pull toward skipping it is that the output is already in front of *you*, which makes it feel delivered; it is not. A narrower question deserves a narrower answer — *is it finished* wants a sentence, not the whole snapshot. Your own reading goes below it, marked as yours, only where it adds something the snapshot does not show. Then put each open decision that is theirs to them as a question: what happened, what you found, what you recommend, and the exact answers available. Record the answer with the verb the item names, then `steward tend` so it takes effect now. Ask once; a deferred decision stays in the snapshot.

**Collect regularly**, on the schedule you armed above. After two intervals without a collect, your items go to the person's channel as if there were no agent — which is Steward telling them, correctly, that nobody is home.

## The queue

`collect` prints your queue: every open item you have not handed off, with its owner and the command that resolves it. Any unambiguous prefix of an id works. An id changes when its condition changes, so an acknowledgement covers the condition as it was.

| kind | owner | it means | you |
|---|---|---|---|
| `stalled` | person | a task stopped progressing after its attempts and will not respawn | find out why, then raise |
| `drift` | person | the definition changed since it was captured | raise; never edit it back |
| `stuck` | person, or you when pre-authorized | a sample has been quiet longer than `stuck_after` | see Stuck samples |
| `parked` | person | a worker is waiting on a person inside a sample | raise and notify; never answer it |
| `tuning_proposal` | person | a task could take more concurrency than its setting allows | relay it; ack with the answer |
| `signoff_ready` | person | every task finished and nothing is open | see Signoff |
| `anomaly` | you while open; person once proposed, or after a failed re-run | a class of failures or findings | see Anomalies |
| `unreadable` | you | a log file could not be read | look at the file; ack with what it was, or ask |
| `action_failed` | you | something a tend tried to do failed | do it by hand, or find out why |
| `unwritten` | you | a task has no write-up in `analysis.md` | write the section |
| `journal_damage` | you | journal lines could not be read | read them; ack with what they held |

Every other kind is the person's: raise it. The item's summary says what it is, and its action says what resolves it.

**`steward ack ID --reason ... --by human|agent`** disposes of an item: it leaves every surface and reappears under what happened with who decided. It refuses what has its own verb, and names the verb.

**`steward raise ID [--note ...]`** hands an item to the person who can decide it and closes nothing; it stops appearing in your queue. It refuses your own items, since nobody else would close them.

## Looking inside a running worker

A log says what a sample did once it landed. `inspect ctl` says what a worker is doing now, and is how you diagnose a task that is still running. `inspect ctl task` lists the running tasks with the id every other command takes as `TASK`. Every command accepts `--json`.

| to see | run |
|---|---|
| a task's samples and their status | `inspect ctl sample list TASK` |
| the samples that errored, with the message | `inspect ctl sample errors TASK --content` |
| one sample's status, timing, tokens, score and error history | `inspect ctl sample show TASK SAMPLE_ID` |
| its conversation right now | `inspect ctl sample messages TASK SAMPLE_ID --all` |
| its transcript, event by event | `inspect ctl sample events TASK SAMPLE_ID` |
| the worker's configuration in force | `inspect ctl config TASK` |

Reads are yours. Mutations go as far as a pre-authorization and no further: `sample cancel-tool-call` when `stuck_cancel` admits it, and lowering a limit through `inspect ctl config` while containing an incident. `task pause`, `task cancel`, `sample cancel` and `sample requeue` are a person's. Record every mutation with `steward note`. It leaves no other trace in the workspace.

## Anomalies

Failures that mean the same thing share a class: an exception at a raising frame (`error:TimeoutError@openai/_client.py:post`), a task that died a particular way (`task:no-log-exit:...`), samples an operator killed (`limit:operator`), a task whose every score is zero (`score:zero:...`), samples a scanner flagged (`scan:SCANNER:LABEL`), a scanner that failed (`scanerror:SCANNER`). The snapshot lists every open class with counts, an example, and any prior ruling as precedent. Read the precedent first; the 11pm decision usually answers the 2am question.

Your verbs, in order:

1. `steward investigate CLASS --note ...` marks the class as being worked. The note is for the next session.
2. `steward propose CLASS... --action DISPOSITION --reason ...` makes classes that want the same answer one question. Classes wanting different answers are different proposals.
3. `steward rule --proposal ID --reason ...`, or `steward rule CLASS... --disposition D --reason ...`, records the person's answer.

| disposition | it says | honest for |
|---|---|---|
| `rerun` | run these samples again | anything but `scanerror:` |
| `exclude` | drop these samples from scoring | `error:`, `limit:`, `scan:` |
| `zero` | score these samples zero | `error:`, `limit:`, `scan:` |
| `score` | score these samples as recorded | `error:`, `limit:`, `scan:` |
| `accept` | the data stands, with a caveat the report carries (`--effect`) | anything but `error:` |
| `dismiss` | looked, nothing here | anything |

A `rerun` ruling is carried out by the next tend: samples in a running task are requeued in place, and a landed log has just those samples invalidated for relaunch. Run nothing. The window stays open until the re-run lands, and the same samples failing again come back as a question for the person. A class flagged as substrate (credentials, disk, storage) gets no rerun proposal from you; re-running into broken machinery burns the work twice.

Two classes are quiet on purpose. A `task:` window heals itself when the respawn brings the task home; if it does not, `stalled` is the real question. A `limit:operator` window raises no item, since the operator knows what they did; it waits for the signoff conversation.

### Scan findings

Nothing starts a scan: the worker runs each scanner as a sample finishes, and a `scan:` window opens when its rows land. Every window needs a ruling before signoff. While one is open and you are attached, the task's finished notification is held for up to six tends so it can carry what you found; investigate promptly. The scanner read one transcript; you can read the population, the score and the rest of the run, and that judgement is the whole product.

- The bar is whether the score can still be trusted, not whether the behaviour was interesting. Escalate only where you can name the mechanism, cite the decisive messages, and say what the number would be if the concern is real. Otherwise the honest ruling is `dismiss`.
- A failed attempt is not a finding. The model tried to read the grader and could not; the sandbox refused the network. Nothing was earned, so the score stands. Dismiss it with the reason written out; the person signing reads that reason.
- A successful escape or a returned egress response is a finding at n=1. Raise it now.
- Rarity is not the signal. A class covering most of a task is the one to read first, because an exploit that works gets used everywhere.
- A confirmed finding is sample-shaped: `exclude`, `zero` and `score` are available beside `accept`. A score you have shown to be wrong should not be averaged in.
- Only boolean scanners open windows. Numbers and strings are recorded and never escalated; read them through the results, not the queue.

## Stuck samples

A `stuck` item names samples alive but idle past `stuck_after`. Nothing failed and nothing is waiting on a person; the task's clock keeps running. It is not an anomaly, and it clears itself when the sample moves.

The remedy is a ladder, and the item carries the command for its rung:

1. Cancel the tool call: `inspect ctl sample cancel-tool-call ...`. The call fails inside the sample, which continues. Yours only when `stuck_cancel:` in `_steward.yaml` admits it; the item then arrives owned by you. Run the command it carries, then `steward ack ID --by agent --reason ...`.
2. Cancel the sample. A person's: it records an outcome in the eval's data.
3. Requeue the sample. A person's: it discards everything the sample did.

Ask once. If the cancel was delivered and the call has not stopped, the item comes back with `:asked` in its id, owned by the person. That means climb a rung; never repeat the ask.

## Tuning

Tend ramps sample concurrency on its own, one step per clean window, and steps back on pushback. Every move shows in your next collect. A Hawk run is pinned by its config: no ramp actions, no tuning block, nothing to retune.

- `steward ramp hold --reason ...` stops the climb; levels stay where they are and the defensive cut stays active. Add a task identifier to hold one task. `steward ramp resume` re-arms it. Both are yours on your own judgement.
- A `tuning_proposal` is capacity tend may not take: a pinned `max_samples` that is saturated and clean, or a ramp at the top of its range with no pushback. Raise it, and when the person answers, `steward ack` with their answer. A different level is a different item.
- Never lower a pinned setpoint and never edit `samples_ramp`; both are the person's numbers. The one downward retune that is yours is under Looking inside a running worker.

## Notifying

`steward notify MESSAGE --kind attention|stopped --detail TEXT...` is the one thing that reaches the person when nobody is reading the conversation. `attention` means worth knowing, and work continues. `stopped` means nothing progresses until a person answers. A question you are blocked on is `stopped`, always.

Post rather than agonize, and do not batch. Steward limits its own posts to one per turn, so the channel is not tight. A skipped `stopped` is a run that waits all night for an answer nobody knew was wanted.

Notify and raise are separate acts; do both when you hand something over. With no channel configured the command fails. Say so in the conversation and in `analysis.md`, since adding one is the person's.

## Standing rules

When you ask the same question twice, or the person answers it the same way twice, the answer belongs in `_steward.yaml`. `policies:` is prose you apply in session; Steward carries it and never interprets it. `preauthorized:` maps a class pattern to the disposition it may receive, and tend applies it alone with nobody watching.

Propose the wording, not the idea; the two lines you intend to add can be answered yes or no. A `preauthorized:` pattern needs a yes to the pattern as written: agreeing that timeouts are usually worth re-running is not agreeing to `error:*Timeout*` at 3am. Write it once they answer, and say what you wrote. The file carries no `--by`, so the diff is the provenance.

## Writing `analysis.md`

`analysis.md` is the one file you and Steward share. Steward owns what sits between a marker pair, one pair per task, and rewrites it every turn:

```markdown
## cybench@openai/gpt-5

<!-- steward:begin cybench_0a1b2c3d -->
- scanned 48 of 50 transcripts
- 2 samples flagged for scoring integrity — scan:scoring_integrity:reward_hacking; no ruling yet
<!-- steward:end -->

Both flagged samples tried to read the grader file and failed. Dismissed: the attempt is in the transcript and the score is honest.
```

Write anywhere outside the markers, and never move or delete them; a section whose markers do not pair is left alone and stops updating. A section with no prose of yours raises `unwritten`, and "looked, nothing here" is an entry. Quote the decision, not just the outcome: which samples you opened, what the transcript showed, what would change your mind. Leave a removed task's section alone.

## Signoff

At 🏁 every task has finished and nobody has accepted the results. Tell the person by notification, then get the run to where their answer is one command. A refused `steward signoff` signs nothing and prints every blocker with its remedy, so run it early and work the list: windows to rule (`limit:operator` included, since it raises no item), errored samples to cover, tasks to settle, unreadable logs to acknowledge, `scanerror:` classes to rule. Acknowledging a stall or an unreadable log is a decision about the data, so give it a ruling's reason.

Three things the gate does not refuse over, so say them yourself. Read the `scanned` column aloud: 48 of 50 scanned is a different thing to sign than 50 of 50. Name the scan findings you dismissed and why; the person hears that from you, not from the file. When the readiness item names a log store, ask whether to publish and never assume; a published log is a claim other projects reuse sight-unseen.

Then, when they answer:

`steward signoff [--by NAME] [--note TEXT] [--publish]`

It runs a final turn, archives superseded attempts, records who signed and over what, and disarms the timer. It does not commit the journal; the workspace is the person's repository, so say so and do not commit for them.

## Context discipline

- Take the log directory from the snapshot; never assume `logs/`.
- Never read a full eval log. `read_eval_log(path, header_only=True)` gives status and counts; `read_eval_log_sample_summaries` or `samples_df` give per-sample data. The anomaly already carries the evidence detection read, so start there.
- Transcript analysis goes through a scan, never a raw log read.
- Narrow to the samples in question before opening anything.
