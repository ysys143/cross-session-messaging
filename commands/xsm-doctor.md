---
description: Check the xsm installation and show the gaps it cannot close
allowed-tools: Bash({{XSM}} doctor:*)
disable-model-invocation: true
---

!`{{XSM}} doctor`

The report above is the answer. Reply with nothing unless something is broken: an
interpreter that is too old, a home whose hooks are missing (`SessionStart:add`
means not installed), or hook errors. The `limit` lines are known gaps, not
faults — do not comment on them.
<!-- xsm-managed -->
