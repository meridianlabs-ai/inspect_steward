"""What a scheduled tend will not inherit, and how to notice before it matters.

A timer runs a tend under an environment nothing set up. launchd gives it a handful of variables, systemd `--user` gives it slightly different ones, and cron gives it almost nothing at all — none of them include the API key the shell that armed the timer is holding. The failure that produces is the worst kind available here: every interval all night, a worker starts, authenticates against nothing, and writes a log that says so, while `status.md` reports a fleet dutifully failing.

**The check is a diff, not a requirement.** Steward cannot know which provider a definition uses and does not try — guessing at a required list would refuse correct setups and miss unusual ones. It compares two environments instead: what the arming shell holds against what the workspace's `.env` holds. A credential in the first and not the second is exactly a credential that will be gone at 02:00, and that is a fact about this machine rather than a judgement about the eval.

**No value ever leaves this module.** The question *will this key exist under cron* cannot be answered without looking at what a `.env` line resolves to, so values are read — by python-dotenv, which is the parser that will read them again at 02:00 — and only names come back out. Nothing here holds one, writes one, or puts one in a message.

**The left-hand side is `os.environ` after `init_dotenv()` has run, and that is correct rather than a leak.** It looks wrong: in a VS Code terminal inspect loads `.env` with `override=True`, so `OPENAI_API_KEY=""` blanks the shell's real key before this comparison sees it, and the check then reports nothing to lose. But *nothing is lost* — the interactive tend the operator types and the scheduled one at 02:00 both read that same empty value and both fail the same way. The check exists to catch the **difference** between now and 02:00, and where `.env` wins in both places there is no difference to catch, only a broken workspace that the next hand-run tend makes obvious. Where `.env` does not override, the diff is the one this module describes and the empty value is caught.
"""

import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

CREDENTIAL = re.compile(
    r"(_API_KEY|_TOKEN|_SECRET|_SECRET_KEY|_PASSWORD|_CREDENTIALS)$"
)
"""Suffixes that mark a variable as a credential. Suffix rather than substring, so `ANTHROPIC_API_KEY` matches and `API_KEY_ROTATION_DAYS` does not."""

KNOWN = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "INSPECT_EVAL_NOTIFICATION",
        "STEWARD_NOTIFICATION",
    }
)
"""Credential variables whose names the pattern would miss. `AWS_ACCESS_KEY_ID` ends in neither a key nor a token, and an S3 `log_dir` is the case that made the environment check worth building.

**Both spellings of the channel are here, and both have to be.** Each holds an Apprise URL, and an Apprise URL is a bearer token with a scheme in front of it — `slack://xoxb-.../...`. They also configure each other (`_notify.channel`), so an arming shell that exports either and a `.env` that names neither is a run whose 02:00 turn cannot reach anybody — the failure notification exists to prevent, arriving through the one door notification cannot watch. `notification` in `_steward.yaml` is the third spelling and needs no entry: a committed file is still there at 02:00, which is the whole reason the key exists."""


CHANNEL = frozenset({"INSPECT_EVAL_NOTIFICATION", "STEWARD_NOTIFICATION"})
"""The two spellings of the notification channel, which are one capability.

**Compared as one, because resolution treats them as one.** Either name puts a channel in front of Steward *and* its fleet (`_notify.channel`), so a shell holding one and a `.env` holding the other is a workspace whose 02:00 turn can reach somebody — and refusing to arm it would be refusing over a difference that does not exist at the point it would matter. The check is about a capability going missing, not about a name.

Identity is not the question either, here or anywhere else in this module: a `.env` naming a *different* `OPENAI_API_KEY` than the shell also passes, because what cannot be known is which one the run should use and what can be known is whether one will be there.
"""

AMBIENT = frozenset(
    {
        "INSPECT_EVAL_MODEL",
        "INSPECT_EVAL_MODEL_ARGS",
        "INSPECT_EVAL_MODEL_BASE_URL",
        "INSPECT_EVAL_LOG_FILE_PATTERN",
    }
)
"""Variables that shape a run without passing through Steward, and so are not carried to 02:00.

**The manifest is what carries a setting overnight, and these never reach it.** Every variable Steward reads is resolved at launch and recorded in the committed manifest, which is what makes an exported `INSPECT_EVAL_LIMIT` still in force at the 02:00 tend. These four are read by inspect's *Python API* instead — by a provider looking up its own base URL, by `eval()` resolving a model when the definition names none — so they reach a worker only through the environment it inherits. Under a scheduler there is no such environment, and the loss is silent in the worst available way: a definition that named no model resolves a different one, computes a different `task_identifier` than the manifest recorded, and writes a log no tend ever looks for. The run then never converges and nothing says why.

Reported alongside the credentials because the remedy is identical — put it in `.env` — and separately worded because none of them is a secret."""


