# 아키텍처 레퍼런스 종합

- 목적: `INTENT.md`의 목표(G1~G8)를 설계하는 데 쓸 레퍼런스를 한 곳에 정리한다.
- 대상: orca, herdr, moai-adk, buzz, agent-comms(소스 코드), agora(arXiv 2609.18094), Claude Code 2.1.278(바이너리), Codex CLI 0.155.1(로컬 CLI)
- 작성일: 2026-09-19 (v2: 적대적 리뷰와 보강 조사 반영)
- 개별 보고서: 이 디렉터리의 `orca.md`, `herdr.md`, `moai-adk.md`, `buzz.md`, `agent-comms.md`, `agora.md`. Claude Code는 `../list-agents-cross-session-messaging.md`. orca의 후속 조사(CLI·스킬 표면, 2026-09-23)는 `orca-benchmark.md`.

## 0. 조사 방법과 출처

소스는 `/tmp/xsm-refs/`에 `git clone --depth 1`로 받았다.

| 대상 | 커밋 | 조사 워커 |
|---|---|---|
| orca (stablyai/orca) | `1ef94739` (2026-09-19) | Claude |
| herdr (herdrdev/herdr) | `3f2a6e7` (2026-09-18) | Codex |
| moai-adk (modu-ai/moai-adk) | `2213871` (2026-09-10) | Codex |
| buzz (block/buzz) | `4e65148` (2026-09-18) | Claude |
| agent-comms (ExaDev/agent-comms) + cc-peer (ExaDev/cc-peer) | `a68c00f` (2026-09-19), `74fd76a` (2026-09-18) | 코디네이터 직접(2026-09-20 추가) |
| agora (arXiv 2609.18094 v1) | 코드 미공개 | Claude |

조사는 Orca 오케스트레이션으로 진행했다.

- Run `run_eb47bc877a6e`, Task 5개(`task-list`에서 모두 `completed`).
- 각 워커는 `worker_done`으로 결과를 보고했다. 코디네이터(이 세션)는 보고마다 인용을 표본 대조한 뒤 수용했다(7장).
- 모든 워커는 **정적 코드 조사만** 했다. 빌드, 실행, 실제 에이전트 간 통신 시험은 하지 않았다.

## 0.5 v2에서 바뀐 결론 (적대적 리뷰 + 보강 조사)

리뷰는 `../reviews/README.md`에, 보강 조사는 `supplement/T1~T7`에 있다. 세션 발견·발신 표시·세션 명명과 네임스페이스는 `supplement/T8-discovery-sender-naming.md`에 따로 정리했다(ADR-0001, 0002, 0008). 각 항목은 코디네이터가 원본과 대조해 확인했다. v1의 본문(1~5장)과 충돌하면 이 절이 우선한다.

