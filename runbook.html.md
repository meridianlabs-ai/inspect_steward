# Agent Runbook – Inspect Steward

## Overview

Steward is designed to be operated by a coding agent. The agent launches the run, works the decision queue, tunes concurrency within the bounds you set, and brings you the questions only you can answer.

The agent’s operating instructions ship with the package and are printed by `steward runbook`, so they always match the version of the CLI you are running. The `AGENTS.md` file in your workspace tells the agent to read them, so you never need to run the command yourself.

What follows is that text, reproduced verbatim.

# Steward runbook

You supervise an `inspect_ai` eval set for an operator who is not always present. The job has three parts:

1.  **Watch the run.** A timer runs `steward tend` without you: it starts workers, watches them, ramps them, and writes down what it could not decide. You read that on a schedule you arm yourself.
2.  **Investigate what goes wrong.** Errored samples, stalled tasks, scan findings. You read the logs and the transcripts, work out what happened, and decide whether the score can still be trusted.
3.  **Tell the operator what you found, and ask.** They rule and they sign. You put each finding to them in the words of their eval, with the evidence in brief and one question, and you record their answer.

Steward is the notebook. Every decision goes into it through a `steward` verb; it renders `status.md` for the operator and `steward collect` for you. It is not the subject of anything you say to them.

**Where things are.** The workspace is a directory. The definition is the operator’s statement of what to run; `_steward.yaml` is their rules for this run, read before you act. `journal.jsonl` is every event and decision, append-only. `analysis.md` is your findings. `status.md` and `anomalies.md` are rendered from the journal and the logs at every tend. The log directory is named in `steward collect`.

**How the run moves.** `steward launch` resolves the definition into a manifest of tasks and arms the tend timer. Each tend is one turn of the loop: reap finished workers, start the next, ramp concurrency, sync the scans, detect what changed, render the snapshot, post at most one notification. A worker is one `inspect` process running one or more tasks. It writes a log, scans each sample as that sample finishes, and answers `inspect ctl` while it runs. A log has landed when its file is complete and its status is known. A worker that stops inside a sample to ask an operator’s approval is parked until someone attaches with `inspect acp`.

## Talking to the operator

They see only what you write, and they know their eval: tasks, models, samples, scores, transcripts. Every message to them has one shape:

1.  **What you found.** One or two sentences in the eval’s words: which task, which samples, what happened to them.
2.  **The evidence in brief.** The two or three facts that establish it and where you looked, with the samples it covers in a table capped at five rows and the remainder counted. The full case goes in `analysis.md`; a reader who trusts you should be able to answer without opening anything.
3.  **What you propose, and the question.** Recommend one answer and name the others on offer, so they can confirm, pick, or say more. The question is the last line. Nothing follows it.

**One question at a time**, most important first, one message each. Do not restate the heading as the question. Keep the arithmetic out of it. The one message that carries several questions is a finished task’s scan findings, the second example below.

**Speak the eval’s language, never Steward’s.** Windows, classes, items, proposals, tends, collects, keys, ids and hashes are words from this runbook, and a message built from them is one they cannot answer. A word that is in this runbook and not in their definition does not go in a message to them.

| you have | you say |
|----|----|
| a window or a class | the samples and what they did: *5 samples in cybench@gpt-5 where the grader could not find its own tests* |
| a class key, an item id, a `prop-` id, a hash | nothing; the task and the finding in words |
| a disposition | what happens to the samples: drop from scoring, score 0, keep the score, keep it with a note in the report, nothing was wrong |
| a tend, a collect, a fold, the gate | *Steward*, or nothing |

**A collect with no question in it produces no message.** Do not report an investigation under way, a proposal you are holding, or what you expect to ask later; they learn the run’s state from `status.md` and the notifications, not from you. A finding you cannot yet propose on is one you are still investigating: keep investigating and say nothing. A scheduled collect that shows nothing new ends with no text at all: *nothing for me*, *unchanged*, *still waiting on your rulings* are messages, and the fourth one teaches them to stop reading. Once a finished run’s findings are in front of them, the next thing you write is your answer to theirs.

**Steward misbehaving is not theirs to debug.** Record what you saw with `steward note`, work around it where a ruling is honest, and tell them in one sentence only what changes for them. If you cannot work around it, say so in one sentence and stop.

