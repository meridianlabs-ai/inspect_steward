# Inspect Flow – Inspect Steward

## Overview

``` bash
pip install "inspect-steward[flow]"
```

An [Inspect Flow](https://github.com/meridianlabs-ai/inspect_flow) spec is a [definition](./index.html.md#quick-tour) like any other, because `flow run` is itself a program culminating in one [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call. So Steward drives **Flow’s own CLI** rather than reaching into its internals, and intercepts at the boundary.

The division is clean: Flow owns everything up to the [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call — includes, implicit `_flow.py` inheritance, defaults merging, `NotGiven` semantics, `@after_load` and `@after_instantiate` hooks, and its `FlowOptions` mapping — and Steward owns execution from the boundary onward.

Name the spec `flow.yaml` in your workspace and everything on the other pages applies unchanged. A Python spec works too; `steward tasks flow.py -A key=value` passes arguments to a spec function.

Live [Task](https://inspect.aisi.org.uk/reference/inspect_ai.html#task) and [Model](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#model) objects in a spec are supported, incidentally — Flow itself rejects them in venv mode, and Steward’s always-re-execute model handles them naturally.

## What Steward overrides in a worker

Three flags, each for a reason worth knowing:

| override | why |
|----|----|
| `--set execution_type=inproc` | One process per task **is** Steward’s isolation model. A frontend that builds a virtualenv per worker is running a second one against it — and it puts the eval in a grandchild, so the pid Steward recorded and every liveness check keyed on it name the wrong process. |
| `--log-dir <scratch>` | Flow writes `flow.yaml` and a requirements snapshot into its log directory, and scans that directory for prior logs, all *before* [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) is reached. Pointing N workers at the run’s directory would mean N concurrent writes to the same two paths and N scans of a directory that grows all run. Each worker gets its own scratch directory instead; the run’s log directory is reached through the selection. |
| `--no-store-read --no-store-write` | Flow’s reuse store indexes logs by task identity so an identical task never runs twice. Under fan-out the read half races — N workers issue N identical queries and N copying writes to the same destinations — and the reuse buys nothing, because a worker runs its selected task regardless. Steward will operate both halves itself; until it does, the store is inert for Steward runs. |

None of this asks anything of Flow. All three are options `flow run` already ships.

## Two declarations do not survive

> **IMPORTANT: Importantexecution_type: venv loses dependencies and python_version**
>
> The virtualenv is where Flow applies what a spec declares in `dependencies:` and `python_version:`. Steward forces `inproc`, so those declarations are not applied — **the environment Steward runs in is the environment the eval runs in**, and provisioning it is yours.
>
> Steward warns once, when the definition is read, if a YAML spec asks for a venv at the top level. It does not refuse: the environment usually *does* satisfy the spec, since you provisioned it, and the check cannot see a declaration that arrives through an `include:` or one built in code by a Python spec. So treat the warning as a reminder rather than a guarantee that it fired.

> **NOTE: Noteflow run --resume will not resume a Steward run**
>
> Every `flow run` records its log directory as a global pointer, and because Steward hands Flow a scratch directory, a Steward run leaves that pointer aimed at a disposable path. A subsequent bare `flow run --resume` would resume nothing and start over into a directory nobody is watching.
>
> Steward does not write another tool’s global state to correct it. **Under Steward, the resume path is `steward launch` against the workspace** — which is also the amend path, and the only one you need.

## Scanners

A spec carrying `scanner:` cannot be run by Steward yet. Enumeration records that the spec scans, so this is refused early with an explanation rather than discovered when every worker fails.

## What fan-out costs

Everything Flow does before reaching [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) happens once per worker. That was measured, against a plain [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) baseline of 3.0s per worker:

|  | empty log directory | 150 logs |
|----|----|----|
| pointing every worker at the run’s log directory | 4.36s | 4.79s |
| a scratch directory per worker | **4.14s** | **4.21s** |

Two things to read out of it. The term that *grew with the run* is gone — the old path paid roughly 2.4ms per log already in the directory, which is about twelve seconds per worker at five thousand logs. And the residual ~1.1s is almost entirely the requirements freeze, which shells out twice and cannot be skipped from outside Flow, only redirected.

Four concurrent Flow workers writing into one flat log directory is verified end to end: four logs, correct identities, and no eval-set metadata written by any worker.
