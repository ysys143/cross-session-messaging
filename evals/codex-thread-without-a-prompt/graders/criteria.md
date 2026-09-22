---
type: llm
weight: 1
---

A good answer explains that the row has no address yet (the `[-]`), so it cannot
be sent to, and that the way forward is to ask the user to type something in
that Codex window or run `/rename` there, after which it registers and can be
addressed.

It is wrong if it claims the session can be messaged now (sending it a prompt
from elsewhere is exactly what cannot be done yet), invents a ref for it, or
tells the user to edit Codex's files.
