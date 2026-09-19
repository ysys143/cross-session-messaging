Herdr는 자체 데몬이 소유한 터미널에서 Claude Code·Codex 등을 발견하고, 로컬 JSON API 요청을 PTY 입력으로 바꾸어 다른 에이전트에 프롬프트를 전달하는 터미널 런타임이다. 프로필과 독립된 주소 체계, 세션 보고 훅, SSH 브리지는 참고할 만하지만, Herdr 바깥에서 실행한 임의 세션을 연결하는 구조와는 다르며 별도 진입점 런타임을 금지한 우리 목표에 그대로 도입하기는 어렵다. 채널·스레드 기록이나 공동 문서 편집 조정은 확인 못 했다. 근거: `src/server/autodetect.rs:194-223`, `src/app/agents.rs:23-36`, `src/app/api/agents.rs:130-215`, `src/persist/snapshot.rs:14-46,97-124`; 비교 기준: 우리 저장소 `INTENT.md:11-21`.

## 1. 정의와 실행 형태

**한 줄 정의:** Rust로 구현한 AI 코딩 에이전트용 터미널 멀티플렉서이며, CLI/TUI 클라이언트와 백그라운드 서버 데몬으로 실행된다. 패키지 버전은 0.9.1이다. 서버를 별도 프로세스로 실행하는 코드는 현재 실행 파일에 `server` 인수를 주고 표준 입출력을 분리한다. 근거: `Cargo.toml:1-11`, `src/server/autodetect.rs:194-223`, `src/main.rs:548-565`.

주요 의존성은 Ratatui·Crossterm(TUI), portable-pty(PTY), interprocess(로컬 IPC), Tokio, Serde/serde_json, bincode이다. vendored libghostty-vt를 Zig로 빌드한다. 근거: `Cargo.toml:23-52`, `build.rs:54-71`.

조사 기준은 `/tmp/xsm-refs/herdr`의 커밋 `3f2a6e743f67bc947cfa06be25df106d00b9ee11`이다(`git rev-parse HEAD`로 확인). 이하 경로는 별도 표시가 없으면 이 체크아웃 기준이다. **정적 코드 조사만 수행했으며**, 빌드·테스트·실제 Claude/Codex 실행·SSH 접속은 수행하지 않았다. 따라서 이 문서의 “확인”은 구현 확인이지 운영 환경 호환성 검증이 아니다.

## 2. 세션 발견과 레지스트리

Herdr의 “session”과 에이전트의 대화 세션을 구분해야 한다.

| 대상 | 등록·발견 방식 | 식별·생존 근거 |
|---|---|---|
| Herdr 서버 세션 | 기본 설정 디렉터리와 `sessions/<name>`를 열거 | 이름과 `herdr.sock` 경로; 소켓 연결 성공으로 running 판정 (`src/session.rs:157-225,415-416`) |
| 관리 터미널의 에이전트 | 메모리의 workspace → tab → pane을 순회해 agent terminal만 반환 | `terminal_id`, name, pane/workspace/tab ID, 상태와 세션 참조 (`src/app/agents.rs:23-36,366-401`) |
| Claude/Codex 대화 세션 | SessionStart 훅이 pane에 native session ID를 보고 | `pane.report_agent_session`, source, seq, agent_session_id (`src/integration/assets/claude/herdr-agent-state.sh:53-84`, `src/integration/assets/codex/herdr-agent-state.sh:51-82`) |

Unix 기본 경로는 `~/.config/herdr`이며 `XDG_CONFIG_HOME`이 우선한다. 디버그 빌드는 `herdr-dev`를 쓴다. 기본 API 소켓은 그 아래 `herdr.sock`, 이름 있는 세션은 `sessions/<name>/herdr.sock`이다. 명시적 세션 선택이 없으면 `HERDR_SOCKET_PATH`로 덮어쓸 수 있다. 근거: `src/config/io.rs:22-34,61-67`, `src/session.rs:161-184`.

터미널 ID는 시각과 프로세스 내 atomic counter로 만든 `term_<hex>`이다. 에이전트 prompt의 target 해석은 공개 pane ID 또는 agent name을 사용하며, `AgentInfo`에 terminal ID가 있다고 해서 해당 prompt 경로가 terminal ID를 받는다고 가정하면 안 된다. 이름 중복은 오류 처리한다. 근거: `src/terminal/id.rs:12-21`, `src/app/terminal_targets.rs:75-105,116-133`, `src/app/agents.rs:165-170`.

