"""Reading a `Post`'s body back out, for tests that assert on what a turn posts.

A post carries its body as blocks (`inspect_steward._notify.block`); these pull
the two a trigger test usually wants — the item bullets and the task table — so
the assertions stay about content rather than about block plumbing.
"""

from inspect_steward._notify import Bullets, Post, Table, Text


def bullets(post: Post) -> list[str]:
    """The post's item lines — what a person is being woken for, or nothing."""
    for block in post.blocks:
        if isinstance(block, Bullets):
            return list(block.items)
    return []


def task_table(post: Post) -> list[str]:
    """The post's first fenced table — the task table — or nothing."""
    for block in post.blocks:
        if isinstance(block, Table):
            return list(block.lines)
    return []


def after_table(post: Post) -> list[str]:
    """The paragraph the task table is followed by — the `... N more` count and the shared model — or nothing."""
    blocks = list(post.blocks)
    for index, block in enumerate(blocks):
        if isinstance(block, Table):
            nxt = blocks[index + 1] if index + 1 < len(blocks) else None
            return list(nxt.lines) if isinstance(nxt, Text) else []
    return []


__all__ = ["after_table", "bullets", "task_table"]
