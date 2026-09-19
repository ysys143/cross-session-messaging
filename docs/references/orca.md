# Orca 소스 코드 분석

## 요약

Orca는 Stably AI가 개발한 오픈소스 오케스트레이션 플랫폼으로, 여러 AI 에이전트(Claude Code, Codex, Cursor, Grok 등)를 병렬로 실행하고 조정하기 위한 데스크톱 앱 및 CLI입니다. 특히 distributed task dispatch, federation (원격 머신 간 동기화), agent-hook 기반 에이전트 통합 등의 기능을 제공하여 cross-session messaging과 multi-agent coordination을 구현하고 있습니다.

---

## 1. 한 줄 정의 및 형태

**정의:** 여러 AI 에이전트를 병렬 worktree에서 실행하고, 메시지 기반의 coordinator-worker 오케스트레이션으로 조정하는 멀티-에이전트 오케스트레이터.

**형태:**
- **데스크톱 앱**: Electron 기반 (macOS, Windows, Linux 지원)
- **CLI**: `orca` 커맨드 라인 도구
- **모바일**: iOS/Android 모바일 컴패니언 앱 (별도)
- **클라우드 relay**: 모바일과 데스크톱 간 통신용 클라우드 서비스

**언어 및 주요 의존성:**
- **언어**: TypeScript (전체 코드베이스)
- **주요 의존성**:
  - SQLite 3 (Orchestration 데이터베이스)
  - zod (스키마 검증)
  - 자체 `src/sqlite/sync-database` 래퍼

**참고 파일**: `src/main/runtime/orca-runtime.ts`, `src/main/runtime/orchestration/coordinator.ts`

---

## 2. 에이전트 세션 발견/레지스트리

### 세션 등록 방식

Orca는 **agent-hook 메커니즘**을 통해 에이전트를 발견합니다:

- **Hook Target**: `AGENT_HOOK_TARGETS`로 정의된 15개 에이전트 지원 (`claude`, `codex`, `cursor`, `gemini`, `antigravity`, `amp`, `droid`, `command-code`, `grok`, `copilot`, `hermes`, `devin`, `kimi` 등)
- **Hook Protocol**: `ORCA_HOOK_PROTOCOL_VERSION = '1'` (형식화된 JSON POST 기반)
- **Transport**: `ORCA_HOOK_RAW_JSON_TRANSPORT = 'raw-json-v1'` (raw JSON 메타데이터 헤더 지원)

**참고 파일**: `src/shared/agent-hook-types.ts` (25-54행)

### 저장 위치

- **Running 세션**: 데이터베이스에 `RunRow` 기록 (`home_database`, `coordinator_handle`, `created_at` 등 저장)
- **Database 경로**: 공유되는 단일 SQLite DB (경로는 runtime에서 결정, 기본적으로 프로젝트 단위 또는 워크스페이스 단위로 관리)
- **Legacy 호환성**: `legacy` 플래그로 이전 형식 세션 지원

**참고 파일**: `src/main/runtime/orchestration/types.ts` (43-53행, RunRow 정의)

### 식별자

- **Run ID**: `run.id` (전역 고유)
- **Coordinator Handle**: 터미널 핸들 (예: `term_xxxxx` 형식)
- **Dispatch ID**: `dispatch_context.id` (시도별 고유)
- **Terminal Handle**: ORCA_TERMINAL_HANDLE 환경변수로 전달

**참고 파일**: `src/cli/handlers/orchestration/message-send-handler.ts` (77-85행)

### 생존 판정

- **Heartbeat**: `heartbeat` 메시지 타입으로 생존 신호 전송
- **Last Heartbeat**: `dispatch_context.last_heartbeat_at` 기록
- **Stale Detection**: Coordinator가 periodically stale dispatch 검증 (`warnStaleDispatches` 함수)

**참고 파일**: `src/main/runtime/orchestration/coordinator.ts` (159-166행)

---

## 3. 메시징/전달 (Transport)

### 메시지 타입

```typescript
MESSAGE_TYPES = [
  'status',        // 상태 보고
  'dispatch',      // 테스크 디스패치
  'worker_done',   // 워커 완료 (lifecycle)
  'merge_ready',   // 병합 준비
  'escalation',    // 블로킹 문제
  'handoff',       // 소유권 이전
  'decision_gate', // 의사결정 게이트
  'question',      // Blocking ask/reply
  'heartbeat'      // 생존 신호
]
```

