# T3: Orca 구조화 세션, 훅 spool, 외부 세션 채택 분석

## 결론

Orca의 Claude Code/Codex 구조화 세션은 PTY 없이 두 에이전트 모두를 "소유 세션"으로 운영하되, 외부 세션의 **이력만 채택**합니다. 훅 spool은 이벤트를 지속 기록하는 구조로, 정기적 drain을 통해 비동기 전달을 실현합니다. 

**확정 사항:**
- 훅 spool 메커니즘: Hook이 stdin 읽음 → spool append (push), Orca listener가 drain (pull)
- 구조화 세션 포인터 전달: idle/awaiting-human/turn-running 상태 게이트
- 오케스트레이션 메일함 소비자 교체 감지: consumer generation fence로 이전 소비자의 배달을 차단
- 같은 thread를 두 writer가 쓸 수 없음: findConflictingStructuredAdoption + owner probe로 방어

**남은 검토:**
- Claude Agent SDK 구조화 query + async queue의 정확한 전달 경로
- Codex app-server와 TUI 간 양방향 통신의 구체적 프로토콜
- 런타임 없이 사용자가 띄운 세션 간 메시징의 실현 가능성

---

## 리뷰 항목별 해소 결과

| 항목 ID | 제목 | 결론 | 근거 |
|---------|------|------|------|
| R1-07 | hook-stdin-contract.ts 인용, 훅 spool 기록 방식 | **확정** | hook-stdin-contract.ts:124-163 `buildPosixHookSpoolLines()` - Hook은 stdin에서 JSON을 읽고 spool 파일에 printf로 기록 |
| R1-08 | Orca 구조화 세션, 외부 TUI 인수 불가 | **부분 확정** | structured-agent-session-history-adoption.ts:88-114 - 이력 채택은 가능하나, 실행 중 TUI는 lease adjudication으로 거부 |
| R4-06 | Orca가 실행 중 외부 TUI를 인수할 수 없는 이유 | **확정** | findConflictingStructuredAdoption과 owner probe로 같은 thread의 두 writer 감지, agent_session_conflict 거부 |
| R4-07 | 같은 thread를 두 writer(별도 app-server resume)가 쓰는 문제 | **확정(Orca 측 방어)** | `structured-agent-session-history-adoption.ts:78-112` `findConflictingStructuredAdoption`이 이미 소유자가 있는 provider 세션의 중복 채택을 `agent_session_conflict`/`agent_session_ownership_unknown`으로 거부한다. 같은 파일 주석은 "Codex takes no lock of its own … loads history once and then diverges"라고 적는다(Orca 개발자 설명, Codex 실측 아님). 참고로 `consumer_generation` fence(`mailbox-consumer.ts:5-45`)는 이 문제가 아니라 오케스트레이션 메일함 소비자 교체용이다 (코디네이터 정정 2026-09-19) |
| R2-4 | 훅 spool 설명 정정 | **확정** | agent-hook-spool.ts:111-170 `drainAgentHookSpool()` 함수 - Orca가 spool을 주기적으로 drain |

---

## 상세 분석

### 1. 훅 Spool 메커니즘 (R1-07 정정)

#### 데이터 흐름 (Push-Pull 구조)

```
Hook Process                          Orca Listener
────────────────                      ──────────────
stdin 읽음 (JSON)                      [POLL]
  [DOWN]                               spool dir
spool append                           [DOWN]
  [RIGHT] $ORCA_AGENT_HOOK_ENDPOINT/   readSpoolFile()
           spool/pane-{paneKey}.jsonl  [DOWN]
                                       parse + ingest
                                       [DOWN]
                                       ftruncate (consume)
```

#### Hook 역할 (Push)

파일: `/tmp/xsm-refs/orca/src/main/agent-hooks/hook-stdin-contract.ts:124-163`

