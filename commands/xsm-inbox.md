---
description: Show recent xsm messages and anything the gate refused
allowed-tools: Bash({{XSM}} ledger:*), Bash({{XSM}} held:*)
disable-model-invocation: true
---

This is a display command. There is nothing to decide.

Reply with the block between the markers, copied exactly, inside one code block.
Nothing before it, nothing after it. Do not translate, reword, summarise or
explain it, and do not call any tool.

<<<
!`{{XSM}} ledger --compact --last 10`
---
!`{{XSM}} held list`
>>>
<!-- xsm-managed -->
