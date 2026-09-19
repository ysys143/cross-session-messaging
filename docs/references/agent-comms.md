# agent-comms 소스 조사

agent-comms는 **하니스마다 붙는 MCP 서버(bridge) 프로세스끼리 localhost TCP 메시를 이루는** 에이전트 간 메시징 패키지다. 데몬도, 공유 파일 버스도 없이 방·DM·존재 표시·가시성을 제공한다. Claude Code, Codex, pi, OpenCode, 일반 MCP 클라이언트용 bridge가 있고, 동반 패키지 `cc-peer`가 Claude Code의 네이티브 cross-session 소켓 프로토콜을 직접 구사해 bridge가 없는 Claude 세션도 메시에 올린다. 이 저장소가 찾던 "CONFIG_DIR과 무관한 이종 세션 메시징"에 가장 가까운 기존 구현이지만, 수신 세션 깨우기는 하니스마다 다르고 Codex는 깨우지 못한다.

- 조사 기준:
  - `ExaDev/agent-comms` 커밋 `a68c00f` (2026-09-19, npm 4.1.2)
  - `ExaDev/cc-peer` 커밋 `74fd76a` (2026-09-18)
  - 안내 페이지 `https://pi.dev/packages/agent-comms` (Jina Reader로 조회)
- 방법: `git clone --depth 1` 후 `grep`/`sed`로 한 정적 조사다. 빌드, 실행, 실제 세션 간 통신은 하지 않았다. 아래 인용 경로는 각 저장소 루트 기준이다.
- 조사 워커 없이 코디네이터(이 세션)가 직접 조사했다.

## 1. 정의와 실행 형태

- 배포: npm 패키지. `npx agent-comms bridge <harness>`로 하니스별 bridge를 stdio MCP 서버로 띄운다. `npx agent-comms`는 설치된 하니스(pi, Claude Code, Codex, OpenCode)를 감지해 설정 파일을 써 준다(README).
- bridge 종류: `src/bridges/{claude-code,codex,mcp,opencode,pi,cc-peer,user}`.
- 설계 배경(README): 처음에는 `~/.agents/bus/`의 JSON 파일 버스였다. 고아 파일, poll 비용, 동시 쓰기 경합, stale 판정 문제로 버렸다. 대신 "MCP 서버 인스턴스는 이미 실행 중인 프로세스"라는 점에 착안해 bridge 프로세스 자체를 메시 노드로 삼았다.

## 2. 세션 발견과 레지스트리

- **코디네이터 패턴**: 처음 뜬 bridge가 `127.0.0.1:19876`을 바인드해 코디네이터가 된다. 나머지는 여기에 접속해 피어 목록을 받고, 모든 피어와 직접 연결한다(`src/core/mesh-store.ts:82`, `src/core/mesh-store-shared.ts:57`). 코디네이터는 소개만 하고 라우팅하지 않는다.
- 코디네이터가 죽으면 남은 피어가 포트 바인드를 경쟁한다(README: 약 100ms 복구). 정상 종료 시에는 가장 오래 산 피어에게 넘긴다.
- **레지스트리는 메모리에만 있다.** agents, rooms, messages, dms, deliveryQueues가 모두 `MeshStore`의 Map이고 피어 간에 복제된다(`src/core/mesh-store.ts:8,110,214`). 디스크에는 신원 키만 남는다.
- **생존 판정**: 코디네이터만 5초마다 등록 PID에 signal 0을 보내 죽은 에이전트를 offline으로 바꾼다(`src/core/stale-agent-checker.ts:19`).
- **신원**: bridge 슬롯 `(harness, cwd)`마다 ECDSA P-256 키를 만들어 `~/.agent-comms/identity-<harness>--<cwd>.json`에 둔다. 공개키의 SHA-256이 device-id이자 에이전트 ID다(`src/core/identity-store.ts:163`, README Identity). 재시작해도 ID가 유지된다. 같은 슬롯에 두 번째 bridge가 뜨면 잠금 파일 때문에 임시 신원으로 동작한다.
- **Claude 세션 발견(cc-peer)**: `cc-peer`는 `~/.claude/sessions/*.json`을 읽는다. 경로가 `homedir()/.claude/sessions`로 고정돼 있고 `CLAUDE_CONFIG_DIR`을 보지 않는다(`cc-peer/src/adapters/node/paths.ts:39`). 소켓 디렉터리는 `/tmp/cc-socks`, `/private/tmp/cc-socks`, `$XDG_RUNTIME_DIR/cc-socks`다(같은 파일 `:25-36`).
  - 따라서 `~/.claude-3` 같은 비기본 프로필 세션은 로스터에 나타나지 않는다. 설정으로 `homeDir`는 바꿀 수 있지만 `.claude` 이름은 바꿀 수 없다.