| 주제 | v1 결론 | v2 결론 | 근거 |
|---|---|---|---|
| Codex wakeup | 사용자가 띄운 Codex TUI에 런타임 없이 닿는 경로를 찾지 못함 | **경로 있음.** `codex queue`가 `$CODEX_HOME/queue_1.sqlite`의 `queued_items`에 쓰면, 실행 중인 TUI(Embedded app-server 포함)의 watcher가 10초마다 변경을 감지하고 로드된 idle thread의 턴을 시작한다. 한계: 같은 CODEX_HOME이어야 하고, Interrupted는 자동으로 깨우지 않고, busy 턴 중간 개입이 없고, 발신자 origin 필드가 없다. **실측은 아직 없다(S2)** | `supplement/T1-codex-queue.md`; `codex-rs/ext/queue/src/service.rs:89-96` (tag `rust-v0.155.1`, 코디네이터 확인); 로컬 `~/.codex/queue_1.sqlite`·`~/.codex-2/queue_1.sqlite` 스키마(코디네이터 확인) |
| Codex 제어 소켓 | `~/.codex/ipc/ipc.sock` | `$CODEX_HOME/app-server-control/app-server-control.sock`. `ipc.sock`은 IDE 문맥용 | `codex-rs/app-server-transport/src/transport/mod.rs:56-72` |
| 같은 thread 두 writer | 다루지 않음 | Codex는 thread에 자체 잠금이 없다는 것이 Orca 개발자의 설명이다(두 번째 app-server는 오류 없이 분기). Orca는 중복 채택 거부로 막는다. **따라서 "별도 app-server로 사용자 TUI의 thread를 resume해 메시지를 넣는" 안은 쓰면 안 된다.** 큐 경로는 TUI 자신의 app-server가 소비하므로 이 문제가 없다 | `orca/src/main/native-chat/structured-agent-session-history-adoption.ts:78-112` (코디네이터 확인); `supplement/T3-orca-structured-sessions.md` |
| Claude 수신 정책 | 설정이 없으면 모드 계열이 같을 때만 자동 전달 | 판정 순서는 다음과 같다(`chunk-9mrd94qp.js` `k()`, 코디네이터 확인). **`fromMode`는 본문 봉투 `<cross-session-message … from-mode="…">`에서 파싱되는 발신자 자기 주장 값이다**(`chunk-cyg1gqsq.js` `TG()`/`b_e()`). 따라서 모드 동등성은 보안 경계가 아니라 실수 방지 장치다 | `supplement/T2-claude-inbound-policy.md` (단, 3절 결과표의 bypass 행은 틀렸다. 코디네이터 정정 아래) |
| 수신 정책 공개 여부 | 바이너리에서만 확인 | **공식 문서화된 설정이다.** `code.claude.com/docs/en/settings`에 두 키가 있다. `isolatePeerMachines`: 어느 범위에서든 `true`면 적용. `crossSessionInbound`: `accept`<`hold`<`refuse` 순서이고, 프로젝트·로컬 값은 더 엄격할 때만 적용. `/config` Connections에는 `dialogExpiry`·`crossSessionInbound`가 노출된다 | Jina Reader 조회(코디네이터 확인), `chunk-gvj1whv8.js` Connections 배열 |
| 이전 자기 소켓 실험 | 다른 세션의 메시지가 도착한다는 실측 | **증거가 아니다.** childToken을 쓴 자기 자식 프로세스는 selfSent 예외로 수락됐을 수 있다 | 리뷰 R1-06, T2 4절 |
| Orca 원격 | "SSH 기반 federation" | 페어링된 WebSocket, 공개키로 유도한 공유키, 기기 토큰. dispatch에 공개키 지문과 pairing revision을 고정하고, 불일치는 `peer_changed`로 거부 | `supplement/T4-remote-transport.md` (인용 39건 검사 통과) |
| buzz mesh | Iroh로 릴레이 간 메시지 연합, relay마다 독립 저장소 | 같은 배포의 서명 키와 Redis 소유권 판정을 공유하는 직접 IP 연결이다(`RelayMode::Disabled`). NAT 너머 도달을 전제할 수 없다 | 리뷰 R4-03~05, T4 |
| Codex 원격 | `--remote`로 원격 세션 통신 | `--remote`는 TUI를 원격 app-server에 붙이는 옵션이다. `remote-control start/pair`는 별도 경로다. 기존 Embedded TUI를 원격으로 옮기지 않는다 | 리뷰 R1-10, R4-08, T4 |
| 문서 협업(G7) | 3개 선택지 | 6개 선택지. 권고는 append-only 기여 로그와 도출된 정본(agora식)이 1순위이고, 절 소유권과의 혼합이 대안이다 | `supplement/T6-shared-document-editing.md` |
| buzz를 채널로(G6) | 자체 호스팅 가능, 최소 구성·연동 미조사 | 기본 `deploy/compose/compose.yml`의 서비스는 `relay`(웹 클라이언트도 서빙), `postgres:17-alpine`, `redis:7-alpine`, `minio`+`minio-init`(S3, T5는 선택적이라고 봄)다. 에이전트가 쓸 가장 가벼운 경로는 `buzz-cli`(REST + NIP-98 Schnorr 서명)이고, MCP는 `buzz-dev-mcp`다. Claude Code/Codex와의 실제 연동은 미구현·미검증이다 | `supplement/T5-buzz-channel.md`; compose 서비스 목록은 코디네이터 확인 |
| G8 | 요구사항 없음 | R-G8-01~10 도출. X 게시물은 GraphRAG 계열 지식 그래프 파이프라인 글이다. 메시징·스웜과의 연결은 해석으로 표시 | `supplement/T7-g8-swarm-graph.md` |

**실측(2026-09-19, `../spikes/S1-S2-results.md`)**:
- **S1**: 판정 순서의 예측 7건이 모두 맞았다.
  - `~/.claude-3` 쪽 독립 프로세스가 `~/.claude-4` 세션에 인증 없이 전달했다.
  - 봉투 `from`으로의 `SendMessage` 답장 왕복도 확인했다.
  - 봉투가 없으면 수락되더라도 모델이 답장 주소를 모른다.
