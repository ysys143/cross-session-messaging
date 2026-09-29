# xsm (Cross-Session Messaging)

English | [한국어](README_ko.md)

Let running Claude Code and Codex sessions find each other and exchange messages.

## Overview

xsm lets Claude Code and Codex sessions running in different environments talk to each other directly.

![How XSM connects agent sessions through native runtime paths and explicit communication scopes](docs/assets/xsm-overview.svg)

### Key features

- **Cross-session messaging**: send and receive messages between sessions on the same machine or on a remote server
- **Config-independent**: works regardless of each session's `CONFIG_HOME` (`~/.claude`, `~/.codex`, and so on)
- **Built on what is there**: runs inside ordinary Claude Code and Codex sessions, with no separate runtime
- **Trust-based**: project-scoped session groups and permissions
- **Uses each runtime's own path**: delivery goes only through what each runtime already has (Claude's inbox
  socket, `codex queue`), so there is no process of ours that has to stay running
- **Observable**: records spans and metrics in OTLP format, with no SDK dependency

## Layout

```
├── .claude-plugin/               # plugin and marketplace manifests
├── hooks/hooks.json, .mcp.json   # hooks and MCP server the plugin provides
├── .codex-plugin/, .agents/plugins/   # the same for Codex
├── hooks/codex-hooks.json, codex-mcp.json   # hooks and MCP server of the Codex plugin
├── bin/xsm                       # launcher (sets PYTHONPATH, then python3 -m xsm)
├── xsm/                          # the whole implementation (Python, stdlib only)
│   ├── cli.py                   # every subcommand
│   ├── registry.py              # session registry (written by hooks, enriched from the runtime on lookup)
│   ├── send.py / receive.py     # sending, and the receive gate the hooks call
│   ├── envelope.py              # message envelope and headers
│   ├── adapters.py              # the two native delivery paths (UDS socket, codex queue)
│   ├── codex_daemon.py          # asks Codex's app-server daemon to start a queued message (Esc)
│   ├── config.py                # project, scope and block settings
│   ├── remote.py                # other machines (two-way SSH, ADR-0007)
│   ├── workers.py               # spawning, approving and stopping workers (ADR-0010)
│   ├── channel.py / doc.py      # channel log (0005), shared documents (0006)
│   ├── telemetry.py             # span and metric recording (ADR-0011)
│   ├── otlp_export.py           # OTLP/HTTP+JSON export
│   ├── install.py               # hook installation and diagnosis
│   └── mcp.py                   # MCP server
├── hooks/                        # hook entry points the runtimes call
├── skills/xsm/                   # one skill: `/xsm <command>` (Codex `$xsm <command>`) and its guide
├── docs/
│   ├── adr/                     # architecture decision records (0001–0012)
│   ├── xsm/                     # protocol and test plan
│   ├── references/              # research notes, overhead measurements
│   ├── plan/ · spikes/ · reviews/
│   └── list-agents-cross-session-messaging.md
├── tests/                        # unittest, with vectors
└── tools/                        # spike and benchmark scripts
```

Most documents under `docs/` are written in Korean.

## Quick start

### Install

Runs on macOS and Linux (it relies on Unix sockets, `ps`, `/dev/tty` and tmux); native Windows is not
supported. The only dependency is the standard library. Installing means making each session run the hooks, and there
are two ways to do it.

**Claude Code: plugin** (recommended). The repository is its own marketplace.

```
/plugin marketplace add ysys143/xsm
/plugin install xsm@xsm
```

This brings in the hooks, the skill, the MCP server and `bin/`, and updates when `version` in `plugin.json`
goes up. Inside a session, call it as `/xsm list`, `/xsm who` (if you have a personal skill with the same
name, use `/xsm:xsm list`). Disabling the plugin disables the hooks too.

**Codex: plugin** (recommended). The same repository is a Codex marketplace too.

```bash
codex plugin marketplace add ysys143/xsm
codex plugin add xsm@xsm
```

