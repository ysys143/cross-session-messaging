# xsm (Cross-Session Messaging)

English | [한국어](README_ko.md)

xsm lets running Claude Code and Codex sessions find each other and exchange messages.

![How XSM connects agent sessions through native runtime paths and explicit communication scopes](docs/assets/xsm-overview.svg)

## TL;DR

Give this prompt to your coding agent (Claude Code or Codex), and it installs xsm on both runtimes and
checks the result:

```
Install xsm from https://github.com/ysys143/xsm on both Claude Code and Codex on this machine.
Then verify it with `xsm doctor` and `xsm selftest`, and tell me anything I still have to do myself,
such as trusting the hooks in Codex or starting a new session.
```

The agent follows this README for both runtimes and reports what it verified. Coding agents often cannot type
slash commands such as `/plugin`; they can use the direct install (`xsm install`) described under Quick start.

Once it is installed, open a new session on either side and ask:

```
How do I use this? Show me with a real demo.
```

The agent reads the skill's guide and runs `xsm` itself, so you do not have to learn the commands first.
To install by hand, read on.

## Quick start

### 1. Install manually

xsm runs on macOS and Linux. It needs Unix sockets, `ps`, `/dev/tty` and tmux, so native Windows is not
supported. Its only dependency is the Python standard library. Installing means making each session run
xsm's hooks.

**Claude Code plugin** (recommended). The repository is its own marketplace.

```
/plugin marketplace add ysys143/xsm
/plugin install xsm@xsm
```

The plugin bundles the hooks, the skill, the MCP server and `bin/`. It updates when `version` in
`plugin.json` goes up, and disabling it also disables the hooks.

**Codex plugin** (recommended). The same repository is also a Codex marketplace.

```bash
codex plugin marketplace add ysys143/xsm
codex plugin add xsm@xsm
```

The plugin bundles two hooks, the skill and the MCP server. Codex runs a plugin's hooks only after you trust
them: in the first session, choose "Trust all and continue" under `/hooks`.

<details>
<summary>More on the Codex plugin: PATH, updating, leftovers of an earlier install</summary>

- Codex does not put a plugin's `bin/` on PATH, so every session start links `~/.local/bin/xsm` to the
  plugin. An existing link or file there that does not point to an xsm plugin folder is left alone.
- To update, run `codex plugin marketplace upgrade xsm`, then `codex plugin add xsm@xsm` again.
- If this home was installed earlier with `xsm install --codex-home`, run `xsm install --refresh` once after
  adding the plugin. It removes the hook groups, MCP entry and skill link that install left behind, which
  would otherwise run alongside the plugin's.

</details>

<details>
<summary>Without a plugin: <code>xsm install</code></summary>

```bash
bin/xsm install --claude-home <my-claude-config-dir>          # install directly instead of the plugin
bin/xsm install --claude-home <dir-A> --claude-home <dir-B>   # several homes in one command
bin/xsm install --codex-home <my-codex-home>                  # Codex hooks, skill and MCP
bin/xsm install --refresh                                     # bring every installed home up to date
bin/xsm doctor                                                # install state, stale copies, what is blocked now
```

`--claude-home` takes Claude Code's config directory (`CLAUDE_CONFIG_DIR`) and `--codex-home` takes Codex's
(`CODEX_HOME`). Unless you changed them, these are `~/.claude` and `~/.codex`. There is no default home, so
except with `--refresh` you must name at least one. Repeat `--claude-home` to cover several homes.

A directly installed copy does not follow changes to the repository. When `xsm doctor` reports it as stale,
update it with `xsm install --refresh`. After a plugin update, run `install --refresh` with the new
version's `bin/xsm`.

</details>

<details>
<summary>Cautions: double hooks, PATH, a name clash on Linux</summary>

- If a home (Claude or Codex) has both the plugin and a direct install, its hooks run twice, which is
  dangerous. `xsm install` refuses such a home unless you pass `--force`.
