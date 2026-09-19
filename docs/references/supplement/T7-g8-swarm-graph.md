# T7: G8 요구사항 도출 (Swarm Agents, Graph Engineering)

## 결론

G8(Swarm Agents 및 Graph Engineering) 관련 요구사항이 두 가지 주요 원본에서 구체적으로 도출되었다:

1. **Swarm Messaging 패턴**: OpenAI의 멀티에이전트 시스템이 "최소 구조(primitive messaging)"를 통해 에이전트 간 자율적 협업을 구현하는 방식. 메시징 채널과 기본 도구만 제공하고 에이전트가 자체적으로 조직화.

2. **Knowledge Graph 기반 작업 기록**: Kirill K3 게시물에서 제시된 8계층 아키텍처(Ingestion, Extraction, Resolution, Storage, Retrieval, Agent, Verification, Update). LLM이 지식 그래프를 통해 구조화된 정보를 검색·검증하며 작업을 수행하는 패턴. **해석**: 이 패턴을 "메시징 채널·작업 기록을 지식 그래프로 구조화해 에이전트 검색·검증에 쓰는 것"으로 해석하면, INTENT.md 추가2)의 에이전트 협업 채널 요구사항과 연결된다.

X 게시물은 접근 제한이 있었으나 **fxtwitter API를 통해 성공적으로 획득**했으며, 게시물의 실제 주제는 GraphRAG 기반의 LLM+지식 그래프 파이프라인이다. 본문 135개 블록 중 "swarm", "message", "messaging"은 0회, "agent"는 4회만 나타난다.

## 리뷰 항목별 해소 결과

