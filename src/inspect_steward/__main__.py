"""`python -m inspect_steward` — the entry point a scheduler uses.

The console script would be shorter, but it lives in a venv's `bin/` that a stripped cron or launchd environment has no reason to have on `PATH`. An absolute interpreter and a module name need no `PATH` at all, and a crontab line is read by people.
"""

from ._cli.main import main

if __name__ == "__main__":
    main()
