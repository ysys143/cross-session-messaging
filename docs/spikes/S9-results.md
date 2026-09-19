# 스파이크 S9 실험 결과: agent-comms·cc-peer 방식 실측

- 실행일: 2026-09-20 03:44~04:29 (KST)
- 제안: 피어 세션 `cross-session-agent-comms`의 보고(E1~E6), 조사 문서 `docs/references/agent-comms.md`
- 관련 ADR: 0001, 0002, 0003, 0004, 0007, 0008, 0009
- 환경: macOS, Claude Code 2.1.278, Codex CLI 0.155.1, node v20.20.1
- 대상:
  - npm `agent-comms@4.1.2`와 그 의존성 `cc-peer@1.5.0`, `wire-mesh-core@1.58.4`
  - 설치 위치는 `/tmp/xsm-spike/s9/pkg`
  - 소스 대조는 `/tmp/xsm-refs/agent-comms`(a68c00f), `/tmp/xsm-refs/cc-peer`(74fd76a)로 했다.
- 드라이버(실험용): `tools/s9/`

| 파일 | 역할 |
|---|---|
| `e2e3.mjs` | 배포본 `cc-peer`를 default front와 같은 방식(`CcPeer.create({name})`, `fromMode` 미지정 `send`)으로 호출한다 |
| `mesh.mjs` | 배포본 core API로 메시에 합류해 목록 조회와 DM을 수행한다 |
| `node.mjs` | 장시간 실행 메시 노드. 수신 이벤트를 기록하고 합류 요청을 자동 수락하며, `cmd.jsonl`로 명령을 받는다(E6) |
| `e1-mcp.json`, `e1-settings.json` | E1 수신 세션의 bridge MCP 설정과 플러그인 훅(`hooks/hooks.json`)을 옮긴 설정 |

## 격리 방법

- **임시 HOME**
  - agent-comms와 cc-peer는 모든 경로를 `os.homedir()`에서 얻는다(`~/.agent-comms`, `~/.agents/bus/pending`, `~/.claude/sessions`).
  - 그래서 모든 agent-comms 프로세스에 `HOME=/tmp/xsm-spike/s9/home`을 줬다.
    - Claude 쪽: MCP 설정의 `env`와 훅 명령 앞의 `HOME=…`
    - Codex 쪽: `-c mcp_servers.agent-comms.env`
  - Claude·Codex 본체는 실제 HOME으로 실행해 인증을 유지했다.
- **로스터 격리**
  - `~/.claude-4/sessions`에는 사용자의 실제 세션(hermes 등)이 살아 있었다.
  - 그래서 세션 디렉터리를 심볼릭 링크로 노출하지 않았다. 전용 테스트 세션의 `<pid>.json`과 `<pid>.<hash>.key`만 임시 HOME의 `.claude/sessions`에 복사했다.
- **실행 금지**
  - `agent-comms`를 인자 없이 실행하면 `setup`이 돌아 하니스 설정 파일을 수정한다(`src/cli.ts:187`).
  - 그래서 항상 하위 명령을 명시했다.
- **포트**
  - 코디네이터 포트 19876은 설정할 수 없다(`DEFAULT_COORDINATOR_PORT`, `src/core/mesh-store.ts:82`).
  - 실험 전에 이 포트를 쓰는 프로세스가 없음을 확인했다.
- **격리하지 못한 것: 외부 허브**
  - E1~E4·E6에서는 코디네이터가 기본값으로 `wss://mesh.exadev.io/`에 접속한다는 사실을 몰랐고, 허브를 끄지 않았다. E5 준비 중에 발견했다(E5 절). E5는 허브를 끄고 실행했다.

## 계획과 달라진 점