| 항목 ID | 원본 결함 | 결론 | 근거 |
|---------|---------|------|------|
| R5-F5 | G8 요구사항이 전혀 도출되지 않음 | **해소됨** | OpenAI 영상(primitive messaging, 메시징 기반 협업), Lauren Tan 영상(에이전트 정체성, 비개발자 접근성), Graph Engineering 가이드(8계층 구조) |
| R5-F7 | X 게시물 Jina Reader 403 AbuseAlleviationError | **해소됨** | fxtwitter API (https://api.fxtwitter.com) 를 통해 성공 획득. 게시물 제목: "Graph Engineering with Kimi K3: Complete A–Z Guide..." |

---

## 상세 분석

### 1. Swarm Agents의 메시징 패턴 (OpenAI 사례)

#### 1.1 기본 철학: Primitive Messaging

**출처**: OpenAI_researcher_on_agent_swarms_&_recursive_self-improvement.txt, 줄 45

> "So we give the agents the ability to message another agent, and when it messages another agent, it is inserted into the context, and then it can do like a few other similar things, but that's basically the core of it, that it can just send a message whenever it wants, just a tool call."

**의미**: 에이전트에게 부여하는 기본 도구는 다른 에이전트로의 메시지 전송 능력뿐. 이를 통해 에이전트들이 자율적으로 협업 방식을 발견하고 조직화.

#### 1.2 스캐폴딩 vs 최소 구조

**출처**: 줄 39-45 (09:30-11:00 섹션)

기존 접근:
- 코디네이터 → 자식 에이전트 위임 구조
- 에이전트 간 직접 통신 불가
- 각 에이전트는 독립적으로 작업 수행

OpenAI 접근:
- 에이전트들에게 **메시지 전송 도구만** 제공
- 자체적으로 협업 방식 결정
- Slack 같은 자연스러운 대화 양상 관찰

#### 1.3 협업의 질감

**출처**: 줄 47-49 (11:30-12:00 섹션)

> "when we finally got it working to see these agents working on problems together... one agent says, I think I've got the answer. And then another agent says, actually, I got a different answer. And then they have this whole discussion about, well, how did you arrive at that answer? ... they finally converge and ... I think he's right. And then it just broadcast to the other agents"

**요구사항**: 
- 에이전트 간 비동기 메시징 채널
- 합의/수렴 메커니즘 (명시적 구현 없이 에이전트가 자체 발현)
- 메시지 히스토리 추적 (context에 삽입되어 지속성 확보)

---

### 2. Grokbot을 통한 다중 에이전트 정체성과 사용성

#### 2.1 에이전트 정체성 부여

**출처**: 한영자막_Cursor_핵심_개발자_Lauren_Tan.txt, 줄 229-231

> "you can give your agent... You can have a fun name... each agent is like a person, and now you got a team of agents like working on"

**요구사항**:
- 각 에이전트는 고유한 정체성(이름, 역할, 특성) 필요
- 사용자 관점에서 에이전트들이 구분 가능해야 함
- 협업 시 "팀"으로 인식 가능한 UI/UX

#### 2.2 비개발자 접근성과 업무 위임

**출처**: 줄 229, 231, 233

> "agents for everyone... very accessible way to use agents in a very comfortable, very familiar interface... if you're a PM, you have, you know, you can have an agent that summarizes all the work that Lauren did last night"

> "designers and PMs are just able to ship features directly"

**요구사항**:
- 메시징 기반 UI (iMessage 같은 친숙한 인터페이스)
- 에이전트 간 협업 결과 명확한 제시
- 비기술자도 에이전트에 작업 위임 가능한 수준

---

### 3. Graph Engineering의 구조화된 메시징 아키텍처

#### 3.1 X 게시물 획득 경과

- **초기 시도**: Jina Reader (403 AbuseAlleviationError)
- **성공 경로**: fxtwitter API (`https://api.fxtwitter.com/kirillk_web3/status/2087619214915826155`)
- **게시물 제목**: "Graph Engineering with Kimi K3: Complete A–Z Guide to the Architecture That Beats Bigger Models"
- **게시물 작성일**: Wed Aug 12 19:16:21 +0000 2026

#### 3.2 8계층 아키텍처

**출처**: 게시물 본문 "The Architecture, Layer by Layer" 섹션 (14개 header 중 6번째)

게시물의 8계층 (원문 인용):
```
1. Ingestion — PDFs, web pages, databases, APIs, Slack, Notion... Raw material only, no processing yet.
2. Extraction — K3 reads each source and pulls out entities and relationships as structured JSON
3. Resolution — Are "Moonshot AI," "Moonshot," "Beijing Moonshot," and "月之暗面" the same entity?
4. Storage — Neo4j, Memgraph, Neptune, or plain PostgreSQL with a graph extension.
5. Retrieval — this is not one method, it's five working together: vector search... entity lookup... path search...
6. Agent — K3 plans the approach, generates Cypher queries, reads the returned subgraph, runs additional searches when it hits a gap
7. Verification — checks that conclusions are actually supported by retrieved paths, flags contradictions
8. Update — new facts go into the graph, contradictions get flagged rather than silently overwritten
```

**구조**: 순환 구조 (8→2): Update 계층의 새 사실이 다시 Extraction 단계로 흘러 지식 그래프를 지속 강화.

**해석**: 이 계층들 간의 "메시징"은 게시물에 명시되지 않음. 각 계층은 LLM(K3)의 역할 전환, 또는 구조화된 프롬프트(Prompt 1-5)를 통한 단계적 처리이다. INTENT.md의 에이전트 메시징 채널 요구사항과 연결하려면, "각 계층을 독립 에이전트로 구성하고 구조화된 메시지로 통신"하는 설계로 확장하는 것이 필요하다.

#### 3.3 5가지 핵심 프롬프트

**출처**: 게시물 본문 "5 Prompts That Run the Pipeline" 섹션 (14개 헤더 중 7번째)

게시물이 제시한 5가지 프롬프트:
1. **Prompt 1 — Extraction**: 문서에서 엔티티, 관계, 신뢰도를 structured JSON으로 추출. 증거는 원문 인용 필수.
2. **Prompt 2 — Entity Resolution**: 엔티티 후보들이 같은 개체인지 판정. 기준: canonical_name, verdict (same/related/unrelated), confidence.
3. **Prompt 3 — Query Translation**: 사용자 질문을 Cypher 쿼리로 변환. 스키마와 노드 레이블을 전달받아 적용.
4. **Prompt 4 — Grounded Answer**: 그래프 경로를 받아서 근거 있는 답변 생성. 각 클레임마다 구체적 노드/경로 인용, 추론 여부 플래그.
5. **Prompt 5 — Graph Maintenance**: 새 사실과 기존 그래프를 비교해 new/duplicate/contradiction/update/uncertain로 분류. 기존 사실 덮어쓰지 않고 타임스탬프 부여.

**설계 함의**: 각 프롬프트는 단일 LLM(K3)의 역할 전환을 위한 지시이다. 이를 "에이전트 간 메시징"으로 구현하려면, 각 프롬프트의 입출력을 메시지 스키마로 정의해야 한다.

---

### 4. 메시징 채널·스웜·그래프 통합 요구사항

#### 4.1 기본 도구 및 메시징 채널 (G1/G6와의 관계)

**추출된 요구사항 R-G8-01**: 다른 에이전트로의 메시지 전송이 최소 단위 도구
- **근거**: OpenAI 영상, 줄 45
- **INTENT.md와의 매핑**: "기본 도구로서의 메시지 전송" (INTENT.md, 추가1) 줄 19 참조)

