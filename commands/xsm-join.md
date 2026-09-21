---
description: Put this project in a named xsm project so sessions in other projects that also joined it can talk to this one
argument-hint: <project-name>
allowed-tools: Bash({{XSM}} join:*)
disable-model-invocation: true
---

This is a display command. There is nothing to decide.

Reply with the block between the markers, copied exactly, inside one code block.
Nothing before it, nothing after it. Do not translate, reword, summarise or
explain it, and do not call any tool.

<<<
!`{{XSM}} join $ARGUMENTS`
>>>
<!-- xsm-managed -->
