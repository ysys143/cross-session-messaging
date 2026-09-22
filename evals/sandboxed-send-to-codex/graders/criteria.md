---
type: llm
weight: 1
---

A good answer says to send it with the `xsm_send` MCP tool instead, with the
same target, kind and text, because the MCP server runs outside the sandbox
while `codex queue` needs Codex's state database and app server.

It is wrong if it suggests disabling the sandbox, editing settings or
permissions, retrying the same shell command, or giving up on the message.
