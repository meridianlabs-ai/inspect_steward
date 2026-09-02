# Inspect Hawk – Inspect Steward

## Overview

[Hawk](https://github.com/METR/hawk) eval set configs are supported as Steward definitions. Name the config `hawk.yaml` in your workspace and the rest of the documentation applies unchanged.

Steward runs `hawk local eval-set --direct` and takes over at the [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call, so Hawk still does its own work first: the tasks by solvers by models crossing, secrets resolution, the provider environment for gateway routing, and its sandbox annotations. Steward sees the fully lowered task list and owns execution from there.

This page is about running a Hawk config from an ordinary machine. Running Steward inside a Hawk runner pod is not supported yet.

## Concurrency

A Hawk infra config always supplies `max_samples`, defaulting to `1000`, and Steward honors an explicit value as written. Sample concurrency is therefore pinned, the [automatic ramp](./concurrency.html.md#automatic-ramp) does not run, and `samples_ramp` has nothing to govern. Set the number you want in the Hawk config, alongside the rest of your infra settings.

`max_sandboxes` comes from the infra config as well. `max_tasks` is honored as fleet width, the same as for any other definition.

## Local Limitations

|  |  |
|----|----|
| `scan:` | Hawk’s local path rejects online scanning, so a config declaring it cannot run locally at all |
| `isolation: strict` | needs an environment variable that only Hawk’s Helm template sets |
| eval-set hooks | a Steward worker runs an [eval()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval) rather than an eval set, so handlers registered at eval-set scope never fire. This costs Hawk’s completion metrics and its stuck-eval watchdog. Run- and sample-scoped handlers are unaffected, which is what keeps token refresh and `hawk stop` working. |

## Startup Cost

Every worker runs `hawk local eval-set`, so Hawk’s setup work happens once per worker. The `uv pip install` it performs is a no-op, because reading the config has already installed into the same interpreter, and uv takes an exclusive lock on the environment so concurrent installs queue instead of racing.

What is genuinely multiplied is remote work. A config with `secrets:` makes a Secrets Manager round trip per worker, and provider setup calls the gateway per worker, all at once. Throttling is the plausible failure, and it would show up as a burst of worker startup failures.
