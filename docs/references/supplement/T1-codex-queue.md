# T1 — Codex 큐 경로 정밀 추적과 S2 실험 설계

Codex 0.155.1에는 **별도 오케스트레이션 진입점 없이, 별도 `codex queue` 프로세스가 공유 SQLite를 통해 실행 중인 Embedded TUI의 로드된 idle thread를 깨우는 구현 경로가 있다**. 큐 파일은 기본 `$CODEX_HOME/queue_1.sqlite`이며 `state_5.sqlite`와 별개다. 그러나 HOME 자동 발견, 에이전트 발신 권한 표시, busy 턴 중간 개입은 이 경로가 제공하지 않고 Interrupted 상태는 자동 깨우지 않는다. 아래 결론은 원본 코드 확인이며, 실제 두 프로세스의 전달·응답 성공은 **확인 못 함**이다. 근거: `C/tui/src/session_queue_commands.rs:27-75`, `C/state/src/sqlite.rs:32-33,173-175`, `C/ext/queue/src/service.rs:89-96,149-178,215-239,439-481,549-563`.

| 항목 ID | 결론 | 근거 |
|---|---|---|
| R1-01 | 해소: 제어 endpoint는 `$CODEX_HOME/app-server-control/app-server-control.sock`; IDE `ipc/ipc.sock`과 다르다 | `C/app-server-transport/src/transport/mod.rs:56-72`; `C/tui/src/ide_context/ipc.rs:179-180,681-714` |
| R1-02 | 구현 경로 해소, 실측 보류: 공유 큐 watcher는 Embedded에도 설치되며 로드된 thread만 깨운다 | `C/app-server/src/message_processor.rs:296-307,323-352`; `C/app-server/src/extensions.rs:80-82`; `C/ext/queue/src/lib.rs:13-20`; `C/ext/queue/src/service.rs:149-178,215-239` |
| R1-03 | 해소: TUI의 데몬 재사용은 조건부이고 `agents`는 데몬을 시작할 수 있어 읽기 전용 발견 명령이 아니다 | `C/tui/src/startup_orchestration.rs:153-162,195-208`; `C/tui/src/lib.rs:510-526,927-998`; `C/cli/src/main.rs:2733-2746` |
| R1-04 | 해소: UUID만으로 다른 HOME을 찾지 않는다. Embedded 대상에는 외부 주소 대신 HOME 및 실제 SQLite 위치를 식별해야 한다 | `C/tui/src/session_queue_commands.rs:32-35,79-118`; `C/core/src/config/mod.rs:3989-4001` |
| R1-09 | 부분 해소: 큐는 UserInput이며 origin 필드가 없다. 훅·본문 규약으로 구분 문맥을 줄 수 있으나 인증된 peer 권한 분리가 되지는 않는다 | `C/app-server-protocol/src/protocol/v2/thread.rs:878-885`; `C/core/src/hook_runtime.rs:662-697`; `C/hooks/src/schema.rs:428-448` |
| R1-10 | 해소: proxy는 기존 서버의 중계, daemon은 별도 소유 서버, 인자 없는 remote-control은 새 foreground 서버다 | `C/cli/src/main.rs:1427-1435`; `C/app-server-daemon/src/lib.rs:301-315`; `C/cli/src/remote_control_cmd.rs:64-93,110-143` |
| R2-10 | 미해소인 실측을 명시: `agents`로 기존 Embedded 세션에 도달했다는 증거 없음. 이번 작업은 S2 절차만 설계했고 실행하지 않았다 | `docs/reviews/R2-discovery-scope-security.md` 항목 10; 본 문서 6절의 미실행 실험 계획 |

## 1. 조사 기준과 범위

- `C`는 `/tmp/xsm-refs/codex/codex-rs`다. 모든 C 경로의 줄 번호는 tag `rust-v0.155.1`, 커밋 `be2951ea34f0d295ed0becf97079f92fa5f6950e`의 원본이다. 이번 실행에서 `git -C /tmp/xsm-refs/codex rev-parse HEAD`와 `command codex --version`을 조회했고 각각 해당 커밋, `codex-cli 0.155.1`이었다. 바이너리와 공개 소스의 바이트 동일성은 확인 못 함.
- `INTENT.md`, `docs/reviews/README.md`, R1·R2 원문을 읽었다. 사용자 세션·소켓에 메시지를 보내지 않았고, 사용자 설정을 변경하지 않았다. 실제 Codex 명령은 버전과 `queue --help` 조회뿐이다. 아래 실험 명령은 **실행하지 않은 제안**이다.
- “확인”은 원본의 분기·자료구조를 확인했다는 의미다. 코드에서 예상한 외부 프로세스 동작은 “예상”, 설계 선택은 “제안”으로 구분한다.

