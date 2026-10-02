# xsm, A2A, agent-comms 비교

세 가지는 푸는 문제의 층이 다르다. A2A는 HTTP 서비스로 노출된 에이전트끼리 쓰는 상호운용 표준이고, agent-comms와 xsm은 사람이 이미 띄워 둔 코딩 에이전트 세션끼리 메시지를 주고받게 하는 로컬 도구다. 따라서 xsm과 직접 겹치는 상대는 agent-comms이고, A2A는 구현 대상이 아니라 메시지·상태 어휘를 빌릴 참고 대상이다.

- 작성일: 2026-10-02
- 기준:
  - xsm: 0.4.15 (`.claude-plugin/plugin.json`), `../xsm/README.md`, `../reviews/E1-v0.4.0-evaluation.md` 5장
  - agent-comms: `agent-comms.md`의 정적 조사(커밋 `a68c00f`, 2026-09-19, npm 4.1.2). 실행 시험은 하지 않았고 그 뒤 변경은 반영하지 않았다.
  - A2A: 명세 원문 `https://a2a-protocol.org/latest/specification/` (Latest Released Version 1.0.0, 2026-10-02 Jina Reader로 조회)

## 1. 한눈에 비교

| 항목 | xsm (0.4.15) | agent-comms (4.1.2) | A2A (명세 1.0.0) |
|---|---|---|---|
| 정체 | 떠 있는 Claude Code·Codex 세션 사이 메시징 도구 | 하니스별 MCP bridge들이 이루는 localhost 메시 | 독립적이고 내부가 감춰진(opaque) 에이전트 시스템 간 개방형 프로토콜 |
| 대상 | 사람이 띄운 대화형 TUI 세션 | Claude Code, Codex, pi, OpenCode, 일반 MCP 클라이언트 | 서버로 노출된 에이전트 |
| 세션 발견 | 프로필(CONFIG_DIR)을 넘는 레지스트리 | 처음 뜬 bridge가 `127.0.0.1:19876`의 코디네이터가 되어 피어를 소개 | Agent Card(능력·skill·엔드포인트·인증 요구 선언, 서명 가능), well-known URI 등록(명세 8장, 14.3절) |
| 전송 | Claude는 네이티브 inbox 소켓, Codex는 `codex queue` | bridge 간 TCP 메시 | JSON-RPC, gRPC, HTTP/REST 바인딩(명세 9~11장) |
| 상주 프로세스 | 없음 | 없음(bridge 프로세스가 곧 노드) | 에이전트마다 서버 |
| idle 세션 깨우기 | Claude는 네이티브 inbox. Codex는 턴이 끝난 뒤 큐로 받고, Esc로 멈춘 스레드는 app-server에 `thread/queue/start`를 요청해 깨운다 | pi는 네이티브. Claude는 개발 채널 플래그나 훅이 필요. Codex는 도구 호출 응답에 덧붙일 뿐 깨우지 못함 | 개념이 없다. 서버가 늘 요청을 받는다 |
| 상호작용 단위 | 메시지, 채널 스레드, 공동 문서, 워커 | 방(public/private/secret), DM | Task(상태 수명주기), Message(Part), Artifact, contextId |
| 비동기 진행 보고 | 답장 메시지 | 방 이벤트 | `SendStreamingMessage`(스트림), `SubscribeToTask`, webhook 푸시(`TaskPushNotificationConfig`) |
| 범위·동의 | 프로젝트 가입(`join`), 폴더 `link`, 세션 한정 `reach`. 범위 확장은 사람이 MCP 양식으로 승인 | 방 종류, 가시성(visible/hidden/ghost). 첫 DM 수락은 수신 **모델**이 판단 | 인증·인가 방식을 Agent Card에 선언. 확장 Agent Card는 인증 후 조회 |
| 기록 지속성 | 채널·문서를 append-only 파일로 남김 | 메모리에서 복제. 모든 bridge가 내려가면 사라짐 | 구현에 맡김(`GetTask`, `ListTasks` 조회 API는 있음) |
| 기계 간 | SSH로 짝지은 프로젝트끼리. 원격 세션은 `xsm list`에 나오지 않음 | `mesh_listen`, Tailscale·UDP 비콘 발견, device-id 고정 신뢰 | 본래 네트워크 프로토콜 |
| Claude 수신 안전장치 | 발신자의 실제 권한 모드를 봉투에 넣고, 보류 여부를 보내기 전에 예측해 알림 | cc-peer가 `fromMode` 기본값으로 `bypass`를 주장(`cc-peer/src/cc-peer.ts:226-231`) | 해당 없음 |
| 사람 UI | 상태줄, `/xsm log` | 웹 UI(메시 그래프, 방 기록), 사람도 `user` bridge로 참여 | 없음(프로토콜) |