**참고 파일**: `src/main/runtime/orchestration/types.ts` (2-12행)

### 메시지 스키마

```typescript
type MessageRow = {
  id: string                              // 메시지 ID
  run_id: string                          // Run 참조
  from_handle: string                     // 발신자 (터미널 핸들)
  to_handle: string                       // 수신자 (그룹 주소는 '@' 시작)
  subject: string
  body: string
  type: MessageType
  priority: 'normal' | 'high' | 'urgent'
  thread_id: string | null                // 스레드 그룹화
  payload: string | null                  // 구조화 데이터 (JSON)
  read: number                            // 읽음 플래그
  sequence: number                        // 순차 번호
  created_at: string                      // UTC timestamp
  delivered_at: string | null
  pointer_enter_pending?: number          // PTY 주입 대기
  pointer_pty_id?: string | null
}
```

**참고 파일**: `src/main/runtime/orchestration/types.ts` (233-253행)

### 전달 메커니즘

#### 3a) Mailbox-Pointer (로컬 터미널 주입)

**메커니즘**: PTY(pseudo-terminal)에 메시지를 직접 입력 문자열로 주입

**구조**:
- `OrchestrationMailboxPointerDelivery` 클래스가 idle 에이전트를 감지하면 메시지를 PTY에 inject
- `pointer_enter_delay_ms` (기본 500ms) 대기 후 입력
- `isAgentSettledForDelivery()` 체크로 safe injection 검증

**상태 머신**:
- `OrchestrationMailboxPointerState`: pending → parked → enter_pending → entered

**참고 파일**: `src/main/runtime/orchestration/mailbox-pointer-delivery.ts` (30-80행), `mailbox-pointer-state.ts`

#### 3b) Federation (원격 머신)

**메커니즘**: 원격 Orca runtime (워커 머신)과 home runtime 사이의 relay

**구조**:
- `FederatedDispatchRow`: 원격 dispatch 추적
- `FederationRelayItemRow`: 양방향 relay 큐 (`to_home`, `to_worker` 방향)
- `federation-sync.ts`: 주기적으로 원격에서 메시지 pull & acknowledge

**동기화**:
```
Home Runtime ←→ Worker Server (SSH/HTTP)
  ↓
FederationRelayItemRow 테이블
  - dispatch_id
  - direction: 'to_home' | 'to_worker'
  - sequence: 단조 증가 번호
  - message_id, payload
  - acked_at: 전송 확인
```

**Protocol**: Versioned capability probing (`ORCHESTRATION_FEDERATION_LIFECYCLE_SETTLEMENT_PROTOCOL_VERSION`)

**참고 파일**: `src/main/runtime/orchestration/types.ts` (177-231행), `federation-sync.ts` (40-100행)

### 수신자 발견 및 깨우기 (Wakeup/Invoke)

#### Claude Code / Codex에 대한 메서드

1. **Agent Hook 주입** (가장 일반적)
   - Orca가 `~/.claude/` 또는 `~/.codex/` 설정 디렉토리에 managed hook script 설치
   - Hook이 메시지 spool 디렉토리 정기 poll
   - 메시지 발견 시 에이전트 프로세스에 signal 또는 파일 trigger

> **정정(2026-09-19, T3):** 161줄의 "Hook이 메시지 spool 디렉토리 정기 poll"은 오류입니다. 실제 동작:
> 
> - **Hook의 역할 (push)**: stdin에서 JSON을 읽어 처리한 뒤, spool 파일에 이벤트를 append합니다. 근거: `/tmp/xsm-refs/orca/src/main/agent-hooks/hook-stdin-contract.ts:124-163` 의 `buildPosixHookSpoolLines()` 함수는 `$spool_file`에 `printf` 명령으로 기록을 추가합니다.
> - **Orca listener의 역할 (pull)**: 정기적으로 spool 디렉토리(`$ORCA_AGENT_HOOK_ENDPOINT/spool/pane-*.jsonl`)를 drain하여 완전한 JSON 라인을 읽고 처리합니다. 근거: `/tmp/xsm-refs/orca/src/shared/agent-hook-spool.ts:111-170` 의 `drainAgentHookSpool()` 함수는 `readdirSync(spoolDir)`로 파일을 나열하고 `readSpoolFile()`로 파싱합니다.
> - **메일 wakeup (PTY 주입)**: 별도 모듈 `OrchestrationMailboxPointerDelivery`가 담당하며, 훅의 spool 기록과는 무관합니다. 근거: `/tmp/xsm-refs/orca/src/main/runtime/orchestration/mailbox-pointer-delivery.ts:30-80`

