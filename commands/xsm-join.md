---
description: Let this folder join a named xsm project, so sessions in other repositories that also joined it can talk to this one
argument-hint: <project-name>
disable-model-invocation: true
---

This is a mechanical command. Do exactly this and nothing else.

Call the MCP tool `xsm_join` with `project` set to the first word of the
arguments below. It shows your user a form; their answer decides. Then reply
with the tool's result, copied exactly, inside one code block.

If there are no arguments, reply with exactly: usage: /xsm-join <project-name>

Arguments: $ARGUMENTS
<!-- xsm-managed -->