- **S2**: `codex queue`로 사용자가 띄운 Codex TUI를 약 13초 만에 깨웠다.
  - 다른 CODEX_HOME에서 보내면 즉시 오류가 난다.
  - 진행 중인 턴에는 끼어들지 않고 끝난 직후 전달된다.
  - Interrupted 상태에서는 다음 정상 턴까지 대기한다.
  - 큐 메시지는 사용자 입력과 구분되지 않게 표시된다.

**Claude 수신 판정 순서** (`chunk-9mrd94qp.js`의 `k(e, n)`, 코디네이터가 원본에서 확인):

1. `crossSessionInbound`가 명시돼 있으면 그 값을 따른다.
2. `selfSent`면 accept.
3. 수신자 권한 모드를 읽을 수 없거나 모르는 값이면 hold(`mode-unknown`).
4. 수신자 모드를 `bypass` 또는 `prompting`으로 정규화한다.
5. 두 번째 인자 `n`이 참이거나 플래그 `tengu_harbor_kite_mode_emit`(기본값 true)가 켜져 있으면 봉투의 `fromMode`를 쓴다. `n`을 T2는 host 주입 여부로 해석했지만 코디네이터는 확인하지 않았다. 플래그 기본값이 true이므로 보통은 `fromMode`를 쓴다.
   - `fromMode`가 있고 수신자와 같으면 accept, 다르면 hold(`mode-mismatch`).
6. `fromMode`가 없으면: 수신자가 bypass면 hold(`no-mode-asserted`), 아니면 accept.

T2 3절 결과표는 bypass 수신자에 bypass 발신을 hold로 적었다. 위 5단계에 따라 **accept가 맞다.**

설계에 주는 의미는 다음과 같다.
- 사용자는 대부분의 세션을 bypass 모드(`--dangerously-skip-permissions`)로 실행한다.
- 따라서 봉투 없이 raw 프레임을 넣으면 다른 프로필의 세션에서는 보류될 가능성이 높다.
- 어댑터는 봉투에 `from-mode`를 정직하게 넣어야 한다. 그래야 모드가 다른 수신자에게는 사람 확인이 걸리는 안전장치가 살아 있다.


| | Claude Code (네이티브) | orca | herdr | moai-adk | buzz | agent-comms | agora |
|---|---|---|---|---|---|---|---|
| 형태 | CLI 내장 기능 | 데스크톱 앱 + CLI + 런타임 | 터미널 멀티플렉서 + 데몬 | Go CLI + MCP 서버 | Nostr 릴레이 + 앱 | 하니스별 MCP 서버(bridge)끼리 localhost TCP 메시 | Git 기반 공유 메모리 서버 |
| 에이전트를 누가 띄우나 | 사용자 | Orca 터미널 | herdr 터미널 | 사용자 (MCP 연결) | 하니스(buzz-acp) | 사용자 (MCP 연결) | 사용자/워커 |
| 발견 | `$CLAUDE_CONFIG_DIR/sessions/*.json` | 런타임 DB의 터미널 핸들 | 데몬이 소유한 pane | `<project>/.moai/state/session-msg/agents/*.json` | 릴레이 구독(pubkey) | 코디네이터(`127.0.0.1:19876`)가 피어 소개, 메모리 레지스트리. Claude는 cc-peer가 `~/.claude/sessions` 조회 | 계정 + DAG 조회 |
| 전달 | 유닉스 소켓 inbox | SQLite 메일함 + PTY 포인터 주입 | PTY 전체 프롬프트 주입 | 파일 우편함 + MCP poll | WebSocket 이벤트 | TCP 이벤트 → Claude: 대기 파일 + 훅 drain + 채널 알림, Codex: 도구 응답에 덧붙임 | git push |
| 수신 세션을 깨우나 | 예 (큐에 직접) | 예 (idle일 때 포인터) | 예 (PTY 입력) | 아니오 (poll 필요) | 하니스가 수신 | 하니스별. pi 예, Claude 부분(채널은 개발 플래그 필요), Codex 아니오 | 아니오 (pull) |
| 원격 | Anthropic API bridge | 서버 간 federation relay | SSH stdio 브리지 | 확인 못 함 | 릴레이 간 mesh(Iroh) | `mesh_listen` + Tailscale 발견 + gateway device-id 고정 신뢰 | 중앙 서버 |
| 영속 기록 | 없음 (대화 기록뿐) | SQLite(Run/Task/메시지, thread_id) | 메모리 이벤트 512개, 스냅샷 | JSON 파일, 24h 만료 | PostgreSQL 이벤트 로그 | 메모리 복제(방·DM·전달 큐). 메시가 모두 내려가면 사라짐 | Git DAG + SQLite 인덱스 |
| 별도 런타임 필요 | 없음 | 있음 | 있음 | MCP 서버(세션별) | 릴레이 서버 | MCP 서버(세션별), 데몬 없음 | 서버 |

