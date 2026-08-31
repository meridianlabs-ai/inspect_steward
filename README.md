# Inspect Steward

Welcome to Inspect Steward, an agent that supervises evaluations on your behalf.

Steward plays the role of the human sitting in front of a running eval. It:

-   Watches progress as a run unfolds
-   Diagnoses problems as they surface
-   Adjusts runtime state in response

A steward acts on behalf of someone who isn't there. That's the relationship this package models: you start a run and step away, and the steward minds it.

Concretely, Steward runs each task of an eval set in its own process — so one crash costs one task rather than the run, and CPU-bound work actually runs in parallel — and reconciles the log directory against what your definition asks for on a schedule. A timer guarantees that happens with nobody watching; what needs judgement waits for someone who can exercise it.

```bash
pip install inspect-steward

steward init my-sweep     # create a workspace
steward tasks evalset.py  # see what a definition resolves to, before running it
steward launch            # start it, arm the timer, and return
steward status            # ask how it's going, whenever you like
```

Steward runs on macOS and Linux. Windows is not supported: a run has to outlive the process that started it, and the detached-process, control-socket, and process-table mechanisms that achieve it are POSIX-only.

Learn more about using Steward at <https://meridianlabs-ai.github.io/inspect_steward>.