def credentials(environ: Mapping[str, str]) -> set[str]:
    """Which of these variables look like credentials.

    Args:
        environ: An environment.

    Returns:
        The names, ignoring any set to an empty value — an exported-but-empty variable carries nothing and its absence loses nothing.
    """
    return {
        name
        for name, value in environ.items()
        if value and (name in KNOWN or CREDENTIAL.search(name))
    }


def dotenv_names(path: Path) -> set[str]:
    """The variable names a `.env` will actually put something in an environment for.

    **Read with python-dotenv rather than by hand, because only the resolved value answers the question.** `OPENAI_API_KEY=`, `=""`, `=''`, `="   "`, `=${SOMETHING_UNSET}`, and a bare `OPENAI_API_KEY` with no `=` at all are six ways of writing a line that looks like an assignment and arrives as nothing. A parser that split on `=` would call most of them defined, and the two halves of this check have to agree about empty or the check is worse than nothing: `credentials` already ignores an exported-but-empty variable, so counting any of the six as defined would let a `.env` satisfy the check while handing the scheduler an unusable credential — precisely the overnight failure the check exists to prevent, now with a clean bill of health attached. Using the same library inspect will use means the two cannot drift.

    **One case is still invisible, and it has to be**: `KEY=${AMBIENT}` resolves against the environment doing the resolving, so it reads as defined here and will be empty under a scheduler that does not have `AMBIENT`. Answering that would mean re-resolving the file against the environment cron is going to supply, which nothing can know. The check is a diff, not a guarantee.

    Args:
        path: A `.env`, which need not exist.

    Returns:
        The names carrying a non-empty value, or an empty set where there is no readable file.
    """
    try:
        values = dotenv_values(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {name for name, value in values.items() if value}


def unavailable(env_file: Path, environ: Mapping[str, str]) -> list[str]:
    """Credentials this shell has that a scheduled tend will not.

    Args:
        env_file: The workspace's `.env`.
        environ: The environment arming is being done from.

    Returns:
        The names, sorted. Empty when a timer would run with everything this shell has.
    """
    ambient = {name for name in AMBIENT if environ.get(name, "").strip()}
    defined = dotenv_names(env_file)
    # either spelling of the channel covers both, since either one configures
    # both halves of the system -- see `CHANNEL`
    if defined & CHANNEL:
        defined = defined | CHANNEL
    return sorted((credentials(environ) | ambient) - defined)


def explain(missing: list[str], env_file: Path) -> str:
    """Why arming stopped, and what to do about it.

    Args:
        missing: What `unavailable` found.
        env_file: Where it should go.

    Returns:
        A message naming every variable, because the one left out is the one that breaks the night. The ambient settings are listed under their own line rather than called credentials, since they are not — and a message that called `INSPECT_EVAL_MODEL` a credential would read as a bug in the check rather than a fact about the shell.
    """
    ambient = [name for name in missing if name in AMBIENT]
    secrets = [name for name in missing if name not in AMBIENT]

    parts: list[str] = []
    if secrets:
        parts.append(
            f"{'this credential' if len(secrets) == 1 else 'these credentials'}:\n"
            + "\n".join(f"  {name}" for name in secrets)
        )
    if ambient:
        parts.append(
            f"{'this setting' if len(ambient) == 1 else 'these settings'}, which "
            f"inspect reads from the environment rather than from the manifest:\n"
            + "\n".join(f"  {name}" for name in ambient)
        )
    plural = "it" if len(missing) == 1 else "them"
    return (
        "a scheduled tend runs under a stripped environment and would not have "
        + "\nnor ".join(parts)
        + f"\nput {plural} in {env_file}, which both the tend and its workers "
        f"read, or arm with --no-env-check if the timer is meant to run "
        f"without {plural}"
    )


__all__ = [
    "AMBIENT",
    "CREDENTIAL",
    "KNOWN",
    "credentials",
    "dotenv_names",
    "explain",
    "unavailable",
]