## 2. 핵심 질문별 비교

### 2.1 수신 세션을 어떻게 깨우는가 (G3)

INTENT.md의 핵심 요구다. "게시판이 아니라 다른 세션에 직접 invoke"를 뜻한다.

| 방식 | 사례 | 근거 | 평가 |
|---|---|---|---|
| **네이티브 inbox** | Claude: `/tmp/cc-socks/<pid>.sock`에 JSON 줄을 쓰면 대화 큐에 들어간다 | `../list-agents-cross-session-messaging.md` 9~13절 (실측 포함) | 권한 경고, 보류 정책, rate limit이 적용된다. 비공개 프로토콜이다 |
| **PTY 포인터 주입** | Orca: 에이전트가 idle일 때만 `You have N orchestration message. Run \`orca orchestration check\`.`를 입력하고, 본문은 에이전트가 CLI로 가져간다 | `orca/src/main/runtime/orchestration/formatter.ts:112-122`, `mailbox-pointer-delivery.ts:70-110` (코디네이터가 직접 확인) | 본문이 입력창을 거치지 않는다. idle 판정과 대기자 확인으로 사용자 입력과의 충돌을 줄인다 |
| **PTY 전체 주입** | herdr: 프롬프트 텍스트 + Enter를 300ms 간격으로 PTY에 넣는다 | `herdr/src/app/api/agents.rs:130-215` | 어떤 CLI에든 적용된다. 사람 입력과 구분되지 않는다(권한 경고 없음). OS·에이전트별 입력 보정이 필요하다 |
| **MCP poll** | moai: `session_msg_poll` 도구를 에이전트가 호출해야 받는다 | `moai-adk/internal/cli/mcp_server.go:458-480` | 공식 확장 지점이지만 idle 세션을 깨우지 못한다 |
| **Codex app-server (소유 세션)** | orca: `codex app-server`를 띄워 `thread/start`·`thread/resume`으로 연 뒤 `turn/start`로 메시지를 넣는다. 턴이 끝난 뒤에만 보낸다. moai: 같은 방식으로 thread를 재개한다 | `orca/src/main/codex/codex-app-server-connection.ts:52-67`, `codex-structured-thread-open.ts:49-87`, `codex-structured-turn-start.ts`, `orca/src/main/runtime/orchestration/structured-session-pointer-delivery.ts:80-95`, `moai-adk/internal/cli/mcp_codex.go:575-630` | 런타임 없이도 쓸 수 있는 공식 프로토콜이다. 단, 전달하는 쪽이 app-server 프로세스를 소유한다. 사용자가 이미 띄운 TUI 프로세스와 같은 thread를 동시에 쓸 때의 동작은 확인 못 함 |
| **Orca → Codex TUI (PTY)** | orca: Orca 터미널에서 띄운 Codex TUI에 작업과 포인터를 입력창으로 전달한다 | 이번 조사에서 실측: Codex 워커 2개가 dispatch를 받고 `worker_done`으로 응답(`worker-show`의 `agent_terminal_handle`, `archive.source=transcript`) | Orca 터미널 안의 세션만 대상 |
| **MCP 채널 + 훅 drain** | agent-comms(Claude bridge): 이벤트를 `~/.agents/bus/pending/…jsonl`에 쌓고 `PostToolUse`·`Stop`·`UserPromptSubmit` 훅이 rename으로 비워 exit 2로 보여 준다. 실행 가능한 이벤트는 `notifications/claude/channel`로도 푸시한다 | `agent-comms/src/bridges/claude-code/channel.ts:184-202`, `hooks/hooks.json`, `hooks/drain.sh` | 채널 푸시는 `--dangerously-load-development-channels`가 있어야 한다. 훅만으로 완전히 idle한 세션이 깨는지는 확인 못 함 |
| **도구 응답 덧붙이기** | agent-comms(Codex·일반 MCP bridge): 대기 메시지를 모든 `agent_comms` 도구 응답에 붙인다 | `agent-comms/src/bridges/codex/tool.ts:1-9,84` | 깨우지 못한다. moai의 poll과 같은 한계 |
| **Codex queue** | Codex 0.155.1의 `codex queue --thread <id> --message` | 로컬 `codex queue --help` | 실행 중인 TUI 세션에 닿는지는 **확인 못 함**(스파이크 S2) |

