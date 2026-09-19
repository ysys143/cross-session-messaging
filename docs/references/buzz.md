# Buzz: 에이전트-우선 팀 워크스페이스 설계 조사

## 요약

Buzz(https://github.com/block/buzz)는 Block, Inc.에서 개발한 오픈소스 팀 워크스페이스로, **Nostr 프로토콜 기반 이벤트 로그**를 핵심으로 하는 자체 호스팅 가능한 협업 플랫폼이다. 사람과 AI 에이전트를 동등한 참여자로 취급하며, 메시지·반응·워크플로우·코드 리뷰·미디어 주석 등 모든 것이 서명된 Nostr 이벤트로 기록된다. 특히 에이전트 발견 및 메시징이 relay 기반 pull 모델과 WebSocket pub/sub으로 구성되어 있어, 별도 orchestration 런타임 없이 relay를 중심으로 작동한다. INTENT.md의 "인간과 에이전트가 함께 보는 채널-스레드"와 "CONFIG_DIR 무관한 세션 발견" 요구사항에 대해 부분적으로 설계 참고가 될 만하다.

---

## 1. 한 줄 정의 및 형태·언어·의존성

**정의:** Buzz는 Nostr 프로토콜을 이용한 자체 호스팅 팀 워크스페이스. 모든 행동(메시지, 반응, 워크플로우, 코드 이벤트)을 암호서명된 이벤트 로그로 기록하며, relay가 단일 진실 공급원(single source of truth)이다.

**형태:**
- **데스크톱 앱:** Tauri 2 + React 19 (`desktop/` 디렉토리, macOS/Windows)
- **모바일 앱:** Flutter (`mobile/` 디렉토리, iOS)
- **CLI:** buzz-cli (agent-first CLI, Rust)
- **웹 클라이언트:** Vite React (relay가 서빙, 저장소 브라우싱 등)
- **릴레이 서버:** buzz-relay (Rust, Axum 웹 프레임워크)
- **에이전트 하니스:** buzz-acp (ACP/JSON-RPC 표준), buzz-agent (minimal 구현)

**언어:** Rust (주요: relay, CLI, agent harness) / TypeScript React (웹, 데스크톱 UI) / Flutter (모바일)

**주요 의존성:**
- Nostr 프로토콜 (NIP-01 wire format)
- Axum (HTTP/WebSocket 서버)
- Tokio (async 런타임)
- PostgreSQL (event store, channels, tokens, workflows, audit log)
- Redis (pub/sub, presence, typing indicators, rate limiting)
- Iroh (inter-relay mesh, UDP 기반 peer-to-peer)
- evalexpr (workflow YAML 조건 평가)

소스 파일: `crates/buzz-relay/src/main.rs:1`, `crates/buzz-acp/src/lib.rs:1`, `crates/buzz-cli/Cargo.toml:18-85`

---

## 2. 에이전트 세션 발견(discovery)/레지스트리: 등록·조회·생존 판정

### 발견 메커니즘

Buzz는 **relay 중심의 pull 모델**을 사용한다. 에이전트와 클라이언트는 relay에 WebSocket으로 연결하고, relay가 모든 상태를 관리한다.

**커뮤니티 기반 스코핑:**
- HTTP 요청의 `host` 헤더로 커뮤니티 결정 (다중 테넌시 지원)
- `TenantContext`를 모든 핸들러에 전파
- 한 relay 인스턴스는 암묵적으로 하나의 커뮤니티, 다중 인스턴스는 각 host당 하나씩

소스: `crates/buzz-relay/src/lib.rs:9-15`

### 에이전트 메타데이터 등록

에이전트는 다음 이벤트로 자신을 표현한다:

- **KIND_AGENT_PROFILE (kind:10100):** 에이전트 메타데이터 + 소유자 참조 (replaceable, agent-authored)
- **KIND_PRESENCE_UPDATE (kind:20001):** 상태(`"online"`, `"away"`, `"offline"`), ephemeral, Redis에 저장

소스: `crates/buzz-core/src/kind.rs:87-94, 141`

### 세션 생존 판정

**ACP 풀 (buzz-acp):**
- `AgentPool`: 최대 8개 동시 세션 유지
- 각 세션은 고유 `session_id` 할당
- `PromptResult` 반환 시 세션 반환, fail-safe로 재사용 또는 재초기화
- 최근 활동 윈도우: 60초 (RECENT_ACTIVITY_WINDOW)
- 세션 회전: turn count 기반 (proactive rotation)

소스: `crates/buzz-acp/src/pool.rs:1-125`

**Presence 임대:**
- 에이전트가 주기적으로 presence 갱신 (WebSocket 통해 kind:20001 발행)
- Redis에 expire TTL로 저장
- 갱신 실패 → 몇 초 내 relay가 거래소에서 제거

소스: `crates/buzz-acp/src/lib.rs:88-100`, `VISION_REMOTE_AGENTS.md:59`

---

## 3. 메시징/전달(transport): 프로토콜·프레임·wakeup 메커니즘

### 전달 프로토콜

**Nostr NIP-01 WebSocket:**
- JSON 이벤트 형식:
  ```json
  {
    "id":      "<sha256>",
    "pubkey":  "<secp256k1 hex>",
    "kind":    <u32>,
    "tags":    [["e", "<event-id>"], ["p", "<pubkey>"], ...],
    "content": "<JSON or text>",
    "sig":     "<Schnorr sig>"
  }
  ```
- `kind`가 유일한 dispatch 스위치
- Replaceable events (NIP-16, kind 10000-19999): 최신 버전만 저장

> 정정(2026-09-19, T5): `crates/buzz-relay/src/architecture.md`는 존재하지 않음. 루트 `ARCHITECTURE.md:102-115`를 참고.

소스: `crates/buzz-relay/src/protocol.rs:1`, `ARCHITECTURE.md:102-115`

### 메시지 종류

**채팅:**
- KIND_STREAM_MESSAGE (kind:9, NIP-29 group chat)
- KIND_STREAM_MESSAGE_V2 (kind:40002, v2 format)
- KIND_STREAM_MESSAGE_EDIT (kind:40003)
- KIND_STREAM_MESSAGE_PINNED (kind:40004)

**에이전트 작업:**
- KIND_JOB_REQUEST (kind:43001): 에이전트에게 보내는 작업 요청

**워크플로우:**
- KIND_WORKFLOW_TRIGGER (kind:46020)
- KIND_WORKFLOW_STEP_STARTED/COMPLETED/FAILED (kind:46002-46004)
- KIND_WORKFLOW_APPROVAL_REQUESTED/GRANTED/DENIED (kind:46010-46012)

소스: `crates/buzz-core/src/kind.rs:442-582`

### Fan-out 및 다중 인스턴스 조정

**로컬 인스턴스 (in-process):**
- `SubscriptionRegistry`: 채널별, kind별 인덱스로 O(1) fan-out
- `IndexKey { channel_id, kind }`와 `GlobalPKindIndexKey`로 효율적 라우팅
- DashMap 기반 thread-safe map

**다중 인스턴스:**
- Redis PUBLISH/SUBSCRIBE: 채널별 pub/sub topic
- 한 relay가 이벤트 발행 → Redis PUBLISH → 다른 릴레이가 구독 → 로컬 WS 연결에 fan-out
- 중복 제거: AppState.local_event_ids로 local echo 필터링

소스: `crates/buzz-relay/src/subscription.rs:1-100`, `ARCHITECTURE.md:57-65`

### 에이전트 Wakeup/Invoke 메커니즘

**buzz-acp (Claude Code/Codex 연동 후보):**
- ACP (Agent Client Protocol) 표준 기반 JSON-RPC 2.0
- buzz-relay가 @mention (tags `["p", "<agent-pubkey>"]`)을 감지 → buzz-acp에 알림
- buzz-acp가 kind:43001 (JOB_REQUEST) 또는 kind:40002 (스트림 메시지) 처리
- 에이전트 내부에서 MCP (Model Context Protocol) 서버와 communicate via stdio

**buzz-cli (직접 REST API 호출):**
- HTTP `/events` (POST), `/query` (POST), `/count` (POST)
- NIP-98 기반 서명 인증

**WebSocket 직접:**
- 클라이언트가 WS 연결 → REQ 구독 → EVENT 수신

소스: `crates/buzz-relay/src/router.rs:63-82`, `crates/buzz-acp/src/lib.rs:1-20`, `crates/buzz-cli/Cargo.toml:44-46`

---

## 4. 이종 에이전트 지원: Claude Code·Codex·기타 추상화

### 에이전트 인터페이스 추상화

Buzz는 **프로토콜 기반** 추상화를 사용:

1. **Nostr 클라이언트 (범용):**
   - 아무 Nostr 클라이언트 (app, 웹, 모바일)도 relay에 연결 가능
   - 서명 방식만 같으면 (secp256k1) 누구든지 참여 가능

2. **ACP 하니스 (buzz-acp):**
   - Agent Client Protocol (JSON-RPC 2.0 over stdio)
   - agent ↔ ACP client (Zed, JetBrains, buzz-acp) 통신
   - MCP (Model Context Protocol) 서버를 통해 tools 제공
   - 여러 LLM 벤더 지원 가능 (환경변수로 선택)

3. **CLI (buzz-cli):**
   - REST API 기반, NIP-98 HTTP 인증
   - 어떤 언어로도 쓸 수 있는 클라이언트 제공

4. **MCP 서버 (buzz-dev-mcp):**
   - 아무 MCP 호환 클라이언트도 사용 가능
   - buzz-agent만 사용 가능한 것 아님

**CONFIG_DIR 무관 발견:**
- Buzz는 relay URL로만 커뮤니티 결정 (host 기반)
- claude config directory 없음 → 모든 세션이 같은 relay 호스트를 보면 발견됨
- 하지만 이는 Buzz 자체가 아직 Claude Code와 통합되지 않았다는 의미 (계획 단계)

소스: `crates/buzz-acp/src/config.rs:1`, `crates/buzz-agent/Cargo.toml:1`, `crates/buzz-dev-mcp/src/lib.rs:1`

---

## 5. 원격/다중 머신: SSH·네트워크 지원

### 현재 상태

**Inter-relay Mesh (BUZZ_MESH):**
- Iroh (UDP 기반 peer-to-peer) 사용
- 릴레이끼리 메시 형성 → 같은 커뮤니티의 여러 relay 인스턴스 통신
- `BUZZ_MESH` 환경변수로 활성화
- 의도: 고가용성, 지연시간 최소화
- 구현: `buzz-relay-mesh` crate에서 endpoint, registry, membership 관리

> 정정(2026-09-19, T5): "mesh crate 미포함"은 오류. `buzz-relay-mesh/src/endpoint.rs`, `registry.rs` 등에서 mesh 구현이 확인됨.

소스: `crates/buzz-relay/src/mesh_boot.rs:1-50`, `crates/buzz-relay-mesh/src/endpoint.rs:17-39`, `crates/buzz-relay-mesh/src/registry.rs:24-50`

**Remote Agents (계획/초안 단계):**
- 비전 문서 있음 (VISION_REMOTE_AGENTS.md)
- Kubernetes deployment provider 지원 계획
- 에이전트는 relay에 의존하여 identity/presence/history 유지
- 호스트 머신은 disposable (body)

> 정정(2026-09-19, T5): `VISION_REMOTE_AGENTS.md:1-74`는 범위 초과. 실제 파일은 73줄.

소스: `VISION_REMOTE_AGENTS.md:1-73`

**Mesh Compute (계획/초안):**
- 커뮤니티 내 idle GPU 풀링
- 에이전트가 같은 커뮤니티 내 모델 사용 가능

> 정정(2026-09-19, T5): `VISION_MESH.md:1-54`는 범위 초과. 실제 파일은 53줄.

소스: `VISION_MESH.md:1-53`

### 제한사항

- **SSH 직접 지원 없음:** 명시적인 SSH 터널링 메커니즘 없음
  - 하지만 relay를 SSH 터널 뒤에 배포 가능 (일반 HTTPS/WSS)
  - 또는 VPN, TLS 리버스 프록시로 보호 가능
- **Cross-host 에이전트 디스커버리:** 현재 미구현 (single relay per host 기본값)
  - 다중 relay 인스턴스는 mesh로 통신하지만, 클라이언트는 하나의 host로만 연결
- **Multi-cloud 지원:** mesh는 Redis ready registry를 통해 피어 발견, Redis fenced session directory로 소유권 판정
  - 일반 이벤트는 Redis PUBLISH/SUBSCRIBE로 다중 노드에 fan-out
  - 따라서 같은 커뮤니티의 relay들은 공유 Redis와 PostgreSQL 경계로 연결됨

> 정정(2026-09-19, T5): "각 relay는 독립적인 Postgres/Redis 사용"은 오류. mesh 구현에서 Redis 공유 registry 및 fenced session directory를 소유권 판정자로 사용 확인.

소스: `crates/buzz-relay/src/config.rs:1`, `crates/buzz-relay-mesh/src/lib.rs:1-20`, `crates/buzz-relay-mesh/src/registry.rs:1-6`, `crates/buzz-relay/src/main.rs:459-470,973-985`

---

## 6. 범위 제어(scope): 누가 누구와 통신할 수 있는지

### 커뮤니티 경계

**호스트 기반:**
- HTTP 요청 host → TenantContext → 모든 연산에 전파
- `resolve_host(req.host)` → CommunityId
- Unknown host → closed fail

소스: `crates/buzz-relay/src/lib.rs:9-15`

**멤버십:**
- 채널별 멤버 목록 (PostgreSQL buzz_db)
- 에이전트도 일반 사용자처럼 채널 멤버십 가능

### 채널·스레드 범위

**공개 vs 비공개 채널:**
- 채널 생성 시 privacy 플래그
- Private: 초대된 멤버만 접근

**스레드:**
- 메시지의 `["e", "<parent-id>"]` 태그로 형성
- 부모 메시지가 있는 채널 내에서만 표시

소스: `crates/buzz-relay/src/subscription.rs:19-48` (SubscriptionScope)

### 에이전트 접근 제어

**NIP-42 인증:**
- 모든 클라이언트 (포함 에이전트)는 secp256k1 keypair로 인증
- 같은 권한 모델: 채널 멤버십으로만 필터링

**API 토큰 (선택적):**
- NIP-98 Bearer token 기반
- 명시적 scope 가능 (하지만 현재는 단순함)

소스: `crates/buzz-auth/src/lib.rs:1` (확인 못 함)

---

## 7. 지속 기록: 채널·스레드·이력·상태 저장

### 저장소

**PostgreSQL:**
- 모든 Nostr 이벤트 (immutable event log)
- 채널 메타데이터 (name, description, created_at, channel_id)
- 토큰, 워크플로우 정의, audit log
- Full-text search 인덱스 (GIN, generated column `search_tsv`)

**Redis:**
- Presence (SET with EX TTL)
- Typing indicators (ZADD, sorted set)
- Pub/sub routing (PUBLISH/SUBSCRIBE)
- Rate limiting 슬라이딩 윈도우

**메시지 포맷:**
- kind별 content 스키마 (mostly JSON)
- KIND_STREAM_MESSAGE: plain text or JSON
- KIND_AGENT_ENGRAM (kind:30174): NIP-44 encrypted

소스: `crates/buzz-relay/src/state.rs:1-30`, `crates/buzz-core/src/kind.rs:99-140`

### 사람이 보는 UI

**웹 클라이언트 (browser):**
- React 기반, relay가 서빙
- 채널, 스레드, 미디어, 검색 UI
- 에이전트와 사람이 같은 인터페이스에서 상호작용

**데스크톱 앱 (Tauri):**
- macOS/Windows GUI
- 저장소 브라우싱, voice huddles 등

**Nostr 클라이언트 (third-party):**
- 표준 Nostr 클라이언트도 사용 가능 (UI는 다를 수 있음)

소스: `web/`, `desktop/` 디렉토리

---

## 8. 동시성/충돌: 잠금·큐·멱등성·재시도·루프 방지

### 이벤트 중복 제거

**Event ID 기반:**
- Nostr 프로토콜: event id = SHA256(canonical JSON)
- 같은 content → 같은 id → relay가 reject (duplicate)

**Replaceable Events (NIP-16):**
- kind 10000-19999: `(pubkey, kind)` 키로 최신만 저장
- kind 30000-39999: `(pubkey, kind, d_tag)` 키로 최신만 저장
- 자동 버전 관리 (충돌 처리)

소스: `crates/buzz-relay/src/protocol.rs:1`, `crates/buzz-core/src/kind.rs:14-70`

### 낙관적 동시성 제어

**ACP 세션:**
- 여러 prompt 요청이 동시에 도착 → 큐에 쌓임
- AgentPool이 순차 처리 (O(1) claim/return)
- 동시 세션은 여러 개 (up to 8) 하지만 per-channel은 single

**워크플로우:**
- YAML-based automation engine
- 단계별 실행, kind:46002-46004로 기록
- 승인 게이트 (kind:46010-46012)

소스: `crates/buzz-acp/src/pool.rs:1-20`, `crates/buzz-workflow/src/lib.rs:1` (확인 못 함)

### 재시도 및 루프 방지

**가능성 (확인 못 함):**
- EventID 기반 중복 제거 → 자동 재시도 안전
- Replaceable events → last-write-wins (명시적 conflict resolution)
- Workflow step 실패 시 재시도 로직 (구체 미확인)

**감지되는 메커니즘:**
- `crates/buzz-acp/src/pool_lifecycle.rs`: task timeout, panic recovery
- Turn ID (`turn_id`) 기반 terminal event 식별
- Stale session detection (60초 recent activity window)

소스: `crates/buzz-acp/src/pool.rs:47-92`, `crates/buzz-acp/src/pool_lifecycle.rs:1` (제목만 확인)

---

## 9. 오케스트레이션 모델: 코디네이터·워커·DAG·게이트

### 워크플로우 엔진 (buzz-workflow)

**모델:**
- YAML-as-code automation
- `kind:30620` (KIND_WORKFLOW_DEF)로 정의
- 단계별 실행 (`kind:46002-46004` events)
- 조건 평가: evalexpr 기반

**구조:**
- Coordinator는 relay (buzz-relay)
- Worker는 에이전트, webhook, 또는 내부 step
- DAG 명시적 (YAML에서 단계 순서 정의)
- 승인 게이트 (kind:46010-46012)

소스: `crates/buzz-workflow/src/lib.rs:1` (제목만), `crates/buzz-core/src/kind.rs:558-582`

### 별도 런타임 없음

**원칙:**
- INTENT.md: "codex, claude 위에 별도 런타임을 만들지 말 것"
- Buzz 설계도 같음: 모든 coordination이 relay를 통함
- 에이전트는 자신의 identity 유지, relay에서 메시지 pull

**대조:**
- Orca (INTENT.md 레퍼런스): 별도 orchestration 런타임 있음 (확인 안 함)
- Buzz: relay가 implicit orchestrator

소스: `INTENT.md:15`, `README.md:41-51`, `ARCHITECTURE.md:1-20`

---

## 10. INTENT.md 목표에 대한 시사점

### 차용할 설계

1. **이벤트 로그 기반 협업:**
   - Buzz의 immutable event log → Claude Code/Codex 세션 간 메시지 기록으로 활용
   - Signed events → audit trail 보증

2. **Relay 중심 coordination:**
   - 별도 런타임 없이 relay가 모든 조정
   - Claude Code/Codex도 별도 orchestrator 없이 relay(또는 유사 중앙 시스템)에 의존 가능

3. **프로토콜 기반 추상화:**
   - Nostr (또는 유사 wire protocol) → CONFIG_DIR 무관한 발견
   - Buzz는 host 기반, 우리는 global namespace (e.g., git-based, 또는 shared socket directory) 가능

4. **에이전트-동등한 참여:**
   - 사람과 에이전트가 같은 메시지 형식 사용
   - 권한도 동등 (keypair 기반)

5. **Mesh/Remote agents 비전:**
   - Multi-machine 장기 비전 제공
   - SSH/네트워크 너머 에이전트 지원 경로 명확

### 피해야 할 설계

1. **중앙 계정 시스템:**
   - Buzz는 keypair 기반 → 사용자 계정 불필요
   - Claude Code/Codex는 암묵적 사용자 동일성 있음 (global한 사용자 프로필)
   - 차용 시 이 차이 고려 필요

2. **복잡한 다중 테넌시:**
   - Buzz의 host-based multi-tenancy는 자체 호스팅 환경에서만 의미
   - Claude Code/Codex는 로컬 사용자 머신이므로 다중 테넌시 불필요 가능

3. **무거운 부가 의존성:**
   - Buzz는 PostgreSQL, Redis 필수
   - 경량 cross-session messaging을 원하면 sqlite, file-based 대안 검토

### 우리 목표와의 불일치

1. **CONFIG_DIR 무관 발견:**
   - Buzz는 미해결 (relay host 기반, 여전히 single relay 가정)
   - INTENT.md는 명시적으로 ~/.claude, ~/.codex 간 발견을 원함
   - **해결책:** Local socket registry (e.g., ~/.xsm/sessions/), SSH key-based auth mesh, 또는 mDNS

2. **Light-touch 협업:**
   - Buzz는 full-featured workspace → 우리가 원하는 "simple messaging + wakeup"보다 무거움
   - **해결책:** 아이디어만 차용, 구현은 경량화 (e.g., JSON event log + local Unix socket)

3. **GitHub issue tracker 보충:**
   - Buzz는 완전한 team workspace → issue tracker 대체 아님
   - INTENT.md는 GitHub issue + lightweight channel-thread 결합 원함
   - **해결책:** Buzz와 별개로, local markdown-based or sqlite-based channel store

---

## 근거 파일 목록

| 파일 경로 | 줄 범위 | 용도 |
|----------|--------|------|
| `/tmp/xsm-refs/buzz/README.md` | 1-100 | 제품 개요, 에이전트 동등성 |
| `/tmp/xsm-refs/buzz/ARCHITECTURE.md` | 1-150 | 시스템 아키텍처, kind 레지스트리, relay 구조 |
| `/tmp/xsm-refs/buzz/AGENTS.md` | 1-100 | 에이전트 기여 가이드, 레포 구조 |
| `/tmp/xsm-refs/buzz/VISION_AGENT.md` | 1-71 | ACP, MCP, 에이전트 설계 철학 |
| `/tmp/xsm-refs/buzz/VISION_REMOTE_AGENTS.md` | 1-73 | Remote agent 비전, identity/presence/history |
| `/tmp/xsm-refs/buzz/VISION_MESH.md` | 1-53 | Mesh compute, 다중 머신 비전 |
| `ARCHITECTURE.md` | 102-115 | 릴레이 상세 아키텍처, Nostr 프로토콜 정의 |
| `crates/buzz-relay/src/router.rs` | 1-100 | Axum 라우터, WebSocket 핸들러 |
| `crates/buzz-relay/src/subscription.rs` | 1-100 | SubscriptionRegistry, fan-out 인덱싱 |
| `crates/buzz-relay/src/state.rs` | 1-100 | AppState, PostgreSQL/Redis 통합 |
| `crates/buzz-relay/src/mesh_boot.rs` | 1-50 | Inter-relay mesh (iroh) 부팅 |
| `crates/buzz-relay-mesh/src/endpoint.rs` | 17-39 | Mesh endpoint 정의 및 identity |
| `crates/buzz-relay-mesh/src/registry.rs` | 24-50 | Mesh 피어 레지스트리 |
| `crates/buzz-acp/src/lib.rs` | 1-100 | ACP 진입점, presence publish, working directory |
| `crates/buzz-acp/src/pool.rs` | 1-150 | AgentPool, session lifecycle, task metadata |
| `crates/buzz-core/src/kind.rs` | 1-100, 442-582 | Kind 정의 (메시지, 워크플로우, 에이전트) |
| `crates/buzz-cli/Cargo.toml` | 1-96 | CLI 의존성 및 설명 |
| `INTENT.md` | 1-58 | 프로젝트 의도 및 요구사항 |

---

## 정정 이력

| 날짜 | 항목 | 내용 |
|------|------|------|
| 2026-09-19 | T5 | 줄 97: `crates/buzz-relay/src/architecture.md` → `ARCHITECTURE.md:102-115` (파일 미존재) |
| 2026-09-19 | T5 | 줄 201: `VISION_REMOTE_AGENTS.md:1-74` → `1-73` (범위 초과, 실제 73줄) |
| 2026-09-19 | T5 | 줄 207: `VISION_MESH.md:1-54` → `1-53` (범위 초과, 실제 53줄) |
| 2026-09-19 | T5 | 줄 218-219: "mesh crate 미포함" 삭제, buzz-relay-mesh 구현 존재 명시 |
| 2026-09-19 | T5 | 줄 216: "각 relay는 독립적인 Postgres/Redis" → mesh는 Redis 공유 및 fan-out 메커니즘 사용 명시 |
| 2026-09-19 | T5-fix | 줄 131: `crates/buzz-relay/src/architecture.md:57-65` → `ARCHITECTURE.md:57-65` (파일 경로 정정) |
| 2026-09-19 | T5-fix | 줄 297: `crates/buzz-relay/src/architecture.md:46-71` → `crates/buzz-core/src/kind.rs:99-140` (근거 파일 정정) |

---

## 추가 확인 필요 사항

- [ ] buzz-workflow의 구체적 DAG 실행 엔진 (evalexpr 조건)
- [ ] buzz-auth의 scope 및 rate limiting 구현
- [ ] inter-relay mesh의 동기화 메커니즘 (confirmed 못 함)
- [ ] Postgres event store의 backup/replication 전략
- [ ] Claude Code/Codex와의 통합 경로 (Roadmap)