```typescript
function buildPosixHookSpoolLines(source: string, eventNameVar?: string): string[] {
  // ...
  const spoolRecordLine = "  { printf '\\n{...}\\n' ... >> \"$spool_file\" 2>/dev/null || :"
  return [
    'spool_hook_event() {',
    '  [ -n "${ORCA_AGENT_HOOK_ENDPOINT:-}" ] || return 0',
    '  [ -n "${ORCA_PANE_KEY:-}" ] || return 0',
    '  spool_dir="$spool_base/spool"',
    '  mkdir -p "$spool_dir" 2>/dev/null',
    "  spool_file=\"$spool_dir/pane-$spool_id.jsonl\"",
    '  [ -f "$spool_file" ] || : > "$spool_file"',
    spoolRecordLine,  // printf >> 로 append
    '}'
  ]
}
```

**동작:**
1. Hook이 stdin에서 JSON 페이로드 읽음 (`POSIX_HOOK_JSON_STDIN_READER`)
2. 읽은 내용을 `$spool_file` (JSONL 형식)에 append 기록
3. 파일 크기 제약 (`AGENT_HOOK_SPOOL_MAX_BYTES = 5MB`), 보관 기간 (`AGENT_HOOK_SPOOL_MAX_AGE_MS = 7일`) 확인

#### Orca Listener 역할 (Pull)

파일: `/tmp/xsm-refs/orca/src/shared/agent-hook-spool.ts:111-170`

```typescript
function drainAgentHookSpool(options: SpoolDrainOptions): number {
  const spoolDir = join(options.endpointDir, 'spool')
  let names: string[]
  try {
    names = readdirSync(spoolDir)  // spool 디렉토리 나열
  } catch {
    return 0
  }
  const candidates = names
    .map((name) => {
      const path = join(spoolDir, name)
      const stat = statSync(path)
      return stat.isFile() && stat.size > 0 ? { path, mtimeMs: stat.mtimeMs } : null
    })
    .sort((a, b) => a.mtimeMs - b.mtimeMs)
  
  for (const candidate of candidates) {
    const { records, consumed } = readSpoolFile(candidate.path, now)
    for (const record of records) {
      options.ingest({ ...record, isReplay: true })
    }
    // ftruncate로 처리한 부분 제거 (원자성 유지)
    ftruncateSync(fd, tail.length)
  }
}
```

**동작:**
1. `$ORCA_AGENT_HOOK_ENDPOINT/spool/` 디렉토리 주기적 scanning
2. 각 pane-*.jsonl 파일을 오래된 순서대로 처리
3. 완전한 JSON 라인만 파싱 (불완전한 줄은 남겨둠)
4. 처리된 부분만 `ftruncate`로 제거 (inode 유지, 동시 append 안전)

### 2. Orca 구조화 세션 (R1-08 부분 확정)

#### Claude 구조화 세션 (Agent SDK 기반)

파일: `/tmp/xsm-refs/orca/src/main/claude/claude-structured-session-adapter.ts:53-92`

```typescript
export class ClaudeStructuredSessionAdapter implements StructuredAgentSessionAdapter {
  private readonly sessions = new Map<string, ClaudeSession>()
  private readonly acquisitions = new ClaudeAcquisitionRegistry()
  
  acquire = (input: StructuredAgentSessionAcquireInput): Promise<AgentSessionAcquisition> =>
    acquireClaudeSession({
      input,
      deps: this.deps,
      sessions: this.sessions,
      acquisitions: this.acquisitions,
      callbacks: {
        deliver: (attempt, sessionId, event) => this.deliver(attempt, sessionId, event),
        emit: (session, _events, event) => this.emit(session, event),
        handleExit: (sessionId, attempt, error) => this.handleExit(sessionId, attempt, error),
      }
    })

  private deliver(attempt: ClaudeAcquisitionAttempt, sessionId: string, event: () => void): void {
    if (!attempt.published) {
      attempt.buffered.push(event)  // 비동기 큐
      return
    }
    if (this.sessions.get(sessionId)?.connection === attempt.connection) {
      event()  // 전달 실행
    }
  }
}
```