## 2. CLI부터 수신 턴까지

### 2.1 송신과 저장

1. `codex queue --thread <UUID 또는 정확한 이름> --message <TEXT>`는 `find_codex_home()`으로 **호출 환경의 HOME**을 선택한다. UUID는 그대로 사용하고 이름은 선택한 서버/HOME의 active session에서 찾는다. 별도의 전체 HOME 스캔은 이 경로에 없다. 근거: `C/tui/src/session_queue_commands.rs:27-35,79-105`; `C/utils/home-dir/src/lib.rs:5-60`.
2. session command는 기존 daemon에 연결하거나 embedded app-server를 시작한다. 명시적 `--remote`가 있으면 그 서버를 사용한다. CLI override 등으로 Embedded가 선택되었는데 기본 daemon 소켓이 살아 있으면 queue는 오류를 반환한다. 따라서 “항상 새 Embedded를 띄워 큐에 기록”도 틀리다. 근거: `C/tui/src/session_archive_commands.rs:222-266`; `C/tui/src/session_queue_commands.rs:36-44`.
3. CLI가 UUIDv7 `client_user_message_id`를 생성하고 `thread/queue/add`에 `thread_id`, Text 입력, 그 ID를 보낸다. 수신 프로세서가 `TurnInput::UserInput { content, client_id }`로 바꿔 enqueue한다. 근거: `C/tui/src/session_queue_commands.rs:47,106-118`; `C/app-server/src/request_processors/thread_queue_processor.rs:72-89,302-309`.
4. 수신 서버에 thread가 로드되지 않아도 `thread_store.read_thread`로 저장된 thread를 검증하면 enqueue할 수 있다. 로드된 ephemeral, 저장된 archived, 미로드 ThreadSpawn subagent 등은 거부한다. 로드된 subagent에는 `can_accept_direct_input` 검사가 적용된다. **enqueue가 thread를 resume/load하는 것은 아니다.** 근거: `C/app-server/src/request_processors/thread_queue_processor.rs:240-298`.
5. app-server는 process-scoped thread store를 사용하며 Local + state DB가 있을 때만 `LocalQueueStore`를 설치한다. InMemory 또는 state DB 없음은 queue unavailable이다. service는 UserInput 검사·첨부 snapshot·JSON 직렬화를 거쳐 SQLite에 넣고 변경 이벤트 및 `wake_if_loaded`를 호출한다. 근거: `C/app-server/src/message_processor.rs:296-307`; `C/ext/queue/src/service.rs:265-279,493-531`.

### 2.2 파일, 스키마, 변경 검출

| 구분 | 원본에서 확인한 내용 | 근거 |
|---|---|---|
| HOME | `CODEX_HOME`이 있으면 존재하는 디렉터리를 canonicalize, 없으면 `~/.codex` | `C/utils/home-dir/src/lib.rs:13-60` |
| SQLite 디렉터리 | 최종 로드된 `sqlite_home` 설정 → `CODEX_SQLITE_HOME` → Codex HOME 순. 환경변수 상대경로는 resolved cwd 기준 | `C/core/src/config/mod.rs:253-262,3989-4001`; `C/state/src/lib.rs:124` |
| 큐 파일 | `<실제 sqlite_home>/queue_1.sqlite` | `C/state/src/sqlite.rs:32,89-93,173-175`; `C/state/src/runtime.rs:186-201` |
| thread 메타데이터 | `<실제 sqlite_home>/state_5.sqlite`; `threads.id`, `rollout_path`, cwd, source, archived 등 | `C/state/src/sqlite.rs:33,51-55,140-142`; `C/state/migrations/0001_threads.sql:1-18` |
| 큐 행 | `queued_items(id TEXT PK, thread_id TEXT, payload_json TEXT, queue_order INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)`, 모든 필드 NOT NULL. `(thread_id, queue_order)` unique | `C/state/queue_migrations/0001_queued_items.sql:1-11` |
| 큐 개정 | `queued_thread_revisions(revision INTEGER PK AUTOINCREMENT, thread_id TEXT UNIQUE NOT NULL)`; insert/update/delete trigger가 개정 증가 | `C/state/queue_migrations/0002_queued_thread_revisions.sql:1-34` |
| 관찰 | 전용 연결의 `PRAGMA data_version`, 이후 로드된 ID 집합에 한정한 `revision > last_revision` 조회 | `C/state/src/runtime/queued_items.rs:33-74` |