2. **PTY 직접 주입** (mailbox-pointer)
   - 터미널에 문자 입력으로 메시지 전달
   - Claude Code/Codex의 구조화된 입력 처리 (예: JSON 형식 프롬프트)

3. **환경변수 기반 신호**
   - `ORCA_TERMINAL_HANDLE`, `ORCA_PANE_KEY` 환경변수로 identify
   - 구독 registry로 메시지 대기 중인 프로세스 깨우기

**참고 파일**: `src/shared/agent-hook-types.ts`, `src/main/runtime/orchestration/mailbox-pointer-delivery.ts`

#### 다른 에이전트에 대한 메서드

- **CLI 기반**: `AGENT_HOOK_TARGETS` 중 CLI 에이전트는 자체 stdin/stdout 또는 파일 기반 IPC 사용
- **Cursor, Grok 등**: 각 에이전트 고유의 hook endpoint 구현 (endpoint-file 기반 delivery contract)

**확인 못함**: 각 에이전트별 구체적인 hook 구현 (스스로 managed script 존재)

---

## 4. 이종 에이전트 지원

### 추상화 전략

**`AgentHookTarget` 열거형**:
```typescript
AGENT_HOOK_TARGETS = [
  'claude', 'openclaude', 'codex', 'gemini', 'antigravity', 'amp', 'cursor',
  'droid', 'command-code', 'grok', 'copilot', 'hermes', 'devin', 'kimi'
]
```

**설치 상태 추적**:
```typescript
type AgentHookInstallStatus = {
  agent: AgentHookTarget
  state: 'installed' | 'not_installed' | 'partial' | 'error' | 'skipped'
  configPath: string
  managedHooksPresent: boolean
  skipReason?: 'agent_disabled' | 'cli_not_found' | ...
}
```

**참고 파일**: `src/shared/agent-hook-types.ts` (6-39행)

### CONFIG_DIR 차이 처리

**현재 상태**: 각 에이전트의 CONFIG_DIR를 독립적으로 지원하지 못함 (Orca의 제약)

- Claude Code: `~/.claude`, `~/.claude-2`, `~/.claude-3` (각 프로필별)
- Codex: `~/.codex`, `~/.codex-2` 
- Cursor: 자체 설정 디렉토리

**Orca의 처리**:
- 런타임 시작 시 현재 활성 에이전트 감지 (프로세스 환경변수 스캔)
- 각 에이전트의 `configPath` 기록 후 hook 설치
- **제약**: 같은 에이전트의 다른 CONFIG_DIR 세션 간은 직접 통신 불가

**INTENT.md 목표와의 관계**: Orca 현재 구조로는 이를 자동으로 풀기 어려우므로, cross-session-messaging이 별도 레지스트리 메커니즘 필요

**참고 파일**: `src/shared/agent-hook-types.ts`, `src/shared/managed-agent-hook-targets.ts`

---

## 5. 원격/다중 머신 구조

### SSH 워크트리 지원

Orca는 **SSH 기반 원격 워크트리** 지원:
- 원격 머신에 Orca 런타임 실행 (headless 또는 다른 사용자 session)
- `callOrchestrationWorkerServer()` RPC로 원격 호출
- 전체 파일 편집, git, 터미널 명령 원격 실행

### Federation 구조

**구조 계층**:
```
Home Runtime (Coordinator 위치)
    ↓
Worker Server (SSH/HTTP endpoint)
    ↓
Remote Orchestration DB (SQLite)
    ↓
Remote Worker Terminal
```

**동기화 방식**:
1. **Pull-based**: Home이 주기적으로 worker server에서 `orchestration.federationPull()` 호출
2. **Relay table**: `FederationRelayItemRow`에 message 큐 저장
3. **Acknowledge flow**: 수신 시 sequence 번호로 ack, 받지 않은 항목은 재전송

