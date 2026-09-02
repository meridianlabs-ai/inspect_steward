# Concurrency – Inspect Steward

## Overview

The two most important concurrency dimensions to consider when running agentic evaluations are how many samples can be run in parallel (bounded by compute) and how much inference can be run in parallel (bounded by provider rate limits). It’s important to try to match these well during execution, so that you don’t pay for compute that sits idle while waiting on rate limits.

## Sandboxes

Sandboxes are either local (e.g. Docker) or remote (e.g. k8s, Daytona). In either case it’s important to optimize allocation of sandboxes. For the local case, available CPU cores and memory create an upper bound. For remote, while you may have infinite elastic capacity you won’t want to pay for an over-allocation of sandboxes idled by model rate limits.

By default, local sandboxes are limited to 2 \* available CPU cores for Docker, and unlimited for other providers. You can set an explicit `max_sandboxes` to override the default behavior.

## Samples and Tasks

By default, Steward runs 40 samples in parallel for each task (`max_samples=40`), and runs all tasks in the eval set in parallel (each in their own process). The 40 is a starting point: Steward [ramps sample concurrency](#automatic-ramp) toward 200 while the provider keeps up. Set `max_samples` yourself and Steward honors that number exactly and never ramps.

You can change these defaults in your call to [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set):

    evalset.py

``` python
eval_set(
  ...,
  max_tasks=10,    # run only 10 tasks at a time
  max_samples=100  # run 100 samples per task with NO auto-ramp
)
```

For the above configuration, 1,000 samples will be run in parallel, and stay there, because setting `max_samples` turns the ramp off. Left unset, the same 10 tasks would start at 400 samples and climb toward 2,000. If your sandbox infrastructure can handle that then the next thing to consider is whether your model API can keep up.

## Model Connections

By default, Steward uses Inspect’s adaptive connections concurrency controller to automatically find the highest sustainable `max_connections` for the model provider endpoint. You can customize this behavior by either setting a fixed `max_connections` or a custom configuration for adaptive connections in your call to [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set):

    evalset.py

``` python
eval_set(
  ...,
  max_connections=100  # fixed connections, no adaptive concurrency
)
```

Sample concurrency and connection concurrency are tuned together: while a ramp is active, Steward raises the adaptive controller’s ceiling to match the ramp’s, so the two do not work against each other.

## Automatic Ramp

While Steward starts with `max_samples=40`, it will also attempt to automatically ramp sample concurrency according to how well the model provider is handling the load.

During each `steward tend` operation, Steward checks to see if the following is true:

- The sample limiter is saturated (demand for more samples);
- The model provider showed no rate-limit pushback;
- No sample errored, and HTTP retries did not surge;

If all checks pass, then tend adds an additional 20 samples to the limit. This ramp continues up to 200 as long as the checks continue to pass.

On sustained pushback (rate-limit episodes in two consecutive windows) the ramp reverses. The connection ceiling is clamped down to where Inspect’s own controllers already fell, and sample concurrency steps back down so new samples stop being admitted against capacity that is not there. Samples already running are never interrupted. The way back up is stepwise, through the same gates.

Set the range with `samples_ramp` in the Steward config file or the `STEWARD_SAMPLES_RAMP` environment variable:

    _steward.yaml

``` yaml
samples_ramp: [40, 300]   # explore this range
samples_ramp: false       # never ramp; tasks stay at 40
```

Narrowing the range works on a live run: a task the ramp had taken to 200 comes back to a new ceiling of 100 at the next tend.

To freeze the climb without changing configuration:

    Terminal

``` bash
steward ramp hold --reason "provider looks unhappy"
steward ramp resume
```

Note that `max_sandboxes` is a machine-wide budget that bounds the fleet-wide sum of sample setpoints, where workers start at a share of the budget instead of at 40. The budget is your definition’s `max_sandboxes` if it sets one, otherwise what the sandbox provider reports (e.g. Docker’s 2 \* available CPUs). Elastic providers such as k8s have no limit.

## Worker Processes

Steward runs each task in its own worker process. This provides two things:

- Fault isolation. An OOM or a segfault costs one task, not the run.

- CPU parallelism. A task’s Python work (transcript construction, JSON serialization, `.eval` compression, non-model scorers) serializes behind the GIL in a single process, and runs in parallel across several.

The cost is startup, since every worker imports your definition and its dependencies before it can run. For plain [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) definitions that is seconds; [Flow](./flow.html.md) and [Hawk](./hawk.html.md) can have longer startup times.

Where startup is expensive, pack the fleet into fewer processes:

    _steward.yaml

``` yaml
max_workers: 8
```

This changes nothing about how much runs at once (that is `max_tasks` and `max_samples`), only how many processes carry it.

## Docker Configuration

Docker has a ceiling on concurrent sandboxes that has nothing to do with the size of your machine. Every sandboxed sample gets its own compose project, and every compose project gets its own bridge network, allocated from Docker’s `default-address-pools`. The built-in value carves two ranges into sixteen networks each, so a host runs about thirty sandboxes however many cores it has. Samples past that fail with:

    could not find an available, non-overlapping IPv4 address pool
    among the defaults to assign to the network

`steward launch` reads the daemon and tells you when your run wants more sandboxes than it can give, offering to write the fix into your `daemon.json`:

    daemon.json

``` json
{
  "default-address-pools": [
    { "base": "172.17.0.0/12", "size": 20 },
    { "base": "192.168.0.0/16", "size": 24 }
  ]
}
```

That is the same two ranges Docker already claims, carved finer: 512 networks instead of 32. Declining is the default, and prints the JSON along with the file to put it in.

Steward never restarts the daemon, and nothing takes effect until you do. A restart stops every running container on the machine, so the timing is yours. The launch prints the command for your platform.

See [Docker’s default address pools](https://straz.to/2021-09-08-docker-address-pools/) for background.