송신 직전에는 대상 PTY의 foreground job에서 실제 에이전트 종류를 다시 확인한다. 이는 소켓 연결로 판단하는 서버 생존과 별개이다. **Herdr 외부 프로세스·모든 CONFIG_DIR를 스캔해 등록하는 일반 세션 발견 기능은 확인 못 함**: 확인한 목록 생성 경로는 Herdr의 pane 소유 관계를 출발점으로 한다. 근거: `src/app/agents.rs:23-36,426-445`, `src/app/api/agents.rs:159-173`.

## 3. 메시징·전달과 wakeup/invoke

### 외부 API와 전달 프레임

에이전트 또는 도구가 Herdr API 소켓으로 한 줄 JSON 요청을 보내고, 서버가 이를 타입이 있는 `Request`로 역직렬화한다. Unix는 filesystem local socket, Windows는 namespaced local socket이다. 응답에도 개행을 붙인다. HTTP/WebSocket은 이 경로의 transport가 아니다. 근거: `src/ipc.rs:35-77`, `src/api/server.rs:168-203,780-799`.

```rust
// src/api/schema.rs:35-43
pub struct Request {
    pub id: String,
    #[serde(flatten)]
    pub method: Method,
}
// Method는 #[serde(tag = "method", content = "params")] 사용
```

스키마에서 구성한 요청 예시는 아래와 같다. 실전송 시험 결과는 아니다. 근거: `src/api/schema.rs:136-139`, `src/api/schema/agents.rs:178-184`.

```json
{"id":"request-1","method":"agent.prompt","params":{"target":"reviewer","text":"변경 사항을 검토해 주세요"}}
```

`id`는 요청·응답 상관관계 필드다. 이 구조와 prompt 큐 경로에서 영속 메시지 ID, 발신자 인증, thread ID, deduplication 저장소는 확인 못 했다. 근거: `src/api/schema.rs:35-43`, `src/api/schema/agents.rs:178-184`, `src/app/api/agents.rs:111-215`.

### Claude Code와 Codex의 실제 수신 경로

두 에이전트 모두 **PTY 입력 주입**으로 prompt를 받는다. text와 Enter를 인코딩하고 300ms 간격을 두어 큐에 넣는다. blocked 상태, 미준비 상태, foreground agent 불일치에서는 오류를 반환한다. 이 경로는 Claude 자체 inbox socket, MCP 호출, Codex app-server 호출을 사용하지 않는다. 근거: `src/app/api/agents.rs:13,130-173,194-215`.

```rust
// src/app/api/agents.rs:207-213
let completion = runtime
    .queue_user_input_submission(
        Bytes::from(text),
        Bytes::from(enter),
        AGENT_PROMPT_SUBMIT_DELAY,
        submit_deadline,
    )
```

Windows Codex에는 paste burst 뒤의 Enter가 줄바꿈으로 처리되는 문제를 피하기 위한 right-key 경계 처리가 있다. 따라서 공통 PTY 추상화에도 에이전트·OS별 입력 보정이 필요하다. 근거: `src/app/api/agents.rs:15-32,196-203`.

Claude 훅은 SessionStart이며 subagent는 제외한다. Codex 훅도 SessionStart를 처리하고 transcript_path가 없거나 상속된 CODEX_THREAD_ID가 불일치하면 무시한다. 둘 다 Herdr 환경·소켓·pane ID가 필요하고, 0.5초 제한의 Unix socket으로 세션 정보를 **밖으로 보고**한다. 훅이 수신 프롬프트를 깨우는 것이 아니다. 근거: `src/integration/assets/claude/herdr-agent-state.sh:20-23,51-99`, `src/integration/assets/codex/herdr-agent-state.sh:20-23,51-97`.

```python
# src/integration/assets/claude/herdr-agent-state.sh:80-84,89-92
request = {
    "id": request_id,
    "method": "pane.report_agent_session",
    "params": params,
}
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.5)
client.connect(socket_path)
client.sendall((json.dumps(request) + "\n").encode())
```

