# T4 — 원격 전송과 신뢰 모델 재조사

Orca federation은 SSH가 아니라 페어링된 WebSocket·공개키 유도 공유키·기기 토큰을 사용하며, Buzz mesh는 같은 배포의 서명 키와 Redis 소유권 판정을 공유하는 직접 IP 연결 구조로 확인됐다. Codex의 원격 app-server 접속과 Remote Control은 별개 진입 경로이고, Claude bridge 내부 API를 외부 어댑터용 공식 계약으로 볼 근거는 확인하지 못했다. 따라서 CONFIG_DIR에 독립적인 발견·기존 세션 wakeup을 원격까지 달성했다는 결론은 아직 불가하며, SSH의 피어 신원·회신·머신 격리 보존은 미실행 S5로 남긴다. 아래 표와 상세의 원본 근거에 한정한 결론이다.

## 리뷰 항목별 해소 결과

| 항목 ID | 결론 | 근거 |
|---|---|---|
| R4-01 | 해소: SSH 기반·추가 인증 없음이라는 설명은 오류. 저장된 pairing으로 WebSocket/E2EE/device-token RPC를 수행한다. | `orca/src/main/startup/main-process-runtime-service.ts:51-71`; `orca/src/shared/remote-runtime-request-websocket.ts:35-56,109-133`; `orca/src/main/runtime/runtime-rpc/runtime-rpc-websocket-dispatch.ts:57-85` |
| R4-02 | 정적 규약 해소: dispatch 서버 지문과 호출별 pairing revision을 별도로 검사한다. 철회는 토큰 삭제와 연결 종료이며 이미 실행된 작업의 롤백을 뜻하지 않는다. 재페어링·진행 중 작업의 실측은 남음. | `orca/src/main/runtime/orchestration/federation-sync.ts:71-103,197-247`; `orca/src/main/ipc/runtime-environment-revision-guard.ts:4-28`; `orca/src/main/runtime/runtime-rpc/runtime-rpc-pairing.ts:110-117` |
| R4-03 | 해소: `buzz-relay-mesh` 크레이트는 존재한다. | `buzz/crates/buzz-relay-mesh/src/endpoint.rs:17-39`; `registry.rs:158-194`; `membership.rs:27-41` |
| R4-04 | 원격 모델 해소: 독립 저장소 federation이 아니라 Redis ready registry·fenced directory·pub/sub를 사용하는 배포 내 mesh. 최소 호스팅·채널 연동은 T5 담당. | `buzz/crates/buzz-relay-mesh/src/registry.rs:1-6`; `buzz/crates/buzz-relay/src/tunnel/directory.rs:1-32`; `buzz/crates/buzz-relay/src/main.rs:459-470` |
| R4-05 | 정적 제약 해소: Iroh relay 비활성, 직접 IP, 부팅별 endpoint 키, 배포 키 attestation 및 admission. NAT 너머 성공은 미확인. | `buzz/crates/buzz-relay-mesh/src/endpoint.rs:18-39,59-69,104-108`; `registry.rs:24-49,83-110`; `membership.rs:78-115` |
| R4-08 | 표면·인증 해소: `--remote`는 TUI의 app-server 접속이고 `remote-control`은 foreground/daemon 및 클라우드 인증 경로. 기존 thread queue는 서버에서 처리 가능하나 임의 프로필의 live TUI 도달 보장은 별개. | `codex/codex-rs/cli/src/main.rs:2820-2852`; `cli/src/remote_control_cmd.rs:52-94,110-143`; `app-server/src/request_processors/thread_queue_processor.rs:72-89` |
| R4-09 | 부분 해소: 내부 bridge 프로토콜과 인증은 확인. 공개 Remote Control 기능과 외부 events API 지원 계약은 구분해야 함. 교차 제공자 어댑터가 불가능하다는 단정도 근거 없음. | `bunfs/chunk-gck8q9zt.js:11`, `ce`; `bunfs/chunk-t9pet8cw.js:11`, `KFe`; [공식 Remote Control 문서](https://code.claude.com/docs/en/remote-control) |
| R4-10 | 위험 분석·S5 설계 완료, 실행 검증 미완료. SSH 포워딩만으로 원래 PID·uid·회신 주소·머신 격리를 보존한다는 전제를 채택하지 않는다. | `bunfs/chunk-6kcckmy2.js:11-12`, `ezt/Pe/Jht`; `bunfs/chunk-z0hsnf69.js:11`, `tn/ke`; `bunfs/chunk-8vtc32rs.js:53`, `isolatePeerMachines` |

## 조사 범위와 원본 표기

- 조사일: 2026-09-19. `orca/`, `buzz/`, `codex/`는 `/tmp/xsm-refs/` 아래 경로다. 같은 디렉터리의 파일명이 축약된 표 항목은 바로 앞 경로의 디렉터리를 따른다.
- 읽은 체크아웃: Orca `1ef947394b9323bdc7c87f9c175b49c77f0a45bc`, Buzz `4e65148e76bd4f8dff757da4014a37fbc95fcc12`; Codex는 `git describe --tags --always` 결과 `rust-v0.155.1`. 실행한 버전 조회는 `command codex --version` → `codex-cli 0.155.1`, `claude --version` → `2.1.278 (Claude Code)`였다.
- `bunfs/`는 `/private/tmp/claude-501/-Users-jaesolshin-Documents-GitHub-cross-session-messaging/90eeb6cf-29e9-4a57-8a88-88d76f1eb08d/scratchpad/bunfs/`다. 인용은 추출 원본의 물리적 줄과 함수명이다. minify 원본에는 긴 줄과 문자열 내 줄바꿈이 섞여 있어 모두 11행으로 일반화하지 않았다.
- 정적 소스·버전·공개 문서만 확인했다. 서버/daemon/relay를 시작하거나 pairing code를 발급하지 않았고, 기존 세션·소켓에 실험 메시지를 보내지 않았다. Orca의 작업 heartbeat와 완료 보고만 배정된 경로를 사용했다. 테스트 소스를 읽은 경우에도 실행 PASS로 표시하지 않는다.

## 1. Orca federation

### 호출 경로와 인증

`worker-start --on`의 원격 분기는 원격 worktree를 정확히 지정하도록 요구하고 `current/new-child`를 거절한다. 저장 environment를 해석해 pairing revision을 잡은 뒤 `status.get`으로 기능을 확인하고, dispatch에 environment ID와 서버 공개키 지문을 저장한다. 이어 `orchestration.federationAttachStart`에 같은 revision fence와 durable request ID를 전달한다. 따라서 이 경로는 기존 외부 Claude/Codex 세션을 CONFIG_DIR에 무관하게 발견하는 API가 아니라 Orca 원격 worker 배치 경로다. [원본: `orca/src/main/runtime/rpc/methods/orchestration/federation/federated-worker-start.ts:62-98,119-154,159-195`]

실제 연결까지의 경로는 다음과 같다.

1. `initializeMainProcessRuntime`의 environment transport가 저장 pairing의 공개키로 fingerprint를 계산하고 `callRuntimeEnvironment`에 위임한다. fingerprint는 공개키 바이트의 SHA-256/base64url이다. [원본: `orca/src/main/startup/main-process-runtime-service.ts:51-71`; `orca/src/main/runtime/orchestration/environment-transport.ts:32-34`]
2. `callRuntimeEnvironment`는 큐에서 꺼내 실행할 때 environment를 다시 읽고 revision을 검사한다. orchestration envelope가 공유 제어 경로에 적합하지 않으면 one-shot, 캐시 대상이면 request connection, 지원 협상이 필요한 메서드는 support routing, 나머지는 one-shot으로 보낸다. “모두 하나의 공유 연결”도 “SSH로만 전송”도 정확하지 않다. [원본: `orca/src/main/ipc/runtime-environment-transport-routing.ts:65-157`]
3. one-shot은 `e2ee_auth`에 deviceToken을 넣고 클라이언트 키쌍과 저장된 서버 공개키로 공유키를 유도한다. response router는 암호화한 인증을 전송한다. 재사용 연결도 같은 공개키 기반 WebSocket을 열고 암호화한 `e2ee_auth`를 보낸다. endpoint는 pairing에 든 URL이다. [원본: `orca/src/shared/remote-runtime-request-socket.ts:55-83`; `remote-runtime-request-response-router.ts:53-89`; `remote-runtime-request-connection.ts:224-243`; `remote-runtime-request-websocket.ts:35-56,109-133`]
4. 서버는 연결에 묶인 인증 토큰과 요청 토큰의 불일치를 거절하고, registry에서 토큰을 다시 검증한다. mobile scope는 RPC allowlist 제한이 있다. 즉 암호화, 기기 인증, 메서드 인가는 별도 검사다. [원본: `orca/src/main/runtime/runtime-rpc/runtime-rpc-websocket-dispatch.ts:57-85`]

### 신뢰 변경과 기존 메시지

| 사건 | 코드에서 확인한 처리 | 해석의 한계 |
|---|---|---|
| environment 이름은 같지만 서버 공개키 교체 | dispatch의 `peer_fingerprint`와 현재 지문 불일치 시 `peer_changed`; pull 이전에 종료 | 새 서버로 기존 메시지를 자동 이관하는 근거가 아니다. 키를 복제한 서버는 지문만으로 구별할 수 없다는 것은 설계상 추론이다. |
| RPC 대기 중 pairing revision 변경 | 실행 직전 검사에서 `runtime_environment_changed` 반환 | 해당 호출의 재해석/재시도는 상위 호출자 책임이며, 성공으로 취급하지 않는다. |
| 같은 서버 키로 재페어링 | 지문 검사는 통과할 수 있고 새 sync는 현재 revision을 잡는다 | 영구적으로 dispatch를 폐기하는 규칙이 아니다. 새 토큰 권한과 원격 dispatch의 존재가 여전히 필요하다. |
| runtime 기기 권한 철회 | registry에서 device 삭제, 해당 token의 연결 종료; 후속 RPC token validation 실패 | 원격 프로세스 종료·기실행 효과 롤백·저장 relay 삭제까지 한다는 증거는 이 철회 함수에 없다. |
| 연결 단절 후 정상 서버 복귀 | 연속 sequence 검증, 중복 import 판정, ack checkpoint 및 미확인 항목 replay 경로가 존재 | 네트워크 전송 성공과 worker 처리 완료를 동일시할 수 없다. |

근거: `orca/src/main/runtime/orchestration/federation-sync.ts:71-103,116-177,180-247`; `orca/src/main/ipc/runtime-environment-revision-guard.ts:4-28`; `orca/src/main/runtime/runtime-rpc/runtime-rpc-pairing.ts:110-117`; `orca/src/main/runtime/runtime-rpc/runtime-rpc-websocket-dispatch.ts:65-74`. 서버 변경 시 show/stop도 `peer_changed`를 기대하는 테스트가 있다(`orca/src/main/runtime/rpc/methods/orchestration/federation/federation.test.ts:712-713`); 이번에는 실행하지 않았다.

**ADR 반영 제안:** 서버 교체는 자동 재전송 대신 보류·명시적 재승인, 연결 실패는 같은 신뢰 주체에만 재시도, 철회 후 이미 실행된 명령은 별도 취소/보상 상태로 기록한다. 이는 위 코드에서 얻은 설계 제안이며 현재 프로젝트의 확정 정책이 아니다.

## 2. Buzz relay mesh

`buzz-relay-mesh`의 endpoint·registry·membership·runtime은 실제 구현이다. `MeshEndpoint::bind`는 매 부팅 새 ed25519 키를 생성하며 그 공개키가 RuntimeId다. `Minimal` preset에서 `RelayMode::Disabled`로 bind하고, advertise와 `direct_addr`는 IP socket 주소를 사용한다. [원본: `buzz/crates/buzz-relay-mesh/src/endpoint.rs:18-39,59-69,96-108`]

deployment의 Nostr/secp256k1 키는 boot RuntimeId를 Schnorr 서명한다. 서명 preimage는 버전 context·runtime 공개키·relay 공개키이며 endpoint 주소 전체를 서명하는 구조는 아니다. ReadyRecord는 별도로 주소·protocol version·capabilities를 담는다. membership은 **기대하는 배포 relay 공개키 일치와 서명 검증 둘 다** 요구하며, anchor가 없으면 거절한다. 따라서 “아무 Nostr 키의 유효한 서명”은 mesh 가입 권한이 아니다. 수신 연결의 RuntimeId도 membership에 있어야 하며 없으면 registry를 한 번 재조회한다. [원본: `buzz/crates/buzz-relay-mesh/src/registry.rs:24-49,83-110`; `membership.rs:35-41,78-115`; `runtime.rs:307-319`]

Redis ready registry는 `mesh:ready:{runtime_id}`를 refresh하고 TTL로 crash 흔적을 지운다. 기본 refresh 15초, TTL 배수 3이며 정상 종료는 DEL이다. 이 값은 피어 발견 힌트이고 세션 소유권이 아니다. fenced directory가 `{session_id,generation,owner_runtime_id}`를 판정하며 Lua acquire는 비만료 generation counter를 증가시키고 lease에 TTL을 준다. 오래된 runtime의 frame을 멤버십 생존 추정만으로 정당화할 수 없다. [원본: `buzz/crates/buzz-relay-mesh/src/registry.rs:19-22,154-204`; `buzz/crates/buzz-relay/src/tunnel/directory.rs:1-32,100-103`]

일반 채널 이벤트의 다중 노드 fan-out은 Redis pub/sub 구독에서 로컬 WebSocket 구독자로 전달한다. mesh 부팅은 같은 Redis pool과 DB를 session directory에 전달한다. 따라서 “각 relay의 독립 Postgres/Redis를 Iroh가 범용 연결”이라는 설명은 이 코드의 배치 모델을 표현하지 못한다. 다만 이 사실만으로 모든 물리 DB 토폴로지나 복제 방식을 확정하지 않는다. [원본: `buzz/crates/buzz-relay/src/main.rs:459-470,973-982`; `buzz/crates/buzz-relay/src/mesh_boot.rs:512`]

**NAT 판단:** 직접 UDP/IP로 도달 가능한 LAN·라우팅된 사설망·적절한 포트 매핑은 검증 후보지만, 직접 경로가 막힌 두 NAT 사이를 Iroh relay가 구제한다고 말할 수 없다. 여기서는 relay가 비활성이고 직접 주소만 사용하기 때문이다. 모든 NAT에서 실패한다고 단정할 근거도 없으며 주소 공개·방화벽·UDP 정책별 실험이 필요하다. [정적 근거: `endpoint.rs:31-35,59-69,104-108`; 도달성은 추론, 미실측]

## 3. Codex remote와 Remote Control

| 표면 | 확인한 역할·인증 | 본 과제와의 관계 |
|---|---|---|
| `codex --remote ws://…`, `wss://…`, `unix://…` | TUI가 지정 app-server로 JSON-RPC/WebSocket 연결. Unix socket도 WebSocket frame을 쓰지만 그 자체는 로컬 주소다. | 원격 TUI 접속과 임의 기존 TUI 발견은 다르다. |
| `--remote-auth-token-env NAME` | 환경변수에서 토큰을 읽는다. `--remote` 필수, `wss://` 또는 loopback `ws://`만 허용하고 `unix://`에는 적용하지 않는다. | app-server 접속 토큰을 Claude OAuth나 원격 계정 pairing code와 혼동하지 않는다. |
| `remote-control start` | remote control을 활성화한 app-server daemon 준비 | 기존 daemon의 상태/프로필 범위 확인이 필요하다. |
| `remote-control stop` | app-server daemon Stop 호출 | remote control 플래그만 끄는 명령으로 설명하면 부정확하다. |
| `remote-control pair` | 수명이 제한된 수동 pairing code 요청·출력 | code 길이·실제 만료 시간은 서버 응답 확인 없이 수치화하지 않는다. |
| 인자 없는 `remote-control` | 임시 디렉터리 `rc.sock`의 foreground app-server, `EnabledEphemeral` | 이미 켜진 임의 TUI 입력에 붙는 동작이라고 해석할 수 없다. |

근거: `codex/codex-rs/app-server-client/src/remote.rs:1-10,67-79,117-124`; `codex/codex-rs/cli/src/main.rs:2820-2852`; `codex/codex-rs/cli/src/remote_control_cmd.rs:52-94,110-143`; `codex/codex-rs/app-server-protocol/src/protocol/v2/remote_control.rs:79-82`.

Remote Control 클라우드 인증 로더는 ChatGPT 인증을 요구하고 API key 인증을 거절하며 account ID도 필요로 한다. 인증 소유자를 고정하고 credential 로딩 전후 현재 owner인지 확인한다. 이는 임의 ws 서버의 bearer token 설정과 다른 계층이다. [원본: `codex/codex-rs/app-server-transport/src/transport/remote_control/auth.rs:1-2,80-129`]

기존 thread에 대한 `thread/queue/add`는 app-server message processor에 라우팅되어 thread 존재·직접 입력 허용 여부를 확인하고 queue service에 enqueue한다. 따라서 원격 클라이언트가 해당 메서드까지 접근하고 그 서버의 thread/store 범위에 대상이 있다면 queue 요청을 수행할 **코드 경로**는 있다. 별도 CONFIG_DIR의 임의 TUI까지 자동 발견·wakeup되었다는 실행 증거는 아니며, 인증 뒤 메서드 노출과 서버 측 thread/store 범위도 조건이다. queue watcher·HOME 경계·Interrupted·단일 writer 검토와 실험은 T1 보강 조사에 교차 참조한다. [원본: `codex/codex-rs/app-server/src/message_processor.rs:1354`; `codex/codex-rs/app-server/src/request_processors/thread_queue_processor.rs:72-89`]

## 4. Claude bridge의 외부 어댑터 가능성과 C2

`postInterClaudeMessage`로 export되는 `ce`는 `prepareApiRequest`로 access token과 org UUID를 얻고 대상 ID를 `session_[A-Za-z0-9_-]+`로 검사한다. cross-session 본문을 만든 후 user event·UUID·session ID를 구성하고 OAuth header, beta header, organization header를 붙여 전송한다. trusted-device token이 있으면 추가하고, `untrusted_device` 403에서 기기 토큰 갱신 후 재시도한다. stale-login 오류 및 cloud credential의 타 세션 전송 거절도 별도 처리한다. 200/201/204 성공은 함수의 전송 성공이지 대상 모델의 처리 완료 확인이 아니다. [원본: `bunfs/chunk-gck8q9zt.js:11`, `ce/O/N`]

`KFe`는 분기에 따라 `/v1/sessions/{id}/events`의 `{events:[…]}` 또는 `/v1/code/sessions/{encoded-id}/events`의 payload-wrapped event를 만든다. 후자는 UUID가 없으면 생성한다. 외부 구현은 URL만 복사하면 되는 것이 아니라 버전 분기·봉투·인증·기기 신뢰·회신 식별을 다뤄야 한다. [원본: `bunfs/chunk-t9pet8cw.js:11`, `KFe`]

공식 문서는 Remote Control을 웹/모바일에서 로컬 세션을 조작하는 기능으로 설명하고 claude.ai 로그인과 API key 비지원을 명시한다. 이번에 조회한 문서에서 위 내부 events API를 외부 어댑터가 안정적으로 재사용하도록 보장한 계약은 확인하지 못했다. “공식 Remote Control 기능이 존재”와 “추출한 API의 외부 사용이 공식 지원”은 별개다. [공식 문서 Requirements 및 연결 절](https://code.claude.com/docs/en/remote-control), 2026-09-19 Jina Reader 조회; 내부 구현은 위 두 chunk.

**C2 평가:** 별도 진입점 런타임 없이 기존 Claude/Codex를 쓰라는 요구(`INTENT.md`의 codex 확장 절)에 대해, native Remote Control을 활성화한 기존 세션은 검토 후보다. 외부 어댑터가 인증된 API 사이를 연결한다는 설계 자체는 논리적으로 가능하지만, 현재 코드가 그 어댑터를 제공하거나 지원 계약을 보장한다는 뜻은 아니다. 특히 내부 `ce`는 발신자를 Claude cross-session 형태로 포장하므로 Codex-origin을 정직하게 표시하는 규약도 따로 필요하다. API 자격증명 제공 방식·계정 간 접근·기기 enrollment·발신자 위조 방지가 해결되기 전에는 C2 충족 확정도, Claude↔Codex 교차 통신 불가 확정도 하지 않는다. [요구: `INTENT.md`; 구현 근거: `bunfs/chunk-gck8q9zt.js:11`, `ce`]

## 5. SSH 소켓 포워딩과 S5

### 무엇을 보존하지 못할 수 있는가

Claude의 `ezt`는 socket fd에서 `Bun.ant.getPeerPid`를 읽는다. inbox 수신 함수 `tn`은 연결의 PID와 process-start token을 잡아 메시지 origin에 전달한다. 이는 원격 payload가 주장한 PID가 아니라 로컬 kernel peer에 관한 증거다. `Pe`는 `expectPeerPid`가 제공된 비-Windows 경로에서 실제 PID·같은 uid·선택적 process-start token을 비교하고 불일치 시 쓰기를 거부한다. 일반적인 모든 inbox 요청에 동일 uid 검사가 무조건 실행된다고 확대 해석하면 안 된다. daemon 제어용 `P3r`와 메시지 inbox 수신도 구분해야 한다. [원본: `bunfs/chunk-6kcckmy2.js:11-12`, `ezt/P3r/Pe`; `bunfs/chunk-z0hsnf69.js:11`, `tn/ke`; `bunfs/chunk-3gwwfgas.js:44`, daemon `P3r` 호출]

SSH stream forwarding이 원격 발신 세션의 OS peer credentials를 보존해 주리라는 근거는 없다. 로컬에서 소켓을 여는 SSH 측 프로세스가 관측될 것으로 예상되지만, 구체적인 PID·uid·start-token은 운영체제별 S5 관측값으로 남겨야 한다. 예상값을 맞추기 위해 검증을 끄거나 원격 PID를 로컬 PID처럼 등록하는 것은 신원 보존의 증명이 아니다. [추론 근거: 위 kernel-peer 검사 코드; SSH 실험 미실행]

`Jht`는 자신의 메시징 소켓을 `from`에 넣으며 `Pe`는 사용 가능한 로컬 IPC 경로인지 검사한다. 따라서 A의 `uds:/tmp/…`는 B에서 자동으로 A를 가리키지 않는다. 왕복 포워딩·주소 매핑을 추가하더라도 B의 동명 경로나 다른 세션에 답장이 들어가지 않는지 검증해야 한다. `isolatePeerMachines`는 SendMessage permission 단계에서 bridge/해석된 cloud destination에 명시적 승인을 요구하는 분기를 갖는다. 원격을 단순 UDS처럼 보이게 만드는 어댑터는 그 분류를 유지하지 못할 위험이 있다. 이는 우회 성공 실측이 아니라 검증해야 할 위험이다. [원본: `bunfs/chunk-6kcckmy2.js:11-12`, `Jht/Pe`; `bunfs/chunk-8vtc32rs.js:53`, `isolatePeerMachines` 분기]

### S5 제안 — 실행하지 않은 절차

목적은 “바이트가 도착했다”가 아니라 **같은 기존 수신 세션의 wakeup·올바른 회신·신뢰 변경 차단**을 각각 판정하는 것이다. 코디네이터가 별도로 승인한 격리 실험 머신/계정과 폐기 가능한 세션만 사용하며 현재 사용자 설정이나 운영 세션은 대상으로 삼지 않는다. 아래는 시험 설계이며 제품 동작 주장이 아니다.

| 단계 | 절차 | 기록·합격 기준 |
|---|---|---|
| S5-0 기준선 | 두 머신 A/B, 별도 임시 Claude/Codex HOME, 동일/상이 uid, 세션 ID·프로필·버전·PID/start token 고정. 우선 각 머신 안에서 전용 테스트 세션 간 정상 왕복을 확인한다. | 메시지 ID, 대상 ID, 수신 상태, 승인 상태, 모델 반응 시작, 회신 수신을 각각 기록. 자기 소켓 selfSent 실험을 기준선으로 대체하지 않는다. |
| S5-1 전송 관측 | 먼저 소켓 credential만 기록하는 테스트 수신기로 단방향 SSH UDS forwarding을 구성한다. A/B의 실제 endpoint peer PID·uid·start token을 수집한 후 전용 Claude 세션으로 같은 경로를 비교한다. | 원래 발신자와 SSH endpoint 신원을 분리. 정상 로컬 검사보다 약화시키지 않고 수신 가능한지 판정. 거부 오류도 결과로 보존. |
| S5-2 왕복 주소 | A/B의 소켓 basename을 일부러 같게 한 경우와 다르게 한 경우를 비교. 원본 `from=uds:` 유지, 양방향 forwarding, 명시적 주소 매핑을 각각 분리한다. | 회신이 정확히 A의 원래 세션으로 돌아와야 함. B의 동명 소켓, 재시작된 세션, 제3 세션으로의 전달은 실패. |
| S5-3 권한·머신 경계 | 실험 설정에서 isolatePeerMachines on/off 및 허용 모드 조합, bridge 대조군과 UDS forwarding군을 비교한다. 승인 거절도 실행한다. | 원격임을 보존하고 필요한 승인을 요구. 원격을 로컬로 오인해 정책을 약화시키면 실패. PID 검사 제거·권한 모드 허위 표기로 통과시키지 않는다. |
| S5-4 신뢰 수명 | 전용 Orca fixture에서 같은 키 재페어링, 다른 키 서버 교체, 대기 중 revision 변경, device revoke를 순서대로 시험. 미확인 메시지와 이미 실행된 메시지를 따로 둔다. | 다른 서버로 자동 재송신 없음, revision 오류 보존, 철회 후 신규 전달 차단, 기존 효과·취소 실패를 별도 기록. ack/중복·순서도 확인. |
| S5-5 NAT·Buzz | 동일 LAN, 라우팅 사설망, NAT 양쪽 비공개·직접 UDP 차단을 나눠 동일 mesh 설정으로 시도. 예상 relay key 불일치와 stale generation도 넣는다. | 연결 실패를 정상 음성 결과로 기록. ready 발견 성공을 데이터 경로 성공으로 세지 않음. 잘못된 키·generation은 거부. |
| S5-6 Codex 원격 큐 | T1 절차에 원격 app-server 연결만 추가. 대상 HOME의 idle/busy/Interrupted와 다른 HOME을 구분한다. 새 resume writer를 만들어 성공을 대신하지 않는다. | 큐 저장·수신 watcher·동일 thread 실제 turn 시작을 분리. 기존 live 세션 ID/프로세스가 유지되는지 기록. |
| S5-7 종료 | 전용 포워딩·테스트 프로세스만 종료, 임시 credential·socket 폐기, 기준선 환경과 비교한다. | 운영 설정·세션 무변경 확인, 성공·실패 원시 로그와 버전/조건 보존. |

설계 근거는 본 절의 Claude 피어·주소·격리 검사, 1절의 Orca trust fence, 2절의 Buzz direct-IP/Redis fence, 3절의 Codex queue 경로다. S5 하나의 PASS로 공식 외부 API 지원이나 CONFIG_DIR 자동 발견까지 인정하지 않는다. 발견, 전송, 입장 허가, wakeup, 회신은 각각 결과를 남긴다.

## 확인 못 한 것

- 실제 원격 연결·NAT 통과·SSH peer credentials·양방향 회신·isolatePeerMachines 보존: 실험하지 않았다.
- Orca 권한 철회와 정확히 동시에 진행 중이던 명령의 모든 취소 경로, relay 기록의 장기 보존/청소 규약, 키를 유지한 서버 복제의 운영 방지: 전체 경로를 입증하지 않았다. 확인한 철회 함수만으로 rollback이나 메시지 삭제를 주장하지 않는다.
- Buzz 실제 운영 Redis/Postgres 배치, 모든 gossip admission 경로의 보안 감사, 키 유출 후 전체 mesh의 기기별 즉시 철회: 확인 못 함. 확인한 것은 ready admission·수신 membership 검사·fenced directory다.
- Codex pairing code의 실제 길이·만료 초, 계정별 서비스 활성화, remote-control 채널별 queue 노출 제한, 서로 다른 CODEX_HOME의 live TUI까지 도달하는 결과: 확인 못 함. T1의 세션/큐 조사를 함께 읽어야 한다.
- Claude 내부 events API의 외부 어댑터용 공식 지원 계약, 서버 측 계정 간 권한 및 cloud credential 조건의 전체 규칙: 확인 못 함. 공개 문서와 내부 클라이언트만 읽었다.
- S5는 제안이며 미실행이다. 이 문서는 ADR-0007을 결정하거나 다른 보고서를 수정하지 않는다.