This brings in the two hooks, the skill and the MCP server. Codex runs a plugin's hooks only once you trust
them: in the first session, choose "Trust all and continue" under `/hooks`. Codex does not put a plugin's
`bin/` on PATH, so each session start links `~/.local/bin/xsm` to the plugin; a link there that points
anywhere but an xsm plugin folder is left alone. To update, run `codex plugin marketplace upgrade xsm` and
`codex plugin add xsm@xsm` again. Inside a session, call it as `$xsm list`. If this home had
`xsm install --codex-home` before, run `xsm install --refresh` once after adding the plugin: it removes the hook
groups, the MCP entry and the skill link that install left, which would otherwise run beside the plugin's.

**Without a plugin: `xsm install`.**

```bash
bin/xsm install --claude-home <my-claude-config-dir>          # install directly instead of the plugin
bin/xsm install --claude-home <dir-A> --claude-home <dir-B>   # several homes in one command
bin/xsm install --codex-home <my-codex-home>                  # Codex hooks, skill and MCP
bin/xsm install --refresh                                     # bring every installed home up to date
bin/xsm doctor                                                # install state, stale copies, what is blocked now
```

`--claude-home` takes Claude Code's config directory (`CLAUDE_CONFIG_DIR`), and `--codex-home` takes Codex's
(`CODEX_HOME`). Unless you changed them, they are `~/.claude` and `~/.codex`. There is no default home, so
apart from `--refresh` you must name at least one. If you use several homes, repeat `--claude-home` to cover
them in one command.

If one home (Claude or Codex) has both the plugin and a direct install, the hooks run twice, which is dangerous.
`xsm install` refuses such a home (`--force` overrides this). A direct-install copy does not follow changes
to the repository by itself, so when `xsm doctor` reports it stale, update it with `xsm install --refresh`.

`install --codex-home` automatically links `bin/xsm` at `~/.local/bin/xsm`; make sure `~/.local/bin` is on
your PATH. With only `--claude-home` and no plugin, link `bin/xsm` onto PATH yourself.
After a plugin update, run `install --refresh` using the new version's `bin/xsm`.
The Claude plugin supplies PATH inside sessions automatically, and the Codex plugin keeps the link above. On Linux, X.Org's session manager is also called
`xsm` (package x11-session-utils); if it is installed, check with `command -v xsm` that this one comes first.

### Session registration

There is no separate registration. A session with the hooks installed registers itself when it starts and
whenever it takes a prompt. Registration is consent, so a session that has never run the hooks appears in
the list only as `unregistered` and cannot be addressed (ADR-0001).

```bash
xsm who                              # how this session appears to others
xsm list                             # sessions you can talk to in this project
```

Inside a session, pass the same commands as arguments to the `xsm` skill: `/xsm` in Claude Code, `$xsm` in
Codex.

```
/xsm list                            # Claude Code
$xsm list                            # Codex
/xsm send ref:a1b2c3 please take a look   # everything after the target is the message
```

The commands are `list`, `who`, `log`, `projects`, `doctor`, `send`, `link`, `join`, `leave` and `reach`. Called with no
command, it prints a one-line usage. If you ask in words ("ask the other session"), the agent reads the
skill's guide and runs `xsm` itself.

### Communication scope

Sessions in the same Git repository can talk to each other, even when they run from different subfolders.
Outside a Git repository, sessions in the same folder can talk. To connect another folder, type `/xsm link`
in a session on either side. One side is enough; the link works both ways and stays until you unlink it.

```bash
/xsm link ~/src/other-repo           # in a session (Codex: $xsm link ...); typing it is your consent
xsm link ~/src/other-repo            # or in a terminal
xsm unlink ~/src/other-repo          # anyone may remove it
```

- `join`: to group several folders, type `/xsm join <name>` in a session in each folder.
- `reach`: to connect only one session, for as long as it runs, type `/xsm reach <folder>` in that session.

### Sending messages

```bash
xsm send agent-name --text "please take a look"
xsm send agent-name --text "fix this test" --kind task --wait 15
xsm send ref:a1b2c3 --text "..."     # use a ref when names collide
xsm send agent@hostB --text "..."    # another machine (after xsm remote add)
```

### Receiving messages

The receiving side runs nothing. A hook acts as the gate: it checks the scope and the sender, then puts the
message directly into the session's prompt. Rejected messages are held, not dropped.

```bash
xsm ledger                           # recent messages and their delivery state
xsm status <message-id>              # the state of one message
xsm held                             # what this machine rejected and held
```

## Observability (telemetry)