`enqueue`는 thread별 MAX(queue_order)+1로 삽입하고 저장 한도를 검사한다. 클라이언트 메시지 ID는 payload 안에 있고 DB의 중복 방지 unique key가 아니다. CLI를 재실행하면 새 client ID와 새 queue ID를 만든다. 따라서 응답 유실 뒤 CLI 재시도를 “같은 메시지의 멱등 재전송”으로 취급하면 안 된다. 근거: `C/state/src/runtime/queued_items.rs:77-104`; `C/tui/src/session_queue_commands.rs:47`; 위 스키마.

### 2.3 watcher와 wakeup

`extensions.rs`가 queue extension을 등록하면 lifecycle contributor와 watcher task가 함께 설치된다. watcher는 10초 interval로 DB 변경을 확인하고 ThreadManager에 **현재 로드된 ID**만 조회한다. 새로 로드된 thread는 revision 0부터 확인하며 thread별 dispatch task를 나누므로 하나의 실패가 다른 큐를 직접 막지 않는다. 이 interval은 배달 지연 상한이나 10초 SLA가 아니다. 근거: `C/app-server/src/extensions.rs:80-82`; `C/ext/queue/src/lib.rs:13-20`; `C/ext/queue/src/service.rs:89-96,115-189,193-242`.

호출 사슬은 다음과 같다. 같은 프로세스의 enqueue에서는 watcher를 기다리지 않고 `wake_if_loaded`부터 시도한다.

```text
외부 SQLite 변경 → watch_external_messages → wake_if_loaded
→ emit_thread_idle_lifecycle_if_idle(Completed)
→ QueuedItemService::on_thread_idle
→ dispatch_if_idle → CodexThread::start_turn_if_idle
→ TurnInputMode::StartIfIdle → core start_if_idle → RegularTask
```

근거: `C/ext/queue/src/service.rs:215-239,439-481,549-563`; `C/core/src/tasks/lifecycle.rs:43-67`; `C/core/src/codex_thread.rs:330-351`; `C/core/src/session/turn_input.rs:202-220,388-465`.

core는 active turn을 원자적으로 확인·예약하고, 이미 busy이면 NotIdle, 서버 admission 불가이면 ServerDraining, 우선 처리 mailbox가 있으면 PendingTriggerTurn을 반환한다. 설정 적용 단계에는 PlanMode 거부 분기도 있지만, 이 이름만 보고 모든 Plan 모드 큐가 차단된다고 일반화하지 않는다. Started 뒤에만 큐 행을 삭제하며 `turn_trigger="queue"`를 기록한다. **Started는 모델 성공·응답 전달이 아니라 턴 시작 승인**이다. 근거: `C/core/src/session/turn_input.rs:370-428`; `C/ext/queue/src/service.rs:439-467`.

| 대상 상태 | 예상 동작과 코드상 조건 |
|---|---|
| 로드된 idle | active turn 없음, interrupted 아님, 선행 mailbox·admission 제약 없음이면 시작 가능. Embedded와 daemon 모두 위 extension 설치 경로에 해당한다 |
| busy | watcher는 Running에서 종료하고 idle lifecycle도 active turn이 있으면 반환한다. 현재 턴을 steer하지 않고 큐에 남는다. 완료 후 idle contributor가 다음 입력을 시도한다 |
| approval 대기 | 명령 승인은 `rx_approve.await`로 현재 턴 안에서 대기한다. 이때 active turn이 남아 있으므로 새 큐 입력으로 승인을 우회하거나 답하지 못한다. 승인 후 턴 완료 시 재평가된다 |
| Interrupted | watcher, wake_if_loaded, on_thread_idle 모두 자동 진행을 억제한다. 명시적 queue start API와 자동 watcher는 구분해야 한다 |
| Failed 뒤 idle | Interrupted와 달리 contributor의 제외 조건이 아니므로 다음 큐 실행 가능. 원본 테스트가 이 차이를 명시한다 |
| 저장만 있고 미로드 | enqueue 가능하더라도 수신 owner가 없으므로 자동 load되지 않는다. 이후 로드될 때 watcher의 newly-loaded 경로가 후보가 된다 |

