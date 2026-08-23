# Inspect Steward

Welcome to Inspect Steward, an agent that supervises evaluations on your behalf.

Steward plays the role of the human sitting in front of a running eval. It:

-   Watches progress as a run unfolds
-   Diagnoses problems as they surface
-   Adjusts runtime state in response

A steward acts on behalf of someone who isn't there. That's the relationship this package models: you start a run and step away, and the steward minds it.

Steward runs on macOS and Linux. Windows is not supported: a run has to outlive the process that started it, and the detached-process, control-socket, and process-table mechanisms that achieve it are POSIX-only.

Learn more about using Steward at <https://meridianlabs-ai.github.io/inspect_steward>.