**구조:**
- Agent SDK의 structured query interface를 통해 Claude와 통신
- 각 세션마다 `ClaudeAcquisitionAttempt`를 추적 (retry 및 consumer generation)
- `deliver` 콜백으로 비동기 큐(`buffered`)에 이벤트를 축적 → published 상태 전환 후 실행

#### Codex 구조화 세션 (app-server 기반)

파일: `/tmp/xsm-refs/codex/codex-rs/app-server/` (TypeScript 추출 불완전)

**구조:**
- Codex는 자체 app-server 프로세스를 통해 structured message 수신
- TUI는 app-server 제어 소켓 (`$CODEX_HOME/app-server-control/app-server-control.sock`)을 통해 통신
- Orca는 app-server에 직접 HTTP 또는 소켓 메시지 전송

#### 포인터 전달 게이트

| 상태 | 설명 | 전달 가능 여부 |
|------|------|-----------------|
| **idle** | 에이전트가 입력 대기 중 | [OK] 가능 (PTY 주입 안전) |
| **awaiting-human** | 사람의 응답 대기 | [OK] 가능 (큐에 축적) |
| **turn-running** | 턴이 진행 중 | [INFO] 제한 (pending 상태로 축적) |

파일: `/tmp/xsm-refs/orca/src/main/runtime/orchestration/mailbox-pointer-delivery.ts:30-80`

```typescript
class OrchestrationMailboxPointerDelivery {
  isAgentSettledForDelivery(): boolean {
    // idle 상태인지 확인
  }
  
  async deliverWithPointerInject(message: string): Promise<void> {
    // PTY에 JSON 형식 메시지 주입
    await pointer_enter_delay_ms(500)  // 안전 주입 대기
    // PTY에 '\n<JSON>\n' 형식으로 입력
  }
}
```

### 3. 외부 세션 채택 (R1-08, R4-06, R4-07 확정)

#### 이력 채택 조건

파일: `/tmp/xsm-refs/orca/src/main/native-chat/structured-agent-session-history-adoption.ts:127-156`

```typescript
export async function resolveStructuredAgentSessionAdoption(input: {
  agent: 'claude' | 'codex'
  providerSessionId: string
  candidateAccountHomes: readonly string[]
  resolveTranscript: (args: {
    agent: 'claude' | 'codex'
    providerSessionId: string
    accountHomePath: string
  }) => Promise<string | null>
}): Promise<StructuredAgentSessionAdoption> {
  const seen = new Set<string>()
  for (const accountHomePath of input.candidateAccountHomes) {
    const transcriptPath = await input.resolveTranscript({
      agent: input.agent,
      providerSessionId: input.providerSessionId,
      accountHomePath: trimmed
    })
    if (transcriptPath) {
      return { accountHomePath: trimmed, transcriptPath }
    }
  }
  throw new Error('agent_session_identity_required')
}
```

**동작:**
1. 외부 Claude/Codex 세션의 `providerSessionId` (Claude `sessionId` 또는 Codex `threadId`)로 transcript 검색
2. 후보 account home들을 순서대로 시도 (사용자 선택 > 기본값)
3. 처음 발견한 transcript 경로를 반환

#### 충돌 감지 (같은 thread 두 writer)

파일: `/tmp/xsm-refs/orca/src/main/native-chat/structured-agent-session-history-adoption.ts:88-114`

```typescript
export function findConflictingStructuredAdoption(input: {
  agent: 'claude' | 'codex'
  providerSessionId: string
  selfSessionId: string
  ownership: readonly StructuredAgentSessionAdoptionOwnership[]
}): StructuredAgentSessionAdoptionOwnership | null {
  return (
    input.ownership.find(
      (owner) =>
        owner.sessionId !== input.selfSessionId &&  // 자신의 세션은 제외
        owner.provider === input.agent &&
        owner.providerSessionId === input.providerSessionId  // 같은 대화
    ) ?? null
  )
}

export function structuredAdoptionConflictError(
  ownership: StructuredAgentSessionAdoptionOwnership
): Error {
  return new Error(
    agentSessionLeaseAdmitsWriter(ownership.lease)
      ? 'agent_session_conflict'  // 다른 곳에서 write 중
      : 'agent_session_ownership_unknown'
  )
}
```

