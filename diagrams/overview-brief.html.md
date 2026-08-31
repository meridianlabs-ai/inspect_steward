# overview-brief – Inspect Steward

## What this is

The hero diagram on the documentation landing page for **Inspect Steward**, sitting directly under the opening paragraph. It is the first visual a newcomer sees. The one idea it must land: *you launch a long evaluation run and walk away; a supervision stack absorbs the work at the lowest level able to handle it, and only the decisions that are genuinely yours reach you.*

The audience is engineers who run large AI-model evaluations. They know what an eval is; they have never seen Steward.

## The system in three sentences

Inspect Steward runs and supervises long-running AI evaluations. You launch a large run and walk away: a scheduled loop keeps it converging with nobody present, a coding agent handles the work that needs judgement, and Steward escalates to a human only the decisions that need one. The diagram is that division of responsibility, drawn as a stack.

## The four layers, top to bottom

1.  **`steward launch`** — a CLI command, and the human’s single foreground act. Runs `evalset.py` (the user’s eval definition), spawns one worker process per task, arms a timer. Happens once.
2.  **`steward tend`** — a CLI command fired by the timer every ~10 minutes with nobody watching. The deterministic loop: respawns what died, reaps finished processes, records what it observed, reports status. Exercises **no judgement** — this is the machine half.
3.  **a coding agent** — an AI agent (e.g. Claude Code) supervising the run. Supplies **judgement**: diagnoses and recovers from errors, tunes concurrency, scans transcripts for unexpected failure modes. Explicitly barred from authority — it may never approve or sign off.
4.  **you** — the human. Supplies **authority**: rulings on anomalies, approvals, final sign-off on results.

Connectives between layers (currently small arrows with grey labels):

- launch → tend: *“then every ten minutes, unattended”* — this is the walk-away moment, the product’s core promise.
- tend → agent: *“escalates what needs judgement”*
- agent → you: *“escalates what needs authority”*

So flow runs downward, and volume attenuates: almost everything is absorbed in the top two layers; very little reaches the bottom.

## Current deliberate choices (change only with a reason)

- **Equal-width layers** — they are layers of one system, not shrinking quantities of work. (An earlier funnel version narrowed the boxes; it was rejected.)
- **Monospace for the two command layers, sans-serif for the two actors** — the type alone distinguishes “things you type” from “parties who act.”
- **Color groups the halves**: indigo for both Steward layers (the deterministic machine), violet for the agent, amber for the human. Grey for all connective tissue.
- **Text is settled** — every label was wordsmithed against the product’s vocabulary. Propose typographic treatment freely, but do not reword. Exact strings:
  - `steward launch` / “execute eval set and schedule automated monitoring”
  - `steward tend` / “respawn failures, report status, send notifications”
  - “coding agent” / “diagnose and recover from errors, tune concurrency”
  - “human operator” / “approve tool calls, rule on ambiguous cases, sign off”

## Constraints on the final artifact

- The production version is an **Excalidraw file** rendered to SVG at docs build time, generated from a small Python script (`docs/diagrams/overview.py`). Mockups may be free-form, but the winning design must be expressible in Excalidraw primitives: rectangles (rounded, solid fills, opacity), straight/curved arrows, text in Excalidraw’s three fonts (hand-drawn, Helvetica-like, monospace). No gradients, shadows, custom fonts, or icons that can’t be drawn from those parts.
- Renders on a white page in a ~700px content column; must stay legible at that width. Light theme only for now.
- House style follows the sibling projects’ docs diagrams: clean lines (no hand-drawn sketch roughness), soft 60%-opacity fills, generous whitespace.

## What is open

Everything aesthetic: proportions, spacing and rhythm, the arrow-and-label treatment between layers, palette refinement within the color-grouping logic above, how the command layers’ monospace titles sit against the subtitle lines, whether the stack wants a frame or background. The structure (four equal layers, downward escalation, the three connective labels) and the wording are fixed.
