---
name: xsm
description: Talk to other Claude Code or Codex sessions on this machine — list them, send a message or a task, answer one, start workers, and see what was delivered. Use when the user asks to contact, hand off to, or coordinate with another session or agent. Your user calls it as `/xsm <command>` in Claude Code and `$xsm <command>` in Codex (list, who, log, projects, doctor, send, link, join, leave, reach); you run the `xsm` shell command (`xsm list`, `xsm send <name|ref:xxxxxx> --text "..."`). Load this skill before naming any other xsm command or address, or before acting on an xsm refusal — a made-up address is a message nobody receives. From a sandboxed shell, reach a Codex session with the `xsm_send` MCP tool instead; a row whose ref is `[-]` has no address yet.
argument-hint: "list | who | log | projects | doctor | send <target> <message> | link <folder> | join <project> | leave <project> | reach <folder>"
allowed-tools: Bash(xsm send:*), Bash(xsm list --table), Bash(xsm who --table), Bash(xsm projects --table), Bash(xsm doctor --table), Bash(xsm ledger --table --mine --last 5), Bash(xsm held list --table)
---

# xsm

`xsm` delivers a message into another session's own input. Everything it does
is a shell command, `xsm …`.

## Called with a command

When your user typed `/xsm <command>` (Claude Code) or `$xsm <command>` (Codex),
the command is the first word after it; in Claude Code it is also passed to
you as `ARGUMENTS:` below. For a command listed here, do exactly what its row says.

**Display commands** — `list`, `who`, `projects`, `doctor`. There is nothing to
decide. Run this one shell command, exactly as written:

    xsm <command> --table

Then reply with its output, copied exactly, as it is — not in a code block, so
the tables show as tables. Nothing before it, nothing after it. Do not
translate, reword, summarise or explain it, and run nothing else.

**`log`** — a display command too. Run these two shell commands, exactly as written:

1. `xsm ledger --table --mine --last 5`
2. `xsm held list --table`

Then reply with the layout below, each [output of command N] replaced by that
command's output copied exactly, as it is — not in a code block. Nothing before
it, nothing after it, and run nothing else.

    **Recent messages to and from this session**

    [output of command 1]

    **Held by the gate** (ids and full text: `xsm held list`)

    [output of command 2]

**`send <target> <message>`** — run one shell command:
`xsm send <TARGET> --wait 10 --text <MESSAGE>`, where TARGET is the word after `send` and
MESSAGE is everything after it, passed as a single shell-quoted argument. Do
not rewrite the message. Reply with the command's output, copied exactly,
inside one code block. If it fails with `sandbox-blocked`, call the MCP tool
`xsm_send` instead (same target and text) and reply with its result the same way.

**`link <folder>`** — call the MCP tool `xsm_link` with `dir` set to the folder
(both ways, until unlinked; what your user typed is the consent).

**`join <project>`** / **`leave <project>`** — call `xsm_join` with `project`
set to the word after it (and `leave` true for `leave`).

**`reach <folder>`** — call `xsm_reach` with `dir` set to the folder (this
session alone, until it ends).

For these four: call the tool once and reply with its result, copied exactly, inside one code block. Say
your user refused only if it says they chose `deny` or declined. If the tool is missing or fails, run
the same `xsm <command>` in the shell; if its result says the form got no answer, do what it says. When
xsm says it needs their yes, ask in plain words (Claude Code only: or AskUserQuestion), run it again to
see their reply, and again only on a yes. Never ask your user to type a command.

**Words that are not a command** (`/xsm tell plugin-worker what I did`): a
request in words, see "Called without one". **Nothing, or nonsense** — reply with exactly:
usage: /xsm list | who | log | projects | doctor | send <target> <message> | link <folder> | join <project> | leave <project> | reach <folder>

## Called without one

When you loaded this skill yourself, or your user asked in words, read
[references/guide.md](references/guide.md) before you choose a command or write
an address. It covers sessions, projects, sending and replying, the channel,
shared documents, other machines, workers, breakage and mixed versions.

One refusal is common enough to know before you open it. From a sandboxed
shell (a background worker, a Codex workspace-write session) `xsm send` to a
Codex peer refuses at once and says to use the `xsm_send` MCP tool: `codex
queue` cannot run inside the sandbox, and the MCP server runs outside it. Send
it with that tool, same target, kind and text. Do not retry the shell command
and do not widen the sandbox.

Two others are not breakage. `… is not registered yet` just after installing
or trusting the hooks means they register this session at its next prompt:
have your user send any message, then retry; do not reinstall. A form tool can
come back unseen by your user (Codex declines forms unshown under
`approval_policy = "never"`); pass the result on as it is; do not call it again.