**R-G8-02**: 메시징 채널은 즉시적 협업(invoke/wakeup)과 비동기 기록(1:N, N:N)을 동시 지원
- **근거**: OpenAI 사례 + Grokbot 사례의 조합
- **INTENT.md와의 매핑**: "cross-sessions messaging" + "messaging channel" (줄 3, 17-20)

#### 4.2 에이전트 정체성 및 협업 토폴로지

**R-G8-03**: 각 에이전트는 고유 정체성 필요 (이름, 역할)
- **근거**: Lauren Tan 영상, 줄 229
- **설계 영향**: 사용자가 메시지 송신자/수신자를 명확히 구분 가능

**R-G8-04**: 협업 토폴로지는 동적으로 형성 (스캐폴딩 불필요)
- **근거**: OpenAI, 줄 45-49 ("자율적으로 협업하게 함", "자연스러운 협업 양상")
- **설계 영향**: 중앙 코디네이터 불필요, 모든 에이전트가 동등한 메시징 능력

#### 4.3 그래프 구조화와 메시징 프로토콜

**R-G8-05**: 에이전트 간 통신은 구조화된 메시지 형식 (JSON 스키마)
- **근거**: Graph Engineering 게시물 (Prompt 1-5에서 엔티티/관계 JSON, Cypher 쿼리, 갈등 분류 등 구조화된 형식 사용)
- **해석**: 게시물은 단일 K3 모델의 역할 전환을 프롬프트로 구현. 이를 "에이전트 간 메시지 프로토콜"로 확장하려면 각 프롬프트의 입출력을 메시지 포맷으로 표준화해야 함.
- **설계 영향**: 메시지 검증, 버전 호환성, 자동화 가능성

**R-G8-06**: 메시징은 루프를 형성 (피드백 루프, 지식 증강)
- **근거**: Graph Engineering, 계층 8(Update) → 2(Extraction) 순환. 게시물 본문: "The loop closes at step 8 and starts again at step 2. That's what makes it compound."
- **해석**: 루프는 데이터 재입력이지 "에이전트 간 메시징"은 아님. 에이전트 협업의 "피드백 루프"로 해석하려면, Update 결과를 다음 에이전트의 입력으로 전달하는 구조가 필요함.
- **설계 영향**: 시스템 지속 강화, 에이전트 간 협업의 누적 학습

#### 4.4 검증 및 신뢰 메커니즘

