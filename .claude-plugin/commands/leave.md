---
description: Take this folder out of a named xsm project
argument-hint: <project-name>
disable-model-invocation: true
---

This is a mechanical command. Do exactly this and nothing else.

Call the MCP tool `xsm_join` with `project` set to the first word of the
arguments below and `leave` set to true. It shows your user a form; their
answer decides. Then reply with the tool's result, copied exactly, inside one
code block.

If there are no arguments, reply with exactly: usage: /xsm:leave <project-name>

Arguments: $ARGUMENTS
<!-- xsm-managed -->
