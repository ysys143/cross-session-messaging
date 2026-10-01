"""The defaults xsm opens, and how a person closes one again.

User decision, 2026-10-01: a conversation the person wants between their own
sessions must never be blocked by a permission, a component failure or a rule
they cannot understand, so every behavior here defaults to the open one. Each
can still be switched: a key of the same name in ~/.xsm/config.json, or the
environment variable XSM_<NAME> (it wins over the file; "0", "false", "no" and
"off" turn a switch off).

`get(name)` is all a caller needs. The value is the default when the file or
the variable holds something the setting does not take, so a typo can never
close anything by accident.
"""
from __future__ import annotations

import os

from . import config

# name -> (default, the values it takes, or None for a switch)
POLICIES = {
    # What `xsm install` points the hooks, the MCP server and `xsm` on PATH at:
    # a copy outside ~/Documents (macOS denies that folder to a session's app and
    # every prompt was blocked, 2026-10-01), or the checkout itself (--dev).
    "runtime": ("snapshot", ("snapshot", "checkout")),
    # Whether install sets crossSessionInbound to "accept" in each Claude home, so
    # Claude delivers a peer message whatever the two permission modes are.
    "claude_inbound": ("accept", ("accept", "leave")),
    # Whether the allow list holds the messaging commands and tools (send, inbox,
    # list, who, held, ledger, status, doctor; xsm_send, xsm_inbox, xsm_post,
    # xsm_channel), so Claude's auto and default modes never stop a message.
    "allow_messaging": (True, None),
    # Whether an `xsm send` a person typed themselves connects the two folders it
    # needs (a link, a join or a reach) and delivers, their typing being the yes.
    "human_send_connects": (True, None),
}
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def default(name: str):
    return POLICIES[name][0]


def get(name: str):
    """The value of one policy: the environment, else config.json, else the default."""
    fallback, choices = POLICIES[name]
    text = (os.environ.get("XSM_" + name.upper()) or "").strip().lower()
    if text:
        if choices is None:
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
        elif text in choices:
            return text
    value = config.load().get(name, fallback)
    if choices is None:
        return value if isinstance(value, bool) else fallback
    return value if value in choices else fallback


def changed() -> dict:
    """{name: value} for each policy that is not at its default, for doctor."""
    return {name: get(name) for name in POLICIES if get(name) != default(name)}