**환경식별**:
```typescript
type FederatedDispatchRow = {
  dispatch_id: string
  environment_id: string              // 원격 환경 식별자
  environment_name: string
  peer_fingerprint: string            // 서버 지문 (신뢰 검증용)
  remote_runtime_epoch: string | null // 런타임 재시작 감지
  protocol_version: number            // 호환성 버전
}
```

**참고 파일**: `src/main/runtime/orchestration/types.ts` (177-189행), `federation-sync.ts`, `federation-sync-capability.ts`

### 인증 및 신뢰 모델

**신뢰 메커니즘**:
- **Peer fingerprint**: 원격 서버의 고정 ID (처음 접속 시 기록)
- **Capability probing**: `resolveFederatedLifecycleSettlementCapability()` → 원격 runtime status 조회로 유효성 확인
- **Pairing revision**: 환경 pairing 상태 버전 관리

**제약**: SSH 자체 인증에 의존 (password/key), Orca 수준의 추가 인증 메커니즘 없음

**참고 파일**: `federation-sync-capability.ts` (10-32행)

---

## 6. 범위 제어 (Scope)

### 현재 범위 메커니즘

**Run 단위 격리**:
- 각 `RunRow.id`는 독립적 orchestration context
- 같은 Run 내 메시지만 상호 전달
- `dispatch_context.run_id` 외래키로 강제

**Host scope**:
- `dispatch_context.host_scope`: 호스트별 격리 가능 (현재는 거의 사용 안 함)

**Consumer generation**:
- `dispatch_context.consumer_generation`: re-attach 시 증가 → 이전 consumer의 delivery fence (중복 방지)

**참고 파일**: `src/main/runtime/orchestration/types.ts` (273-305행)

### 프로젝트/워크스페이스 범위

**확인 못함**: INTENT.md에서 요청한 "프로젝트별 통신 범위 제한" 기능은 Orca 현재 구현에서 찾을 수 없음

- Orca는 현재 **Run ID 기반** 완전 격리만 제공
- 프로젝트별 메타데이터 필드는 없음 (Run spec 필드에 임의 정보 가능하지만 built-in 격리는 아님)

---

## 7. 지속 기록 (Persistence)

### 저장 위치 및 형식

**Database**: SQLite 3 (WAL 모드)
- **파일**: 기본적으로 프로젝트/워크스페이스 단위로 `~/.orca/` 또는 `$ORCA_STATE_DIR/` 아래
- **Schema version**: `CURRENT_CONTRACT_VERSION` (migration 관리)

**테이블 구조**:
```
runs                    -- Coordinator run 레코드
├ id, spec, status, coordinator_handle, created_at
├ home_database         -- 이 run의 권위 DB (federation 시)
└ ...

dispatch_contexts       -- 테스크 시도 (attempt history)
├ id, task_id, status, assignee_handle, dispatch_id (retry 참조)
├ consumer_generation   -- re-attach fence
├ depth                 -- nesting 깊이
└ ...

messages                -- 메시지 로그
├ id, run_id, from_handle, to_handle, type, thread_id
├ delivered_at, read
├ pointer_pty_id        -- mailbox-pointer 추적
└ ...

delivery                -- mailbox delivery 상태
├ mailbox_handle, status ('outstanding'|'acknowledged'|'fenced')
├ consumer_generation
└ ...

federation_relay        -- 원격 메시지 큐
├ dispatch_id, direction ('to_home'|'to_worker')
├ sequence, message_id, acked_at
└ ...

questions               -- decision gate (blocking ask)
├ message_id, asker_handle, status, answer_message_id
└ ...

decision_gates          -- DAG 분기 게이트
├ id, task_id, options, resolution, status
└ ...
```

**참고 파일**: `src/main/runtime/orchestration/db/schema/create-tables.ts` (생성 로직), `orchestration-db.ts` (25-41행)

### 사람이 보는 UI

**현재 상태**: SQLite 데이터베이스를 직접 UI로 렌더링하지 않음

- Orca 데스크톱 앱의 **Orchestration 패널**: Run, Task, Dispatch 상태를 트리 뷰로 표시
- **메시지 스레드**: `thread_id`로 묶여 스레드 뷰 지원
- CLI: `orca orchestration run list`, `orca orchestration messages` 등으로 조회