근거: `C/ext/queue/src/service.rs:215-239,367-402,472-481,549-563`; `C/core/src/tasks/lifecycle.rs:43-67`; `C/core/src/tasks/mod.rs:857`; `C/core/src/session/mod.rs:2814-2834`; `C/ext/queue/tests/queue_service.rs:479-513`.

동일 thread를 서로 다른 프로세스가 동시에 로드한 경우의 단일 소비 보장은 확인 못 함이다. dispatch lock은 service 내부 메모리 mutex이며, DB 행을 읽고 core Started를 받은 **뒤** 삭제한다. 두 owner가 같은 행을 읽고 실행할 가능성을 배제하는 원자적 claim은 이 경로에서 보이지 않는다. 이는 코드 기반 경합 위험 추론이지 중복 실행 재현 결과가 아니다. 근거: `C/ext/queue/src/service.rs:247-262,413-448`; `C/state/src/runtime/queued_items.rs:107-125,152-160`.

## 3. 다른 CODEX_HOME과 대상 발견

**기본값 그대로 분리된 HOME끼리는 UUID만으로 전달되지 않는다.** 송신자는 자신의 HOME/서버에서 thread 존재를 검증한다. 대상 A를 알고 있다면 송신 프로세스의 `CODEX_HOME`을 A로 지정하고 실제 SQLite 설정도 맞추거나, A를 소유하는 외부 app-server endpoint에 `--remote`로 연결하는 방법이 코드상 가능하다. Embedded에 외부 endpoint가 있다고 가정해서는 안 된다. 근거: `C/tui/src/session_queue_commands.rs:32-44,86-118`; `C/app-server/src/request_processors/thread_queue_processor.rs:240-279`; `C/tui/src/lib.rs:510-526,927-954`.

다른 HOME이 같은 `CODEX_SQLITE_HOME`을 선택하는 구성 자체는 가능하지만, 큐만 공유한다고 thread 존재 확인·rollout 경로·설정·단일 owner 문제가 모두 해결되는 것은 아니다. 일반 해법으로 채택하기 전 별도 검증이 필요하다. 근거: `C/core/src/config/mod.rs:3989-4001`; `C/app-server/src/message_processor.rs:296-307`; `C/state/migrations/0001_threads.sql:1-18`.

| 외부 식별 자료 | 얻는 정보 | 한계와 근거 |
|---|---|---|
| `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-…` | 파일 위치로 HOME 후보, 첫 SessionMeta의 thread ID·cwd·source·CLI version | 생성 위치 근거 `C/rollout/src/recorder.rs:1700-1721`; 메타데이터 `:915-943`. 복사·이관된 경로는 현재 실행 소유자의 증거가 아님 |
| `state_5.sqlite`의 threads | ID → rollout_path, cwd, source, archived | `C/state/migrations/0001_threads.sql:1-18`. state 파일 자체 위치는 sqlite_home일 뿐 HOME과 같지 않을 수 있음 |
| `session_index.jsonl` | thread ID·이름·갱신 시각 | `C/rollout/src/session_index.rs:21-29,235-236`. PID/생존/loaded 상태 없음 |
| `CODEX_THREAD_ID` | 현재 도구 자식 프로세스의 thread ID | `C/core/src/exec_env.rs:30-37`. HOME이나 다른 세션 목록을 담지 않음. `CODEX_SESSION_ID`는 별도의 root-session 정체성이라 queue ID 대신 사용하면 안 됨 (`:40-49`) |
| 제어 소켓/daemon recovery 경로 | 해당 HOME의 서버 접속 후보·복구 상태 위치 | `C/app-server-transport/src/transport/mod.rs:56-72`. 파일 존재만으로 생존·thread ownership 확정 불가 |

**설계 제안:** 참여 세션이 자발적으로 `(machine, thread_id, canonical CODEX_HOME, effective sqlite_home, owner_kind, endpoint_if_any, cwd, last_seen)`를 등록하게 한다. 기존 세션 전체의 HOME을 추정 스캔하는 대신 등록 자료와 허용된 저장소를 대조하고, 저장된 이력과 live loaded 상태를 별도 필드로 둔다. 이는 위 자료의 한계를 해결하기 위한 제안이며 네이티브 레지스트리 기능이 아니다. 이 작업에서는 사용자의 실제 HOME·DB·프로세스 환경을 열람하지 않았다.