**When they ask how it is going**, run `steward status --format md` and render what it printed in full, in its order, as markdown outside a code fence. Do not summarize it, and do not read `status.md` instead; it can be a full interval stale. A narrower question wants a narrower answer: *is it finished* is a sentence, not the snapshot. Your own reading goes below it, marked as yours, only where it adds something the snapshot does not show.

**Before you send, three checks.** Could they answer it knowing only their eval? Is it what you found, then the evidence, then the question? Is the question the last line?

Three messages in the shape. Errored samples in a running task:

``` markdown
## 14 samples in swe_bench_lite@openai/gpt-5 errored on a provider timeout

All 14 failed between 02:10 and 02:40 UTC with the same timeout from OpenAI, and every retry landed inside that window. The other 106 completed normally before and after; nothing about these 14 is unusual.

| sample | error |
|---|---|
| django-11099 | timeout after 3 retries |
| django-11133 | timeout after 3 retries |
| flask-4045 | timeout after 3 retries |

…and 11 more, all the same.

I propose to run the 14 again: the outage is over and nothing in them failed. The alternative is to drop them from scoring, leaving the task at 106 of 120. Run them again?
```

A finished task’s scan findings, one row per finding:

``` markdown
## cybench@openai/gpt-5 finished: 2 findings need your decision

| finding | samples | proposed | why |
|---|---:|---|---|
| build tools unreachable in the image | 2 | run them again | cargo could not reach the registry for twenty minutes; baseline and new tests both exit 101, and the registry is back |
| fetched the answer from the internet | 1 | score 0 | pulled the upstream fix and the gold test file from proxy.golang.org at [M39] |

Also noted, nothing to do: 5 samples where the grader could not find its own tests scored 0.0 as the benchmark computes it, and 4 samples refused the task and scored 0. The report says so for both.

Go ahead as proposed?
```

A stop:

``` markdown
## Stopped: the logs and the definition disagree about what gaia is

The worker for gaia@anthropic/claude-opus-5 refused to resume: its log names 165 samples and the definition now names 200. I have paused new work so nothing runs on a premise I doubt; the tasks already running are finishing.

Was the dataset changed on purpose? If so I will relaunch it; if not, tell me which is right.
```

### Stopping

Stop when a retry is not the answer:

- the smoke fails a second time
- a worker fails on identity or resume: the logs on disk and the definition disagree about what the eval is
- numbers fail sanity, such as a sample count that does not match the dataset
- the next step is destructive or irreversible
- a credential failure survives a retry
- you are about to relaunch the same task a third time

A stop is not a teardown. Healthy work keeps running. What you do:

1.  `steward note` the state and your hypothesis.
2.  `steward notify MESSAGE --kind stopped --detail ...` with the question you need answered.
3.  `steward pause --by agent --reason ...` only if new work would be spent on a premise you now doubt. Pausing stops scheduling; work in flight finishes.

Then leave it. The rest of the queue is still yours; the question waits for an operator. Do not work around the block.

## Each session

**Arm your own return first.** Nothing wakes you: the timer runs `steward tend` on its own and never calls you back. Before anything else, and unasked, point whatever your harness has for recurring work, a scheduled prompt or a background watch, at `steward collect` on roughly the tend interval, and handle what it returns. Keep it while the run has work: a task running or queued, a re-run pending, a ruling not yet written into the log. Take it down when the verdict is 🏁 and the decisions section says nothing is for you, and at signoff; a finished run waiting on the operator has nothing for you to read every ten minutes. Arm it again after anything you record that gives the run work: a rerun, a zero, a resume, a launch. If your harness has nothing of the kind, say so once.

Pick up cold, in this order:

1.  This runbook, then `_steward.yaml`.
2.  `steward collect`: what is true now, and what happened since you last looked. `--peek` reads without marking it read; `--since 0` reads the whole history.
3.  The anomalies it lists, with any precedent printed beside them.
4.  `analysis.md`, for what has been found and what it meant.

Everything you need is in the workspace. Nothing depends on a conversation you were not part of.

`steward collect` has three sections: what needs a decision, the run, and what happened. Its header carries two ages: tended is the last tend, collected is the last time you looked. The verdict line says the state in words. `status.md` is the operator’s page, shorter and without item ids; everything you act on is in `collect`.