| 항목 | 내용 |
|---|---|
| bypass 수신 세션 | `--dangerously-skip-permissions` 실행을 auto mode 분류기가 거부했다(Create Unsafe Agents). 우회하지 않고 `--permission-mode bypassPermissions --tools ''`(bypass 모드, 도구 없음)로 띄웠다. 수신 판정에 쓰이는 권한 모드 부류는 같다 |
| prompting 수신 세션 | 기본 모드가 auto였다. auto는 bypass가 아니므로 prompting 부류로 판정된다(아래 E2 결과로 확인) |
| E1 대조군(개발 채널) | `--dangerously-load-development-channels server:agent-comms` 실행 시 "인터넷에서 받은 채널에 쓰지 말라"는 경고와 "I am using this for local development" 확인이 나왔다. npm에서 받은 패키지이므로 확인하지 않고 종료했다. **미실행** |
| E4c(`codex queue` 결합) | 두 Codex 홈 모두 주간 한도가 5% 미만이어서 턴이 필요한 실험을 하지 않았다. **미실행**(코드 근거만) |
| E5(원격) | 사용자가 "tailnet 한정"을 선택해 진행했다(04:25~04:29). 기본 허브 연결을 끄고(`hubUrl`을 닫힌 로컬 포트로), listener를 Tailscale 주소에만 열었다. `gateway_trust`는 허브 중계용이라 이 범위에서 제외했다 |

## 결과 요약

| ID | 질문 | 결과 |
|---|---|---|
| E1 | bridge를 설치한 Claude가 개발 채널 없이 훅만으로 완전 유휴 상태에서 깨어나는가 | **깨어나지 않는다.** 이벤트는 pending 파일에 쌓이고, 사람의 프롬프트 등 다른 활동이 턴을 열어야 Stop·PostToolUse 훅이 전달한다. 첫 DM은 동의 절차 때문에 228.7초 동안 막혀 있었다 |
| E2 | cc-peer 기본 `fromMode="bypass"`의 판정 | **예측과 같다.** bypass 수신은 자동 수락해 턴을 실행했고, prompting 수신은 "permission mode class doesn't match"로 보류했다. 발신자는 Claude 세션이 아닌 node 프로세스였다 |
| E3 | 비기본 프로필 세션이 로스터에서 빠지는가 | **빠진다.** 로스터와 전송 키 조회 모두 `$HOME/.claude/sessions`만 본다. pid를 직접 지정해도 `NoLiveInboxError`가 났다. 레코드를 그 경로에 복사하면 보이고, default front가 자동으로 메시에 올린다 |
| E4 | `~/.codex`·`~/.codex-2` bridge가 같은 메시에 들어오는가, 유휴 Codex 경로 | **같은 메시에 들어온다.** 같은 폴더의 두 번째 세션은 신원 슬롯이 충돌해 임시 ID를 받는다. 유휴 Codex는 DM을 받아도 45초 동안 반응이 없었고 첫 접촉 동의에 응답할 수 없다. bridge에 `codex queue` 경로는 없다 |
| E5 | 원격 `mesh_listen` 왕복 | **동작한다(tailnet 한정).** macmini listener(`100.77.24.16:19877`)에 `mesh_connect` → 연결 요청 → macmini에서 `mesh_accept` → DM 왕복(약 70ms, 140ms)과 read 확인. 연결 요청의 `fingerprint`는 빈 문자열이었다. **별도 발견: 코디네이터는 기본으로 제3자 허브 `wss://mesh.exadev.io/`에 접속한다** |
| E6 | 코디네이터 kill -9 복구, 큐 재생, 전체 다운 시 기록 | **크래시 복구가 없다.** kill -9 후 10초, 30초가 지나도 아무도 포트를 다시 열지 않았다. 새 노드가 별도 메시를 만들어 분할됐다. 정상 종료 인계는 약 192ms. 수신자 크래시 중 보낸 메시지는 재시작 후에도 재생되지 않았다. 전체 다운 후 room과 기록이 사라졌다 |

## E3: 로스터가 `~/.claude/sessions` 고정인가