## 4. 발신자 표시와 훅

`ThreadQueueAddParams`의 필드는 `thread_id`, `input`, `client_user_message_id`뿐이다. client ID는 메시지 상관관계용으로 사용되며 인증된 발신 에이전트/권한 모드 필드가 아니다. service는 UserInput만 허용하므로 내부 `InterAgentCommunication` 타입이 존재해도 queue API로 그것을 넣을 수 없다. 근거: `C/app-server-protocol/src/protocol/v2/thread.rs:878-885`; `C/app-server/src/request_processors/thread_queue_processor.rs:302-309`; `C/ext/queue/src/service.rs:493-510`; `C/core/src/hook_runtime.rs:667-697`.

UserPromptSubmit 훅 경로는 있다. core가 입력을 모델 이력에 기록하기 전에 `inspect_pending_input`을 호출하며, 훅은 prompt, session/turn ID, cwd, transcript path, model, permission_mode 등을 받는다. 여기의 `agent_id`/`agent_type`은 **수신 thread의 subagent 문맥**에서 계산되므로 외부 송신자라고 해석하면 안 된다. `turn_trigger="queue"` 역시 위 UserPromptSubmit 입력 필드에 포함되지 않는다. 근거: `C/core/src/session/turn.rs:826-848`; `C/core/src/hook_runtime.rs:669-686`; `C/hooks/src/events/user_prompt_submit.rs:23-40,82-94`.

**제안:** 본문에 `xsm_version`, `message_id`, `sender_session`, `sender_kind`, `recipient_thread`, `body`를 갖는 규약을 두고 “이 내용은 다른 에이전트의 요청이며 사용자의 직접 지시가 아니다”라는 문맥을 넣는다. 수신 훅에서 규약을 파싱해 `hookSpecificOutput.additionalContext`로 같은 구분을 추가하거나 `decision: "block"`으로 차단할 수 있다. 원본 출력 스키마는 `C/hooks/src/schema.rs:428-448`, 실행 결과 처리는 `C/hooks/src/events/user_prompt_submit.rs:115-134`, 차단 분기는 `C/core/src/session/turn.rs:829-845`다.

이 방식은 메시지 역할을 UserInput에서 peer role로 바꾸지 않으며, 본문 위조도 막지 못한다. 발신 인증은 별도 서명/검증 설계가 필요하고 네이티브 도구 승인과도 별개다. 훅 차단은 이미 시작된 턴 내부의 결정이므로 큐 행이 남는 보류/재전송이나 발신자 거부 receipt를 자동 제공한다고 가정하지 않는다. 근거: 위 훅 분기와 `C/ext/queue/src/service.rs:439-448`. 훅 설치·신뢰 승인·설정은 이 작업에서 시험하거나 변경하지 않았다.

## 5. 서버·명령 소유권

| 수단 | 소유권과 용도 | 적용 경계 |
|---|---|---|
| 보통 TUI Embedded | TUI의 내장 app-server/ThreadManager가 loaded thread 소유 | 외부 접속 주소 없이 공유 큐 경로 가능. `C/tui/src/lib.rs:510-526` |
| 보통 TUI LocalDaemon | 기존 default socket을 탐색해 daemon에 붙음; 실패하면 Embedded로 fallback | `-c`, profile, strict config, worktree, OSS, executor 설정 등으로 선택 달라짐. `C/tui/src/lib.rs:927-998`; `C/tui/src/startup_orchestration.rs:153-162,195-208` |
| `codex agents` | 대화형 agents overview. 로컬 모드에서 TTY 확인 후 daemon Start | 읽기 전용 글로벌 발견 API가 아님. `C/cli/src/main.rs:2733-2746` |
| `app-server daemon …` | HOME별 daemon 생명주기와 PID/settings 관리; 그 서버의 loaded thread 소유 | 기존 Embedded의 메모리 ThreadManager를 이전하는 기능으로 볼 근거 없음. `C/app-server-daemon/src/lib.rs:301-315`; `C/app-server/src/message_processor.rs:296-307,323-352` |
| `app-server proxy` | 기본 또는 명시 socket으로 stdio 중계 | 자체 thread 소유 서버를 새로 만들지 않음. `C/cli/src/main.rs:1427-1435` |
| 제어 socket | `$CODEX_HOME/app-server-control/app-server-control.sock` | IDE socket과 구분. `C/app-server-transport/src/transport/mod.rs:56-72` |
| `--remote` | 명시 endpoint를 소유 서버로 선택 | 외부 Embedded TUI를 자동으로 찾아 인수하는 옵션 아님. `C/tui/src/lib.rs:943-954` |
| `remote-control start / pair` | daemon 원격 제어 준비 / pairing 시작 | 기존 Embedded TUI 인수와 별개. `C/cli/src/remote_control_cmd.rs:77-93` |
| 인자 없는 `remote-control` | `/tmp/codex-rc-…/rc.sock`을 가진 새 foreground app-server | 기존 default daemon과도 다른 실행 주체. `C/cli/src/remote_control_cmd.rs:110-143` |
| `remote-control stop` | daemon Stop 호출 | 원격 접속만 끊는 무해한 진단으로 사용 금지. `C/cli/src/remote_control_cmd.rs:85-88` |

