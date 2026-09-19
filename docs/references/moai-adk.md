# MoAI-ADK 소스 조사

MoAI-ADK에는 Claude↔Codex용 **프로젝트 로컬 파일 우편함과 MCP 도구가 실제 구현**되어 있다. 그러나 수신은 명시적인 `session_msg_poll` 호출이며, 이것을 기존 세션에 대한 즉시 invoke/wakeup으로 볼 수 없다. Claude 네이티브 메시징 설정, Codex app-server 작업 실행, 칸반 협업은 서로 다른 경로다. 따라서 CONFIG_DIR 밖의 공통 저장소와 전달 재시도 설계는 참고할 수 있지만, 직접 깨우기와 사람·에이전트 채널 기록은 별도 설계가 필요하다. 근거: `internal/cli/mcp_server.go:430-481`, `internal/cli/mcp_session_msg.go:99-145`, `internal/cli/crosssession_settings.go:47-91`, `internal/cli/mcp_codex.go:575-630`.

조사 기준은 `/tmp/xsm-refs/moai-adk`의 커밋 `2213871afb7d655f411c46a34fd38bb8153278fc`다. 아래 경로는 별도 표시가 없으면 이 소스 루트 기준이다. 이번 조사는 `rg`, `nl -ba`, `git rev-parse HEAD`, `git status --short`를 사용한 정적 코드 조사다. 소스 checkout의 상태 출력은 비어 있었다. 프로그램 실행·테스트·실제 Claude/Codex 통신·원격 연결은 수행하지 않았으며, 런타임 작동 여부는 **확인 못 함**이다. “확인”은 코드에서 확인한 구현을 뜻하고, 설계 권고와 실행 결과는 구분한다.

## 1. 한 줄 정의와 실행 형태

**Go로 구현한 코딩 에이전트 개발·협업 지원 CLI이며, 하위 명령으로 stdio MCP 서버와 로컬 웹 콘솔을 실행한다.** 실행 진입점은 `cmd/moai/main.go:13-22`의 `cli.Execute()`다. MCP 서버는 stdin이 닫힐 때까지 서비스하는 프로세스이고, 웹 콘솔은 loopback TCP listener다. 데스크톱 앱이 필수인 구조는 이 진입점들에서 확인되지 않는다. 근거: `internal/cli/mcp_server.go:84-97`, `internal/web/server.go:42-44,177-189`.

`go.mod:1-35`는 Go `1.26.4`, Cobra, `mcp-go`, Bubble Tea/Bubbles/Lip Gloss/Huh, templ, fsnotify, tree-sitter, YAML 및 `x/sys` 등을 선언한다. 이는 의존성 선언이며, 이번 조사에서 빌드 호환성까지 검증한 것은 아니다.

## 2. 발견과 레지스트리

서로 다른 두 레지스트리를 구분해야 한다.

| 구분 | 저장·등록 | 식별·생존 판정 |
|---|---|---|
| 메시징용 agent registry | `<project>/.moai/state/session-msg/agents/<agentId>.json`; MCP `session_msg_register(kind,name,description)` | `claude-<hex8>` 또는 `codex-<hex8>`; 같은 kind+name이면 기존 ID 재사용; heartbeat 경과가 30분 이하면 online |
| 세션 작업 조정 registry | `<project>/.moai/state/active-sessions.json`; SessionStart 훅이 `input.SessionID` 등록 | session ID와 SPEC/phase, PID, host, CWD, 시작·heartbeat 시각; purge는 heartbeat 기준 |

첫 행 근거: `internal/sessionmsg/store.go:20-24,100-110`, `internal/sessionmsg/agent.go:27-64,77-148,155-174`, `internal/config/defaults.go:412-414`. 둘째 행 근거: `internal/session/registry.go:35-39,155-200,288-304`, `internal/hook/session_start.go:1316-1347`.

메시징 등록의 핵심은 다음과 같다 (`internal/sessionmsg/agent.go:93-105`).

```go
if rec.Kind == kind && rec.Name == name {
    rec.LastHeartbeat = now
    // description 갱신 후 같은 AgentID의 레코드를 저장
    out = rec
    return nil
}
```