Orca의 포인터 방식은 이번 조사 중에 실제로 관찰됐다. 사용자 메시지 중간에 `You have 1 orchestration message. Run \`orca orchestration check\`.`가 섞여 들어왔다. 코디네이터 세션의 입력창에 Orca가 포인터를 주입한 결과다. 사용자가 입력 중일 때도 주입이 일어날 수 있다는 한계를 보여 준다.

### 2.2 발견과 레지스트리 (G1, G2)

- **Claude**: 레지스트리가 CONFIG_DIR 아래에 있어 프로필끼리 서로 보이지 않는다. 소켓은 공용 `/tmp/cc-socks/`에 모인다.
- **herdr**: 프로필 경로와 독립된 통신 endpoint와, 에이전트 고유 세션 ID를 분리한다. 세션은 `SessionStart` 훅으로 herdr 소켓에 자기 세션 정보를 보고한다. subagent는 부모 정보를 덮어쓰지 않게 제외한다(`herdr/src/integration/assets/claude/herdr-agent-state.sh:20-23,51-99`, `.../codex/herdr-agent-state.sh:20-23,51-97`). 단, herdr가 띄운 pane 안의 세션만 대상이다.
- **moai-adk**: 프로젝트 로컬 `.moai/state/session-msg/agents/`에 등록한다. 식별자는 `kind+name`이라, 같은 이름의 두 세션이 한 주소를 공유하는 문제가 있다(`moai-adk/internal/sessionmsg/agent.go:93-105`). 생존은 heartbeat 30분으로 판정한다.
- **agent-comms**: 세션마다 붙은 MCP 서버(bridge)가 곧 메시 노드다. 처음 뜬 bridge가 `127.0.0.1:19876`을 잡아 코디네이터가 되고 피어를 소개한다. 레지스트리는 메모리에서 복제되고, 신원은 `(harness, cwd)` 슬롯별 키쌍(`~/.agent-comms/identity-*.json`)이다. 생존은 코디네이터가 5초마다 PID로 판정한다. CONFIG_DIR과 무관하지만, bridge가 없는 Claude 세션을 찾는 cc-peer는 `~/.claude/sessions`를 고정 경로로 읽어 비기본 프로필을 보지 못한다(`cc-peer/src/adapters/node/paths.ts:39`). 세부는 `agent-comms.md`.
- **orca**: 런타임이 띄운 터미널 핸들과 pane key가 주소다. 환경변수 `ORCA_TERMINAL_HANDLE`로 자기 자신을 식별한다. Orca 밖의 세션은 대상이 아니다.

공통 교훈: **발견은 모두 "누군가가 띄운 세션" 또는 "스스로 등록한 세션"에 한정된다.** 사용자가 직접 띄운 임의 세션을 CONFIG_DIR과 무관하게 발견하려면, 세션 훅 기반의 CONFIG_DIR 밖 공용 등록이 필요하다. 이것은 herdr와 moai가 쓰는 방식을 우리 목적에 맞게 조합한 것이다.

### 2.3 범위 제어 (G5)

이번 종합에서 새로 확인한 사실이다. **Claude Code에는 수신 정책 설정이 이미 있다.**

- `crossSessionInbound`:
  - `accept`: 전달한다.
  - `hold`: 사용자가 검토할 때까지 보류하고 Claude가 행동하지 못하게 한다.
  - `refuse`: 이 세션을 메시징에서 뺀다.
  - 값이 없으면 **모드 동등성** 규칙이 적용된다. 보낸 세션과 받는 세션의 권한 모드 계열이 같을 때(bypass↔bypass, prompting↔prompting)만 자동 전달하고, 다르면 보류한다.
  - 저장소 설정(project/local)은 더 엄격하게만 바꿀 수 있다. 관리 정책(managed)은 무엇보다 우선한다.
