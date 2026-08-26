"""Display keys, shortened against the rows actually on screen.

`compute_display_keys` computes one key per task that is unique across the whole
manifest; this shortens those against a much smaller question — *which of these
five rows is which* — and the answers differ a lot. A sweep whose tasks all run
one model against their own solvers needs no `[default]@openai/gpt-5` on any
line, and that is twenty-two characters per row in a table with seventy-six.

The property under test throughout is that a rendering **still names exactly one
row**. Every case is therefore two assertions: what it renders as, and that the
renderings are distinct.
"""

from inspect_steward._evalset.display import KeyParts, shorten_keys


def parts(*rows: tuple[str, str, str]) -> list[KeyParts]:
    return [
        KeyParts(
            name=name, solver=solver, model=model, full=f"{name}[{solver}]@{model}"
        )
        for name, solver, model in rows
    ]


def test_names_alone_when_names_are_enough() -> None:
    short = shorten_keys(
        parts(("alpha", "default", "gpt-5"), ("beta", "default", "gpt-5"))
    )

    assert short.keys == ["alpha", "beta"]


def test_a_model_everyone_shares_is_stated_once_rather_than_per_row() -> None:
    # a sweep that is entirely one model is a fact about the run; twenty rows
    # each repeating it is not
    short = shorten_keys(parts(("alpha", "default", "gpt-5"), ("beta", "cot", "gpt-5")))

    assert short.keys == ["alpha", "beta"]
    assert short.model == "gpt-5"


def test_a_solver_everyone_shares_is_dropped_silently() -> None:
    # `[default]` on every row is the absence of information rather than a
    # shared fact worth a caption
    short = shorten_keys(
        parts(("alpha", "default", "gpt-5"), ("beta", "default", "o3"))
    )

    assert short.keys == ["alpha", "beta"]
    assert short.model is None


def test_a_colliding_name_expands_by_model() -> None:
    short = shorten_keys(
        parts(("alpha", "default", "gpt-5"), ("alpha", "default", "o3"))
    )

    assert short.keys == ["alpha@gpt-5", "alpha@o3"]
    # the model is now on the rows, so it is not also a caption
    assert short.model is None


def test_only_the_colliding_group_expands() -> None:
    # the property that keeps a sweep readable: one arm varying by model must
    # not lengthen every other row in the table
    short = shorten_keys(
        parts(
            ("solo", "default", "gpt-5"),
            ("beta", "default", "gpt-5"),
            ("beta", "default", "o3"),
        )
    )

    assert short.keys == ["solo", "beta@gpt-5", "beta@o3"]


def test_a_collision_the_model_cannot_split_falls_to_the_solver() -> None:
    short = shorten_keys(parts(("alpha", "cot", "gpt-5"), ("alpha", "react", "gpt-5")))

    assert short.keys == ["alpha[cot]", "alpha[react]"]
    assert short.model == "gpt-5"


def test_both_segments_when_both_are_needed() -> None:
    rows = parts(
        ("alpha", "cot", "gpt-5"),
        ("alpha", "cot", "o3"),
        ("alpha", "react", "gpt-5"),
    )

    short = shorten_keys(rows)

    assert len(set(short.keys)) == 3
    assert all(key.startswith("alpha") for key in short.keys)


def test_the_manifest_key_is_the_terminating_fallback() -> None:
    # a config-only sweep is invisible in name, solver, and model alike, and
    # `compute_display_keys` already resolved it with an ordinal suffix
    rows = [
        KeyParts(name="alpha", solver="default", model="gpt-5", full="alpha #1"),
        KeyParts(name="alpha", solver="default", model="gpt-5", full="alpha #2"),
    ]

    short = shorten_keys(rows)

    assert short.keys == ["alpha #1", "alpha #2"]


def test_an_orphan_carries_a_bare_name_and_sits_out() -> None:
    # no manifest row, so no solver and no model to expand by -- and it must
    # not drag the rows that do have them into expanding either
    rows = [
        *parts(("alpha", "default", "gpt-5"), ("beta", "default", "gpt-5")),
        KeyParts(name="left-behind", full="left-behind"),
    ]

    short = shorten_keys(rows)

    assert short.keys == ["alpha", "beta", "left-behind"]
    # one row has no model, so there is no model they all share
    assert short.model is None


def test_nothing_to_shorten() -> None:
    assert shorten_keys([]).keys == []