**확인 못함**: 공개 협업용 channel-thread UI (INTENT.md의 "1:N, N:N 채널" 요구사항)는 Orca에 없음 (내부 협업만)

**참고 파일**: `src/main/runtime/orchestration/formatter.ts` (출력 포맷팅), `src/cli/handlers/orchestration/` (CLI 핸들러)

---

## 8. 동시성/충돌 처리

### 데이터베이스 레벨

**Lock & Transaction**:
- SQLite WAL 모드: 다중 reader, 단일 writer
- `busy_timeout = 5000ms` (5초 재시도)
- `synchronous = NORMAL` (durability와 성능 균형)

**멱등성 설계**:
```typescript
type MutationReceiptRow = {
  caller_fingerprint: string      // 발신자 ID
  request_id: string              // 중복 감지
  method: string
  payload_hash: string
  state: 'pending' | 'completed'  -- 중복 request skip
  receipt: string | null
}
```

**참고 파일**: `src/main/runtime/orchestration/types.ts` (125-135행)

### 메시지 레벨

**큐 기반 도달**:
- `OrchestrationMailboxPointerDelivery`: 단일 점 입력 (race 안전)
- `FederationRelayItemRow`: monotonic sequence (재정렬 불가)

**Delivery 상태 머신**:
```
outstanding  → acknowledged  (수신 확인)
           → fenced         (consumer generation 변경으로 무효화)
```

**Consumer generation fence**:
- 터미널 re-attach 시 `consumer_generation` 증가
- 이전 generation의 delivery는 `fenced` 표시 (중복 전달 방지)

**참고 파일**: `src/main/runtime/orchestration/dispatch-mailbox-consumer-fencing.test.ts`, `mailbox-pointer-state.ts`

### 루프 방지

**Nesting depth**:
```typescript
// dispatch_context.depth: root=1, nested worker=2, ...
const NESTED_WORKER_MAX_DEPTH_DEFAULT = 10
```

**DAG 검증**:
- Coordinator가 `evaluateDagConvergence()` 호출로 순환 검사
- Task dependencies로 순환 방지

**Heartbeat & stale detection**:
- Worker가 주기적으로 `heartbeat` 메시지 전송
- Coordinator가 `last_heartbeat_at` 기반 stale dispatch 경고

**참고 파일**: `coordinator-dag-convergence.ts`, `coordinator-task-dispatch.ts` (warnStaleDispatches)

---

## 9. 오케스트레이션 모델

### Coordinator-Worker 패턴

**Coordinator 역할**:
1. Task 분해 (현재는 수동, AI 분해는 미래)
2. Ready task → worker terminal로 dispatch
3. Worker_done / heartbeat / escalation 메시지 수신 & 처리
4. DAG convergence 판정 (완료/실패)

**Worker 역할**:
1. Dispatcher로부터 `dispatch` 메시지 + preamble 수신
2. 환경 setup (worktree, 의존성)
3. 테스크 실행
4. `worker_done` 메시지 + 결과 전송
5. 필요시 `escalation` (블로킹 이슈) 또는 `decision_gate` (의사결정 대기)

**참고 파일**: `src/main/runtime/orchestration/coordinator.ts` (94-166행), `src/main/runtime/orchestration/preamble.ts`

### Task DAG 구조

```typescript
type TaskRow = {
  id: string
  parent_id: string | null        -- parent 테스크
  spec: string                    -- 테스크 정의 (JSON)
  status: 'pending'|'ready'|'dispatched'|'completed'|'failed'|'blocked'
  deps: string                    -- 의존 task ID 배열 (JSON)
  result: string | null           -- 완료 후 결과
}
```

**Task 상태 전이**:
```
pending → (deps 완료 시) ready → dispatched → completed/failed
         ↑                                           ↓
         └─── (escalation/decision) blocked ────────┘
```

**참고 파일**: `src/main/runtime/orchestration/types.ts` (255-271행), `lifecycle-reconciliation.ts`

### 의사결정 게이트 (Decision Gate)

**용도**: Coordinator가 worker의 result에 대해 승인/거부 결정

```typescript
type DecisionGateRow = {
  id: string
  task_id: string
  question: string                -- 코디네이터에게 제시
  options: string                 -- 선택지 (JSON array)
  status: 'pending'|'resolved'|'timeout'
  resolution: string | null       -- 선택된 옵션
}
```