**해석:** 동일 종류·이름을 사용하는 독립 세션 둘은 하나의 메시징 주소를 공유하게 된다. 이름이 세션 인스턴스의 고유성을 대신하기 때문이다. `PID`는 `os.Getpid()`이므로 MCP 도구로 등록하면 MCP 서버 프로세스 PID다. online은 PID 탐지가 아니라 heartbeat 시각 비교이며, 등록·송신·poll이 이를 갱신한다. 자동 전역 프로세스 스캔이나 임의 CONFIG_DIR 탐색은 이 등록 경로에 없다. 근거: `internal/sessionmsg/agent.go:96-105,124-137,155-191`, `internal/sessionmsg/store.go:269-277,420-426`.

## 3. 전달 방식과 wakeup/invoke

### 메시징 브로커: Claude와 Codex 공통

에이전트가 `moai mcp-server`에 MCP stdio JSON-RPC로 도구를 호출한다. 서버끼리 네트워크로 전달하는 대신, 수신자별 JSON 파일을 공유한다. 저장 단위는 JSON envelope 한 파일이며 JSONL 로그가 아니다. `Send`는 수신자 mailbox의 `pending`에 원자적으로 기록하고, `Poll`은 `claimed`로 옮겨 반환한다. 근거: `internal/cli/mcp_server.go:84-97`, `internal/sessionmsg/store.go:100-110,113-147,262-277,375-426`.

```go
err = s.withAgentLock(toAgentID, func() error {
    return writeJSONAtomic(filepath.Join(s.pendingDir(toAgentID), msgID+".json"), env)
})
```

발췌: `internal/sessionmsg/store.go:262-264`. 이 다음에는 송신자 heartbeat와 반환만 있다 (`:269-277`). 수신 터미널 주입, 소켓 호출, 프로세스 시작은 이 경로에 없다.

스키마는 `message{messageId,contextId?,taskId?,role,parts,metadata?}`와 `delivery{senderId,senderKind,sentAt,expiresAt,claimedAt?}`다. part는 text 또는 JSON data다. 필드명은 A2A를 참고하지만 A2A 전송 프로토콜 구현은 아니다. 근거: `internal/sessionmsg/envelope.go:1-9,31-80,90-139`.

**Claude와 Codex 모두 이 브로커에서 자동으로 깨어나지 않는다.** 다음 poll 호출이 수신 계기이며, 도구 설명에도 그렇게 명시돼 있다. 세션이 멈춰 있거나 도구 호출을 하지 않으면 파일이 존재해도 처리가 시작된다는 보장이 없다. 근거: `internal/cli/mcp_server.go:458-480`, `internal/cli/mcp_session_msg.go:126-145`.

### Claude 네이티브 경로와 Codex 실행 경로

- **Claude↔Claude:** 저장소 운영 규칙은 Claude 런타임의 `ListAgents`/`SendMessage`를 권장한다. 이는 문서상 위임 관계이며 MoAI 자체 소켓 구현의 증거가 아니다. MoAI 실제 코드는 `crossSessionInbound`, `dialogExpiry`, `isolatePeerMachines` 설정을 만들어 Claude 실행 인자 `--settings`에 전달한다. 네이티브 inbox 프레임이나 실제 wakeup 내부는 이 저장소에서 **확인 못 함**이다. 근거: `.claude/rules/moai/workflow/cross-session-messaging.md:125-139`, `internal/cli/crosssession_settings.go:47-91`.
- **Codex 작업 실행:** `exec.CommandContext`로 `codex app-server` 하위 프로세스를 띄우고, stdin/stdout 줄 단위 JSON-RPC를 사용한다. `initialize` 후 `thread/start` 또는 `thread/resume`을 사용하며 작업 turn을 실행한다. **기존에 열려 있는 임의 Codex TUI의 발견·입력 주입과는 다르다.** 재개할 thread ID를 전달하는 기능은 있으나, 연결 대상 프로세스는 여기서 새로 시작한다. 근거: `internal/cli/mcp_codex.go:395-412,426-447,575-630`, `internal/cli/codex_task.go:225-252`.

## 4. 이종 에이전트와 프로필

메시징 추상화는 복잡한 provider adapter가 아니라 동일한 register/list/send/poll API와 `kind` 값이다. 등록 허용 종류는 Claude와 Codex뿐이며, 임의 종류 확장은 현재 검증 조건을 바꿔야 한다. 근거: `internal/sessionmsg/agent.go:53-55,77-82`, `internal/cli/mcp_server.go:430-481`.