`agent.start`는 기존 Herdr pane의 사용 가능한 shell에 agent 실행 명령을 입력한다. 이미 종료한 임의 외부 세션의 재기동 기능과 혼동하면 안 된다. 기본 prompt 성공 응답은 PTY submission 완료이며 작업 완료가 아니다; 선택적인 wait 상태 조건은 별도 필드다. 근거: `src/app/agents.rs:172-220`, `src/app/api/agents.rs:90-99`, `src/pty/actor/unix.rs:860-875`, `src/api/schema/agents.rs:25-42,178-184`.

## 4. 이종 에이전트와 CONFIG_DIR

공통 `Agent` enum에 Claude, Codex, Gemini, Cursor, OpenCode, Pi, Omp 등 여러 구현을 두고, `AgentInfo`와 prompt/read/start API를 공유한다. 화면·OSC title 기반 상태 감지는 agent별 TOML manifest로 분리한다. 예컨대 Claude spinner와 Codex의 Action Required/working 표식을 각각 규칙으로 판별한다. 근거: `src/detect/mod.rs:22-68`, `src/api/schema/agents.rs:166-220`, `src/detect/manifests/claude.toml:7-24`, `src/detect/manifests/codex.toml:6-20`.

프로필 경로는 무시하지 않고 명시적으로 존중한다. Claude 설치 대상은 `CLAUDE_CONFIG_DIR` 또는 `~/.claude`, Codex는 `CODEX_HOME` 또는 `~/.codex`이다. Claude는 settings.json과 hooks 디렉터리, Codex는 hooks.json 및 config.toml을 갱신한다. **한 번 설치하면 모든 프로필에 자동 설치되는 것은 확인 못 함**: 이 함수들은 호출 환경에서 결정한 한 디렉터리를 처리한다. 근거: `src/integration/env.rs:58-64,91-100`, `src/integration/targets.rs:124-151,160-214`.

메시지 주소는 그 프로필 경로가 아니라 Herdr 소켓·pane ID이다. pane 생성 시 `HERDR_ENV`, socket 경로, workspace/tab/pane ID를 주입하며, 잘못 상속된 CODEX_THREAD_ID는 제거한다. **해석:** 서로 다른 프로필이라도 동일 Herdr 관리 영역에 들어오면 공통 transport를 이용할 설계이지만, 외부에서 독립 실행한 세션까지 프로필 불문 연결되는 것은 아니다. 근거: `src/pane.rs:148-173`, `src/integration/env.rs:28-32`, `src/app/agents.rs:23-36`.

## 5. 원격·다중 머신과 신뢰

SSH를 통한 원격 Herdr 서버 연결이 구현돼 있다. TUI 측 원격 브리지는 원격 서버 실행 여부를 확인한 뒤 client socket과 stdio를 중계하고, 별도 `remote-api-bridge`는 JSON API 소켓과 stdio를 중계한다. 원격 명령에는 `--session <name>`이 포함된다. 근거: `src/remote/host.rs:6-35,38-60`, `src/remote.rs:13-35`, `src/remote/attach.rs:2500-2513`.

```rust
// src/remote.rs:15-17,26 (오류 처리 생략)
let path = crate::api::socket_path();
let stream = crate::ipc::connect_local_stream(&path) /* ... */;
crate::platform::forward_remote_bridge_stdio(stream, false)
```

원격 인증은 SSH 사용자·키·host key 신뢰에 의존한다. 비대화식 SSH 옵션은 `BatchMode=yes`, `NumberOfPasswordPrompts=0`, `StrictHostKeyChecking=yes`, 접속 timeout 10초, 시도 1회이다. 이 엄격 옵션을 모든 대화식 SSH 경로까지 일반화하지 않는다. 근거: `src/remote/attach.rs:1029-1044`, `src/remote.rs:39-57`.

로컬 API는 Unix에서 소켓 권한 0600을 적용한다. 확인한 prompt 요청에는 agent별 capability나 프로젝트 ACL 토큰이 없다. **해석:** 소켓에 접근할 수 있는 로컬 사용자/SSH 계정이 주요 신뢰 경계이다. Windows API named pipe의 실효 접근 통제와 Cloud 관련 전체 인증 구조는 이번 조사에서 확인 못 함. 근거: `src/api/server.rs:27,83-88,141-142`, `src/ipc.rs:54-77`, `src/api/schema/agents.rs:178-184`.