INTENT의 “별도 진입점 런타임 금지”와의 관계는 다음처럼 해석한다. **제안:** 사용자가 평소 `codex`를 실행하고 송신 도구가 단발 `codex queue`를 호출하는 Embedded 경로는 별도 오케스트레이션 런타임을 요구하지 않는다. 반면 모든 사용자를 새 daemon/remote 서버로 이주시키는 것을 필수 전제로 삼으면 요구사항 해석을 다시 합의해야 한다. 근거가 되는 요구는 `INTENT.md`의 Codex runtime 확장 절이며, 기술 경로는 2절이다.

## 6. S2 — 기존 사용자 세션에 영향 없는 실험 계획

**아래는 전부 미실행이다.** 부작용 “없음”은 기존 사용자 세션·설정에 영향이 없도록 격리한다는 뜻이며 전용 HOME/로그 생성, 테스트 프로세스와 로컬 모델 실행은 발생한다. 성공 지표를 enqueue, wakeup, model completion, application receipt 네 단계로 나눈다. 단순 `Queued message …` 출력은 첫 단계만 증명한다 (`C/tui/src/session_queue_commands.rs:73-75`; `C/ext/queue/src/service.rs:439-448`).

### 6.1 준비와 인증

1. 코디네이터가 `/tmp/xsm-s2-<unique>/` 아래 `home-a`, `home-b`, `workspace`, `evidence`를 생성한다. 전용 일반 셸에서 시작하여 `CODEX_HOME`, `CODEX_SQLITE_HOME`, `CODEX_PROFILE`, remote/exec-server 관련 변수가 상속되지 않도록 선택적으로 제거하고 **환경 전체를 로그에 출력하지 않는다**. HOME A/B는 사전에 생성해야 한다 (`C/utils/home-dir/src/lib.rs:9-10,25-49`). 기존 사용자의 `auth.json`, config, 세션 파일은 복사하지 않는다.
2. 인증 복사 없이 가능한 후보는 **인증을 요구하지 않는 전용 로컬 provider 또는 loopback mock Responses endpoint**다. OSS provider는 env_key/bearer 없음, `requires_openai_auth=false`로 구성된다 (`C/model-provider-info/src/lib.rs:671-690`). 소스의 queue 테스트도 mock server를 사용한다 (`C/ext/queue/tests/queue_service.rs:479-484,572-592`). 이는 로컬 TUI 전체 절차의 성공을 실측했다는 뜻은 아니다.
3. daemon 비교를 오염시키지 않도록 `--oss` 플래그 대신 A의 전용 `config.toml`에 로컬 provider/model을 구성한다. TUI의 `--oss`는 implicit daemon 재사용을 끄기 때문이다 (`C/tui/src/startup_orchestration.rs:153-162`). mock은 로컬 테스트 fixture에서 Responses SSE를 만들고, 모델 없이도 사용자 입력 수신과 턴 시작/완료를 결정적으로 기록하게 설계한다. mock 준비가 안 되면 로컬 모델을 사용하되 OpenAI 계정 로그인 화면이 나오면 중단한다. **기존 인증을 가져와 통과시키지 않는다.**
4. 전용 프로필에 `cli_auth_credentials_store="file"`, read-only sandbox, 테스트 작업 경로를 명시하는 방안을 검토해 keyring/실사용 credential 접근을 피한다. File 모드가 지정 HOME의 FileAuthStorage를 선택하는 근거는 `C/login/src/auth/storage.rs:173-189,511-527`, 설정 선택은 `C/core/src/config/mod.rs:4228-4231`이다. 인증 저장 방식까지 실제 격리되는지는 시작 전에 해당 빌드 설정/로그로 검증한다. 승인 대기 테스트만 전용 정책으로 별도 구성한다. mock/로컬 provider 설정 예제의 실제 기동 성공은 확인 못 함이다.