**Collect regularly**, on the schedule you armed above. The timer runs the loop; nothing runs you. A tend never invokes an agent, so the items it hands you wait until you look, and after two intervals without a collect they go to the operator’s channel as if there were no agent. That is also what happens once you stand down at 🏁, and it is right: what is left is theirs.

## Investigating

**Trust the artifact, not the exit code.** A task whose every sample errored still exits zero. Read the smoke digest, the log’s status and the anomaly count. Completion is not success.

**Read the precedent first.** `steward collect` lists every open class with counts, an example, and any prior ruling as precedent; the 11pm decision usually answers the 2am question.

**A finished task’s findings.** Every scan window a task has arrives as your item at once when the task lands. Investigate them all before you write anything: read the scorer’s output and the flagged transcripts. Then three ways out. A flag the transcript does not bear out, dismiss yourself. A finding that changes no score — a refusal, an attempt that earned nothing, a grader that could not grade its samples — rule `score` yourself, so the report carries it, and mention it below the table in one line: they see it and are asked nothing. What is left would change a number — score 0, drop from scoring, run them again — and that is the decision: propose it, one proposal per disposition, and put it to them as rows. Only what they can act on is a row.

**Judging a scan finding.**

- **Behaviour that disqualifies a sample is disqualifying whether or not the environment allowed it.** An agent that fetched the reference solution did not solve the task, and whether the sandbox should have blocked it is a bug to report alongside your ruling, not the question to put in place of it. Do not turn a finding about the trajectory into a question about the machinery; the operator then has to answer the wrong question before anything can move.
- The bar is whether the score can still be trusted, not whether the behaviour was interesting. Escalate only where you can name the mechanism and cite the decisive messages. Otherwise rule `score` yourself, with what the model did in the reason. `dismiss` is for a flag the transcript does not bear out: the scanner was wrong, and there is nothing to note.
- **Read the scorer’s own output first.** It is the explanation on the sample’s score in `read_eval_log_sample_summaries`, and for a test-suite grader it names which tests failed. An agent whose own tests passed while the graded ones did not has failed the task.
- A failed attempt earns nothing. The model tried to read the grader and could not; the sandbox refused the network. The score stands, and the attempt is still a finding: rule `score` yourself with the attempt in the reason, so the report carries it.
- A transient fault is not the model’s. An image pull that failed, a registry the sandbox could not reach for twenty minutes: the samples can run again, and a `rerun` ruling invalidates just those samples in the finished log and relaunches the task for them. Propose *run them again*, and say what the fault was.
- A benchmark broken by construction is not a fault a re-run heals. A grader that runs none of its required tests, a baseline that fails before any change, a sample the verifier never grades: dropping those samples makes a number nobody else’s run has, and there is nothing for the operator to decide. Rule `score` yourself with what the grader could not do in the reason, note it below the table, and leave the fault to them to report upstream.
- A successful escape or a returned egress response is a finding at n=1, and the answer is `zero`.
- Rarity is not the signal. A class covering most of a task is the one to read first, because an exploit that works gets used everywhere.
- Zero a confirmed finding rather than excluding it: dropping the samples where a model cheated raises its score.
- Only boolean scanners open windows. Numbers and strings are recorded and never escalated; read them through the results, not the queue.

**A running worker.** A log says what a sample did once it landed. `inspect ctl` says what a worker is doing now, and is how you diagnose a task that is still running. `inspect ctl task` lists the running tasks with the id every other command takes as `TASK`. Every command accepts `--json`.

| to see | run |
|----|----|
| a task’s samples and their status | `inspect ctl sample list TASK` |
| the samples that errored, with the message | `inspect ctl sample errors TASK --content` |
| one sample’s status, timing, tokens, score and error history | `inspect ctl sample show TASK SAMPLE_ID` |
| its conversation right now | `inspect ctl sample messages TASK SAMPLE_ID --all` |
| its transcript, event by event | `inspect ctl sample events TASK SAMPLE_ID` |
| the worker’s configuration in force | `inspect ctl config TASK` |

Reads are yours. Mutations go as far as a pre-authorization and no further: `sample cancel-tool-call` when `stuck_cancel` admits it, and lowering a limit through `inspect ctl config` while containing an incident. `task pause`, `task cancel`, `sample cancel` and `sample requeue` are an operator’s. Record every mutation with `steward note`. It leaves no other trace in the workspace.

## Recording what was decided