원격 브리지가 있다는 사실은 임의 Claude/Codex 세션 간 자동 federation의 증거는 아니다. 확인한 원격 종착점 역시 Herdr 서버 소켓이다. 근거: `src/remote.rs:15-26`, `src/remote/host.rs:22-35`.

## 6. 범위 제어

workspace/tab/pane은 조직·주소 단위다. 에이전트 목록은 현재 서버의 모든 workspace를 순회하고 target은 pane 또는 이름으로 해석한다. **프로젝트별 송신 권한 제한은 확인 못 함**: 확인한 list/prompt 경로에는 발신 workspace와 수신 workspace의 권한 비교가 없다. 근거: `src/app/agents.rs:23-36`, `src/app/terminal_targets.rs:75-105`, `src/app/api/agents.rs:130-173`.

AgentViewFilter는 workspace/tab/pane/status 등을 필터링할 수 있으나 표시·정렬 모델이다. 통신 허가 정책의 증거로 삼을 수 없다. 이름 있는 Herdr session은 별도 데이터 디렉터리와 소켓을 사용하므로 연결 대상을 나누는 수단이다. **해석:** 표시 필터, 서버 인스턴스 분리, 보안 ACL은 서로 다른 층위다. 근거: `src/api/schema/agents.rs:69-127`, `src/session.rs:161-184`.

## 7. 지속 기록과 사람이 보는 UI

`session.json`에 workspace·tab·pane 구성, cwd, agent name/kind, 세션 resume 참조, launch argv 등을 저장한다. `session-history.json`은 pane의 ANSI 화면과 line 수를 저장하는 선택적 이력이다. SQLite 대화 DB나 Markdown 채널 로그로 구현된 것은 확인 못 했다. 근거: `src/persist/io.rs:10-16,63-70`, `src/persist/snapshot.rs:14-46,49-69,97-124`.

이벤트 허브는 Mutex로 보호한 메모리 Vec에 sequence를 붙이며 최대 512개만 유지한다. 따라서 **그 자체는 영속 채널 로그가 아니며**, 오래된 이벤트를 무제한 재생하는 기록으로 사용할 수 없다. 근거: `src/api/event_hub.rs:1-37`.

```rust
// src/api/event_hub.rs:13,21-24
const MAX_EVENTS: usize = 512;
state.events.push((sequence, event));
let overflow = state.events.len().saturating_sub(Self::MAX_EVENTS);
if overflow > 0 {
    state.events.drain(0..overflow);
}
```

사람용 UI는 Ratatui 터미널 화면이며 pane, sidebar agent rows, tab surface가 있다. 그러나 확인한 저장 스키마에는 channel/thread/message 관계나 작업 DAG가 없다. **확인 못 함:** 사람과 에이전트가 함께 쓰는 1:N/N:N 대화 스레드 UI, 메시지 읽음·답장 관계, 별도 task 상태 정본. 근거: `Cargo.toml:29-36`, `src/ui.rs:1-11,34-43`, `src/persist/snapshot.rs:14-124`.

## 8. 동시성·충돌·재시도

| 구현 | 실제 보장 범위 | 근거 |
|---|---|---|
| PTY actor 입력 큐 | full이면 WouldBlock, closed이면 BrokenPipe; 현재 submission을 마칠 때까지 다음 user command 처리를 끊음 | `src/pty/actor/unix.rs:143-176,614-669` |
| text → delay → Enter | text 완료 시점부터 지연을 계산하고 Enter write 완료 후 응답 | `src/pty/actor/unix.rs:860-897` |
| source별 report sequence | 이전 이하 seq를 버려 오래된 hook report의 상태 덮어쓰기 억제 | `src/terminal/state.rs:1673-1687` |
| 서버 중복 실행 억제 | 소켓 연결 성공 시 AddrInUse; 연결 오류 종류에 따라 stale socket 제거 | `src/ipc.rs:81-114` |
| JSON snapshot 교체 | 같은 디렉터리의 `.json.tmp`에 쓴 뒤 rename | `src/persist/io.rs:48-60` |
| 이벤트 접근 직렬화 | Mutex와 제한된 메모리 버퍼 | `src/api/event_hub.rs:1-37` |

