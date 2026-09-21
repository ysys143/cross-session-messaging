---
description: Show the other agent sessions this machine can message right now
allowed-tools: Bash({{XSM}} list:*)
disable-model-invocation: true
---

Sessions registered with xsm:

!`{{XSM}} list --all`

The list above is the answer. Reply with nothing at all unless a row carries a
flag that would stop a message arriving (`stale`, `unregistered`, `out-of-scope`,
`would be held`) — then say only that, in one line.
<!-- xsm-managed -->