**R-G8-07**: 메시지 검증 계층 필수 (Graph Engineering의 Verification 계층)
- **근거**: Graph Engineering, 계층 7 (Verification): "checks that conclusions are actually supported by retrieved paths, flags contradictions, evaluates confidence, verifies sources"
- **해석**: Verification은 LLM의 회답 검증 역할이지, "에이전트 메시지 검증"은 아님. 에이전트 협업에서 "신뢰도 평가"로 해석하면, 에이전트 간 메시지에 confidence score를 부여하고, 낮은 신뢰도 메시지는 플래그 처리하는 설계가 필요함.
- **설계 영향**: 에이전트가 신뢰할 수 있는 정보만 후속 메시징에 사용, 갈등 메시지 추적

**R-G8-08**: 인간 개입 지점 명확화
- **근거**: Grokbot 사례 (PM이 결과 검토 후 승인), Graph Engineering (Update 계층에서 갈등 플래그)
- **설계 영향**: 자동화와 검증 사이 균형점 설정

#### 4.5 비개발자 접근성

**R-G8-09**: 메시징 인터페이스는 기술 배경 없이 사용 가능
- **근거**: Lauren Tan, 줄 231 (PMs, designers가 이미 사용 중)
- **설계 영향**: 메시지 형식 숨김, 사용자 관점에서는 "에이전트에 작업 위임" 수준

#### 4.6 기록과 재생

**R-G8-10**: 모든 에이전트 간 메시지는 지속성 있게 기록되고 추적 가능
- **근거 (OpenAI)**: OpenAI_researcher_on_agent_swarms.txt, 줄 45: "when it messages another agent, it is inserted into the context" — 메시지는 컨텍스트에 포함되어 다음 에이전트가 볼 수 있음.
- **근거 (Graph Engineering)**: 게시물, 계층 8 (Update): "timestamps on everything, and a scheduled maintenance pass with Prompt 5" — 모든 변화를 타임스탐프와 함께 기록.
- **해석**: OpenAI의 "context insertion"은 메시지 기록이 아니라 현재 컨텍스트 윈도우 내의 포함. Graph Engineering의 타임스탐프는 그래프 갱신 히스토리. 이 둘을 "에이전트 메시지 기록 시스템"으로 통합하려면, 메시지 ID, 송수신자, 타임스탐프, 컨텍스트 참조를 포함한 로그 스키마가 필요.
- **설계 영향**: 에이전트 협업 히스토리 재구성, 감사(audit) 가능, 모니터링

---

### 5. 현재 계획·ADR 중 반영 필요 위치

#### 5.1 계획(docs/plan/README.md)

**P8 (G8 Swarm·Graph 패턴 정리)**가 이제 구체적 요구사항으로 세분화 가능:

- **P0-P1 선행 작업**: 메시징 채널 기본 프로토콜 정의
  - 메시지 스키마 (JSON 포맷)
  - 에이전트 정체성 표현 (name, role, identity)
  
- **P2 (발견 범위·보안)**: 에이전트 정체성 발견 범위 명확화
  - 같은 CONFIG_DIR 내 에이전트만 메시징 가능한지, 외부 에이전트 포함 가능한지 결정

- **P6 (채널-스레드 저장소)**: Graph Engineering의 검증/갱신 계층 구현
  - 메시지 검증 프로토콜
  - 갈등 플래그 메커니즘

- **P8**: 에이전트 간 메시징 루프와 피드백 메커니즘 설계

#### 5.2 ADR 대응

**ADR-0001** (세션 레지스트리)
- 요구사항 R-G8-03 반영: 에이전트 정체성 저장소 필요
- 현재 "Proposed" 상태이나, 이제 구체적 필드 정의 가능

**ADR-0005** (채널-스레드 저장소)
- 요구사항 R-G8-10 반영: 메시지 기록 모델
- Graph Engineering의 Update 계층 (contradiction flagging)과 병렬화 가능

**ADR-0007** (멀티에이전트 오케스트레이션) - 신규
- 요구사항 R-G8-02, R-G8-04, R-G8-06 통합
- 스웜 협업 토폴로지와 메시징 루프 정의

---