```rust
// src/terminal/state.rs:1678-1686
if self.hook_report_sequences.get(source)
    .is_some_and(|last_seq| seq <= *last_seq)
{
    return false;
}
self.hook_report_sequences.insert(source.to_string(), seq);
```

이 sequence 억제는 hook 상태 보고용이며 prompt 중복 실행 방지가 아니다. prompt request ID를 처리 완료 목록에 기록하고 재송신을 deduplicate하는 로직은 확인 못 했다. 기본 prompt 완료가 PTY write에 해당하므로, timeout 후 무조건 재전송하면 중복 입력 가능성이 있다는 것은 **설계상 추론**이다. 근거: `src/app/api/agents.rs:90-99,111-215`, `src/pty/actor/unix.rs:860-875`.

초기 API 요청에는 5초 제한과 1MiB 크기 제한이 있다. Claude/Codex hook의 socket 오류는 삼키며 해당 코드에 재시도 루프는 없다. 이 제한들은 공동 문서 lock 만료 정책이 아니다. 근거: `src/api/server.rs:27-32`, `src/integration/assets/claude/herdr-agent-state.sh:88-99`, `src/integration/assets/codex/herdr-agent-state.sh:86-97`.

**공동 문서 문제는 미해결이다.** 확인한 atomic rename은 Herdr snapshot 파일용이고 temp 이름도 고정이므로 임의의 다중 writer 문서 편집 프로토콜로 그대로 재사용할 수 없다. input lease 역시 물리 키 press/repeat와 source/target을 관리하는 코드이며 파일 편집 lock이 아니다. 파일별 CAS/버전 비교, CRDT, 문서 lease 만료, 에이전트 대화 hop limit은 확인 못 함. 근거 범위: `src/persist/io.rs:48-60`, `src/input/lease.rs:6-50,67-107`, `src/api/schema/agents.rs:178-184`.

## 9. 오케스트레이션 모델과 런타임 의존성

제공하는 기본 단위는 `agent.list/get/read/start/prompt/wait/send_keys`이며, start는 shell 명령 실행, prompt는 PTY 입력, wait는 상태 조건이다. **해석:** 외부 스크립트나 에이전트가 이 API를 조합해 coordinator-worker 흐름을 만들 수 있지만, API 자체가 coordinator/worker 역할을 강제하지는 않는다. 이 조사에서 task DAG·dispatch capability·승인 gate·worker_done 상태 머신의 구현은 확인 못 함. 근거: `src/api/schema.rs:116-139`, `src/api/schema/agents.rs:25-42,166-184`, `src/app/agents.rs:197-220`.

기존 `claude`, `codex` 실행 파일을 이용하면서도 **별도 Herdr 서버와 관리 PTY를 요구한다**. `agent.start`는 공통 executable 선택 후 pane shell에 실행 명령을 넣고, Herdr 자체는 서버 데몬을 실행한다. 따라서 “기존 CLI를 사용한다”와 “별도 진입점 런타임이 필요 없다”는 같은 말이 아니다. 근거: `src/app/agents.rs:197-220`, `src/server/autodetect.rs:194-223`, `src/pane.rs:148-173`.

## 10. INTENT.md에 대한 시사점

다음은 구현 사실을 바탕으로 한 **설계 제안**이며 Herdr가 이미 제공하는 기능으로 읽으면 안 된다. 요구 근거는 우리 저장소 `INTENT.md:11-21`이다.