- `install --codex-home` links `bin/xsm` at `~/.local/bin/xsm` for you, so make sure `~/.local/bin` is on
  your PATH. With only `--claude-home` and no plugin, link `bin/xsm` onto your PATH yourself. The Claude
  plugin sets PATH inside sessions, and the Codex plugin keeps the link described above.
- On Linux, X.Org's session manager is also called `xsm` (package x11-session-utils). If it is installed,
  run `command -v xsm` to check that this one comes first.

</details>

### 2. Try it

There is no separate registration. A session with the hooks installed registers itself at start and at every
prompt. Registration is consent: a session that has never run the hooks shows up in the list only as
`unregistered` and cannot be addressed (ADR-0001, session registry).

Inside a session, pass commands to the `xsm` skill as arguments: `/xsm` in Claude Code, `$xsm` in Codex.

```
/xsm list                            # Claude Code: sessions you can talk to in this project
$xsm list                            # Codex
/xsm who                             # how this session appears to others
/xsm send ref:a1b2c3 please take a look   # everything after the target is the message
```

The same commands work in a terminal as `xsm list`, `xsm who`, `xsm send ...`. With no command, the skill
prints a one-line usage. You can also ask in words ("ask the other session"), and the agent reads the skill's
guide and runs `xsm` itself. If you have a personal skill named `xsm`, call the plugin's as `/xsm:xsm list`.

All commands: `list`, `who`, `log`, `projects`, `doctor`, `send`, `link`, `join`, `leave`, `reach`.

## Use cases

### 1. Naming and finding sessions

Names belong to the runtime, so there is no `xsm rename`. xsm reads the name from the runtime on every
lookup, and it follows when you rename the session itself.

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

### 5. Diagnosing a home that lacks the install

```bash
xsm doctor                                     # which homes lack hooks, stale copies, what is blocked now
xsm selftest                                   # whether the hooks actually run
```

## Reference

### Communication scope

Sessions in the same Git repository can talk to each other, even from different subfolders. Outside a Git
repository, sessions in the same folder can talk. To connect another folder, type `/xsm link` in a session on
either side. One side is enough: the link works both ways and stays until you remove it.

```bash
/xsm link ~/src/other-repo           # in a session (Codex: $xsm link ...); typing it is your consent
xsm link ~/src/other-repo            # or in a terminal
xsm unlink ~/src/other-repo          # anyone may remove it
```

- `join`: to group several folders, type `/xsm join <name>` in a session in each folder.
- `reach`: to connect a single session for as long as it runs, type `/xsm reach <folder>` in that session.

### Sending messages

```bash
xsm send agent-name --text "please take a look"
xsm send agent-name --text "fix this test" --kind task --wait 15
xsm send ref:a1b2c3 --text "..."     # use a ref when names collide
xsm send agent@hostB --text "..."    # another machine (after xsm remote add)
```

### Receiving messages

The receiving side runs nothing. A hook acts as the gate: it checks the scope and the sender, then puts the
message directly into the session's prompt. A rejected message is held, not dropped. Claude's own
cross-session messages (`SendMessage`, no xsm header) pass the same scope check: they arrive untouched when the
sending session shares a scope with this one, or is a session on this machine xsm does not know (a profile
without xsm); they are held when xsm knows the sender and it is out of scope, or when they come from off this
machine (Remote Control, cloud). When xsm holds a `SendMessage` from a session it can name, that sender sees once,
on its next prompt or xsm command, that the message was not delivered and why; for an out-of-scope hold its agent is
told to ask you with the `xsm_link` tool (an approval form that connects the folders on yes) and send it again. Set `"strict_peers": true` in
`~/.xsm/config.json` to hold every message without an xsm header instead (ADR-0013). Claude's own gate still
decides first: a message it holds for a permission-mode mismatch never reaches xsm.

```bash
xsm ledger                           # recent messages and their delivery state
xsm status <message-id>              # the state of one message
xsm held                             # what this machine rejected and held
```