- `isolatePeerMachines: true`: 다른 머신으로 보내는 `SendMessage`와 `SendFile`은 매번 명시적 승인을 받는다. bypass 모드에서도 적용되고, 분류기가 대신 승인할 수 없다.
- 근거: Claude Code 2.1.278의 `chunk-papg5w8x.js`(설정 스키마 설명), `chunk-9mrd94qp.js`(정책 결정 함수: policy > flag > user, repo는 강화만), `chunk-8p8cqzh8.js`(보류 사유 문구), `chunk-zq11t17p.js`·`chunk-8vtc32rs.js`(`isolatePeerMachines` 승인 요구). moai-adk가 이 키들을 `--settings`로 넘기는 코드는 `moai-adk/internal/cli/crosssession_settings.go:47-91`에 있다.
- 다른 레퍼런스:
  - orca: 그룹 주소(`@worktree:<id>`, `@claude` 등)가 있다. 프로젝트 단위 ACL은 확인 못 함(`orca.md` 6절).
  - herdr: workspace 필터만 있고 접근 통제는 아니다(`herdr.md` 10절).
  - moai: 저장소 자체가 범위다. 레지스트리가 프로젝트 로컬에 있기 때문이다.
  - agent-comms: cwd별 프로젝트 방 자동 생성, 방 종류(`public`/`private`/`secret`), 에이전트 가시성(`visible`/`hidden`/`ghost`), 첫 DM의 접근 요청과 수락이 있다. 수락을 판단하는 것은 수신 에이전트이고 사람이 아니다. cc-peer는 `from-mode`를 지정하지 않으면 `bypass`를 주장한다(`cc-peer/src/cc-peer.ts:226-231`).

### 2.4 원격 (G4)

| 방식 | 사례 | 신뢰 모델 |
|---|---|---|
| SSH stdio 브리지 | herdr: 원격 API 소켓을 SSH stdio로 중계. `BatchMode=yes`, `StrictHostKeyChecking=yes` (`herdr/src/remote.rs:13-35`, `src/remote/attach.rs:1029-1044`) | SSH 키와 호스트 키 |
| 서버 간 relay | orca: `FederationRelayItemRow`(`to_home`/`to_worker` 방향, sequence, ack)로 원격 워커와 메시지를 동기화 (`orca/src/main/runtime/orchestration/types.ts:177-231`) | 연결된 Orca 서버 간 신뢰 |
| 벤더 API | Claude bridge: `/v1/sessions/{id}/events`, OAuth | Anthropic 계정 |
| 릴레이 mesh | buzz: 릴레이끼리 Iroh로 연결 | Nostr 서명(secp256k1) |
| WebSocket | Codex: `--remote ws://|wss://|unix://` + 토큰 환경변수 | 토큰 |
| TCP 메시 + gateway | agent-comms: 기본은 `127.0.0.1` 고정. `mesh_listen`으로 외부 listener를 추가하고 Tailscale·UDP 비콘으로 발견한다(`src/core/mesh-network-actions.ts:102-118`, `src/core/discovery-tailscale.ts:1-11`) | 상대 device-id를 양쪽이 명시적으로 신뢰(기본 거부). 일회용 연결 코드, PGP 서명 선택 |

### 2.5 채널-스레드 기록 (G6)

| 후보 | 데이터 모델 | 사람용 UI | 에이전트 연동 | 운영 부담 |
|---|---|---|---|---|
| buzz | 서명된 Nostr 이벤트 로그. 채널, 스레드(e 태그), 멘션(p 태그), 작업 요청(kind 43001), 워크플로 승인(kind 46010~46012) (`buzz/crates/buzz-core/src/kind.rs:442-582`) | 데스크톱·모바일·웹 | buzz-acp(ACP) 하니스, CLI, MCP | PostgreSQL + Redis + 릴레이 |
| agora | append-only Git DAG. 노드에 태그(result, insight, hypothesis, verification, report, wip)와 부모 간선이 있다. SQLite 인덱스는 Git에서 재구축할 수 있다(논문 §3.2, §3.4) | Next.js 웹(리더보드, 계보, 프론티어) | CLI, HTTP API | Git 서버 + 인덱서. 코드 미공개 |
| orca | SQLite 메시지 테이블. `thread_id`, `type`, `priority`, `sequence` (`orca/src/main/runtime/orchestration/types.ts:233-253`) | Orca 앱 내부 | orca CLI | Orca 런타임 |
| moai | 수신자별 JSON 파일. 24시간 만료, 영속 기록 아님 | 로컬 웹 콘솔(칸반) | MCP | 낮음 |
| agent-comms | 방·DM·전달 큐를 메모리에서 복제. delivered/read 수신 확인. 영속화 없음 | 로컬 웹 대시보드(에이전트·방·기록·메시 그래프), 사람용 `user` bridge | MCP 도구 `agent_comms`(pi, Claude, Codex, OpenCode) | 낮음(npx, 데몬 없음) |