브로커 루트는 `CLAUDE_PROJECT_DIR`, 없으면 CWD에서 계산한다. `CLAUDE_CONFIG_DIR`나 `CODEX_HOME`별 mailbox를 만들지 않는다. **추론:** 서로 다른 프로필이라도 MCP가 동일 프로젝트 루트와 저장소 접근권한으로 실행되면 같은 등록·우편함을 사용한다. 이것은 모든 프로필을 자동 탐색·연결한다는 뜻이 아니며 실제 다중 프로필 통신은 확인 못 함이다. 근거: `internal/cli/session.go:260-271`, `internal/cli/mcp_session_msg.go:40-56`.

Codex wiring은 프로젝트 `.codex/config.toml`에 `[mcp_servers.moai]`와 `moai mcp-server` 등록을 제공하고 `.codex/hooks.json`도 생성한다. Claude 프로필 관리에는 `CLAUDE_CONFIG_DIR`를 설정하는 별도 코드가 있다. 이 기능들은 브로커 발견 범위와 별개다. 근거: `internal/codexwiring/wire.go:28-44`, `internal/codexwiring/configtoml.go:8-22`, `internal/profile/profile.go:217-234`.

## 5. 원격·다중 머신 및 신뢰 모델

브로커의 확인된 전달 경로는 로컬 파일과 MCP stdio다. host 필드는 기록되지만 네트워크 주소로 사용되지 않는다. SSH relay, 원격 등록, 원격 wakeup, 머신 간 인증 handshake는 이 경로에서 **확인 못 함**이다. 웹 콘솔의 TCP listener도 `127.0.0.1`에만 bind하므로 원격 메시징 서버의 근거가 아니다. 근거: `internal/sessionmsg/agent.go:124-137`, `internal/sessionmsg/store.go:262-277`, `internal/cli/mcp_server.go:84-97`, `internal/web/server.go:42-44,177-189`.

Claude용 `isolatePeerMachines` 설정을 전달하는 코드는 존재한다. 그러나 이를 MoAI가 자체 원격 transport를 구현했다는 근거로 사용할 수 없다. 근거: `internal/cli/crosssession_settings.go:61-65`.

신뢰 경계는 주로 로컬 프로세스·파일 접근이다. 송신자는 요청의 `from_agent_id`로 지정하고, 코드가 검사하는 것은 ID 형식과 등록 존재 여부다. poll 역시 caller가 지정한 `agent_id`를 받는다. **정적 해석:** 이 API 자체에는 호출자와 agent ID를 묶는 인증 증명이 없으므로, 공통 저장소에 접근하는 신뢰된 협력자를 전제해야 한다. ID 정규식은 경로 순회를 방지하는 장치이지 인증이 아니다. 근거: `internal/cli/mcp_session_msg.go:108-115,133-136`, `internal/sessionmsg/store.go:206-228,289-305`, `internal/sessionmsg/ids.go:19-28,50-55`.

## 6. 범위 제어

프로젝트 루트 아래 저장소 분리가 실질적인 발견·전달 범위다. 도구에는 임의 `project_root` 인자가 없으며 서버 환경과 CWD가 이를 정한다. 그룹 ACL, 수신자별 허용 목록, 명시적 workspace membership 검사는 해당 네 도구에서 확인 못 함이다. `contextId`와 `taskId`는 전달되는 메타데이터이며 접근 제어 조건으로 사용하지 않는다. 근거: `internal/cli/mcp_session_msg.go:40-56,99-145`, `internal/sessionmsg/store.go:243-264`.

**주의할 차이:** 칸반 보드는 common Git directory를 이용해 primary checkout을 찾지만 메시징 브로커는 CWD fallback이다. 따라서 worktree마다 CWD가 다르면 mailbox도 갈릴 수 있다. 동일 저장소 worktree 전체가 자동으로 하나의 메시징 공간이 된다고 가정하면 안 된다. 근거: `internal/kanban/board.go:74-94`, `internal/cli/session.go:264-271`, `internal/cli/mcp_session_msg.go:40-56`.

## 7. 지속 기록과 사람용 UI

| 기능 | 실제 저장·표면 | 제약 |
|---|---|---|
| 메시지 | agent JSON, pending/claimed envelope JSON | ACK로 삭제; TTL 만료 메시지도 poll 때 삭제 |
| 칸반 보드 | primary checkout의 `.moai/state/kanban-board/board.json` | card에 SPEC ID, column, holder, 마지막 전환 시각 저장 |
| 작업 ledger | state directory의 `task-ledger.md`에 append | phase/action/detail 기록이며 채널 스레드 모델은 아님 |
| 사람용 화면 | 로컬 웹 `/kanban`, `/monitor`, `/todo`, `/settings`, `/events` 등 | 이 라우팅 및 메시지 코드에서 mailbox 채널·스레드 UI는 확인 못 함 |