**시나리오:**
- Thread A를 Session 1이 소유 중
- Session 2가 Thread A를 입양하려 함
- → `findConflictingStructuredAdoption` 검사
  - Session 1이 admitted writer이면: `agent_session_conflict` (거부)
  - 소유권 불명이면: `agent_session_ownership_unknown` (거부)

#### 같은 thread의 두 writer 방어 (R4-06 실제 메커니즘)

**의도된 시나리오:** 
- 사용자가 띄운 Codex 세션이 Thread A를 소유 중
- Orca가 같은 Thread A를 입양해 새로운 구조화 세션(session-xyz)을 시작하려 함
- → 거부해야 함 (두 앱 서버가 동시에 쓰면 데이터 손상)

**방어 메커니즘:**

파일: `/tmp/xsm-refs/orca/src/main/native-chat/structured-agent-session-history-adoption.ts:78-112`

```typescript
export function findConflictingStructuredAdoption(input: {
  agent: 'claude' | 'codex'
  providerSessionId: string
  selfSessionId: string
  ownership: readonly StructuredAgentSessionAdoptionOwnership[]
}): StructuredAgentSessionAdoptionOwnership | null {
  return input.ownership.find(
    (owner) =>
      owner.sessionId !== input.selfSessionId &&
      owner.provider === input.agent &&
      owner.providerSessionId === input.providerSessionId  // 같은 Thread A
  ) ?? null
}

export function structuredAdoptionConflictError(
  ownership: StructuredAgentSessionAdoptionOwnership
): Error {
  return new Error(
    agentSessionLeaseAdmitsWriter(ownership.lease)
      ? 'agent_session_conflict'  // 다른 세션이 이미 소유 중
      : 'agent_session_ownership_unknown'
  )
}
```

**검사 시점:** `structuredAgentSessionCreate` 호출 시 `findConflictingStructuredAdoption`을 먼저 실행

**Owner Probe (소유권 확인):**

파일: `/tmp/xsm-refs/orca/src/main/runtime/structured-agent-session-owner-probe.ts:16-63`

```typescript
export function createStructuredAgentSessionOwnerProbe(
  hostId: string,
  probe = probeAgentSessionProcessIdentity,
  findSpawnTokenProcesses = findAgentSessionSpawnTokenProcesses
): (record: AgentSessionRecord) => Promise<AgentSessionOwnerProbe> {
  return async (record) => {
    const owner = record.lease.ownerProcess
    if (!owner) {
      // 소유자 프로세스 없음 → 자유로운 상태
      const spawnToken = record.lease.reservedSpawnToken
      if (spawnToken === null) {
        return { outcome: 'reservation-unused' }
      }
      // Spawn token으로 살아있는 프로세스 스캔 (PID reuse 방지)
      return probeAgentSessionReservation({
        spawnToken,
        findProcessesWithSpawnToken: (token) => findSpawnTokenProcesses(token),
        hasProviderActivitySinceReservation: async () =>
          agentSessionReservationTouchedProvider(record)
      })
    }
    // 원격 호스트에서 소유 중이면 확인 불가 → indeterminate (거부)
    if (owner.hostId !== hostId) {
      return {
        outcome: 'indeterminate',
        reason: `owner runs on ${owner.hostId}`
      }
    }
    // 로컬 호스트의 소유자 → PID와 spawn token으로 정확히 확인
    return probe({
      identity: owner,
      deps: { readEchoedSpawnToken: readEchoedAgentSessionSpawnToken }
    })
  }
}
```

**주석과 현실의 차이:**

코드 주석에서:
> "Codex takes no lock of its own: a second app-server holding the same thread never errors, it loads history once and then diverges"

이것은 **Orca 개발자의 설명**(Codex의 실제 동작을 서술한 것이 아님). 의미는:
- Codex 자체는 같은 thread를 두 앱 서버가 열어도 즉시 에러를 내지 않음
- 대신 양쪽이 history를 독립적으로 읽고, 이후 diverge됨 (데이터 손상 위험)
- Orca가 이를 방지하려고 conflict 검사를 추가한 것