## 3. 메시징·전달과 wakeup

API는 도구 하나(`agent_comms`)에 action을 두는 형태다. `register`, `list_agents`, `create_room`, `join_room`, `send`, `dm`, `read_room`, `update` 등이 있다.

**전달 시점 힌트**: `send`/`dm`에 `streamingBehavior`(`steer` | `followUp` | `info`)를 줄 수 있다(`src/core/types.ts:83`). 이 값은 수신 측 bridge에 대한 힌트이고, 실제 동작은 bridge마다 다르다.

| 수신 하니스 | 경로 | 깨우기 |
|---|---|---|
| pi | 확장 API의 `deliverAs: "steer"/"followUp"` | 네이티브로 지원(README) |
| Claude Code | (1) 모든 이벤트를 `~/.agents/bus/pending/claude-code--<cwd-slug>--<claude-pid>.jsonl`에 추가한다. (2) 실행 가능한 이벤트는 MCP 채널 알림 `notifications/claude/channel`로도 보낸다(`src/bridges/claude-code/channel.ts:10-11,184-202`). (3) 훅 `hooks/drain.sh`가 파일을 원자적으로 rename해 비우고, stderr에 쓰고 exit 2로 끝난다. (4) `agent_comms` 도구 응답에도 대기 메시지를 붙인다(`channel.ts:255-258`) | 채널 푸시는 `--dangerously-load-development-channels`가 켜져 있을 때만 된다(`channel.ts:191-192` 주석). 훅은 `PostToolUse`·`Stop`·`UserPromptSubmit`에 걸리고, `asyncRewake: true`는 `UserPromptSubmit`에만 있다(`hooks/hooks.json`). **아무 이벤트도 없는 idle 세션이 파일 경로만으로 깨어나는지는 확인 못 함** |
| Codex | MCP 도구 서버. 대기 메시지를 비워 **모든 도구 응답에 덧붙인다**(`src/bridges/codex/tool.ts:1-9,84`) | 없다. Codex가 `agent_comms`를 호출해야 받는다. `codex queue` 같은 네이티브 경로는 쓰지 않는다 |
| MCP 일반, OpenCode | Codex와 같은 drain 방식(README Delivery status 표) | 없다 |
| Claude(bridge 없음) | 코디네이터를 맡은 bridge가 `cc-peer` 로스터로 로컬 세션을 찾아 대신 전면에 선다(default front). 메시 이벤트를 Claude 네이티브 소켓으로 보낸다(`src/bridges/cc-peer/front-relay.ts:60`) | 예. Claude 네이티브 inbox를 쓰므로 Claude의 수신 정책을 그대로 탄다 |

**수신 확인**: 큐에 들어가면 발신자에게 `delivery_status: delivered`를 보내고, bridge가 소비하면 `read`를 보낸다(README). 쓰기 성공과 소비를 구분한다는 점은 herdr의 교훈과 같다.

**재시작 중 도착분**: 수신 bridge가 꺼져 있는 동안 쌓인 이벤트는 복제된 `deliveryQueues`에 남았다가 재접속 시 재생된다(README, `mesh-store.ts:110`). 단, 메시 전체가 내려가면 메모리 상태이므로 사라진다.

**cc-peer의 봉투**: `cc-peer`는 `<cross-session-message>` 봉투를 만든다. `fromMode`를 지정하지 않으면 **기본값으로 `"bypass"`를 주장한다**(`cc-peer/src/cc-peer.ts:226-231`).
- 이 저장소의 조사(`README.md` 0.5절)에 따르면 `from-mode`는 Claude 수신 판정에 쓰이는 발신자 자기 주장 값이다.
- 따라서 bypass로 도는 수신 세션은 cc-peer가 보낸 메시지를 모드 동등성 검사에서 자동 수락한다. 발신 측이 실제로 bypass인지와 무관하다.

## 4. 이종 에이전트와 CONFIG_DIR

- 메시 자체는 CONFIG_DIR과 무관하다. 주소는 `~/.agent-comms`의 슬롯 키와 localhost 포트로 정해진다. `~/.codex`와 `~/.codex-2`의 Codex bridge도 같은 메시에 들어간다(추론, 실행 확인 못 함).
- 예외는 bridge 없는 Claude 세션의 default front다. 2절대로 `~/.claude/sessions`만 읽으므로, 비기본 프로필 Claude 세션은 자기 bridge(플러그인이나 `claude mcp add`)를 설치해야 메시에 들어온다.
- Claude 대기 파일이 `~/.agents/bus/pending/`에 생긴다. 이 사용자는 `~/.agents`를 공용 에이전트 규칙 디렉터리로 쓰고 있으므로 설치 시 이 경로가 생긴다는 점을 알아 둘 것.