## 2. xsm만 하는 것

- **Codex 깨우기.** agent-comms는 Codex가 `agent_comms` 도구를 부를 때 응답에 메시지를 덧붙일 뿐이다(`src/bridges/codex/tool.ts`). xsm은 `codex queue`로 넣어 턴이 끝나면 전달되게 하고, Esc로 멈춘 스레드도 깨운다. E1 평가는 이 Claude-Codex 구간을 xsm의 핵심 가치이자 가장 취약한 곳으로 꼽았다.
- **프로필 간 발견.** cc-peer는 `~/.claude/sessions`를 고정 경로로 읽어 `~/.claude-3` 같은 비기본 프로필 세션을 보지 못한다(`cc-peer/src/adapters/node/paths.ts:39`).
- **사람의 동의.** agent-comms의 DM 수락은 수신 모델이 정한다. xsm은 범위를 넓히는 일을 사람의 승인 양식에 묶는다.
- **기록이 남는다.** 채널과 공동 문서가 파일로 남아 세션이 모두 꺼져도 유지된다.
- **워커.** 실제 TUI를 tmux에 띄운다. agent-comms와 A2A에는 이에 해당하는 기능이 없다.

## 3. agent-comms가 앞서는 것

- 지원 하니스가 더 많다(pi, OpenCode, 일반 MCP).
- 원격 신뢰 모델이 더 정교하다. device-id 고정, 일회용 연결 코드, listener별 정책(full/observe/rooms-only/gateway)이 있다. xsm의 원격은 SSH 짝짓기까지이고 원격 세션이 목록에 나오지 않는다.
- 수신 확인을 두 단계(`delivered`: 큐 진입, `read`: bridge 소비)로 나눈다.
- 방 가시성 단계와 사람이 보는 웹 UI가 있다.

## 4. A2A와의 관계

A2A는 에이전트를 HTTP 서비스로 노출하는 것을 전제로 한다. 사람이 터미널에서 다루는 세션에 붙는 xsm과는 출발점이 다르고, xsm이 A2A를 구현할 이유는 없다. 다만 개념 두 가지는 빌릴 만하다.

- **Task 상태 수명주기.** `input-required`, `auth-required` 같은 중단 상태와 completed/failed/canceled/rejected 종료 상태. 워커 보고나 ADR-0012(누가 다음 노드를 고르나, Proposed)에서 "답장 메시지" 대신 상태 어휘로 쓸 수 있다.
- **Agent Card.** 세션이 할 수 있는 일을 스스로 선언하는 방식. `xsm list`의 세션 메타데이터를 넓힐 때 참고가 된다.

moai-adk도 같은 방식으로 필드명만 A2A에서 빌리고 전송 프로토콜은 구현하지 않았다(`moai-adk.md` 51행).

## 5. 관련 문서

- `agent-comms.md`: agent-comms·cc-peer 소스 조사
- `README.md`: 레퍼런스 종합
- `../reviews/E1-v0.4.0-evaluation.md` 5~6장: 유사 제품 표와 포지셔닝