배포본 확인:
- `sessionsDir()`는 `join(config.homeDir ?? homedir(), ".claude", "sessions")`이다(`cc-peer/dist/cc-peer-BduoSupO.mjs:175-177`).
- 로스터(`FsRegistryStore.list`, `:313-330`)와 전송 키 조회(`keyFilePath`, `:201-204`)가 모두 이 경로를 쓴다.
- `CLAUDE_CONFIG_DIR`은 읽지 않는다.

| 절차 | 결과 |
|---|---|
| 임시 HOME의 `.claude/sessions`가 빈 상태에서 `roster()` | `[]`. `~/.claude-4`의 `s9-rb`(99625), `s9-rp`(99243)가 보이지 않는다 |
| 같은 상태에서 pid 99625로 직접 `send` | `NoLiveInboxError: no auth key published for /tmp/cc-socks/99625.sock`. 소켓은 살아 있지만 키를 `$HOME/.claude/sessions`에서만 찾는다 |
| 두 세션의 `.json`과 `.key`를 임시 HOME에 복사한 뒤 `roster()` | 두 세션이 모두 보인다. 로스터는 경로 한 곳에만 의존한다 |
| 이후 E1 bridge가 코디네이터가 되었을 때 | default front가 복사된 두 레코드를 자동으로 붙잡았다. `identity-claude-code--…_rb.json`, `…_rp.json`을 만들고 메시에 `s9-rb`, `s9-rp`로 올렸다(`mesh.mjs list` 결과) |

해석:
- `~/.claude-3`, `~/.claude-4` 세션은 default front에 보이지 않고, 자기 bridge를 설치해야 메시에 들어온다. 피어 보고와 같다.
- default front는 `~/.claude/sessions`에 있는 세션을 **그 세션의 동의 없이** 메시에 올린다. 이 메시가 원격 gateway와 연결돼 있다면 노출 범위가 넓어진다(ADR-0004).

## E2: 기본 `fromMode="bypass"`의 판정

배포본 확인:
- `send()`는 `options.fromMode === void 0`이면 `fromMode: "bypass"`를 넣는다(`cc-peer-BduoSupO.mjs:856`).
- default front는 `peer.send({ pid }, body)`로 호출한다(`src/bridges/cc-peer/front-relay.ts:80,86,92`).

| 수신 세션 | 권한 모드 | 결과 |
|---|---|---|
| `s9-rb` | bypass(도구 없음) | **수락.** "Message from @s9-ccpeer-front: [S9-E2 probe=E2-bypass-rcv] …"를 받고 곧바로 턴을 실행해 `ack E2-bypass-rcv`로 답했다 |
| `s9-rp` | auto | **보류.** "Held peer message — from uds:/tmp/cc-socks/2085.sock [verified pid 2085] (peer claims name: s9-ccpeer-front) … The sending session's permission mode class doesn't match this session's." 거부로 정리했다 |

- 두 사용자 설정을 모두 뺀 상태(`--setting-sources project,local`)이므로 `crossSessionInbound`는 없다.
- 발신자는 Claude 세션이 아닌 일반 node 프로세스(pid 2085)였다. 그런데도 `from-mode=bypass`라는 자기 주장만으로 bypass 수신 세션에서 턴이 실행됐다.
- Claude가 표시하는 "verified"는 pid뿐이고, 이름은 "peer claims"로 구분된다.
- S1 판정 표와 같다.

## E1: 훅만으로 유휴 Claude를 깨우는가

구성:
- 수신 세션 `s9-rh`(auto 모드, `~/.claude-4`)에 bridge를 `--mcp-config`로 붙였다.
- 플러그인 훅 세 개를 `--settings`로 옮겼다: `PostToolUse`, `Stop`, `UserPromptSubmit`(`asyncRewake: true`). 모두 `drain.sh`를 실행한다.
- bridge는 첫 노드라 코디네이터가 됐다.
- 조상 프로세스의 `comm`이 `/Users/jaesolshin/.local/bin/claude`여서 Claude pid(5954)를 정상적으로 찾았다. pending 파일은 `claude-code--_private_tmp_xsm_spike_s9_rh--5954.jsonl`이다.

