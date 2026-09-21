---
description: Check the xsm installation and show the gaps it cannot close
allowed-tools: Bash({{XSM}} doctor:*)
disable-model-invocation: true
---

!`{{XSM}} doctor`

Report what is wrong, if anything: an interpreter that is too old, a home whose
hooks are missing (`SessionStart:add` means not installed), hook errors, or a
missing codex binary. Repeat the `limit` lines as they are — they are known gaps,
not faults to fix.
<!-- xsm-managed -->