INTENT.md의 "GitHub issue는 정본만, 일상 소통은 별도 채널" 구분과 가장 잘 맞는 것은 다음 둘이다.
- buzz: 사람이 보는 UI가 강하다. 대신 운영 부담이 크다.
- agora의 모델: append-only로 기록하고, 정본(report)을 도출된 뷰로 만든다. 대신 사람용 채팅 UI가 약하다.

### 2.6 공동 문서 편집과 동시성 (G7)

| 기법 | 사례 | 사본 증식 | 무한 대기 |
|---|---|---|---|
| append-only + 도출된 정본 | agora: 기여는 불변 커밋, 같은 부모에서의 병렬 기여는 모두 DAG에 남는다. 정본은 report 노드와 인덱스 뷰 | 증식하지만 계보로 연결됨 | 잠금이 없어 대기도 없다 |
| 기한 있는 잠금 + 원자적 쓰기 | moai: 수신자별 advisory lock(`flock`), 임시 파일 + atomic replace, 잠금 획득 재시도 최대 2초 (`moai-adk/internal/sessionmsg/lock.go:12-113`) | 없음 | 잠금 획득 루프에만 시간 제한이 있다. 호출 전체가 제한되는 것은 아니다(`moai-adk.md` 8절) |
| claim + 재전달 + 만료 | moai: claim 후 10분이 지나면 재전달, 24시간 만료, at-least-once | 없음 | 없음 |
| FIFO 배치 + 명시적 ack + 재생 | orca: `check`가 가장 오래된 배치를 ack 전까지 반복 반환한다. 이번 조사에서 실제로 사용했다 | 없음 | `--wait` 타임아웃은 실패가 아니라 체크포인트로 취급 |
| 소유권 분할 | 이번 조사: 워커마다 파일 하나 | 파일 수만큼 | 없음 |
| 경고 | herdr: 원자적 rename만으로는 다중 writer의 lost update를 막지 못한다 (`herdr/src/persist/io.rs:48-60`) | | |

### 2.7 "별도 런타임 금지"(C1)와의 적합성

- orca, herdr: 자체 런타임이 에이전트 터미널을 소유한다. 이 구조를 그대로 가져오면 C1에 어긋난다. herdr 조사 워커도 같은 판단을 했다(`herdr.md` 10절).
- moai: 세션마다 MCP 서버를 붙이는 방식이라 C1에 가장 가깝다. 대신 wakeup이 없다.
- agent-comms: moai와 같은 부류로 C1에 맞는다. wakeup은 하니스마다 다르다. pi는 네이티브, Claude는 채널·훅, Codex는 없다.
- Claude 네이티브: 런타임 없이 wakeup까지 된다. 한계는 CONFIG_DIR 격리와 Claude 전용이라는 점이다.
- 따라서 C1을 지키면서 G3를 만족하는 조합은 다음과 같다.
  - Claude: 네이티브 inbox를 쓴다.
  - Codex: 네이티브 수단(`codex queue`/app-server)이 실행 중 세션에 닿는지 확인해야 한다. 닿지 않으면 PTY 포인터 방식이 대안인데, 이것은 터미널을 소유하는 누군가를 전제한다.

## 3. 설계 시사점