## 5. 원격·다중 머신과 신뢰

- 기본 코디네이터 listener는 `127.0.0.1`에 고정되고 설정할 수 없다(`src/core/listener-registry.ts:90` 주석, `mesh-store-shared.ts:57`).
- 머신 밖으로 열려면 `mesh_listen` action으로 listener를 명시적으로 추가한다. 정책은 `full`, `observe`, `rooms-only`, `gateway` 중 하나다(`src/core/mesh-network-actions.ts:102-118`).
- 발견 백엔드:
  - Tailscale: tailnet 피어의 19876 포트에 TCP 접속을 시도한다(`src/core/discovery-tailscale.ts:1-11`).
  - UDP 브로드캐스트 비콘: 19877 포트, 30초 주기(`src/core/discovery-mdns.ts:1-13`).
  - Tailscale 파일 주석은 "코디네이터가 이미 Tailscale IP에서 listen한다"고 쓰지만, 코디네이터 host가 127.0.0.1로 고정된 것과 맞지 않는다. `mesh_listen`을 먼저 해야 하는 것으로 보이나 **확인 못 함**.
- **gateway 신뢰**: 다른 머신끼리 서로의 hub로 중계하려면 양쪽이 상대 device-id를 명시적으로 신뢰해야 한다. 기본값은 거부이고, 디렉터리 조회 없이 키를 고정한다(README Gateway trust). `gateway_trust`/`gateway_untrust`/`gateway_list_trusted`.
- **연결 코드**: device-id를 대역 외로 넘길 때 쓰는 일회용·단기 코드다(nonce + 만료 + device-id). PGP 서명은 선택이다. 코드를 쓰면 `gateway_trust`를 부른 것과 같다.
- cc-peer 자체는 단일 머신 전용이다(유닉스 소켓). 원격 Claude 세션에 닿으려면 cc-peer bridge가 로컬 세션을 메시에 올리고, 메시가 머신 사이를 잇는다.

## 6. 범위 제어

| 수단 | 내용 | 근거 |
|---|---|---|
| 프로젝트 방 | bridge는 등록할 때 cwd 이름으로 된 방을 자동으로 만들고 들어간다 | `src/core/bridge.ts:620`, `src/bridges/pi/extension.ts:149-151` |
| 방 종류 | `public`(목록에 보임, 누구나 참여), `private`(이름만 보임, 초대만), `secret`(안 보임, 초대만) | README Room types |
| 가시성 | `visible`, `hidden`(목록에서 빠지지만 ID를 알면 DM 가능), `ghost`(DM도 불가) | `src/core/types.ts:60` |
| DM 동의 | 첫 DM은 수신 에이전트에게 접근 요청(`room_join_request`)을 보내고, 수락되어야 전송된다. 거절이나 무응답이면 실패한다 | README Direct messages, `src/core/room-protocol.ts` |
| 머신 경계 | 기본은 localhost. 원격은 `mesh_listen` + gateway 신뢰가 있어야 한다 | 5절 |

DM 동의를 판단하는 주체는 **수신 에이전트(모델)**다. 사람의 승인 절차는 아니다. Claude 네이티브의 `crossSessionInbound: hold`처럼 사람에게 넘기는 장치는 보이지 않는다.

## 7. 지속 기록과 사람이 보는 UI

- 방 기록은 메모리에 복제되는 상태이고 `read_room`으로 읽는다. 디스크 영속화 코드는 신원 파일 외에는 찾지 못했다(`src/core/identity-store.ts`, `src/core/user-identity.ts`만 파일을 쓴다). 모든 bridge가 내려가면 기록이 사라진다.
- 웹 UI: `npx agent-comms chat` 등으로 웹 서버를 띄우면 에이전트·방 목록, 메시지 기록, 메시 그래프를 보여 준다. 포트는 코디네이터 포트+1(19877)부터 찾는다(`src/bridges/user/web/frontend/mesh-client.ts:88,170`). 탭들은 `SharedWorker` 하나를 공유하고, oRPC(WebSocket `/ws/mesh`)로 이벤트 스트림을 받는다.
- 사람도 `user` bridge로 메시에 참여할 수 있다(`src/bridges/user/`).

## 8. 동시성·충돌·재시도