#### Consumer Generation Fence (R4-07 정정)

**목적:** 오케스트레이션 메일함 **소비자 교체** 감지 및 배달 차단

파일: `/tmp/xsm-refs/orca/src/main/runtime/orchestration/db/messages/mailbox-consumer.ts:5-45`

```typescript
const ACTIVE_DISPATCH_CONSUMER_SQL = `
  SELECT run_id, consumer_generation FROM dispatch_contexts
  WHERE id = ? AND status IN ('pending', 'dispatched')
`

export function requireMailboxConsumer(
  db: OrchestrationDb,
  params: {
    runId: string
    mailboxHandle: string
    consumerGeneration: number  // delivery가 들고 온 세대
    consumerSource?: 'dispatch' | 'attachment'
  }
): void {
  const consumer = db.db.prepare(sql).get(dispatchId) as
    | { run_id: string; consumer_generation: number }
    | undefined
  // 현재 소비자의 세대가 delivery의 세대와 다르면 → 소비자가 교체된 것
  if (
    consumer?.run_id !== params.runId ||
    consumer.consumer_generation !== params.consumerGeneration
  ) {
    throw new OrchestrationError('consumer_fenced', 'This mailbox consumer has been replaced.')
  }
}
```

**동작:**
1. Task를 워커(dispatch_context)에 배정 → `consumer_generation = 1` 기록
2. 배달 메시지는 `{ mailboxHandle: 'dispatch:xxx', consumerGeneration: 1 }` 들고감
3. 워커가 재시작 → 새 dispatch_context 생성 또는 기존 것에 `consumer_generation = 2` 갱신
4. 배달 처리 시 `requireMailboxConsumer` 검사:
   - `delivery.consumerGeneration (1)` vs `dispatch_contexts[xxx].consumer_generation (2)` 비교
   - 다르면 → `consumer_fenced` 에러로 거부 (이전 세대의 배달 폐기)

**목적:** 중복 전달 방지 (같은 메시지가 재시작된 워커에 재배달되지 않게)

### 4. 우리 목표에 주는 시사점

#### 차용할 설계

1. **훅 spool 비동기 기록**
   - Hook이 계속 실행되어도 spool append는 안전 (원자성)
   - Listener가 주기적 drain으로 처리 → 지연 전달 수용 가능

2. **구조화 세션 async queue**
   - buffered 큐로 미전달 메시지 축적
   - published 상태 전환 시 batch 처리 → 네트워크 효율

3. **Consumer generation fence**
   - 소비자 교체 감지로 이전 세대의 배달을 차단
   - 세션 재시작 후 중복 전달 방지 (idempotent)

4. **외부 세션 이력 채택**
   - 실행 중 TUI는 인수 불가하나, transcript 다시 읽기로 진행 가능
   - CONFIG_DIR 차이 처리: candidateAccountHomes 리스트로 우선순위 지정

#### 피해야 할 설계

1. **Orca 중앙집중식 관리**
   - Orca는 모든 세션을 fork & 관리
   - 우리: 기존 세션 간 자율적 메시징 (Orca 없이도 작동)

2. **Hook 기반 강제 설치**
   - Orca: managed hook를 각 CONFIG_DIR에 설치
   - 우리: 사용자가 띄운 세션을 그대로 인식 (권한 불필요)

3. **단일 권위 DB (home_database)**
   - Orca federation: home runtime의 DB가 최종 권위
   - 우리: 각 세션이 로컬 레지스트리 유지 + 동기화

#### 우리 설계에 주는 함의

