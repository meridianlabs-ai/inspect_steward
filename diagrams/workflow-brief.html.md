# workflow-brief – Inspect Steward

## What this is

The second diagram on the documentation landing page for **Inspect Steward**, occupying the **Workflow** section and replacing the five-row phase table sitting there now. It is full content width, and it comes after the Quick Tour — so the reader has already seen `steward init`, a definition, and a coding agent being told to run the eval.

The one idea it must land: *between the one command you type and the results you sign off on, there is a loop you never see that keeps the run converging — and only what genuinely needs you reaches you.*

The audience is engineers who run large AI-model evaluations. They know what an eval is. By this point in the page they know Steward supervises a run; what they still do not know is **what it is actually doing all night**. This diagram answers that, and it is the last thing many readers will look at before deciding whether to try it.

## Its relationship to the hero diagram

The page already carries one diagram (`overview-brief.md`, the four-layer supervision stack under the opening paragraph). The two must not appear to disagree. They are two axes of one system:

|  | axis | answers |
|----|----|----|
| hero, top of page | authority | who absorbs what — a stack, escalation downward |
| **this one** | time | what happens when — `init` through `signoff` |

Two consequences, both binding:

- **Inherit the hero’s palette and type conventions exactly** (listed under *Constraints*). Two diagrams on one page in two visual languages read as two products.
- **Where this diagram shows the three parties, keep the hero’s vertical order** — Steward’s machinery above, the coding agent below it, the human below that. A second diagram that puts the human on top would read as a contradiction rather than as a second view.

## The system in three sentences

You create a workspace and write a definition; one `launch` captures it, commits it as the run’s desired state, and arms a timer. From then on a scheduled `tend` reconciles what is on disk against that desired state every ten minutes with nobody present — spawning what should be running, reaping what died, recording what it saw — while a coding agent reads the accumulated record and supplies the judgement the mechanical layer refuses to. Only decisions that need authority reach the human, and the run ends when a person signs off.

## The spine: four stages

The diagram is a sequence of four stages. Three are small; the third is the diagram.

**1. `steward init`** — you, once. Produces the workspace: a directory you and the agent co-inhabit and that a third party can pick up cold. Worth showing as two or three named artifacts rather than a featureless box, because “everything is written down” is a real claim of the product: `evalset.py` (your definition), `_steward.yaml` (your standing rules), `journal.jsonl` (the record).

**2. `steward launch`** — the agent, on your word. Once, and again to amend. Four acts in one command: capture the definition into a manifest, commit it as desired state, **arm the timer**, spawn the first workers. This is the walk-away moment and the diagram should feel like it — everything to the left is setup, everything to the right happens without you.

**3. The run** — nobody present. **This is the largest element on the canvas.** A *cycle*, not a stage that is passed through. Its contents are enumerated in the next section.

**4. `steward signoff`** — you, once. Accept the results, unschedule the monitor, archive superseded logs. The one command the agent may never run, and the only thing that ends a run.

## Inside stage 3, which is most of the work

Six components. The first four are the mechanical loop; the last two are the escalation path.

| component | what it is | note for the drawing |
|----|----|----|
| **the timer** | fires `steward tend` on an interval | The reason the run survives you closing the laptop. It is not the agent, and it is not you. |
| **`steward tend`** | one turn: observe, decide, act, record | The hub. Its four verbs are worth showing; they are the whole loop in four words. |
| **the fleet** | one worker process per task | Spawned and reaped by `tend`, and *detached* — they outlive the turn that started them. Several of them, not one. This is also where transcript scanning happens, if the definition asks for it (see *Scanning is not a stage*). |
| **`logs/` and `journal.jsonl`** | where results and the record accumulate | The closing arm of the cycle: the next turn reconciles the log directory against the manifest. This is what makes it a converging loop rather than fire-and-forget, and it is the single most important arrow in the diagram. |
| **the coding agent** | reads the record, supplies judgement | Diagnoses errors, groups them into classes, oversees the concurrency ramp, investigates. Reads through `steward collect`. |
| **you** | receive only what needs authority | Rulings on error classes, tool-call approvals, and eventually signoff. Deliberately outside the cycle. |

Two arrows leave the loop and one comes back in:

- **`tend` → agent** — *“escalates what needs judgement”*
- **agent → you** — *“escalates what needs authority”* (both labels are the hero diagram’s; reuse them verbatim so the two diagrams reinforce each other)
- **you → `steward launch`** — the amend path. Edit the definition, launch again, and only new or changed work runs. It should be visible but quiet: it is a real part of the workflow and not the main path.

Volume attenuates along that chain: almost everything is absorbed by the loop, some reaches the agent, very little reaches you.

## What must not be implied

These are the ways a diagram of this system goes wrong. Each one is a claim about the product that would be false.

- **The agent does not drive the loop.** The timer does. The agent may be attached, periodic, or absent for eight hours, and the fleet keeps converging regardless — only decisions accumulate. Anything that puts the agent *inside* the cycle, or makes it look like the thing calling `tend`, inverts the product’s central guarantee.
- **The human is not in the loop.** They are reached by it.
- **It is not a clean pipeline.** The middle overlaps: errors are investigated while the eval is still running, and transcripts are scanned as individual samples finish. Hard phase boundaries with a baton passing between them would be a lie about how a run actually goes.
- **`tend` does no long work.** Everything slow — a worker, a scan — is a detached child that a *later* turn observes finishing. A turn takes seconds.
- **Nothing signs off automatically.** The last arrow is a person’s.