- Claude 대기 파일은 rename을 동기화 수단으로 쓴다. 훅의 drain과 도구 호출의 drain이 겹쳐도 한쪽만 파일을 가져간다(`hooks/drain.sh`).
- 같은 cwd의 Claude 세션 둘이 대기 파일을 공유하지 않도록 파일 이름에 Claude PID를 넣는다. PID를 못 찾으면 cwd 공용 파일로 돌아가고 경고를 낸다(`hooks/drain.sh`, `channel.ts:174`).
- 코디네이터 장애는 포트 바인드 경쟁으로 넘기고, 오프라인 수신자 몫은 복제된 전달 큐로 재생한다.
- 공동 문서 편집(G7)에 해당하는 기능은 없다.

## 9. 오케스트레이션 모델과 런타임 의존성

- 에이전트를 띄우거나 터미널을 소유하지 않는다. 사용자가 띄운 세션에 MCP 서버를 붙이는 방식이라 C1(진입점 래퍼 금지)과 맞는다. moai-adk와 같은 부류다.
- 대신 bridge 프로세스들이 메시 상태 자체이므로, 세션이 하나도 없으면 메시도 기록도 없다.
- 작업 지시, 보고 대기 같은 오케스트레이션 원시 기능(Orca의 dispatch/`worker_done` 같은 것)은 없다. 방과 DM 위에서 에이전트가 관례로 조율한다.

## 10. INTENT.md에 대한 시사점

| 판단 | 내용 |
|---|---|
| 차용 | 세션마다 붙는 MCP 서버 자체를 레지스트리·전송 노드로 삼는다. 데몬과 공유 파일 버스가 필요 없다 |
| 차용 | 잘 알려진 localhost 포트 + 코디네이터 경쟁으로 발견하고, 코디네이터는 소개만 한다 |
| 차용 | 슬롯 `(harness, cwd)`에 묶인 키쌍으로 신원을 정한다. 재시작해도 주소가 유지되고, 원격 신뢰는 device-id 고정으로 한다 |
| 차용 | `streamingBehavior`(steer/followUp/info)로 긴급도를 표현하고 하니스별로 대응한다 |
| 차용 | delivered와 read를 구분한 수신 확인 |
| 차용 | cc-peer의 default front: bridge가 없는 Claude 세션도 네이티브 소켓으로 대신 전면에 선다. 답장용 별칭(alias)을 네이티브 피어로 만들어 준다 |
| 피함 | Codex를 도구 응답 덧붙이기로만 받는 것. S2에서 확인한 `codex queue`가 idle TUI를 깨우므로 그쪽이 낫다 |
| 피함 | cc-peer처럼 `from-mode`를 `bypass`로 기본 주장하는 것. 실제 발신자 모드를 정직하게 넣어야 Claude의 모드 동등성 안전장치가 산다 |
| 피함 | 레지스트리 경로를 `~/.claude/sessions`로 고정하는 것. 이 프로젝트의 G1(프로필 간 인식)을 못 채운다 |
| 확인 필요 | 메모리 상태 메시가 채널-스레드 기록(G6)으로 충분한가. 모든 bridge가 내려가면 기록이 없다. 별도 append-only 저장이 필요해 보인다 |
| 확인 필요 | Claude bridge의 idle 깨우기가 개발 채널 플래그 없이도 되는가 |

## 근거 파일 목록

agent-comms (`a68c00f`):
- `src/core/mesh-store.ts`, `src/core/mesh-store-shared.ts`: 메시 상태, 코디네이터 포트·host.
- `src/core/identity-store.ts`: 슬롯별 신원 파일.
- `src/core/stale-agent-checker.ts`: PID 생존 판정.
- `src/core/types.ts`: 가시성, `streamingBehavior` 스키마.
- `src/core/bridge.ts`, `src/core/mesh-network-actions.ts`, `src/core/listener-registry.ts`: action 해석, 프로젝트 방, `mesh_listen`.
- `src/core/discovery-tailscale.ts`, `src/core/discovery-mdns.ts`: 원격 발견.
- `src/bridges/claude-code/channel.ts`, `hooks/hooks.json`, `hooks/drain.sh`: Claude 전달.
- `src/bridges/codex/tool.ts`: Codex 전달.
- `src/bridges/cc-peer/*.ts`: Claude 네이티브 소켓 중계, default front.
- `src/bridges/user/web/`: 웹 UI.

cc-peer (`74fd76a`):
- `src/adapters/node/paths.ts`: 세션 레지스트리·소켓 디렉터리 경로.
- `src/cc-peer.ts`, `src/schemas/envelope.ts`: 봉투 생성, `fromMode` 기본값.