**Flow**:
1. Worker sends `decision_gate` message
2. Coordinator opens gate & waits for resolution
3. Coordinator sends result → worker continues or fails

**참고 파일**: `src/main/runtime/orchestration/coordinator-decision-gates.ts`

### 별도 런타임 필요성

**결론**: 별도 진입점 런타임 **불필요**

- Orca orchestration은 **Orca 앱 내부 기능**으로 통합
- Coordinator는 RPC handler로 실행 (데스크톱 앱의 main process)
- Worker는 각 에이전트 CLI 프로세스 (Orca가 fork해서 관리)
- 메시징은 SQLite + 터미널 주입 (Orca 자체가 중개)

**INTENT.md 요구사항 부합**: "별도 오케스트레이션 런타임을 만들지 말 것" → Orca는 이미 기존 에이전트 CLI 위에 얹혀있는 구조

**참고 파일**: `src/main/runtime/orchestration/coordinator.ts`, `src/cli/handlers/orchestration/run-handlers.ts`

---

## 10. INTENT.md 목표에 대한 시사점

### 차용할 설계

1. **Coordinator-worker 메시지 기반 조정**
   - 의도: cross-session-messaging도 유사 패턴 (A session이 B, C를 worker처럼 invoke)
   - 차용점: structured message type, thread_id 기반 대화, worker_done/heartbeat 패턴

2. **Federation 동기화 구조**
   - 의도: SSH 원격 세션 간 메시징
   - 차용점: relay queue (monotonic sequence), ack flow, peer fingerprint 신뢰

3. **Mailbox-pointer (PTY 주입)**
   - 의도: Claude Code/Codex의 stdin에 메시지 주입 (별도 IPC 불필요)
   - 차용점: agent-hook과 협력하여 deliver

4. **SQLite 기반 지속 기록**
   - 의도: 가볍고 가시적인 저장소
   - 차용점: messages, delivery, federation_relay 테이블 구조

5. **Consumer generation fence**
   - 의도: 중복 delivery 방지
   - 차용점: re-attach 시 generation 증가로 이전 consumer의 mail 무효화

### 피해야 할 설계

1. **고정 CONFIG_DIR 의존성**
   - Orca의 문제: `~/.claude`, `~/.claude-2` 등 프로필별 CONFIG_DIR를 자동으로 스캔하지 않음
   - 회피: cross-session-messaging은 모든 활성 세션을 자동 발견해야 함 (예: `/tmp/xsm-sessions/` 레지스트리)

2. **단일 DB 강제**
   - Orca의 제약: 각 Coordinator run마다 shared SQLite, 원격 federation은 home DB 권위 기반
   - 회피: Claude Code/Codex 세션별 독립 레지스트리 허용 (federation처럼 sync하되, 각자 write 가능)

3. **Re-attach 없는 delivery**
   - Orca: process restart 후 새 consumer_generation → 이전 mail 무효화
   - 문제: 세션이 장시간 대기 중이면 메시지 손실 가능
   - 회피: 메시지 보관 기간 명시 & 만료 정책 문서화

4. **Worker-only nesting**
   - Orca: worker 안에서 또 다른 orchestration run 시작 가능하지만, 깊이 제한 강제
   - 회피: 무한 위임 방지하되, 필요시 명시적 delegation 지원

### 우리 목표와 맞지 않는 점

1. **Orca는 "Orca 앱이 중심"**
   - Orca는 모든 세션을 자신이 fork & 관리 (각 agent의 CLI를 Orca 내부 terminal에서 실행)
   - 우리 목표: 기존 Claude Code/Codex 세션 간 자율적 메시징 (Orca 없이도 작동)

2. **Hook 기반 통신**
   - Orca: managed hook를 각 에이전트 설정 디렉토리에 설치 (권한 필요)
   - 우리: 세션 간 자동 발견 가능해야 함 (hook 설치 여부 무관)

3. **Federation = Orca-to-Orca만**
   - Orca federation: 원격 Orca runtime과의 통신
   - 우리: 다른 머신의 Claude Code/Codex 세션과의 통신 (SSH depth 0에서 직접)

4. **내부 협업 (closed loop)**
   - Orca: Coordinator ↔ Worker ↔ Coordinator 폐쇄 루프
   - 우리: 인간 + 에이전트의 1:N/N:N 채널 필요 (Orca에는 협업 채널 UI 없음)

