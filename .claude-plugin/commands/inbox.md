---
description: Show recent xsm messages and anything the gate refused
allowed-tools: Bash(xsm ledger:*), Bash(xsm held:*)
disable-model-invocation: true
---

This is a display command. There is nothing to decide.

Reply with the text between the markers, copied exactly, as it is — not in a
code block, so the tables show as tables. Nothing before it, nothing after it.
Do not translate, reword, summarise or explain it, and do not call any tool.

<<<
**Recent messages to and from this session**

!`xsm ledger --table --mine --last 5`

**Held by the gate** (ids and full text: `xsm held list`)

!`xsm held list --table`
>>>
<!-- xsm-managed -->