| 시각 | 사건 | 관찰 |
|---|---|---|
| 03:52 | 드라이버가 DM E1a를 보냄 | DM은 첫 접촉 동의부터 요청한다. pending에 "… is asking to join …: room_accept or room_reject to answer"가 기록됐다. 발신 호출은 응답을 기다리며 막혔다 |
| 03:52~03:55 | 유휴 | 세션 화면 변화 없음. 약 3분 동안 깨어나지 않았다 |
| 03:55 | 사람이 "hi, just say hello" 입력 | "Hello!" 턴 뒤 **Stop hook feedback**으로 pending이 전달됐다. 에이전트는 수락하지 않고 사람에게 수락 여부를 물었다 |
| 03:56 | 실험 운영자가 "accept it" 입력 | 에이전트가 `room_accept`를 호출했다(auto 모드에서 허용). 그 턴 안에서 PostToolUse 훅("hook returned blocking error")으로 E1a, E1b가 전달됐다. 발신 호출은 **228.7초** 만에 반환됐다. 답장은 드라이버가 이미 종료돼 실패했다(`JOIN_REFUSED`, 상대 쪽 동의가 없음) |
| 03:56:39 | 동의가 성립한 상태에서 E1c(기본), E1d(`steer`) 전송 | 60초 동안 깨어나지 않았다. pending에 두 줄이 남았다(`[STEER] DM from …`) |

해석:
- 플러그인 훅은 PostToolUse·Stop·UserPromptSubmit에만 걸려 있고, 세 이벤트 모두 턴이 있어야 발생한다. 완전 유휴 세션에는 훅이 실행될 계기가 없다.
- `asyncRewake`도 UserPromptSubmit에 붙어 있어서 사람의 입력이 먼저 필요하다.
- 유휴 깨우기는 채널 푸시(`notifications/claude/channel`)에 의존한다. 그런데 채널은 `--dangerously-load-development-channels`가 필요하고, 이 옵션은 내려받은 채널에 쓰지 말라는 경고가 붙는다. 이 경로는 확인하지 않았다.
- 대조: default front(bridge 없는 Claude)는 Claude 네이티브 inbox를 쓰므로 유휴 세션도 깨운다(E2의 `s9-rb`). 다만 E2의 모드 판정을 그대로 탄다.

## E4: Codex bridge와 여러 CODEX_HOME

구성:
- MCP 서버를 사용자 설정에 쓰지 않고 `-c mcp_servers.agent-comms.*` 인자로만 지정했다.
- 프롬프트는 한 번도 넣지 않았다(Codex 턴 0회).

| 세션 | CODEX_HOME | 작업 폴더 | 메시 이름·ID |
|---|---|---|---|
| A | `~/.codex` | `.local/s9c` | `codex-6N2V` / `ced29a6cb4d6` |
| B | `~/.codex-2` | `/private/tmp/md-test`(codex-2가 이미 신뢰하는 경로. 실험 동안만 만들었다가 지웠다) | `codex-_Bsv` / `92a1ec4d439b` |
| C | `~/.codex` | `.local/s9c`(A와 같은 폴더) | `codex-OtfJ` / `339d5aee660b` |

- **E4a 같은 메시**
  - 세 bridge가 모두 한 메시에 들어왔다. 메시 주소는 HOME과 포트로만 정해지고, CODEX_HOME은 관여하지 않는다.
  - 그 결과 메시의 에이전트 기록에는 CODEX_HOME이 없어서, A와 C 중 어느 쪽이 어느 프로필인지 메시에서는 구분할 수 없다.
- **신원 슬롯 충돌**
  - C는 A와 같은 슬롯 `(codex, .local/s9c)`이라 신원 파일을 새로 만들지 않고 임시 ID로 합류했다(README Identity의 설명과 같다).
  - 이 ID는 재시작하면 바뀐다.