### 6. 기존 레퍼런스와의 대응

#### Orca DAG·게이트

**관련성**: 높음
- Orca의 Task 구조 (task_id, dispatch_id, phase)는 Graph Engineering의 계층 구조와 유사
- 메시징 프로토콜: Orca의 `orchestration send --type` 과 G8의 Prompt 1-5 비교
- **차이점**: Orca는 명시적 DAG 정의, G8은 메시징만으로 토폴로지 동적 형성

#### Agora DAG (논문: arxiv.org/abs/2609.18094)

**관련성**: 중간
- Agora는 협업 문서 편집에 초점 (CRDT/OT)
- G8은 에이전트 간 작업 협업 (메시징 기반)
- **교집합**: 동시성 제어, 갈등 해결 메커니즘 (R-G8-08과 유사)

#### Buzz 워크플로

**관련성**: 높음
- Buzz는 채널-기반 메시징 (1:N, N:N)
- G8의 메시징 채널 구현 시 Buzz 모델 검토 필요
- **구체화 필요**: INTENT.md 추가1) 의 "buzz, slack, discord 검토" (줄 20) 이행

---

## 확인 못 한 것

### 1. OpenAI 영상의 후속 세부사항

- **현황**: 요약과 전문에서 확인된 기본 메시징 철학, 협업 사례는 명확하나,
  - 메시지 형식의 구체적 스키마 미언급
  - 시간초과(timeout), 실패 처리, 재시도 로직 관련 내용 없음
- **확인 필요**: 추가 OpenAI 문서 또는 o1/o3 릴리스 노트

### 2. Lauren Tan 영상의 상용 구현 세부사항

- **현황**: Grokbot의 에이전트 정체성과 PM 접근성 확인됨.
  - 내부 메시징 프로토콜 구체화 미언급
  - Cursor 클라우드 에이전트 간 통신 방식 (socket, HTTP, 기타) 불명
- **확인 필요**: Cursor 공개 문서 또는 심화 기술 세션

### 3. Graph Engineering 게시물의 멀티에이전트 미커버

- **현황**: 게시물의 8계층 아키텍처는 **단일 K3 모델이 각 프롬프트를 순차 실행**하는 구조.
  - 게시물 본문 135개 블록에 "agent"는 4회만 등장: (1) "Agent layer (6계층)", (2) "cloud agents", (3) "agents" (일반), (4) "sub-agents"
  - "swarm", "message", "messaging" 등 멀티에이전트 협업 용어는 0회
  - 멀티에이전트 동시성 제어, 메시지 프로토콜 미언급
- **확인 필요**: 이 게시물은 G8의 "협업" 패턴을 직접 제시하지 않음. INTENT.md 추가2)의 "메시징 채널을 지식 그래프로 구조화"하는 해석은 **추가 설계 작업 필요**

### 4. INTENT.md의 "별도 진입점 런타임 금지" 정책과 G8의 양립성

- **현황**: 
  - R-G8-04 ("협업 토폴로지는 동적으로 형성")는 스캐폴딩 불필요를 의미
  - INTENT.md 줄 15: "codex, claude 위에 오케스트레이션을 위한 별도의 진입점이 되는 런타임을 만들지 말 것"
  - 두 정책이 충돌할 여지 있음 (동적 토폴로지 생성이 숨겨진 코디네이터 필요 가능성)
- **확인 필요**: P8 설계 단계에서 명시적 토론 필요

### 5. OpenAI 모델과 Kimi K3의 메시징 호환성

- **현황**: 
  - OpenAI o1/o3 (primitive messaging)
  - Kimi K3 (Graph Engineering, 긴 컨텍스트, Delta Attention)
  - 서로 다른 모델의 메시징 포맷이 상호 운용 가능한지 불명
- **확인 필요**: LLM 모델 중립적(model-agnostic) 메시징 프로토콜 설계 필요성 검토

---

## 참고: 원본 인용 위치 정리 및 근거 수준