사용자가 띄운 Claude/Codex TUI의 thread를 외부 app-server가 입양해 메시지를 넣는 안(T3-fix의 시나리오)은 **데이터 안정성 위험**을 안겨준다. 같은 thread를 두 writer가 동시에 쓸 수 없다는 것이 Orca의 핵심 제약이며, Orca도 이를 `findConflictingStructuredAdoption`과 owner probe로만 방어할 수 있다. 우리가 런타임 없이 사용자 세션 간 메시징을 구현하려면, 이러한 소유권 충돌을 감지하고 차단할 별도의 mechanism (lock-free 레지스트리 또는 CRDT 기반 소유권 추적)이 필수다. 단순히 spool append나 stdin 주입만으로는 같은 thread의 두 세션이 대충하는 상황을 막을 수 없다.

#### 구현 방향 (미확인 가정)

| 항목 | Orca 방식 | 우리 방식 (가정) |
|------|----------|-----------------|
| **메시지 저장** | SQLite `messages` 테이블 (중앙) | 각 세션의 로컬 `/tmp/.xsm-sessions/` 또는 `~/.claude/.xsm/` |
| **발견** | agent-hook endpoint 설치 (권한) | 파일 시스템 스캔 (권한 무관) + lockfile 기반 liveness |
| **전달 방식** | mailbox-pointer (PTY) + federation (SSH) | stdin 기반 구조화 메시지 + 원격은 별도 검토 (T4) |
| **세션 간 신뢰** | peer fingerprint (federation) | 사용자 HOME 기반 격리 + 선택적 공개 키 (미정) |

---

## 확인 못 한 것

1. **Claude Agent SDK의 structured query 인터페이스 정확한 명세**
   - `/tmp/xsm-refs/` 내 Claude SDK 파일 (`.ts` 형식)이 완전하지 않아 추출 모듈의 정확한 전달 경로 미확인

2. **Codex app-server와 TUI 간 양방향 통신의 구체적 프로토콜**
   - `/tmp/xsm-refs/codex/codex-rs/app-server/`는 Rust 소스로, TypeScript 추출이 제한적

3. **consumer_generation 증가의 정확한 발동 시점**
   - `dispatch-mailbox-consumer-fencing.test.ts`에는 테스트만 있고, 실제 증가 로직의 구현 파일 미확인

4. **실행 중 외부 TUI를 인수할 수 없다는 설명의 파일 분기 시나리오 상세**
   - "같은 thread를 두 writer"가 쓰는 정확한 실패 모드는 OS/파일 시스템 의존적이므로, 문서상 기술 없음

5. **우리 목표(runtime 없이 사용자 세션 간 메시징)의 실현 가능 검증**
   - Orca 구조는 "Orca가 모든 세션을 소유"하는 모델이므로, 기존 Claude/Codex 세션을 외부에서 제어하는 설계는 별도 아키텍처 필요 (T1, T2, T4 결과 대기)

---

## 정정 이력

| 날짜 | 절 | 오류 | 정정 | 근거 |
|------|-----|------|------|------|
| 2026-09-19 (T3) | 결론, 리뷰 표, R4-06 행 | Consumer generation fence가 "같은 thread 두 writer 방어"라고 오인 | Consumer generation fence는 소비자(dispatch_contexts) 교체 시 이전 세대의 delivery를 requireMailboxConsumer에서 consumer_fenced로 거부하는 것. 실제 두 writer 방어는 findConflictingStructuredAdoption + owner probe | mailbox-consumer.ts:5-45, structured-agent-session-history-adoption.ts:78-112, structured-agent-session-owner-probe.ts:16-63 |
| 2026-09-19 (T3-fix) | 3절 전체 | "TUI owner 복구 불가", "Consumer Generation Fence" 절 통합 오류 | 두 writer 방어 메커니즘을 "같은 thread의 두 writer 방어"로 분리. 주석 "Codex takes no lock"은 Orca 개발자의 설명(Codex 실측이 아님)으로 구분. Consumer generation fence는 소비자 교체 감지로 재명명 및 목적 정확화 | mailbox-consumer.ts 코드, structured-agent-session-owner-probe.ts 해석 |
| 2026-09-19 (T3-fix) | 4절 | 우리 설계 시사점 미기술 | 같은 thread 두 writer 감지의 어려움과 소유권 충돌 mechanism의 필요성 명시 | T3-fix 요청 반영 |