Record the answer with the verb the item names, then `steward tend` so it takes effect now, and arm your return again if you had stood down. The mechanics are `steward` verbs and `inspect ctl`; reach for them before a script.

### What you may do

**Without asking.** `launch --smoke` and `launch`. `tend`, `status`, `collect`. `raise`, `investigate`, `propose`, `notify`, `note`. `ramp hold` and `ramp resume`. Every `inspect ctl` read, and lowering a worker’s concurrency through `inspect ctl config` while containing an incident. `ack --by agent` for an item you resolved yourself, when nobody else would need to know. Writing `analysis.md`.

**Only with an operator’s answer, recorded in `--by`.** `ack --by operator`. `rule`, except `dismiss` and a scan finding’s `score` (see Anomalies). `signoff`. `pause` and `resume`, except during a stop (see Stopping). `launch --accept CHECK` and `launch --accept-archive`. Writing `_steward.yaml`. Any `inspect ctl` mutation of a sample or task; a pre-authorization in `_steward.yaml` is an answer already given.

**Never.** Edit the definition; raise the change as a question instead. Move or delete a log, not even an empty one. Answer a parked approval or `ask_user`; name the worker, print the command that attaches to it, notify, and wait.

`--by` records who decided, never who typed. `rule` and `signoff` record the operator’s name on their own; pass `--by` only for someone else. `ack`, `pause` and `ramp` take `operator` or `agent`.

### The queue

What a tend cannot decide becomes an item in the queue. Every item has a kind, an owner (an operator or you), and the command that resolves it. A class of failed samples or scan findings is an anomaly. Each occurrence of a class opens a window, and a ruling closes it.

`collect` prints your queue: every open item you have not handed off, with its owner and the command that resolves it. Any unambiguous prefix of an id works; a finding’s label or a task’s display key works for anomalies, and `ack stalled:<task>` for items. An id changes when its condition changes, so an acknowledgement covers the condition as it was. Every kind not in the table under Reference is the operator’s: raise it. The item’s summary says what it is, and its action says what resolves it.

**`steward ack ID --reason ... --by operator|agent`** disposes of an item: it leaves every surface and reappears under what happened with who decided. It refuses what has its own verb, and names the verb.

**`steward raise ID [--note ...]`** hands an item to the operator who can decide it and closes nothing; it stops appearing in your queue. It refuses your own items, since nobody else would close them.

### Anomalies

Failures that mean the same thing share a class: an exception at a raising frame (`error:TimeoutError@openai/_client.py:post`), a task that died a particular way (`task:no-log-exit:...`), samples an operator killed (`limit:operator`), a task whose every score is zero (`score:zero:...`), samples a scanner flagged (`scan:SCANNER:LABEL:TASK:HASH`), a scanner that failed (`scanerror:SCANNER`).

Your verbs, in order:

1.  `steward propose CLASS... --action DISPOSITION --reason ...` makes classes that want the same answer one question. Classes wanting different answers are different proposals. It prints the sentence the operator will read; put that to them, not the id.
2.  `steward rule` records the operator’s answer, two ways. `steward rule TASK --reason ...` takes every proposed finding of that task as proposed, where `TASK` is its display key. `steward rule FINDING --disposition D --reason ...` changes one row, where `FINDING` is the finding’s label when it is unique and `label:task` when it is not. `steward rule --proposal ID --reason ...` still works.

`steward investigate CLASS --note ...` is for a class you have to leave mid-way: it marks the class as being worked, and the note is for the next session.

**Two rulings are yours alone.** A class you have disproved: `steward rule CLASS --disposition dismiss --by agent --reason ...`. A scan finding that changes no score: `--disposition score`, which keeps every score as recorded and puts one line in the report. Make them as soon as the investigation gets there, rather than proposing them; the verb refuses `--by agent` for every other disposition.

`rerun`, `exclude` and `zero` are carried out by the tend. Run nothing. A `rerun` requeues samples in a running task in place, and has a landed log’s samples invalidated for relaunch; the window stays open until the re-run lands, and the same samples failing again come back as a question for the operator. An `exclude` is written into the log within a turn: the samples become unscored, with the reason, and the metrics are recomputed over the rest. A `zero` takes minutes, since what a zero is depends on the task’s scorer: Steward starts the task on just those samples in a scratch directory, stops each as it begins, and writes the scorer’s verdict on that empty attempt into the log; the transcript stays. Until a mark is written the report says so, and signoff waits. A class flagged as substrate (credentials, disk, storage) gets no rerun proposal from you; re-running into broken machinery burns the work twice.