| 판단 | 내용 | 출처 |
|---|---|---|
| 차용 | 세션 훅이 CONFIG_DIR 밖의 공용 레지스트리에 자기 주소와 고유 세션 ID를 보고한다. subagent는 제외한다 | herdr 훅, moai 레지스트리 |
| 차용 | 전달은 "포인터만 밀고 본문은 당겨 간다(push pointer, pull payload)". 수신 측이 idle일 때만 포인터를 주입하고, 이미 기다리는 중이면 주입하지 않는다 | orca `mailbox-pointer-delivery.ts` |
| 차용 | 쓰기 성공과 작업 완료를 구분한다. 수신 대상의 생존, 준비, blocked 상태를 구분해 응답한다 | herdr `agents.rs:130-173` |
| 차용 | 수신 정책은 Claude 네이티브 `crossSessionInbound`/`isolatePeerMachines` 의미를 따른다. 우리 어댑터가 Codex에 전달할 때도 같은 규칙(모드 동등성, 저장소는 강화만)을 적용한다 | Claude Code 2.1.278 |
| 차용 | 채널-스레드 기록은 append-only로 하고 정본은 도출한다. 태그로 결과, 가설, 검증, 보고를 구분한다 | agora §3.2 |
| 차용 | at-least-once 전달, claim 만료 후 재전달, 메시지 만료 | moai `store.go` |
| 차용 | 세션별 MCP 서버를 메시 노드로 삼고, 잘 알려진 localhost 포트의 코디네이터가 소개만 한다. 데몬과 파일 버스가 필요 없다 | agent-comms `mesh-store.ts` |
| 차용 | 슬롯 `(harness, cwd)`에 묶인 키쌍으로 재시작해도 유지되는 주소를 만들고, 원격은 device-id 고정 신뢰로 연다 | agent-comms `identity-store.ts`, gateway trust |
| 차용 | 긴급도 힌트(steer/followUp/info)와 delivered/read 구분 | agent-comms `types.ts:83` |
| 피함 | 에이전트 터미널을 소유하는 런타임을 전제로 한 발견·wakeup | orca, herdr |
| 피함 | `from-mode`를 실제 모드와 무관하게 `bypass`로 주장하는 것, 레지스트리를 `~/.claude/sessions`로 고정하는 것 | cc-peer |
| 피함 | 화면 텍스트 판별만으로 세션 상태를 정하는 것 | herdr `detect/manifests/*.toml` |
| 피함 | `kind+name`을 세션 주소로 쓰는 것. 같은 이름의 세션끼리 충돌한다 | moai `agent.go:93-105` |
| 확인 필요 | 프로필이 다른 Claude 세션에 inbox로 보낼 때 모드 동등성 규칙 때문에 보류되는지(보낸 쪽이 `from-mode`를 어떻게 주장하는지) | 스파이크 S1 |
| 확인 필요 | `codex queue`가 실행 중인 TUI 세션을 깨우는지 | 스파이크 S2 |

## 4. 개별 보고서 품질과 정오표

v2 추가: 보강 조사 7건 중 5건(T2, T3, T5, T6, T7)에서 코디네이터 검증으로 사실 오류가 나왔다. 모두 같은 워커에 정정 Task를 이어서 맡겨 고쳤다(T2-fix, T3-fix, T5-fix, T6-fix, T7-fix). T3 해소표 한 줄(R4-07)과 T2 결과표의 bypass 행은 코디네이터가 직접 정정하거나 이 문서 0.5절에서 바로잡았다. 오류는 대부분 2차 도구 출력(Jina Reader 추출, 키워드 검색)을 원본처럼 믿은 데서 나왔다.

코디네이터가 인용 경로와 줄 범위를 스크립트로 일괄 검사하고, 일부는 원본과 직접 대조했다.

| 보고서 | 인용 수 | 결과 | 정정 사항 |
|---|---|---|---|
| `orca.md` | 파일+줄 범위 형식 | 인용한 파일은 모두 존재. `types.ts:43-53`의 `RunRow`는 실제 `run-create` 응답 필드와 일치 | 3절 "Agent Hook이 메시지 spool 디렉터리를 정기 poll"은 **근거가 없다.** 실제 wakeup은 PTY 포인터 주입이다(2.1절). orca 훅(`src/main/agent-hooks/`)은 상태 보고용으로 보이나, 이 부분은 확인 못 함 |
| `herdr.md` | 78 | 저장소 밖 `INTENT.md` 인용을 빼면 모두 유효 | 없음 |
| `moai-adk.md` | 92 | 모두 유효 | 없음 |
| `buzz.md` | 37 | 일부 오류 | `crates/buzz-relay/src/architecture.md`는 **존재하지 않는다**(실제는 루트 `ARCHITECTURE.md`). `VISION_MESH.md:1-54`, `VISION_REMOTE_AGENTS.md:1-74`는 파일 길이(53줄, 73줄)를 1줄 넘는다. `:1`처럼 파일 전체를 가리키는 인용이 많아 정밀도가 낮다 |
| `agora.md` | 절·표 번호 | 초록의 수치(워커 13개, 약 12일, 기여 1,703건, 3.39→1.899 bpb, 격차 62%)가 arXiv 초록과 일치 | 저자 목록은 초록 페이지에서 대조하지 못했다 |

## 5. 다음 단계와의 연결

- 계획: `../plan/README.md`
  - 2.3절 표를 이 문서로 대체한다.
  - 스파이크 S1에 모드 동등성 확인을 추가한다.
- ADR: `../adr/` — 0001(레지스트리), 0002(전달), 0004(범위), 0005(채널), 0006(문서 편집)의 맥락과 근거에 이 문서의 2장을 링크한다.
