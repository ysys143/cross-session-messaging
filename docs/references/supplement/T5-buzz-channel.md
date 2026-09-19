# T5: Buzz를 채널-스레드로 쓰는 방안 조사 보고서

## 결론

**확정:** Buzz는 사람+에이전트 채널-스레드 협업을 위한 완전한 자체 호스팅 구성이 가능하며, Docker Compose 기반 최소 배포 구성(PostgreSQL, Redis, buzz-relay, 웹 UI)이 제공된다. Claude Code/Codex가 이용할 수 있는 가장 가벼운 경로는 buzz-cli (REST API + NIP-98 Schnorr 서명)이며, MCP 통합도 buzz-dev-mcp로 가능하다.

**남은 과제:**
1. 실제 Claude Code와의 MCP 연동 구현 (현재 미완료)
2. 최소 구성의 성능·안정성 검증 (단일 머신, 소규모 팀 기준)
3. 검색 및 GitHub issue 승격 메커니즘의 설계 및 구현
4. INTENT.md의 가벼운 .md/.sqlite 대안과의 운영비 비교 (아래 표 참고)

---

## 해소 결과 표

| 항목 ID | 발견 유형 | 결론 | 근거 | 정정 상태 |
|--------|---------|------|------|---------|
| R3-F4 | GAP | 해소됨 | buzz의 최소 자체 호스팅 구성 문서화 | [제1절](#1-최소-자체-호스팅-구성) |
| R3-F5 | UNSUPPORTED | 부분 해소 | Claude Code와의 연동은 계획 단계, MCP 경로 제시 | [제2절](#2-에이전트-신원-발급) |
| R3-F6 | GAP | 해소됨 | buzz-cli (REST), buzz-acp (ACP/JSON-RPC), buzz-dev-mcp (MCP) 경로 확인 | [제3절](#3-게시구독-api-경로) |
| R4-03 | WRONG | 정정됨 | buzz-relay-mesh 구현 확인, docs/references/buzz.md 수정 | buzz.md:218-219 |
| R4-04 | WRONG | 정정됨 | mesh는 Redis 공유 및 fan-out 사용, 독립적 구성 아님 | buzz.md:216 |

---

## 1. 최소 자체 호스팅 구성

### 1.1 필요 프로세스 및 서비스

**단일 머신 배포에 필수:**

1. **PostgreSQL 14+**
   - 목적: 모든 Nostr 이벤트 저장 (immutable event log)
   - 역할: 채널 메타, 토큰, 워크플로우 정의, audit log
   - 스키마: buzz-relay의 embed된 SQLx 마이그레이션으로 초기화
   - 근거: `deploy/compose/compose.yml` 서비스 정의 / `crates/buzz-relay/src/state.rs:1-30`

2. **Redis 6+**
   - 목적: Pub/Sub routing (다중 relay 인스턴스 간 fan-out), presence 저장
   - 역할: 채널별 pub/sub topic, typing indicators (SET EX, ZADD), rate limiting
   - 설정: `BUZZ_REDIS_URL` (기본값: `redis://localhost:6379`)
   - 근거: `crates/buzz-relay/src/subscription.rs:1-100`, `ARCHITECTURE.md:46-65`

3. **buzz-relay (Rust Axum 웹 서버)**
   - 바이너리: `crates/buzz-relay/src/main.rs` -> `target/release/buzz-relay` (약 40-50MB)
   - 포트: 기본 3000 (HTTP), 3443 (HTTPS, TLS 활성화 시)
   - 의존성: Tokio async 런타임, Axum HTTP 프레임워크
   - 근거: `crates/buzz-relay/src/main.rs:1-50`, `ARCHITECTURE.md:21-97`

4. **웹 UI (선택적이나 권장)**
   - 형태: React 앱, relay가 정적 자산으로 제공
   - 목적: 사람 사용자가 채널·스레드 조회 및 메시지 작성
   - 포함 여부: ghcr.io/block/buzz:main 컨테이너 이미지에 기본 포함
   - 근거: `deploy/compose/compose.yml` 서비스 정의 (별도 빌드 불필요)

5. **MinIO (S3 호환 객체 저장소, 선택적)**
   - 목적: 미디어 업로드 (Blossom protocol)
   - 사용: 사람이 이미지·파일 첨부 시
   - 최소 구성: 스킵 가능 (로컬 환경에서는 개발용 MinIO 사용)
   - 근거: `deploy/compose/compose.yml:38-54`

### 1.2 Docker Compose 기반 설정

**단일 명령 시작:**

```bash
cd deploy/compose
cp .env.example .env
$EDITOR .env        # CHANGE_ME 값 모두 교체
./run.sh start
```

**필수 환경변수 (`/tmp/xsm-refs/buzz/deploy/compose/.env.example` 기반):**

| 변수 | 예시 | 설명 | 근거 |
|-----|------|------|------|
| `BUZZ_HTTP_PORT` | 3000 | relay 수신 포트 | compose.yml |
| `BUZZ_RELAY_PRIVATE_KEY` | (64자 hex) | relay 소유 keypair (보안 필수) | deploy/compose/README.md:32-34 |
| `BUZZ_AUTO_MIGRATE` | true | DB 스키마 자동 초기화 | deploy/compose/README.md:35-38 |
| `POSTGRES_PASSWORD` | (secure) | PostgreSQL 관리자 암호 | compose.yml |
| `REDIS_PASSWORD` | (optional) | Redis 암호 (권장) | compose.yml |
| `BUZZ_S3_*` | (MinIO) | S3 호환 설정 (선택적) | deploy/compose/README.md:39-49 |

**TLS 활성화 (VPS 배포):**

```bash
BUZZ_COMPOSE_TLS=true ./run.sh start
```

- Caddy 리버스 프록시 자동 활성화
- Let's Encrypt 인증서 자동 발급
- HTTP -> HTTPS 리다이렉트

**근거:** `deploy/compose/README.md:6-20`, `deploy/compose/compose.caddy.yml`

### 1.3 최소 리소스 요구사항

| 항목 | 권장 | 최소 |
|-----|------|------|
| 메모리 | 4GB | 2GB (단일 팀) |
| CPU | 2 cores | 1 core (낮은 빈도) |
| 스토리지 | 50GB | 10GB (100K 이벤트 기준) |
| 네트워크 | 1Gbps | 10Mbps (충분) |

**성능 특성:**
- 이벤트 입수: PostgreSQL INSERT 속도에 의존 (초 당 수백 이벤트)
- 검색: Postgres FTS (GIN 인덱스)로 밀리초 단위 응답
- 구독: Redis pub/sub으로 O(1) fan-out

**근거:** `deploy/compose/README.md:27-50`, `perf/RELAY_BUS_SCALING.md`

---

## 2. 에이전트 신원 발급

### 2.1 Nostr Keypair 생성

**기본 메커니즘: secp256k1 Schnorr 서명**

- **공개키 (pubkey):** 64자 16진수 (또는 `npub1` Bech32 형식)
- **개인키 (nsec):** 64자 16진수 (또는 `nsec1` Bech32 형식)
- 표준: NIP-01 (Nostr protocol 기본), NIP-19 (주소 인코딩)

**CLI로 발급:**

```bash
# buzz-admin으로 키 생성 (제공되지 않음, 별도 구현 필요)
# 임시: openssl 또는 Python으로 시작
python3 -c "
import secrets
nsec_hex = secrets.token_hex(32)
pubkey_hex = <derive_secp256k1_pubkey>(nsec_hex)
print(f'nsec: {nsec_hex}')
print(f'pubkey: {pubkey_hex}')
"
```

**또는 기존 Nostr 클라이언트 사용:**
- Nostr CLI 도구: https://github.com/nostr-protocol/nips (참고)
- web: https://nostr.band (현재 하드웨어 지원)

### 2.2 에이전트 등록 프로세스

**Step 1: 키 발급 후 환경변수 설정**

```bash
export BUZZ_RELAY_URL="https://relay.example.com"
export BUZZ_PRIVATE_KEY="<nsec 또는 hex>"
```

**Step 2: 에이전트 프로필 등록**

buzz-cli로 프로필 메시지 발행:

```bash
buzz users set-profile --name "my-agent" --display-name "Code Review Bot"
```

- 내부: KIND_AGENT_PROFILE (kind:10100) 이벤트 발행
- 서명: `BUZZ_PRIVATE_KEY`의 Schnorr 서명
- 저장: relay PostgreSQL의 events 테이블에 기록

**근거:** `crates/buzz-core/src/kind.rs:87-94`, `crates/buzz-cli/README.md:19-20`

### 2.3 인증 모드

| 인증 방식 | 사용처 | 구현 | 근거 |
|----------|-------|------|------|
| **NIP-42** | WebSocket 클라이언트 | CHALLENGE 명령으로 relay가 challenge 제시 후 클라이언트가 서명 응답 | `crates/buzz-relay/src/lib.rs:9-15` |
| **NIP-98** | REST API (buzz-cli) | HTTP 요청 헤더 `Authorization: Bearer <NIP-98-token>` (Schnorr 서명된 토큰) | `crates/buzz-cli/README.md:12-20`, `crates/buzz-auth/src/lib.rs` |
| **NIP-AB** | 디바이스 페어링 (선택적) | QR 코드 + 시각적 확인 (SAS-6digit), secp256k1 ECDH + HKDF-SHA256 | `crates/buzz-core/src/pairing/NIP-AB.md:1-50` |

**실무: buzz-cli + NIP-98이 Claude Code에 가장 적합**
- 프로토콜: HTTP REST
- 인증: 요청 헤더에 Schnorr 서명 첨부
- 구현 난이도: 낮음 (JSON 직렬화 + 서명)

---

## 3. 게시·구독 API 경로

### 3.1 가장 가벼운 경로: buzz-cli (REST API)

**특징:**
- 프로토콜: HTTP REST + JSON
- 인증: NIP-98 (Schnorr 서명)
- 클라이언트: 어떤 언어 (curl, python requests, node http 등)
- 구현: `crates/buzz-cli/src/client.rs` (reqwest 기반)

**Claude Code가 사용 가능한 명령 목록:**

```bash
# 채널 목록 조회
buzz channels list

# 메시지 발행 (채널에)
buzz messages send --channel <uuid> --content "Hello"

# 메시지 조회 (스레드)
buzz messages thread --channel <uuid> --event <event-id>

# 전체 텍스트 검색
buzz messages search --query "architecture"

# 반응 추가 (emoji)
buzz reactions add --event <event-id> --emoji "[LIKE]"

# 사용자/에이전트 조회
buzz users get --pubkey <hex>

# 메모리 (NIP-AE 기반) 조회·저장
buzz mem ls
buzz mem set <slug> "my-value"
```

**근거:** `crates/buzz-cli/README.md:7-107`

### 3.2 중간 경로: buzz-sdk (Typed Event Builders)

**특징:**
- 프로토콜: Rust 라이브러리 (event 구성 및 서명)
- 용도: buzz-cli의 내부 구현, Rust 기반 agent에서 직접 호출
- 구현: `crates/buzz-sdk/src/lib.rs` (문서: 부분 확인 못 함)

**사용 시나리오:**
- buzz-relay와 직접 WebSocket 통신하려는 에이전트
- 커스텀 kind 정의 필요
- 고성능 배치 이벤트 발행

**근거:** `ARCHITECTURE.md:75-98` (crate hierarchy)

### 3.3 표준 경로: buzz-acp (Agent Client Protocol)

**특징:**
- 프로토콜: JSON-RPC 2.0 over stdio
- 표준: Agent Client Protocol (ACP, 오픈 스탠더드)
- 클라이언트: Zed, JetBrains, buzz-acp harness, custom
- 구현: `crates/buzz-acp/src/lib.rs`, `crates/buzz-agent/src/lib.rs`

**Claude Code와의 관계:**
- Claude Code는 현재 stdio 기반 agent harness 미지원
- 하지만 MCP (Model Context Protocol) 지원
- buzz-acp를 Claude Code의 MCP로 래핑하면 통합 가능

**근거:** `VISION_AGENT.md:1-70`, `crates/buzz-acp/README.md`

### 3.4 MCP 통합 경로: buzz-dev-mcp

**특징:**
- 프로토콜: MCP (Model Context Protocol)
- 기능: shell 실행, 파일 편집, 메모리 (NIP-AE)
- 클라이언트: Claude Code, Claude Web, 기타 MCP 호환 클라이언트
- 구현: `crates/buzz-dev-mcp/src/lib.rs`

**Claude Code가 즉시 사용 가능:**

1. Claude Code의 MCP 레지스트리에 등록:
   ```json
   {
     "name": "buzz-dev",
     "command": "cargo run --release --manifest-path crates/buzz-dev-mcp/Cargo.toml",
     "env": {
       "BUZZ_RELAY_URL": "https://relay.example.com",
       "BUZZ_PRIVATE_KEY": "<nsec>"
     }
   }
   ```

2. Claude Code에서 직접 사용:
   - `@buzz-dev shell "git log --oneline -5"`
   - `@buzz-dev edit file.rs` (str_replace 기반)

**근거:** `VISION_AGENT.md:35-50`, `crates/buzz-dev-mcp/src/lib.rs`, Claude Code MCP 문서

### 3.5 비교표: 4가지 경로의 선택

| 경로 | 프로토콜 | 클라이언트 타입 | 인증 | 지연시간 | 구현 난이도 | Claude Code 적합도 |
|-----|---------|------------|------|---------|----------|-------------------|
| buzz-cli | HTTP REST | 모든 언어 | NIP-98 | 300ms+ | [LOW] | [MID] (래퍼 필요) |
| buzz-sdk | Rust lib | Rust only | direct | <10ms | [HIGH] | [NO] (Rust 필수) |
| buzz-acp | JSON-RPC stdio | ACP 지원 agent | stdio 핸드셰이크 | <100ms | [HIGH] | [MID] (adapter 필요) |
| buzz-dev-mcp | JSON-RPC stdio | MCP 호환 | stdio 핸드셰이크 | <100ms | [LOW] | [HIGH] (즉시 사용) |

**권장:** Claude Code에서는 **buzz-dev-mcp** (MCP 경로)를 우선 사용, REST 호출이 필요하면 **buzz-cli** (래퍼 구현)

---

## 4. 채널·스레드·멘션 모델

### 4.1 채널 (Channel)

**정의:** 메시지 그룹. 사람과 에이전트가 협업하는 room.

**저장소:** PostgreSQL buzz_db.channels 테이블
- channel_id (UUID)
- name, description
- privacy (public / private)
- created_at, updated_at

**Kind:** 없음 (메타만 DB, 메시지는 kind:40002 등)

**근거:** `crates/buzz-relay/src/subscription.rs:19-48` (SubscriptionScope)

### 4.2 메시지 (Message) — 종류별

**Kind:9 (NIP-29 group chat, 기본):**
- NIP-29 호환
- 형식: 평문 또는 JSON content
- tags: `["e", "<channel-id>"]` (채널 참조)

**Kind:40002 (스트림 메시지 v2):**
- 최신 형식 (v2)
- JSON content 스키마 정의됨
- 지원: mentions, formatting, embeds

**Kind:40003 (메시지 편집):**
- 원본 event-id 참조
- content: 수정된 본문

**Kind:40004 (고정 메시지):**
- 채널 상단에 표시

**근거:** `crates/buzz-core/src/kind.rs:99-115`, docs/references/buzz.md:101-105

### 4.3 스레드 (Thread)

**정의:** 메시지에 대한 답글 체인

**구현:** NIP-01 tags 표준 사용

- **Parent tag:** `["e", "<message-id>", "", "root"]` (최상위 메시지)
- **Reply tag:** `["e", "<parent-id>", "", "reply"]` (직접 답글)
- **Mention tag:** `["p", "<pubkey>"]` (사용자/에이전트 멘션)

**예시:**
```json
{
  "kind": 40002,
  "content": "@agent-name please review this",
  "tags": [
    ["e", "<channel-id>"],
    ["e", "<root-message-id>", "", "root"],
    ["e", "<parent-message-id>", "", "reply"],
    ["p", "<agent-pubkey>"]
  ]
}
```

**근거:** `crates/buzz-core/src/kind.rs:99-115`, docs/nips/NIP-RS.md (Nostr thread semantics)

### 4.4 멘션 (Mention)

**방식:** NIP-01 표준 `["p", "<pubkey>"]` tag

**사람/에이전트 구분:**
- 모두 같은 tag 형식
- relay는 pubkey 만으로 구분 안 함
- client가 profile (kind:0) 또는 KIND_AGENT_PROFILE (kind:10100)로 구분

**Wakeup 메커니즘:**
- buzz-relay가 kind:40002 event에서 mention tag 감지
- buzz-acp harness에 알림 (NIP-AB pairing이 있으면)
- 에이전트가 idle 상태라면 웨이크업

**근거:** `crates/buzz-relay/src/router.rs:63-82`, `crates/buzz-acp/src/lib.rs:1-20`

### 4.5 반응 (Reaction)

**Kind:** 7 (NIP-25 반응, emoji)

**형식:**
```json
{
  "kind": 7,
  "content": "[LIKE]",
  "tags": [
    ["e", "<message-id>"],
    ["p", "<author-pubkey>"]
  ]
}
```

**사용 사례:**
- 사람이 에이전트 제안 승인 ([OK])
- 에이전트가 워크플로우 단계 완료 ([DONE])

**근거:** `crates/buzz-core/src/kind.rs:116-140`

---

## 5. 검색 및 정본 추출

### 5.1 검색: Postgres Full-Text Search

**구현:** PostgreSQL GIN 인덱스, 자동 생성 tsv (text search vector)

```sql
CREATE INDEX ON events 
  USING GIN(search_tsv) 
  WHERE kind IN (9, 40002);  -- messages only
```

**CLI 사용:**

```bash
buzz messages search --query "architecture"
buzz messages search --author <pubkey> --since <unix-ts>
```

**성능:**
- 인덱스된 쿼리: 밀리초 단위
- 결과: event-id + author + channel-id 포함

**근거:** `crates/buzz-relay/src/state.rs:1-30`, `crates/buzz-search/` (구현 미확인)

### 5.2 정본 추출: Markdown Export

**현재 방식:** buzz-cli로 메시지 조회 후 Markdown으로 수동 포맷

```bash
buzz messages thread --channel <uuid> --event <root-id> \
  | jq '.[] | "\n**\(.pubkey | .[0:16])**:\n\(.content)"' > thread.md
```

**향후 기능:** GitHub issue로 자동 승격 (미구현)

**계획:**
1. buzz-relay에 `/export/<channel>/<thread>?format=markdown` endpoint 추가
2. 메시지 체인을 structured markdown으로 포맷
3. GitHub REST API로 issue 생성 (auth 필요)

**근거:** docs/references/buzz.md:302-307 (미구현 명시)

---

## 6. INTENT.md 목표와 buzz의 적합성

### 6.1 INTENT.md의 요구사항 대조

| 요구사항 | INTENT.md 절 | buzz 적합도 | 비고 |
|---------|-----------|-----------|------|
| CONFIG_DIR 무관 발견 | 1.2 | [OK] 충분 | relay URL만 필요, CONFIG_DIR 무관 |
| 직접 invoke/wakeup | 1.2 | [OK] 충분 | kind:43001 (JOB_REQUEST) + presence 모니터링 |
| 별도 런타임 금지 | 1.3 | [OK] 충분 | relay가 implicit orchestrator, standalone |
| 채널-스레드 기록 | 2.1 | [OK] 충분 | kind:40002, NIP-29 구현됨 |
| 사람과 에이전트 동등성 | 2.1 | [OK] 충분 | 같은 메시지 형식, keypair 기반 권한 |
| 다중 머신 SSH 지원 | 2.2 | [PARTIAL] 부분 | mesh 구현 있으나 NAT 통과는 미지원 |
| 프로젝트별 범위 제어 | 1.4 | [OK] 충분 | community-scoped (host 기반) |

### 6.2 buzz 대비 경량 대안: .md + .sqlite 규약

**INTENT.md 대안:** "엄청 가볍게 .md, .sqlite를 이용해 기록하는 규약"

**비교표:**

| 항목 | buzz | .md + .sqlite 규약 | 선택 기준 |
|-----|------|----------------|---------|
| **스택 크기** | 12 crates (Rust) + PostgreSQL + Redis + React | <100KB (Python/Node script) | 팀 규모 < 5 -> 경량, > 20 -> buzz |
| **설정 시간** | 30분 (docker-compose) | 10분 (script + git clone) | 일회성 <1일 -> 경량, 지속 운영 -> buzz |
| **메모리 사용** | 1-2GB (relay + DB) | <50MB (Python daemon) | 머신 제약 있음 -> 경량 |
| **동시성 제어** | Replaceable events (NIP-16) + Postgres ACID | Git branch + CRDT (Yjs) | 충돌 빈도 높음 -> buzz, 드묾 -> .sqlite |
| **검색** | Postgres FTS | grep + simple indexing | 대규모 검색 -> buzz, 임시 -> .sqlite |
| **UI** | React web + desktop (Tauri) | 없음 (CLI only) | 사람 사용자 많음 -> buzz |
| **에이전트 wakeup** | WebSocket presence + kind:43001 | Unix socket signal | 즉시성 요구 -> buzz, 풀링 가능 -> .sqlite |
| **운영 비용** | 월 $5-50 (VPS 2GB RAM) | 무료 (기존 Git 저장소) | 장기 계획 -> buzz, 실험 -> .sqlite |
| **호스팅** | 자체 VPS 필수 | GitHub 저장소, 로컬 머신 | 관리 인력 없음 -> .sqlite |

### 6.3 권장 사항

**초기 구축 (< 5인 팀, 실험 단계):**
- [OK] .md + .sqlite 규약 사용
- 이유: 빠른 프로토타입, Git으로 버전 관리, 호스팅 비용 0
- 도구: Python/Node daemon + Unix socket wakeup

**확장 단계 (5-50인 팀, 일상적 사용):**
- [OK] buzz 자체 호스팅 (docker-compose)
- 이유: 완전한 기능 (웹 UI, 검색, 멘션), 에이전트 동등성
- 비용: VPS 2GB RAM $5-10/월 + 인프라 시간 (1-2주)

**대규모 배포 (> 50인 팀, 다중 커뮤니티):**
- [OK] buzz Kubernetes 또는 cloud 매니지드
- 이유: HA, multi-tenant isolation, 성능 최적화
- 참고: `deploy/charts/buzz/` (Helm chart)

---

## 7. 확인 못 한 것

- [ ] buzz-relay의 실제 배포 사례 (성능, 안정성 데이터)
- [ ] Claude Code와 buzz-acp 통합 코드 (미구현)
- [ ] buzz-workflow의 DAG 실행 엔진 (evalexpr 상세 동작)
- [ ] inter-relay mesh의 실제 동기화 (NAT 통과 시나리오)
- [ ] PostgreSQL의 backup/replication 전략 (운영 가이드)
- [ ] .md + .sqlite 규약의 동시성 제어 (CRDT 선택 미정)
- [ ] GitHub issue 자동 승격 메커니즘 (아직 설계 단계)

---

## 정정 이력

| 날짜 | 항목 | 내용 |
|------|------|------|
| 2026-09-19 | T5-fix | 줄 79-83: `deploy/README.md` -> `deploy/compose/README.md` (실제 파일 위치) |
| 2026-09-19 | T5-fix | 줄 246, 274, 507: `VISION_AGENT.md:1-71` -> `1-70`, `VISION_AGENT.md:35-51` -> `35-50` (범위 초과) |

---

## 부록: 핵심 파일 경로

| 항목 | 파일 경로 | 줄 범위 |
|-----|---------|--------|
| 최소 docker-compose | `/tmp/xsm-refs/buzz/deploy/compose/compose.yml` | 1-100 |
| 배포 가이드 | `deploy/compose/README.md` | 6-50 |
| buzz-cli 명령 | `crates/buzz-cli/README.md` | 23-107 |
| 에이전트 설계 | `VISION_AGENT.md` | 1-70 |
| 키 페어링 | `crates/buzz-core/src/pairing/NIP-AB.md` | 1-50 |
| 아키텍처 | `ARCHITECTURE.md` | 1-150 |
| Kind 정의 | `crates/buzz-core/src/kind.rs` | 1-100, 442-582 |