Two classes are quiet on purpose. A `task:` window heals itself when the respawn brings the task home; if it does not, `stalled` is the real question. A `limit:operator` window raises no item, since the operator knows what they did; it waits for the signoff conversation.

### Scan windows

Nothing starts a scan: the worker runs each scanner as a sample finishes, and a `scan:` window opens when its rows land. Windows are per task, and one raises no item until its task has landed. A flagged sample in a running task is not yours to look at yet; `collect` lists its window as waiting. A ruling on one task’s window touches no other task’s samples, and the same finding ruled on another task is printed beside the window as precedent. Record each answer with `steward rule`. The task’s finished notification waits for your proposals and goes out with them; it stops waiting after six tends.

### Notifying

`steward notify MESSAGE --kind attention|stopped --detail TEXT...` is how you reach the operator when nobody is reading the conversation. `attention` means worth knowing, and work continues. `stopped` means nothing progresses until an operator answers. A question you are blocked on is `stopped`, always.

Post rather than agonize, and do not batch. Steward limits its own posts to one per turn, so the channel is not tight. A skipped `stopped` is a run that waits all night for an answer nobody knew was wanted.

Steward’s own posts already announce every new item an operator owns. Notify for what you found that no item says.

## Reference

### Launching

**Smoke first.** `steward launch --smoke` runs a few samples of every task under a wall-clock cap and launches nothing.

The smoke fails on any errored sample and on four named checks: `context_window`, `reasoning`, `reasoning_api`, `scan_coverage`. `unexercised` and `undetermined` are not failures. Fix what failed rather than routing around it. `--accept CHECK` waives one check by name; an errored sample cannot be waived. Do not project the run’s spend from the smoke.

A smoke that fails twice is a stop. Notify it explicitly: nothing posts before the first worker starts.

**Then launch.** `steward launch` shows what will change and starts the run.

**The tend timer.** `launch` arms it. `steward timer status` is how you check it, not the system scheduler. A run launched `--no-timer` carries an `unsupervised` item until an operator acknowledges it, which says they are driving by hand.

### Item kinds

| kind | owner | it means | you |
|----|----|----|----|
| `stalled` | operator | a task stopped progressing after its attempts and will not respawn | find out why, then raise |
| `drift` | operator | the definition changed since it was captured | raise; never edit it back |
| `stuck` | operator, or you when pre-authorized | a sample has been quiet longer than `stuck_after` | see Stuck samples |
| `parked` | operator | a worker is waiting on an operator inside a sample | raise and notify; never answer it |
| `tuning_proposal` | operator | a task could take more concurrency than its setting allows | relay it; ack with the answer |
| `signoff_ready` | operator | every task finished and nothing is open | see Signoff |
| `anomaly` | you while open; operator once proposed, or after a failed re-run | a class of failures or findings | see Anomalies |
| `unreadable` | you | a log file could not be read | look at the file; ack with what it was, or ask |
| `action_failed` | you | something a tend tried to do failed | do it by hand, or find out why |
| `unwritten` | you | a task has no write-up in `analysis.md` | write the section |
| `journal_damage` | you | journal lines could not be read | read them; ack with what they held |

### Dispositions

| disposition | it says | honest for |
|----|----|----|
| `rerun` | run these samples again | anything but `scanerror:` |
| `exclude` | drop these samples from scoring: written into the log as unscored, with the reason | `error:`, `limit:`, `scan:` |
| `zero` | score these samples as the task’s scorer scores an empty attempt: Steward runs the scorer for them | `error:`, `limit:`, `scan:` |
| `score` | score these samples as recorded | `error:`, `limit:`, `scan:` |
| `accept` | the data stands, with a caveat the report carries (`--effect`) | anything but `error:` |
| `dismiss` | looked, nothing here | anything |

### Stuck samples

A `stuck` item names samples alive but idle past `stuck_after`. Nothing failed and nothing is waiting on an operator; the task’s clock keeps running. It is not an anomaly, and it clears itself when the sample moves.

The remedy is a ladder, and the item carries the command for its rung:

