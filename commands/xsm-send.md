---
description: Send a message to another agent session
argument-hint: <name@home | ref:xxxxxx> <message>
allowed-tools: Bash({{XSM}} send:*)
disable-model-invocation: true
---

This is a mechanical command. Do exactly these two steps and nothing else.

1. Run one Bash command: `{{XSM}} send <TARGET> --text <MESSAGE>`, where TARGET is
   the first word of the arguments below and MESSAGE is everything after it,
   passed as a single shell-quoted argument. Do not rewrite the message.
2. Reply with the command's output, copied exactly, inside one code block.
   Nothing before it, nothing after it. Do not translate or explain it.

If there are no arguments, reply with exactly: usage: /xsm-send <target> <message>

Arguments: $ARGUMENTS
<!-- xsm-managed -->