- **E4b 유휴 Codex**
  - A에 DM(`steer`)을 보내고 45초 동안 관찰했다. 화면이 바뀌지 않았다.
  - 발신 호출은 첫 접촉 동의에서 계속 막혀 있었다. 유휴 Codex는 `room_accept`를 호출할 수 없으므로 첫 접촉 자체가 끝나지 않는다.
  - 동의가 성립한 뒤에도 Codex bridge는 도구 응답에 덧붙이는 방식뿐이라(`src/bridges/codex/tool.ts:1-9,84`) 유휴 Codex를 깨울 경로가 없다.
- **E4c**
  - `src/bridges/codex/`에는 `queue`, `execFile`, `spawn` 사용이 없다.
  - 유휴 Codex를 깨우려면 S2의 `codex queue`를 별도로 써야 하고, 그러면 깨울 때마다 턴 1회가 든다. 이번에는 실행하지 않았다.
- **Codex 쪽 부수 관찰**
  - `-c 'projects."<dir>".trust_level="trusted"'` override로는 폴더 신뢰 대화상자를 건너뛰지 못했다. 저장소 루트를 지정해도 같았다.
  - 대화상자에서 아무것도 고르지 않고 세션을 종료했다.
  - B에서는 사용자 훅 신뢰 대화상자가 떠서 "Continue without trusting (hooks won't run)"을 골랐다.

## E6: 코디네이터 크래시, 큐 재생, 전체 다운

시간 값(10초, 30초, 약 192ms)은 노드 로그가 아니라 20ms·10ms 간격 lsof 폴링 스크립트의 출력이다. 구성: 임시 HOME을 공유하는 독립 node 노드 P1~P3(각자 다른 폴더, 즉 다른 신원 슬롯). 먼저 뜬 P1이 코디네이터가 됐다. `node.mjs`는 합류 요청을 자동 수락한다.

| 단계 | 절차 | 결과 |
|---|---|---|
| 기준선 | P2가 공개 room 생성, P1·P3 참가, 메시지와 DM | 전달과 `read` 수신 확인이 정상이다. **공개 room 참가에도 소유자의 `room_accept`가 필요했다** |
| E6-1 크래시 | P1(코디네이터) `kill -9`, 20ms 간격으로 19876 LISTEN 확인 | **10초 동안 아무도 포트를 다시 열지 않았다.** 이어서 P2가 보낸 room 메시지와 DM은 P3에 전달됐다. 피어끼리 직접 연결을 유지하기 때문이다 |
| E6-1b 분할 | 새 노드 P4 시작 | P4가 빈 포트를 차지해 **별도 메시**의 코디네이터가 됐다. P4의 목록에는 자기만 있고, P2·P3는 30초 넘게 지나도 P4에 접속하지 않았다(lsof상 연결 없음) |
| E6-2 정상 종료 | 새로 구성한 메시에서 코디네이터 P1에 SIGTERM(`store.shutdown()`) | 가장 오래 실행된 P2가 **약 192ms** 만에 포트를 넘겨받았다(폴링 지연 포함). 이후 메시지는 정상 전달됐다 |
| E6-3 수신자 크래시 | P3 `kill -9`, P2가 room 메시지 m2와 DM d2 전송, 3초 뒤 P3를 같은 신원으로 재시작 | P2는 두 전송 모두 "Sent"로 기록했지만 `read` 수신 확인은 없었다. 재시작한 P3에는 30초가 지나도 m2·d2가 오지 않았고, `read_room`은 "No messages."였다. 재시작 뒤 보낸 m3·d3는 곧바로 도착했다. **이 경우 재생이 관찰되지 않았다** |
| E6-4 전체 다운 | 모든 노드 `kill -9` 후 P2 재시작 | room `e6b`가 사라지고 자동 생성된 프로젝트 room만 남았다. 기록 없음. 디스크에는 `~/.agent-comms`의 신원 파일과, 크래시로 **남은 `.lock` 파일**만 있다 |