xsm records its own sends and receives as spans and metrics. It builds the OTLP format directly instead of
installing the OpenTelemetry SDK, so it still depends only on the standard library while working with
standard backends (ADR-0011).

```bash
xsm metrics                 # call counts, errors and p95 recorded on this machine
xsm metrics --json
```

Spans and metrics are kept for 7 days by default (`telemetry_retention_days`). Cleanup removes only lines
already exported, and only from the head of the file, so records not yet sent are kept. Message bodies are
never put in spans.

To send them to a collector:

```bash
# http://localhost:4318 if OTEL_EXPORTER_OTLP_ENDPOINT is not set
xsm otlp-export --once
xsm otlp-export --follow --interval 5
```

Records are appended to `$XSM_HOME/otel-spans.jsonl` and `otel-metrics.jsonl`, and they are sent only when a
separate command does it. Nothing on the send or receive path touches the network.

Verified against a real OpenTelemetry Collector (v0.161.0):

```bash
docker run --rm -p 4318:4318 otel/opentelemetry-collector:latest
xsm otlp-export --once
```

The whole path of one message (sender → SSH → receiving machine → the target session's hook) forms a single
trace, so Jaeger or Grafana Tempo shows directly where a message stopped.

To turn it off, set `XSM_NO_TELEMETRY=1`. Instrumentation was measured at about 0.175 ms per send
([telemetry-overhead.md](docs/references/telemetry-overhead.md)).

## Design principles

- **No Wrapper Runtime**: no separate orchestration runtime on top of Claude Code or Codex
- **Project-Scoped Trust**: the scope of who may talk is defined per project
- **Session-Independent**: session discovery is not affected by `CONFIG_HOME`
- **Async-First**: asynchronous messaging for collaboration without blocking

## Documents

- **[INTENT.md](INTENT.md)**: project goals and vision
- **[ADR (Architecture Decision Records)](docs/adr/)**: design decisions
  - [Session Registry](docs/adr/0001-session-registry.md)
  - [Communication Scope](docs/adr/0004-communication-scope.md)
  - [Remote Transport & Trust](docs/adr/0007-remote-transport-and-trust.md)
- **[Protocol Spec](docs/xsm/)**: message protocol specification
- **[Test Protocol](docs/xsm/TESTPLAN.md)**: hands-on check procedure (developer tests live here)
- **[Agent Messaging Guide](docs/list-agents-cross-session-messaging.md)**: implementing agent messaging

## How it works

xsm puts a message directly into a session's input. There is no server and no resident process.

```
Claude Code Session A → xsm → Claude Code Session B     same machine
Claude Code ↔ Codex                                     even across different CONFIG_HOMEs
Local Machine → SSH → Remote Server                     xsm installed on both + `xsm remote add`
```

Each session's hook registers it at start and at each prompt (registration is consent), and sending uses only
what each runtime already has (the Claude inbox socket, `codex queue`). The receiving hook acts as the gate,
checks the scope and the sender, then puts the message into the session's prompt; rejected messages are held.

## Use cases

### 1. Naming and finding sessions

Names belong to the runtime. There is no `xsm rename`: xsm reads the name from the runtime every time it looks
one up, so when you rename the session itself, xsm follows.

```bash
# in a Claude Code session
/rename my-reviewer                            # rename the session

# then, from another session
xsm list                                       # sessions you can talk to in this project
xsm who                                        # how this session appears
```

### 2. Connecting to a session in another repository

```bash
/xsm link ~/src/other-repo                     # in a session on either side (Codex: $xsm link ...)
xsm projects                                   # which folders are linked or joined
```

### 3. Sending a message and getting a reply

```bash
xsm send my-reviewer --text "please take a look"        # just a notice
xsm send my-reviewer --text "fix this test" --kind task --wait 15   # hand off work and wait for the reply
xsm ledger                                     # delivery state
```

### 4. Giving work to a worker

```bash
xsm spawn codex --task "fix this test"         # start it, instruct it, get the result as a reply
xsm workers                                    # worker state
```

### 5. Diagnosing a home without the install

```bash
xsm doctor                                     # which homes lack hooks, stale copies, what is blocked now
xsm selftest                                   # whether the hooks actually run
```

## License

MIT

## Feedback

Issues, suggestions and PRs are welcome.
