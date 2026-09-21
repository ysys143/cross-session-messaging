---
description: Show the other agent sessions this machine can message right now
allowed-tools: Bash({{XSM}} list:*)
disable-model-invocation: true
---

Sessions registered with xsm:

!`{{XSM}} list --all`

Present this as it is. One line per session, keep the `name@home [ref]` form, and
keep the flags (`stale`, `unregistered`, `out-of-scope`, `would be held`) — each
one changes whether a message would arrive. Add nothing else.
<!-- xsm-managed -->