### Observability (telemetry)

xsm records its own sends and receives as spans and metrics. It builds the OTLP format itself instead of
installing the OpenTelemetry SDK, so it still depends only on the standard library and works with standard
backends (ADR-0011, telemetry).

```bash
xsm metrics                 # call counts, errors and p95 recorded on this machine
xsm metrics --json
```

Message bodies are never put in spans. To turn telemetry off, set `XSM_NO_TELEMETRY=1`.

<details>
<summary>Retention, exporting to a collector, and overhead</summary>

Spans and metrics are kept for 7 days by default (`telemetry_retention_days`). Cleanup removes only lines
that were already exported, and only from the head of the file, so records not yet sent are kept.

Records are appended to `$XSM_HOME/otel-spans.jsonl` and `otel-metrics.jsonl`. They are sent only when you run
a separate command, so nothing on the send or receive path touches the network.

```bash
# defaults to http://localhost:4318 if OTEL_EXPORTER_OTLP_ENDPOINT is not set
xsm otlp-export --once
xsm otlp-export --follow --interval 5
```

This was verified against a real OpenTelemetry Collector (v0.161.0):

```bash
docker run --rm -p 4318:4318 otel/opentelemetry-collector:latest
xsm otlp-export --once
```

A message's whole path (sender, SSH, receiving machine, the target session's hook) forms a single trace, so
Jaeger or Grafana Tempo shows where a message stopped.

Instrumentation costs about 0.175 ms per send
([telemetry-overhead.md](docs/references/telemetry-overhead.md)).

</details>

## How it works

xsm puts a message directly into a session's input. There is no server and no resident process.

```
Claude Code Session A -> xsm -> Claude Code Session B     same machine
Claude Code <-> Codex                                     even across different CONFIG_HOMEs
Local Machine -> SSH -> Remote Server                     xsm installed on both + `xsm remote add`
```

Each session's hook registers it at start and at every prompt (registration is consent). Sending uses only
what each runtime already has: the Claude inbox socket and `codex queue`. The receiving hook is the gate: it
checks the scope and the sender, puts the message into the session's prompt, and holds what it rejects.

Principles:

- **No wrapper runtime**: xsm runs inside ordinary Claude Code and Codex sessions, with no separate runtime
  and no process of ours that has to stay running.
- **Project-scoped trust**: each project defines who may talk to whom.
- **Session-independent**: it works regardless of each session's `CONFIG_HOME` (`~/.claude`, `~/.codex`, ...).
- **Async-first**: messages are asynchronous, so collaboration never blocks.
- **Observable**: spans and metrics are recorded in OTLP format, with no SDK dependency.

<details>
<summary>Repository layout</summary>

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
│   ├── adr/                     # architecture decision records (0001-0012)
│   ├── xsm/                     # protocol and test plan
│   ├── references/              # research notes, overhead measurements
│   ├── plan/ · spikes/ · reviews/
│   └── list-agents-cross-session-messaging.md
├── tests/                        # unittest, with vectors
└── tools/                        # spike and benchmark scripts
```

</details>

## Documents

Most documents under `docs/` are written in Korean.

- **[INTENT.md](INTENT.md)**: project goals and vision
- **[ADR (Architecture Decision Records)](docs/adr/)**: design decisions
  - [Session Registry](docs/adr/0001-session-registry.md)
  - [Communication Scope](docs/adr/0004-communication-scope.md)
  - [Remote Transport & Trust](docs/adr/0007-remote-transport-and-trust.md)
- **[Protocol Spec](docs/xsm/)**: message protocol specification
- **[Test Protocol](docs/xsm/TESTPLAN.md)**: hands-on check procedure (developer tests live here)
- **[Agent Messaging Guide](docs/list-agents-cross-session-messaging.md)**: implementing agent messaging

## License

MIT

## Feedback

Issues, suggestions and PRs are welcome.
