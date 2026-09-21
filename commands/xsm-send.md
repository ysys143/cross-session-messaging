---
description: Send a message to another agent session
argument-hint: <name@home | ref:xxxxxx> <message>
allowed-tools: Bash({{XSM}} send:*)
disable-model-invocation: true
---

Send this to another session. The first word of `$ARGUMENTS` is the target, the
rest is the message.

Run `{{XSM}} send` with the target as one argument and the message as the value
of `--text`, quoted so the shell passes it through unchanged. Do not build the
command by pasting `$ARGUMENTS` in as shell text.

Then report the result line exactly as printed. It means:

- `delivered` — the receiving session recorded it
- `sent-unconfirmed` — queued; nothing has confirmed arrival yet, so do not
  claim it was delivered
- `refused` — nothing was sent; the reason says why, and if a name matched
  nothing or several sessions the output lists what exists
- `error` — the delivery path failed; the reason names the cause

If the target was ambiguous or unknown, show the candidate list and ask which
one, rather than picking for the user.

Arguments: $ARGUMENTS
<!-- xsm-managed -->