근거: `internal/sessionmsg/store.go:100-110,314-373`; `internal/kanban/board.go:27-32,56-94`; `internal/session/store.go:357-376`, `internal/session/task_ledger.go:7-22`; `internal/web/app.go:157-196`.

**해석:** `contextId`를 붙이는 것만으로 채널·스레드 이력이 생기지 않는다. 단일 수신자 enqueue API와 ACK 삭제 구현은 작업 전달용 mailbox이며, 사람과 에이전트가 함께 보는 영구 N:N 기록을 충족하지 않는다. 별도의 정본 이벤트 저장소가 필요하다. 근거: `internal/cli/mcp_server.go:460-480`, `internal/sessionmsg/store.go:359-373`.

## 8. 동시성·충돌·재시도·루프

메시징은 수신자별 advisory lock, 등록 전체 lock, 같은 프로세스의 경로별 mutex를 조합한다. Unix는 nonblocking `flock`, Windows는 `LockFileEx`이며 파일 쓰기는 같은 디렉터리의 임시 파일과 atomic replace다. lock 획득 재시도는 5ms 기반, 최대 50ms 지수 backoff와 jitter를 사용한다. 근거: `internal/sessionmsg/lock.go:12-23,32-48,69-113`, `internal/sessionmsg/lock_unix.go:34-65`, `internal/sessionmsg/lock_windows.go:31-75`, `internal/sessionmsg/store.go:113-147`.

```go
ipm.Lock()
defer ipm.Unlock()
// ...
deadline := time.Now().Add(timeout)
```

발췌: `internal/sessionmsg/lock.go:83-93`. **2초는 OS lock 획득 루프의 제한**이다. 위 mutex 대기와 lock 내부 작업은 그 시간 예산 밖이다. 따라서 모든 호출이 2초 안에 종료되거나 무한 대기가 불가능하다고 단정할 수 없다. 실제 hang은 재현하지 않았다. 또한 heartbeat 갱신은 mailbox lock 밖에서 수행해 중첩 lock을 피한다. 근거: `internal/sessionmsg/store.go:26-29,269-277,420-426`.

전달은 at-least-once다. claim은 10분 후 재전달 가능 상태로 돌아가며 메시지는 24시간 만료, poll batch는 16개다. sweep은 poll 시 실행되고 별도 상시 청소기가 아니다. FIFO는 `sentAt` 정렬 및 message ID tie-breaker로 구현된다. ACK는 claimed 또는 pending의 첫 존재 파일을 지운다. 근거: `internal/config/defaults.go:406-417`, `internal/sessionmsg/store.go:314-413,465-475`.

등록은 kind+name에 대해 멱등적이지만 send는 매번 새 무작위 ID를 만든다. 처리 측 중복 제거, end-to-end exactly-once, 업무별 idempotency key는 이 API에서 확인 못 함이다. claim 이동도 “새 파일 쓰기 후 이전 파일 삭제” 두 단계이므로 프로세스 충돌까지 포함한 다중 파일 트랜잭션으로 해석하면 안 된다. 근거: `internal/sessionmsg/agent.go:93-105`, `internal/sessionmsg/store.go:175-180,230-234,401-411`.

메시지 자동 회신이나 hop-count 루프 차단은 확인 못 함이다. 도구 설명에 “짧은 사실만 보내고 상태 변경 지시를 보내지 말라”는 규율이 있으나 이는 실행 시 강제되는 내용 검증이 아니다. 테스트의 `pollUntilDrained` 종료 로직은 테스트 helper이며 프로덕션 수신 데몬으로 오인하면 안 된다. 근거: `internal/cli/mcp_session_msg.go:24-28,99-115`, `internal/sessionmsg/stoprule_test.go:10-25,48-66`.

공동 상태는 더 강한 경계를 사용한다. 칸반은 lead 역할을 검사하고 보드 전체 read-modify-write를 잠근다. 병합 구간에는 session/PID를 기록한 integration lock이 있으며, 생존 여부를 판정 못 하면 live로 취급한다. 이는 협조적 보호이며 파일 쓰기 권한을 차단하지 않는다. 임의 Markdown 문서의 동시 편집, CRDT/OT 병합, 충돌 없는 공동 문서 편집기는 조사한 경로에서 확인 못 함이다. 근거: `internal/kanban/board_store.go:100-114,137-164`, `internal/kanban/board_lock.go:68-95`, `internal/kanban/integration_lock.go:13-26,65-106`.

