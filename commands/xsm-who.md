---
description: Show how other sessions address this session
allowed-tools: Bash({{XSM}} who:*)
disable-model-invocation: true
---

This session's xsm identity:

!`{{XSM}} who`

Report the address (`name@home`), the `[ref]`, and the working directory. If the
command says this session is not registered, say so plainly: the xsm hooks were
not installed when it started, so it cannot send or be addressed until it is
restarted after `xsm install`.
<!-- xsm-managed -->