---

## 근거 파일 목록

### 핵심 런타임
- `src/main/runtime/orchestration/coordinator.ts` - Coordinator 메인 로직
- `src/main/runtime/orchestration/types.ts` - 데이터 스키마
- `src/main/runtime/orchestration/db.ts` - DB 인터페이스
- `src/main/runtime/orca-runtime.ts` - Runtime 진입점
- `src/shared/agent-hook-types.ts` - Agent hook 정의

### 메시징 & 전달
- `src/main/runtime/orchestration/mailbox-pointer-delivery.ts` - PTY 주입
- `src/main/runtime/orchestration/federation-sync.ts` - 원격 동기화
- `src/main/runtime/orchestration/federation-sync-capability.ts` - Federation 신뢰
- `src/cli/handlers/orchestration/message-send-handler.ts` - CLI 메시지 전송

### DB & Schema
- `src/main/runtime/orchestration/db/orchestration-db.ts` - DB 생성자
- `src/main/runtime/orchestration/db/schema/create-tables.ts` - 테이블 정의
- `src/main/runtime/orchestration/db/contract-constants.ts` - Schema 버전

### Agent Hook
- `src/shared/agent-hook-types.ts` - Hook target & protocol
- `src/shared/managed-agent-hook-targets.ts` - Hook 상태 추적
- `src/shared/agent-hook-spool.ts` - Hook message spool

### Federation 상세
- `src/main/runtime/orchestration/federation-ack-checkpoints.ts` - Ack 관리
- `src/main/runtime/orchestration/federation-sync-message.ts` - Message 파싱
- `src/main/runtime/orchestration/types.ts` (177-231행) - Federation 테이블

### Task & Dispatch
- `src/main/runtime/orchestration/coordinator-task-dispatch.ts` - Task dispatch
- `src/main/runtime/orchestration/lifecycle-reconciliation.ts` - Task lifecycle
- `src/main/runtime/orchestration/coordinator-decision-gates.ts` - Decision gate

### CLI
- `src/cli/handlers/orchestration/` (directory) - 모든 orchestration CLI 핸들러
- `src/cli/handlers/orchestration/message-send-handler.ts` - send 명령
- `src/cli/handlers/orchestration/worker-launch-handler.ts` - worker 실행

### 설정 & 호환성
- `src/cli/handlers/orchestration/runtime-compatibility.ts` - Runtime version 호환
- `src/main/runtime/orchestration/db/contract-constants.ts` - DB contract 버전
- `src/shared/protocol-version.ts` - Federation protocol 버전

### 테스트 & 검증
- `src/main/runtime/orchestration/db-task-dispatch-lifecycle-guards.test.ts` - Task lifecycle 검증
- `src/main/runtime/orchestration/federation-sync.test.ts` - Federation 동작
- `src/main/runtime/orchestration/coordinator.test.ts` - Coordinator 로직

---

## 확인 못한 부분

1. **각 에이전트별 hook 구현** - Cursor, Grok, 기타 에이전트의 managed script 코드는 Orca 소스에 포함되지 않음 (별도 관리)

2. **프로젝트/워크스페이스 범위 제어** - INTENT.md의 "프로젝트별 통신 범위" 기능은 Orca 현재 구현에서 찾을 수 없음 (Run ID 기반 완전 격리만 있음)

3. **공개 협업 채널 UI** - "인간과 에이전트가 함께 보는 1:N/N:N 채널"은 Orca 현재 기능에 없음 (내부 worker-coordinator 메시징만)

4. **CONFIG_DIR 자동 스캔** - Orca가 `~/.claude-*`, `~/.codex-*` 등을 자동으로 발견하는 메커니즘 없음 (각 hook 설치 시점에 수동)

---

## 정정 이력

| 날짜 | 절 | 항목 | 오류 | 실제 | 근거 |
|------|-----|------|------|------|------|
| 2026-09-19 (T3) | 3절 | Agent Hook 주입 메커니즘 | "Hook이 메시지 spool 디렉토리 정기 poll" | Hook은 stdin 읽음 + spool append (push), Orca listener가 drain (pull) | hook-stdin-contract.ts:124-163, agent-hook-spool.ts:111-170 |

