"""Semantic logging protocol.

Library code produces :class:`Message` objects carrying a semantic level and
plain text; it never emits console markup, colors, or escape codes.  A
:class:`Logger` routes those messages to any sink (``print``, ``list.append``,
or a Rich-aware console function in the CLI).  ``render``/``render_all``
produce the plain ``[+]``/``[!]``/``[#]``/``[?]`` lines used by tests and
plain-sink callers.

The CLI owns the only ``rich`` dependency; nothing in the library imports it.
"""

from dataclasses import dataclass
from typing import Callable, Optional

LEVEL_INFO = "info"
LEVEL_SUCCESS = "success"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVEL_DIM = "dim"
LEVEL_QUESTION = "question"

LEVELS = (
    LEVEL_INFO,
    LEVEL_SUCCESS,
    LEVEL_WARNING,
    LEVEL_ERROR,
    LEVEL_DIM,
    LEVEL_QUESTION,
)

_PREFIX = {
    LEVEL_INFO: "[+]",
    LEVEL_SUCCESS: "[+]",
    LEVEL_WARNING: "[!]",
    LEVEL_ERROR: "[!]",
    LEVEL_DIM: "[#]",
    LEVEL_QUESTION: "[?]",
}


@dataclass(frozen=True)
class Message:
    """One log line: a semantic level plus plain, markup-free text."""

    level: str
    text: str

    def __post_init__(self):
        if self.level not in _PREFIX:
            raise ValueError(f"unknown level: {self.level!r}")


def render(message: Message) -> str:
    """Render a Message as a plain ``[+]/[!]/[#]/[?]`` prefixed line."""
    return f"{_PREFIX[message.level]} {message.text}"


def render_all(messages) -> list:
    """Render a sequence of Messages into plain ``[+]/[!]/[#]/[?]`` lines."""
    return [render(message) for message in messages]


class Logger:
    """Route :class:`Message` objects to a ``sink(message)`` callable."""

    def __init__(self, sink: Optional[Callable[[Message], None]] = None):
        self._sink = sink or (lambda _message: None)

    def log(self, level: str, text: str) -> "Logger":
        self._sink(Message(level=level, text=text))
        return self

    def info(self, text: str) -> "Logger":
        return self.log(LEVEL_INFO, text)

    def success(self, text: str) -> "Logger":
        return self.log(LEVEL_SUCCESS, text)

    def warning(self, text: str) -> "Logger":
        return self.log(LEVEL_WARNING, text)

    def error(self, text: str) -> "Logger":
        return self.log(LEVEL_ERROR, text)

    def dim(self, text: str) -> "Logger":
        return self.log(LEVEL_DIM, text)

    def question(self, text: str) -> "Logger":
        return self.log(LEVEL_QUESTION, text)