코드 대조:
- 코디네이터 역할을 넘겨받는 경로는 정상 종료 때 보내는 `become_coordinator` 메시지의 처리(`PeerLifecycle.handleBecomeCoordinator`, `dist/core/peer-lifecycle.js:68-81`)뿐이다.
- 피어 연결이 끊겼을 때의 `handlePeerDisconnected`는 `peerInfo`에서 지우기만 한다(`:83-85`).
- `init()`의 합류 시도도 "Single attempt"라고 주석에 적혀 있다(`src/core/mesh-store.ts:415-417`).
- 따라서 README의 "On crash, remaining peers race to bind the port (~100ms recovery)"(README 67행. 59행의 다이어그램 주석도 같은 주장)는 4.1.2 배포본에서 구현도, 관찰도 되지 않았다.
- 100ms 수준의 인계는 정상 종료에만 해당한다.

## E5: 원격 (tailnet 한정)

### 먼저 발견한 것: 기본 허브 연결

- 코디네이터가 되면 `CoordinatorGateway.onBecameCoordinator()`가 허브에 접속한다(`src/core/coordinator-gateway.ts`). 기본 주소는 `DEFAULT_HUB_URL = "wss://mesh.exadev.io/"`이다(`src/core/mesh-store-shared.ts:60`). CLI 브리지에는 이를 끄는 설정이 없고, 라이브러리 옵션 `hubUrl`(`src/core/bridge-mesh.ts:45,69`)로만 바꿀 수 있다.
- 실측: 기본 설정 노드 하나를 6초 띄우자 `104.21.54.153:443`(`mesh.exadev.io` 해석값)과 `104.16.9.34:443`에 연결이 생겼다. 후자는 코드 주석의 npm 레지스트리 버전 조회(`VersionDriftChecker`)로 보인다(해석 확인은 안 함). `hubUrl`을 닫힌 로컬 포트로 바꾸자 외부 연결이 하나도 생기지 않았다.
- **S9 앞 실험(E1, E4, E6)의 코디네이터도 이 허브에 접속했을 것이다.** 당시에는 이를 몰라 끄지 않았다. 코드상 허브로 가는 것은 다음과 같다(트래픽 캡처로 확인하지는 않았다).
  - 자기 장치의 self-advert: 장치 ID(공개키 해시)와 광고 주소 목록. 광고 주소는 기본 코디네이터 listener를 빼므로(`listener-registry.ts:90-96`) 당시에는 비어 있었다. 허브는 접속 IP를 본다.
  - 로컬 메시의 다른 장치(에이전트 이름, 작업 폴더) 광고는 신뢰한 원격 장치가 있을 때만 나간다(`gatewayTrust.hasAny`, `wire-mesh-transport.ts:251`). 당시 신뢰 장치는 없었다.
  - 메시지 본문은 허브 중계 대상(원격 장치로 가는 요청)만 허브를 거친다. 당시 원격 장치는 없었다.
- 피어 세션 `cross-session-agent-comms`는 허브에 접속하지 않았다. 대화 기록의 도구 호출은 Bash 21회, Write 1회, ListAgents 1회, ToolSearch 1회, SendMessage 1회이고, Bash 명령은 모두 소스 복제·읽기(`git clone`, `grep`, `sed`, Jina 조회)였다. 하위 에이전트는 없었고, 복제한 소스에 `node_modules`나 `dist`도 없어 실행할 수 없는 상태였다. 확인 시점(04:3x)에 실행 중인 agent-comms 프로세스, exadev 연결, 19876 포트, 사용자 홈의 `~/.agent-comms`·`~/.agents/bus` 모두 없었다.
- 시사점: `mesh_listen`으로 주소를 열면 그 주소가 self-advert에 실려 허브에 접속한 다른 에이전트에게 퍼진다. "tailnet 한정"을 지키려면 허브를 반드시 꺼야 한다.

### 절차와 결과