| 판단 | 제안과 이유 | 구현 근거 |
|---|---|---|
| 차용 | 프로필 경로와 독립된 통신 endpoint 및 native session ID를 분리한다. 다만 우리 endpoint 등록은 기존 CLI lifecycle에 붙여야 한다. | `src/integration/env.rs:28-32,58-64`, `src/api/schema/agents.rs:186-213` |
| 차용 | SessionStart에서 명시적 session ID를 보고하고, child/subagent가 부모 정보를 덮어쓰지 않게 한다. | `src/integration/assets/claude/herdr-agent-state.sh:53-84`, `src/integration/assets/codex/herdr-agent-state.sh:55-82` |
| 차용 | 수신 대상 생존·준비·blocked를 구분하고 write 성공과 작업 완료를 분리한다. | `src/app/api/agents.rs:130-173`, `src/pty/actor/unix.rs:860-875` |
| 차용 | 원격 endpoint를 SSH stdio로 연결하고 기존 SSH 신뢰 체계를 활용하는 선택지를 검토한다. | `src/remote.rs:13-26`, `src/remote/attach.rs:1029-1044` |
| 피함 | Herdr 소유 PTY를 보편적 세션 발견·wakeup 전제로 채택하지 않는다. 외부 CLI 실행을 그대로 유지한다는 제약과 충돌한다. | `src/app/agents.rs:23-36,172-220`, `src/server/autodetect.rs:194-223` |
| 피함 | 화면 텍스트/스피너 판별만으로 세션 상태 계약을 만들지 않는다. CLI 화면·OS 변화에 대응하는 규칙과 보정이 필요하다. | `src/detect/manifests/claude.toml:7-24`, `src/detect/manifests/codex.toml:6-20`, `src/app/api/agents.rs:15-32` |
| 별도 설계 | project membership와 송신 ACL을 명시한다. workspace 필터를 접근 통제로 간주하지 않는다. | `src/app/agents.rs:23-36`, `src/api/schema/agents.rs:69-127` |
| 별도 설계 | 영속 channel/thread/message 정본과 wakeup 전달 상태를 나눈다. 메모리 이벤트 512개나 ANSI snapshot으로 대화 기록을 대체하지 않는다. | `src/api/event_hub.rs:13-24`, `src/persist/snapshot.rs:120-124` |
| 별도 설계 | 공동 문서는 한 정본에 대한 version/CAS, 제한된 재시도와 충돌 반환을 검토한다. 원자적 rename만으로 다중 writer의 lost update를 막는다고 가정하지 않는다. | `src/persist/io.rs:48-60` |

적합성 판단: **PTY 기반 이종 에이전트 제어의 구현 참고자료로는 유용하지만, 런타임 없는 cross-session messaging·영속 채널·공동 문서 조정의 완성된 해법은 아니다.** 이는 위 구현과 `INTENT.md:15,19-21`의 요구를 비교한 판단이다.

## 근거 파일 목록

아래는 본문에서 직접 인용한 파일이다. 경로는 조사 커밋 기준이며, 행 범위는 본문에 표시했다.

- `Cargo.toml`, `build.rs`: 실행 패키지·의존성·Zig 빌드.
- `src/main.rs`, `src/server/autodetect.rs`: 진입점·서버 데몬.
- `src/config/io.rs`, `src/session.rs`: 설정 위치·서버 세션과 소켓 발견.
- `src/terminal/id.rs`, `src/app/agents.rs`, `src/app/terminal_targets.rs`: 식별자·관리 에이전트 목록·대상 해석.
- `src/api/schema.rs`, `src/api/schema/agents.rs`, `src/api/server.rs`: 요청·응답·API transport.
- `src/ipc.rs`, `src/app/api/agents.rs`, `src/pty/actor/unix.rs`: 로컬 IPC·prompt 주입·입력 큐.
- `src/integration/env.rs`, `src/integration/targets.rs`, `src/pane.rs`: 프로필 경로·통합 설치·자식 환경.
- `src/integration/assets/claude/herdr-agent-state.sh`, `src/integration/assets/codex/herdr-agent-state.sh`: 세션 등록 훅.
- `src/detect/mod.rs`, `src/detect/manifests/claude.toml`, `src/detect/manifests/codex.toml`: 종류 추상화·화면 상태 규칙.
- `src/remote.rs`, `src/remote/host.rs`, `src/remote/attach.rs`: SSH와 원격 API/TUI 브리지.
- `src/persist/io.rs`, `src/persist/snapshot.rs`, `src/api/event_hub.rs`, `src/ui.rs`: 저장·이벤트·TUI.
- `src/terminal/state.rs`, `src/input/lease.rs`: 상태 sequence와 키 입력 lease.
- 우리 저장소 `INTENT.md:11-21`: 적합성 비교 기준.