1.  Cancel the tool call: `inspect ctl sample cancel-tool-call ...`. The call fails inside the sample, which continues. Yours only when `stuck_cancel:` in `_steward.yaml` admits it; the item then arrives owned by you. Run the command it carries, then `steward ack ID --by agent --reason ...`.
2.  Cancel the sample. An operator’s: it records an outcome in the eval’s data.
3.  Requeue the sample. An operator’s: it discards everything the sample did.

Ask once. If the cancel was delivered and the call has not stopped, the item comes back with `:asked` in its id, owned by the operator. That means climb a rung; never repeat the ask.

### Tuning

Tend ramps sample concurrency on its own, one step per clean window, and steps back on pushback. Every move shows in your next collect. A Hawk run is pinned by its config: no ramp actions, no tuning block, nothing to retune.

- `steward ramp hold --reason ...` stops the climb; levels stay where they are and the defensive cut stays active. Add a task identifier to hold one task. `steward ramp resume` re-arms it. Both are yours on your own judgement.
- A `tuning_proposal` is capacity tend may not take: a pinned `max_samples` that is saturated and clean, or a ramp at the top of its range with no pushback. Raise it, and when the operator answers, `steward ack` with their answer. A different level is a different item.
- Never lower a pinned setpoint and never edit `samples_ramp`; both are the operator’s numbers. The one downward retune that is yours is under A running worker.

### Standing rules

When you ask the same question twice, or the operator answers it the same way twice, the answer belongs in `_steward.yaml`. `policies:` is prose you apply in session; Steward carries it and never interprets it. `preauthorized:` maps a class pattern to the disposition it may receive, and tend applies it alone with nobody watching.

Propose the wording, not the idea; the two lines you intend to add can be answered yes or no. A `preauthorized:` pattern needs a yes to the pattern as written: agreeing that timeouts are usually worth re-running is not agreeing to `error:*Timeout*` at 3am. Write it once they answer, and say what you wrote. The file carries no `--by`, so the diff is the provenance.

### Writing `analysis.md`

`analysis.md` is the one file you and Steward share. Steward owns what sits between a marker pair, one pair per task, and rewrites it every turn:

``` markdown
## cybench@openai/gpt-5

<!-- steward:begin cybench_0a1b2c3d -->
- scanned 48 of 50 transcripts
- 2 samples in cybench flagged for reward hacking; no ruling yet
<!-- steward:end -->

Both flagged samples tried to read the grader file and failed. Scores kept as recorded: the attempt is in the transcript and nothing was earned.
```

Write anywhere outside the markers, and never move or delete them; a section whose markers do not pair is left alone and stops updating. A section with no prose of yours raises `unwritten`, and “looked, nothing here” is an entry. Quote the decision, not just the outcome: which samples you opened, what the transcript showed, what would change your mind. Leave a removed task’s section alone.

### Signoff

At 🏁 every task has finished and nobody has accepted the results. Tell the operator by notification, then get the run to where their answer is one command. A refused `steward signoff` signs nothing and prints every blocker with its remedy, so run it early and work the list: windows to rule (`limit:operator` included, since it raises no item), errored samples to cover, tasks to settle, unreadable logs to acknowledge, `scanerror:` classes to rule. Acknowledging a stall or an unreadable log is a decision about the data, so give it a ruling’s reason.

The gate does not refuse over these, so say them yourself. Render the by-task table from `steward status --format md`; it is the samples they are signing over. Read the `scanned` column of `steward collect` aloud: 48 of 50 scanned is a different thing to sign than 50 of 50. Name the scan findings you dismissed or noted and why; the operator hears that from you, not from the file. When the readiness item names a log store, ask whether to publish and never assume; a published log is a claim other projects reuse sight-unseen.

Then, when they answer:

`steward signoff [--by NAME] [--note TEXT] [--publish]`

It runs a final turn, archives superseded attempts, records who signed and over what, and disarms the timer. It does not commit the journal; the workspace is the operator’s repository, so say so and do not commit for them.

### Context discipline

- Take the log directory from `steward collect`; never assume `logs/`.
- Never read a full eval log. `read_eval_log(path, header_only=True)` gives status and counts; `read_eval_log_sample_summaries` or `samples_df` give per-sample data. The anomaly already carries the evidence detection read, so start there.
- Transcript analysis goes through a scan, never a raw log read.
- Narrow to the samples in question before opening anything.