구성: 양쪽 모두 `tools/s9/node.mjs`, `S9_HUB=ws://127.0.0.1:9/`(허브 끔), 임시 HOME. macmini는 `/tmp/xsm-e5`에 같은 버전을 설치했다.

| 단계 | 관찰 |
|---|---|
| macmini M1 시작, `mesh_listen host=100.77.24.16 port=19877 policy=full` | LISTEN은 `127.0.0.1:19876`(기본)과 `100.77.24.16:19877`뿐이었다. 외부로 나가는 연결 없음. 응답 문구가 "100.77.24.1619877"로 콜론이 빠진다(표시 버그) |
| 로컬 L1 시작, `mesh_connect 100.77.24.16:19877` | M1에 `connection_request` 도착. `{"connectionId": …, "peerId": …, "dataPort": 62045, "name": "e5-local", "fingerprint": ""}`. **지문이 비어 있어 승인자가 대조할 값이 없다** |
| 승인 전 L1의 `list_agents` | 자기 자신만 보인다 |
| M1에서 `mesh_accept` | L1 목록에 `e5-macmini`와 원격 작업 폴더가 나타났다 |
| L1 → M1 DM | DM 동의 요청(자동 수락) 후 약 70ms 만에 도착 |
| M1 → L1 DM | 약 140ms 만에 도착, read 확인 |
| 연결 경로 | `100.123.30.11:62051 -> 100.77.24.16:19877`(tailnet) |

해석:
- 원격 연결은 사람(또는 에이전트)의 `mesh_accept` 한 번으로 성립한다. 이 action도 MCP 도구에 있으므로 E1의 `room_accept`와 같은 문제가 있다. S8의 원격 pair가 SSH 호스트 키 지문을 고정한 것과 달리, 승인 시점에 대조할 지문이 비어 있었다.
- `policy=full`이면 승인 뒤 상대 머신의 에이전트 이름과 작업 폴더가 보인다. 로컬 메시에 default front로 올라온 Claude 세션이 있다면 그것도 원격에 노출된다(이번에는 확인하지 않음).

정리: 양쪽 노드 종료, macmini `/tmp/xsm-e5` 삭제(로그만 `/tmp/xsm-spike/s9/e5/m1-node.log`로 가져옴), 양쪽 모두 19876·19877 LISTEN 없음, macmini `~/.agent-comms`·`~/.agents/bus` 없음. macmini 노드는 kill 전에 이미 종료돼 있었다(로그에 종료 기록 없음, 원인 미확인).

## 설계에 주는 시사점

1. **유휴 깨우기는 네이티브 경로가 맞다(ADR-0002).**
   - 훅 기반 전달은 턴이 있어야 동작한다.
   - agent-comms도 유휴 Claude를 깨우는 실제 경로는 Claude 네이티브 inbox(default front)나 개발 채널뿐이고, Codex는 깨우지 못한다.
   - Claude inbox와 `codex queue`를 쓰는 제안 A가 이 결과와 맞는다.
2. **`from-mode` 자기 주장은 현실의 위험이다(ADR-0009).**
   - 널리 쓰이는 클라이언트가 기본값으로 `bypass`를 주장한다.
   - 그러면 bypass로 도는 모든 세션은 같은 uid의 어떤 프로세스가 보낸 메시지라도 자동 실행한다.
   - 수신 측 검문(S8의 UPS 훅 게이트)이 필요하다는 근거다.
3. **동의 절차는 누가 답하는지가 핵심이다(ADR-0009).**
   - agent-comms에는 장치 단위 첫 접촉 동의(`requestDmAccess`)가 있어서, S8의 pair 수립과 성격이 같다.
   - 그러나 수락 action `room_accept`가 **수신 에이전트의 MCP 도구에 노출돼 있다**(`src/core/bridge.ts:56`). 이번 실험에서도 에이전트가 운영자 지시 한 줄에 수락을 실행했다.
   - 발신 호출은 시간 초과가 없다(228.7초 관찰). `requestDmAccess`는 `sendManageRequest`를 `timeoutMs` 없이 호출하고, 이 경우 상한 없는 promise를 그대로 반환한다(`wire-mesh-core` `mesh-session.mjs:357-382`), 유휴 에이전트는 답할 수 없다.
   - S8처럼 동의는 정책이나 사람만 할 수 있어야 하고, 발신 측은 막히지 않고 "보류됨"을 받아야 한다.
