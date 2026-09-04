"""Notification — the only channel that reaches a human who is not watching.

`status.md` is a file on a machine nobody is looking at, and the journal is a record nobody is reading at 02:00. This package is what makes an unattended run answerable: a channel resolved from one setting that both halves of the system honour (`channel`), a post whose reason is explicit (`post`), rendered in whatever markup its target actually understands (`dialect`, `render`), and sent in a way that cannot cost a turn (`send`).
"""

from .channel import (
    COMMAND_LINE,
    DIRECTIVES,
    INSPECT_NOTIFICATION,
    STEWARD_NOTIFICATION,
    Channel,
    channel_apprise,
    describe_channel,
    establish_channel,
    usable_channel,
)
from .dialect import SLACK_FAMILY, Dialect, by_dialect, dialect_of
from .post import AGENT_KINDS, GLYPH, NARROW, WIDTH, Kind, Post
from .render import body_format, render
from .send import SEND_TIMEOUT, Delivery, send_post

__all__ = [
    "AGENT_KINDS",
    "COMMAND_LINE",
    "DIRECTIVES",
    "GLYPH",
    "INSPECT_NOTIFICATION",
    "NARROW",
    "SEND_TIMEOUT",
    "SLACK_FAMILY",
    "STEWARD_NOTIFICATION",
    "WIDTH",
    "Channel",
    "Delivery",
    "Dialect",
    "Kind",
    "Post",
    "body_format",
    "by_dialect",
    "channel_apprise",
    "describe_channel",
    "dialect_of",
    "establish_channel",
    "render",
    "send_post",
    "usable_channel",
]