### 6.2 Embedded 기본 경로

1. A에 daemon/제어 socket이 없는 초기 상태를 파일과 자신이 만든 프로세스 목록으로 기록한다. `codex agents`는 실행하지 않는다. 전용 터미널에서 `CODEX_HOME="$s2_root/home-a" command codex -C "$s2_root/workspace"`를 실행한다. `$s2_root`는 이번 실험의 전용 경로 변수이며 쉘 HOME을 바꾸지 않는다.
2. 짧은 초기 턴을 완료해 영속 thread를 만든다. 해당 전용 session의 첫 rollout 메타데이터와 로그에서 thread ID 및 Embedded 선택을 확인한다. 다른 터미널에서 같은 ID를 `resume`하여 두 owner를 만들지 않는다.
3. 독립 송신 셸에서 `CODEX_HOME="$s2_root/home-a" command codex queue --thread "$s2_thread_a" --message 'XSM-S2-A-001: 도구 없이 ACK XSM-S2-A-001만 답하라'`를 실행한다. 전용 config 외의 `-c` override는 넣지 않는다.
4. 송신 exit code/queue ID/시각, 대상 queue 행 변화, 수신 TUI 또는 로그의 턴 시작·완료, 정확한 ACK를 기록한다. 최소 세 번의 watcher 주기와 startup 여유를 포함해 예컨대 45초를 관찰하되 이는 실험 timeout이지 제품 SLA가 아니다. 최초 소실된 메시지는 재전송하지 말고 원래 ID로 증거를 보존한다.
5. 전용 `queue_1.sqlite`는 읽기 전용 조회만 허용한다. 짧은 메시지는 행이 즉시 없어질 수 있으므로 행을 못 본 것만으로 미전달 판정하지 않고 receiver rollout/event와 함께 본다. 직접 DB insert/update는 금지한다.

예상: A의 원래 TUI가 살아 있고 thread가 idle이면 별도 sender의 Embedded 서버는 thread를 load하지 않고 enqueue하며, 원래 TUI watcher가 메시지를 가져간다. 원본 근거는 2.1~2.3절이다.

### 6.3 daemon 비교 및 교차 HOME

1. 별도의 깨끗한 A 실험 HOME에서 `command codex app-server daemon --help`로 해당 빌드의 start/status/stop 문법을 확인한 뒤 전용 daemon을 시작한다. 같은 A로 **override 없는** TUI를 새로 열고 LocalDaemon 선택을 로그로 입증한다. `agents`는 선택적으로 이 조건에서만 관찰하며 최초 daemon 시작 수단으로 섞지 않는다.
2. 6.2와 같은 메시지/지표로 재검증한다. daemon 실패 후 Embedded fallback이 있으면 daemon 실험 성공으로 세지 않는다. 명시적 `--remote unix://<전용 A 제어 소켓 절대경로>` 조건을 별도 행으로 기록할 수 있다.
3. B에 A와 다른 sqlite_home을 유지한 상태에서 `CODEX_HOME="$s2_root/home-b" command codex queue --thread "$s2_thread_a" --message 'XSM-S2-B-NEG'`를 시도한다. 예상은 thread not found이며 A의 큐/턴 변화가 없어야 한다.
4. 동일 송신 셸에서 target HOME만 A로 선택해 정상 전달되는지 비교한다. A가 custom sqlite_home을 쓴다면 해당 선택도 동일하게 해야 한다. daemon/remote 조건에서는 B HOME의 송신자가 명시적 A endpoint로 전달하는 조건을 추가해 “송신 HOME”과 “대상 서버”를 분리한다.
5. 서로 다른 HOME이 같은 sqlite_home을 공유하는 조건은 선택 실험으로 남긴다. A의 실제 사용자 데이터나 rollout을 B로 복제하지 않는다. thread 조회 성공, writer 중복, rollout 경로까지 검증되지 않으면 일반 지원으로 선언하지 않는다.