4. **메모리 전용 레지스트리의 실패 방식이 확인됐다(ADR-0001, 0005).**
   - 코디네이터 크래시 → 복구 없이 분할된다.
   - 수신자 크래시 → 전송이 조용히 유실되고, 발신자에게는 "Sent"만 남는다.
   - 전체 다운 → 기록이 사라진다.
   - 파일 레지스트리와 내구성 있는 기록 저장소를 두는 방향을 지지한다. "쓰기 성공"과 "전달·읽음"을 구분해야 한다는 점도 확인됐다.
5. **프로필과 신원 슬롯(ADR-0001, 0008).**
   - 레지스트리 탐색은 모든 CONFIG_DIR·CODEX_HOME을 열거해야 한다(E3).
   - 신원을 `(harness, cwd)`에 묶으면 같은 폴더의 여러 세션이 충돌하고, 임시 ID는 재시작하면 바뀐다(E4a). 주소는 세션 단위여야 한다.
   - 메시 기록에 프로필(홈) 정보가 없으면 `name@homeAlias` 같은 한정 이름을 만들 수 없다.
6. **자동 편입은 범위 문제다(ADR-0004).** default front는 `~/.claude/sessions`의 세션을 동의 없이 메시에 올린다. 범위 정책은 세션 편입 시점에도 적용돼야 한다.
7. **기본값이 외부 서비스에 접속한다(ADR-0003, 0007).** agent-comms 코디네이터는 설정 없이 제3자 허브에 접속한다. "런타임 없음, 사용자 범위 안" 원칙과 맞지 않는다. 원격 전송은 명시적으로 켠 경로(ADR-0007 D의 SSH)만 쓰고, 원격 승인에는 대조 가능한 지문을 보여야 한다.

## 정리와 설정 확인

- **종료한 것**:
  - 테스트 Claude 세션 3개(`/exit`)와 Codex 세션 3개
  - 모든 bridge·노드 프로세스
  - tmux 서버
  - 테스트 세션의 레지스트리 레코드는 Claude가 종료 시 스스로 지웠다.
- **삭제한 것**: 임시 HOME에 복사해 둔 레코드와 키, `/private/tmp/md-test`, `.local/s9c`
- **남긴 것**: 실험 상태 `/tmp/xsm-spike/s9/`(임시 HOME 포함)
- **사용자 홈**: `~/.agents/bus`, `~/.agents/comms`, `~/.agent-comms`는 생기지 않았다.
- **설정 수정 시각**(실험 전 기록과 비교):

| 파일 | 결과 |
|---|---|
| `~/.claude/settings.json`, `~/.claude-3/settings.json`, `~/.claude-4/settings.json` | 변경 없음 |
| `~/.codex/config.toml`, `~/.codex/hooks.json`, `~/.codex-2/hooks.json`, `~/.claude.json` | 변경 없음 |
| `~/.claude-4/.claude.json` | 변경됨. Claude가 세션 실행마다 갱신하는 상태 파일이다 |
| `~/.codex-2/config.toml` | **변경됨(04:05).** 원인을 확정하지 못했다. 변경 전 사본이 없다. B 세션에서만 "Tip: This is GPT-6 …" 안내가 떴고 `[tui.model_availability_nux] gpt-6-astra = 4`(표시 상한)가 있어, 안내 표시 횟수 기록일 가능성이 가장 크다. 신뢰·훅·MCP 관련 항목은 추가되지 않았다(`projects`, `hooks`, `mcp_servers` 검색 결과 기존 항목뿐) |