## 9. 오케스트레이션 모델과 별도 런타임

Factory에는 lead와 번호가 붙은 lane, `moai cc -f [N]` / `moai glm -f [N]` 진입이 있다. launcher는 run ID와 lead 주소 등을 환경변수로 전달하고 SessionStart 훅은 세션 자신의 ID에 역할·lane·card 기록을 붙인다. 칸반 board 변경은 lead 역할을 요구한다. 근거: `internal/cli/factory.go:3-12,247-267`, `internal/hook/session_start_record.go:58-94,109-129`, `internal/kanban/board_store.go:100-114`.

`FactoryLeaderSocketPath`로 환경변수를 채우는 것 자체는 socket bind·수신기의 구현 증거가 아니다. 해당 코드도 conventional address라고 설명하며, 이 주소를 서비스하는 MoAI 자체 listener는 확인 못 함이다. 근거: `internal/cli/factory.go:256-260`.

메시징 네 도구 자체에는 task DAG 실행기나 scheduler가 없다. card 상태와 lead gate는 존재하지만 이를 범용 DAG 엔진으로 단정할 근거는 확인 못 함이다. Codex task 경로에는 새 app-server 프로세스와 thread를 관리하는 실행 계층이 별도로 존재한다. 근거: `internal/cli/mcp_session_msg.go:63-145`, `internal/kanban/board.go:56-72`, `internal/cli/mcp_codex.go:395-412,575-630`.

**INTENT 관점의 구분:** 프로젝트 MCP 도구만 붙이는 방식은 기존 Claude/Codex CLI 안에서 사용할 수 있는 보조 서버 형태다. 그러나 Factory launcher와 app-server 작업 소유 계층까지 그대로 채택하면 별도 진입점·실행 관리가 생긴다. 사용자가 금지한 별도 런타임을 피하려면 이들을 메시징의 필수 조건으로 삼지 않아야 한다. 근거: 이 저장소 `INTENT.md:11-15`; 위 MCP·Factory·app-server 코드.

## 10. INTENT 목표에 대한 시사점

아래는 구현 완료 판정이 아니라 코드에 근거한 설계 제안이다.

1. **차용: 프로필 저장소와 메시징 주소 공간을 분리한다.** MoAI의 프로젝트 로컬 broker는 CONFIG_DIR에 mailbox를 종속시키지 않는다. 다만 CWD 대신 합의한 project/workspace ID로 scope를 고정하고, 표시 이름과 실제 세션 인스턴스 ID를 분리하는 편이 목적에 맞다. 근거: `internal/cli/session.go:264-271`, `internal/sessionmsg/agent.go:93-105`; 목표 `INTENT.md:11-14`.
2. **차용: 전달 상태와 업무 완료 상태를 분리한다.** claim/ACK/TTL은 처리 실패 뒤 재수신에 유용하다. 여기에 업무별 idempotency key와 처리 결과 기록을 추가해야 중복 실행을 제어할 수 있다. 근거: `internal/sessionmsg/store.go:314-413`; 목표 `INTENT.md:5,19-21`.
3. **보완 필수: wakeup은 별도 검증 항목이다.** 파일 기록 성공이나 MCP send 응답을 상대 세션 실행 개시로 표현하지 않는다. Claude와 Codex 각각 기존 세션을 깨울 수 있는 host 접점을 입증해야 한다. MoAI의 Codex app-server 신규 실행은 대체 증거가 아니다. 근거: `internal/cli/mcp_server.go:462-477`, `internal/cli/mcp_codex.go:395-412,607-630`; 목표 `INTENT.md:5,11-15`.
4. **피할 것: ACK로 삭제되는 mailbox를 영구 채널 기록으로 재사용한다.** channel/thread/message identity와 참여자·이력 보존을 갖춘 정본을 별도로 두고, mailbox에는 기록 참조와 전달 상태를 보관하는 설계를 검토한다. 근거: `internal/sessionmsg/envelope.go:56-79`, `internal/sessionmsg/store.go:359-373`; 목표 `INTENT.md:17-21`.
5. **차용하되 축소 적용: 공동 문서의 정본 갱신자를 명시한다.** 보드의 전체 read-modify-write lock 및 역할 검사를 참고해 변경 제안을 모으고 정본 반영을 직렬화할 수 있다. arbitrary 파일 편집을 advisory lock만으로 막을 수 있다고 가정하지 않으며, 획득·작업 시간 예산, stale 소유자 확인, 버전 충돌 판정까지 따로 설계한다. 근거: `internal/kanban/board_store.go:137-164`, `internal/kanban/integration_lock.go:22-26,84-106`, `internal/sessionmsg/lock.go:83-113`; 목표 `INTENT.md:21`.
6. **맞지 않는 부분: 메시지로 업무 지시를 금지하는 운영 규율.** MoAI는 peer 메시지를 사실 전달로 제한하고 상태 변경 지시를 금지한다. INTENT는 업무 지시·보고를 원하므로, 이 규율을 그대로 가져오면 목적과 충돌한다. 사용자 승인과 peer 요청을 구분하는 원칙은 남기되 위임 권한을 별도 정의해야 한다. 근거: `.claude/rules/moai/workflow/cross-session-messaging.md:129-137`, `internal/cli/mcp_session_msg.go:24-28`; 목표 `INTENT.md:5`.
7. **범위 밖으로 남는 부분: 원격 transport와 인증.** 로컬 파일 공유가 SSH 연결·분산 lock·머신 간 신뢰를 해결한다고 가정하지 않는다. 또한 Factory launcher를 필수화하지 않고 기존 CLI에 붙는 최소 도구 경로만 취하는 것이 현재 제약에 맞다. 근거: `internal/sessionmsg/store.go:262-277`, `internal/cli/factory.go:3-12`, `internal/cli/mcp_server.go:84-97`; 목표 `INTENT.md:13-15`.

