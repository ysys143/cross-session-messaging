"""The defaults xsm opens, and how a person closes one again.

User decision, 2026-10-01: a conversation the person wants between their own
sessions must never be blocked by a permission, a component failure or a rule
they cannot understand, so every behavior here defaults to the open one. Each
can still be switched: a key of the same name in ~/.xsm/config.json, or the
environment variable XSM_<NAME> (it wins over the file; "0", "false", "no" and
"off" turn a switch off).

`get(name)` is all a caller needs. A file or variable that holds something the
setting does not take is passed over (a bad variable falls through to the file,
a bad file to the default), so a typo can never close anything by accident. It is
config.policy that reads them, the same for every switch.
"""
from __future__ import annotations

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


def default(name: str):
    return POLICIES[name][0]


def get(name: str):
    """The value of one policy: the environment, else config.json, else the default.
    One reader with config.policy, which every other switch uses."""
    fallback, choices = POLICIES[name]
    return config.policy(name, fallback, choices=choices)


def defaults() -> dict:
    """Every switch xsm has and its open default: the ones above and the
    receive-side ones in config.POLICY_DEFAULTS (fail_open, remote_native, ...)."""
    out = dict(config.POLICY_DEFAULTS)
    out.update({name: default(name) for name in POLICIES})
    return out


def report() -> dict:
    """Every switch as it stands now, for `xsm doctor`."""
    out = config.policy_report()
    out.update({name: get(name) for name in POLICIES})
    return out