| 요구사항 | 출처 | 좌표 | 근거 수준 |
|---------|------|------|---------|
| R-G8-01 | OpenAI 영상 전문 | OpenAI_researcher_on_agent_swarms_&_recursive_self-improvement.txt: 45 | **확인됨** |
| R-G8-02 | OpenAI + Grokbot | 영상 45-49줄 + Lauren Tan 한영자막...txt: 229-233줄 | **확인됨** |
| R-G8-03 | Lauren Tan 영상 | 한영자막_Cursor...txt: 229 | **확인됨** |
| R-G8-04 | OpenAI 영상 | OpenAI...txt: 39-49줄 | **확인됨** |
| R-G8-05 | Graph Engineering | X 게시물, Prompt 1-5 섹션 | **해석**: 게시물은 단일 K3 역할 전환. 에이전트 메시징으로 확장은 추가 설계 필요 |
| R-G8-06 | Graph Engineering | X 게시물, 8계층 루프 (8→2) | **해석**: 루프는 데이터 재입력이지 에이전트 협업 메시징이 아님 |
| R-G8-07 | Graph Engineering | X 게시물, Layer 7 설명 | **해석**: Verification은 LLM 회답 검증. 에이전트 메시지 검증으로 해석 필요 |
| R-G8-08 | Grokbot | Lauren Tan 한영자막...txt: 233줄 | **확인됨** (Graph Engineering 근거 제거) |
| R-G8-09 | Lauren Tan 영상 | 한영자막_Cursor...txt: 231줄 | **확인됨** |
| R-G8-10 | OpenAI + Graph Engineering | OpenAI...txt: 45줄 + 게시물 Update 계층 | **부분 확인**: OpenAI 근거는 확실. 게시물은 데이터 타임스탐프로 해석 필요 |

---

## 정정 이력

### T7-fix (2026-09-19)

코디네이터 검토 결과 다음 항목을 정정했다:

1. **X 게시물 내용 재평가**
   - 게시물 제목: "Graph Engineering with Kimi K3: Complete A–Z Guide to the Architecture That Beats Bigger Models"
   - 실제 주제: LLM+지식 그래프 기반의 검색·검증 파이프라인 (GraphRAG 계열)
   - 본문 통계: 135개 블록, "swarm" 0회, "message(ing)" 0회, "agent" 4회만 등장
   - 결론 수정: "메시징/스웜 아키텍처와 직접적인 관련성"은 삭제. 게시물은 멀티에이전트 협업 메시징을 다루지 않음.

2. **8계층 정확화**
   - 원문 인용: Ingestion, Extraction, Resolution, Storage, Retrieval, Agent, Verification, Update (8개)
   - 계층 간 "메시징"은 게시물에 명시되지 않음. 각 계층은 K3의 역할 전환 또는 프롬프트 기반 단계.
   - INTENT.md와의 연결: "메시징 채널·작업 기록을 지식 그래프로 구조화"하는 **해석**으로 표시

3. **5가지 프롬프트 재정의**
   - 게시물의 실제 프롬프트: Extraction, Entity Resolution, Query Translation, Grounded Answer, Graph Maintenance
   - 각 프롬프트는 단일 K3 모델의 역할 전환을 위한 지시. "에이전트 간 메시징 프로토콜"로 해석하려면 추가 설계 필요.

4. **근거 수준 명확화**
   - Graph Engineering 관련 요구사항 (R-G8-05, R-G8-06, R-G8-07, R-G8-10): "**해석**" 표시 추가
   - OpenAI 영상 + Lauren Tan 영상: "**확인됨**" 표시 유지
   - 각 요구사항별 근거의 강도와 한계 명시

5. **"확인 못 한 것" 섹션 보완**
   - 항목 3 (Graph Engineering 멀티에이전트 미커버): 게시물이 단일 K3 모델 기준임을 강조
   - 게시물의 협업 패턴은 INTENT.md 추가2)과 직접 매칭되지 않으며, 추가 해석 필요함을 명시