## Exact strings

Wordsmithed against the product’s vocabulary. Propose typographic treatment freely; do not reword.

- `steward init` / “create the workspace: your definition, your standing rules, the record”
- `steward launch` / “capture the definition, commit it, arm the timer, spawn the fleet”
- “the timer” / “every ten minutes, unattended”
- `steward tend` / “observe · decide · act · record”
- “the fleet” / “one process per task”
- “logs and journal” / “results land; every turn is recorded”
- “coding agent” / “diagnose and recover from errors, tune concurrency, investigate”
- “human operator” / “rule on error classes, approve tool calls, sign off”
- `steward signoff` / “accept the results, unschedule the monitor, archive superseded logs”
- connectives: “escalates what needs judgement” · “escalates what needs authority” · “edit and launch again — only new or changed work runs” · “reconcile against the manifest”

## Constraints on the artifact

- **Excalidraw**, generated by a small Python script in this directory and rendered to SVG at docs build time — the same pipeline as `overview.py`. The design must therefore be expressible in Excalidraw primitives: rectangles (rounded, solid fills, opacity), straight and curved arrows, text in Excalidraw’s three fonts (hand-drawn, Helvetica-like, monospace). No gradients, shadows, custom fonts, or icons that cannot be drawn from those parts. No italic and no bold — size, weight of color, and spacing have to carry emphasis.
- **Palette, inherited and not renegotiated**: indigo for both Steward layers (the deterministic machine), violet for the coding agent, amber for the human, grey for all connective tissue.
- **Type, inherited**: monospace for things you type (the four commands), sans-serif for parties who act (the agent, you). The typeface alone distinguishes a command from an actor.
- **Renders on a white page in a ~700px content column**, light theme only for now, and must stay legible at that width. This is the hard one: four stages side by side at 700px gives each stage ~170px, which is not enough for the loop. **A vertical or L-shaped spine with the loop expanded is likely the right answer**, and it has the side benefit of matching the hero’s verticality.
- **Mobile**: the content column narrows to roughly 340px. The hero solves this by folding below the text; this diagram has no text to fold under, so it must degrade to a phone width on its own. Design for the narrow case first if the two conflict.
- House style follows the sibling projects’ docs diagrams: clean lines (no hand-drawn sketch roughness), soft 60%-opacity fills, generous whitespace.

## Scanning is not a stage

Worth stating plainly, because the prose this diagram replaces gets it wrong and a diagram would enshrine the mistake.

**Transcript scanning** — looking for the things a score cannot show you: a task the model declined, a grader that was gamed or simply broken, a misconfigured environment, a model that behaves differently when it can tell it is being tested — happens **online, inside the eval**. Scanners are authored with [Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout/) and attached to the run through the definition (`eval_set(scanner=...)`), like every other property of what is being measured. Transcripts are scanned as their samples complete, right after scoring, in the worker process that ran them; findings land in `<log_dir>/scans/` alongside the logs. **Steward orchestrates none of it** — there is no scan pass for `tend` to spawn and no scan phase for a run to enter.

So scanning is not a box on the spine, and it is not a component of the loop either. It is something the fleet is *already doing while it runs*, and if it appears at all it is a line on the worker, not an arrow between stages.

**What genuinely sits at the end is reading those results.** Any one worker sees one transcript; Steward sees the distribution across the whole run, which is what turns a pile of measurements into a shortlist — a scanner that fires on 4% of one run’s samples and 81% of the next run’s is a result no single transcript can give you. The agent investigates that shortlist and writes up what it meant. That is the terminal stage before signoff, and **investigation** is what to call it.

## What is not built yet

Three things in the workflow are designed and not shipped, and the brief should not hide it: **`launch --smoke`** (a bounded rehearsal before the full run), **investigation** (the reading of scan results described above, and the agent’s writeup of what they meant), and **`steward signoff`** itself. The prose on the page currently describes all three in the present tense.

Scanning is a fourth case and a different one: it exists and works in Inspect today, but a definition that declares a scanner is **refused at `steward launch`**, because one `scans/` directory shared by an eval set assumes a single writer and Steward’s whole execution model is N concurrent workers writing into one flat directory. That is an upstream fix rather than a Steward feature.

Two ways to handle the unshipped parts, and this is the user’s call rather than the designer’s:

- **Draw the full arc undifferentiated.** Consistent with the phase table the diagram replaces, and describes the product rather than the current commit. Costs an edit to the diagram when each piece ships — which is cheap, since the diagram is generated from a script.
- **Draw only the shipped spine** — `init` → `launch` → the loop → *(a person ends the run)*. Honest to the letter, and quieter; costs the reader the shape of where the product is going.

## What is open

Everything aesthetic, and one structural question. Aesthetically: orientation (subject to the width constraint above), proportions and rhythm, how the cycle in stage 3 is drawn — a literal ring, a back-arrow, a spiral, something else — the arrow-and-label treatment, whether the stages want a shared frame or a background, and how the fleet’s multiplicity is shown (three boxes, a stack, a fan).

Structurally: **how much of stage 3’s interior to draw at all.** Six components and eight arrows is a lot for a landing page, and there is a real version of this diagram that shows the timer, `tend`, and the fleet only, with the agent and the human as two arrows leaving the frame. The tension is that the escalation path is what makes the product interesting and the mechanical loop is what makes it trustworthy. Resolving that is the main design judgement here, and it is worth trying both.