예상 분기의 근거: `C/tui/src/session_queue_commands.rs:32-44,86-118`; `C/tui/src/session_archive_commands.rs:247-266`; `C/tui/src/lib.rs:515-523,943-954`.

### 6.4 상태 행렬과 판정

| 조건 | 전용 세션의 준비 | 확인할 결과 |
|---|---|---|
| idle | 초기 턴 종료 | queue 성공, 자동 새 턴, ACK 각각 확인 |
| busy | mock SSE 완료를 지연시키거나 전용 긴 응답 | busy 동안 두 번째 턴/steer 없음, 완료 뒤 큐 실행 |
| approval | read-only 영역 밖의 **전용 실험 경로** 쓰기처럼 검토 가능한 무해한 동작의 승인을 보류 | 승인 대기 중 새 큐가 승인 응답으로 쓰이지 않음, 큐 유지; 거절/허용 뒤 실제 상태를 기록 |
| Interrupted | 전용 TUI에서만 진행 턴 중단 | 30초 이상 자동 시작 없음, 큐 유지. 전용 TUI의 명시적 사용자 재개/별도 queue start 검증 후의 동작은 별도 기록 |
| unloaded | 전용 TUI 종료 후 저장 thread에 enqueue | enqueue 성공만 있고 모델 실행 없음; 이후 단 하나의 owner로 resume할 때 결과 기록 |
| hook 규약 | 전용 프로필에서 검토·신뢰된 훅으로 경고 추가/차단 | receiver 훅 입력·추가 문맥·차단 확인; queued 행 삭제를 receipt로 오해하지 않음 |

각 행의 원본 예상은 2.3·4절에 연결된다. 결과 양식은 `case, CLI/version, argv, owner_kind, CODEX_HOME, sqlite_home, thread_id, message_id, queue_id, enqueue_at, turn_started_at, turn_completed_at, ACK, queue_remaining, verdict, evidence_paths`로 제안한다. 실제 에이전트 발신자를 별도 Claude/Codex 테스트 세션으로 만드는 것은 그 후의 확장 단계이며, 셸 queue 성공만으로 Claude↔Codex 왕복 검증 완료를 선언하지 않는다.

실험 종료 시 자신이 만든 TUI·daemon·mock만 종료하고 evidence를 보존한다. daemon Stop 대상 HOME을 반드시 전용 경로로 고정한다. 전용 경로 삭제는 코디네이터가 증거 보존 후 판단하며 기존 프로필이나 소켓을 정리 대상으로 삼지 않는다.

## 확인 못 한 것

- S2는 실행하지 않았다. Embedded/LocalDaemon에서의 실제 큐 전달, 지연, ACK, 교차 HOME 송신, 승인 UI 유지, Interrupted 이후 수동 재개를 확인 못 함.
- 인증 복사 없는 전용 TUI/daemon/mock 전체 조합의 기동과 credential 격리는 확인 못 함. 인증 불필요 provider의 소스 경로와 mock 기반 원본 테스트만 확인했고 테스트도 실행하지 않았다.
- watcher/queue 원본 테스트가 이 로컬 환경에서 통과하는지 확인 못 함. 특히 기존 `externally_changed_queues_dispatch_independently_and_retry_failed_wakes`는 별도 StateRuntime을 쓰는 테스트이며 OS의 독립 TUI 두 프로세스 실측으로 대체할 수 없다 (`C/ext/queue/tests/queue_service.rs:572-614`).
- 복수 loader, 장애 직전 Started와 큐 삭제 사이, 재시도 중복, 서버 재시작에서 정확히 한 번 처리·무손실 보장은 확인 못 함. 앞의 경합 가능성은 정적 추론이다.
- 실행 중인 사용자 세션의 실제 HOME/sqlite_home, daemon/Embedded 여부, 훅 활성화·신뢰 상태는 조회하지 않았다. 레지스트리의 live owner 확인 및 authenticated peer 권한은 추가 설계 과제다.
- 계획·ADR·기존 리뷰 파일은 수정하지 않았다. 이번 산출물의 적용과 S2 실행 판단은 코디네이터에 남겨 두었다.