## 근거 파일 목록

모든 MoAI 경로의 루트: `/tmp/xsm-refs/moai-adk`.

- `cmd/moai/main.go:13-22`, `go.mod:1-35` — 실행 진입점·언어·의존성.
- `internal/cli/mcp_server.go:84-105,430-481` — MCP 서버와 메시징 도구 등록.
- `internal/cli/mcp_session_msg.go:24-145` — scope 계산·호출자 인자·송수신 handler.
- `internal/sessionmsg/agent.go:27-191` — agent schema, 등록, online, heartbeat.
- `internal/sessionmsg/envelope.go:1-9,31-80,90-159` — envelope·검증.
- `internal/sessionmsg/store.go:20-29,100-147,175-180,206-277,289-426,465-475` — 저장·송신·claim·ACK·만료.
- `internal/sessionmsg/ids.go:19-28,50-55` — 경로에 쓰이는 ID 검증.
- `internal/sessionmsg/lock.go:12-113`, `lock_unix.go:34-65`, `lock_windows.go:31-100` — 잠금과 시간 제한.
- `internal/sessionmsg/stoprule_test.go:10-66` — 테스트 전용 poll 종료 helper.
- `internal/config/defaults.go:406-433` — TTL·batch·크기 제한.
- `internal/session/registry.go:35-39,155-200,288-304` — 별도 세션 registry.
- `internal/hook/session_start.go:1316-1347` — 세션 등록 훅.
- `internal/cli/session.go:260-271` — 프로젝트 루트 결정.
- `internal/codexwiring/wire.go:28-44`, `configtoml.go:8-22` — Codex 연결 설정.
- `internal/profile/profile.go:217-234` — Claude CONFIG_DIR 설정.
- `internal/cli/crosssession_settings.go:47-91` — Claude 네이티브 메시징 설정 전달.
- `internal/cli/mcp_codex.go:395-447,575-630`, `codex_task.go:225-252` — Codex app-server 프로세스와 thread.
- `internal/cli/factory.go:3-12,247-267` — Factory launcher·run 환경.
- `internal/hook/session_start_record.go:58-94,109-129` — 역할·lane 세션 기록.
- `internal/kanban/board.go:27-32,56-129`, `board_store.go:100-164` — 보드 scope·schema·쓰기.
- `internal/kanban/board_lock.go:68-95`, `integration_lock.go:13-26,65-106` — 보드 잠금·통합 구간 소유.
- `internal/session/store.go:357-376`, `task_ledger.go:7-22` — Markdown ledger.
- `internal/web/server.go:42-44,177-189`, `app.go:157-196` — 로컬 웹 서버·UI 라우트.
- `.claude/rules/moai/workflow/cross-session-messaging.md:125-139` — 운영 규약. 코드 구현·실행 증거와 구분해 사용.
- 조사 대상 프로젝트 `/Users/jaesolshin/Documents/GitHub/cross-session-messaging/INTENT.md:5-21` — 비교 목표와 제약.
