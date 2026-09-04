"""Bytes, as an operator reads them.

Two places report memory — the startup bound a capture measured and the resident memory a running fleet is holding — and a reader compares them directly: *at most 2.1 GiB per worker* against *4.3 GiB across 2 processes* is a sentence only if both were rounded the same way. One function, so they cannot come to disagree about what 1.05 GiB is called.
"""


def format_bytes(value: int) -> str:
    """Render a byte count at the largest unit that still says something.

    Args:
        value: Bytes.

    Returns:
        GiB to one decimal, dropping to whole MiB below a tenth of a gibibyte — where `0.0 GiB` would read as *nothing* for something that is really 60 MiB.
    """
    gib = value / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 0.1 else f"{value / (1024**2):.0f} MiB"
