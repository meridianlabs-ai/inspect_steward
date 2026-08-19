from inspect_steward import hello


def test_hello_returns_greeting() -> None:
    assert hello() == "Hello from inspect_steward"
