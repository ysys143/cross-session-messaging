# AgentGrid 조사

AgentGrid는 "Open Agentic Web"이라는 비전 아래, SDK로 새로 만든 에이전트를 Kubernetes 위에서 돌리는 Python 에이전트 프레임워크다. 사람이 띄운 Claude Code·Codex 세션을 잇는 xsm과는 범주가 다르고, ADR-0014의 보드 후보로도 맞지 않는다. SDK 소스를 확인한 결과 Claude Code·Codex 같은 범용 에이전트 런타임에 준하는 기능(도구 호출 루프, 파일·셸 도구, 권한 모델, 컨텍스트 관리)은 없다(5장). 구현을 가져올 것은 없고, 개념 두 가지만 참고할 만하다(6장).

- 작성일: 2026-10-03
- 출처(모두 2026-10-03 조회):
  - 문서: `https://docs.agentgr.id/`의 첫 화면, `agents/agent-chat-p2p/`, `agents/concepts/communication-system/`, `agents/a2a/` (Jina Reader)
  - 저장소 메타데이터: GitHub API `repos/opencyber-space/AgentGr.id`, `orgs/opencyber-space/repos`
  - 소스: `git clone --depth 1`, 커밋 `f67296a` (2026-07-09). Python 파일 264개, 37,747행
- 방법: 문서 네 페이지, GitHub 메타데이터, 소스의 정적 조사(grep과 일부 파일 열람). 비전 논문(`resources.agentgr.id`)과 하위 프로젝트 저장소(AIOS 등)는 읽지 않았고, 실행하지 않았다.

## 1. 정의

- 문서는 에이전트 아키텍처를 세대로 나눈다. Gen-1은 단일 AI 에이전트, Gen-2는 멀티 에이전트 시스템, Gen-3이 AgentGrid다. Gen-3을 "수십억 에이전트가 서로 발견하고, 협상하고, 협업하는 에이전트 문명"으로 설명한다.
- 내세우는 능력: 발견, 탈중앙 통신 메시, 개방형 프로토콜, 신원과 평판, 경제와 거래, 협상과 계약, 집단 거버넌스, 지식 공유, 연합, 확장성.
- 10개 하위 프로젝트의 묶음이다: AIOS(런타임), PolicyGrid(거버넌스), OpenArcade(에이전트 집단 전략), ServiceGrid(도구 발견), Xchange.id(작업 거래소), OpenMe.sh(통신 메시), ContractGr.id(계약), Pervasive.Link(메타 프로토콜), OpenHub.ai(시장 허브), AgencyGr.id(조직).

## 2. 에이전트 SDK

- **계획과 실행:** LLM이 목표를 플래너 작업으로 나누고, 작업 DAG(순환과 재귀 포함)를 재시도·대체·dry-run과 함께 실행한다. 실행 대상은 도구, LLM, DSL 워크플로, 다른 에이전트 API다.
- **위임과 검증:** 작업을 입찰, 투표, DSL 라우팅으로 에이전트에 배정한다. 사람과 에이전트의 검증 흐름이 있고, 진행 상황은 WebSocket과 DB watcher로 전한다.
- **레지스트리:** 도구, 함수, DSL 워크플로를 메타데이터와 버전으로 등록한다.
- **런타임 코드 생성:** LLM이 만든 Python을 샌드박스에서 실행한다.
- **기술 스택:** NATS, WebSocket, Redis Pub/Sub, MongoDB, FrameDB(Redis 기반), S3 호환 저장소, 벡터 DB(FAISS, Milvus, Weaviate, Qdrant, LanceDB). Kubernetes 네이티브를 전제로 한다.

## 3. 통신

- **P2P 모듈(`agent_sdk.p2p`):** 에이전트마다 HTTP API 서버(예: 6666)와 WebSocket 서버(예: 8766)를 띄운다. `P2PManager.send_recv`, `send_api`로 메시지, API 호출, 파일 전송, 원격 작업 실행을 한다.
- **통신 시스템:** 수신 경로가 단계로 나뉜다. 수신(NATS 주제 또는 WebSocket) → 발신자별 rate limit(DSL로 설정, 넘으면 버림) → 파싱 → 버퍼 → 우선순위 지정 → 부하 분산 → 에이전트 인스턴스. 자원 추정과 자동 확장 단계에는 사람의 개입을 넣을 수 있다.
- **"A2A" 페이지:** 제목은 "A2A - Agent to Agent Communication Protocol"이지만, 내용은 자체 `MessageHandler` 프레임워크(주체별 입출력 스키마 검증, DSL 변환, 메시지 종류 조회)다. Agent Card, JSON-RPC, A2A 명세에 대한 언급이 없다. `a2a.md`의 A2A 표준과는 다른 것이다.

## 4. 프로젝트 상태

| 항목 | 값 |
|---|---|
| 저장소 | `opencyber-space/AgentGr.id` |
| 라이선스 | AGPL-3.0 |
| 별 | 6 |
| 커밋 | 20 |
| 생성 | 2025-06-17 |
| 마지막 푸시 | 2026-07-09 |
| 문서상 상태 | Alpha. "Not Production-Ready", API·스키마·설정이 예고 없이 바뀔 수 있다 |

같은 조직의 하위 저장소 17개는 별이 0~1개이고, 대부분 2026-07-09에 마지막으로 푸시됐다.

## 5. SDK 소스 확인

Claude Code·Codex 같은 범용 에이전트 런타임에 준하는 기능이 있는지 소스에서 찾았다. 경로는 저장소의 `src/` 기준이다.

| 범용 에이전트 런타임의 기능 | AgentGrid SDK |
|---|---|
| 도구 호출 루프(모델이 부른 도구를 실행하고 결과를 다시 넣어 반복) | 없다. OpenAI에 넘길 인자 목록에 `tools`, `tool_choice`가 있지만(`libs/llm-interface/agents_llm/custom.py:388`), 응답의 `tool_calls`를 읽는 코드가 소스 전체에 없다 |
| 파일 읽기·편집, 셸 실행 도구 | 없다. `subprocess`가 나오는 10개 파일은 모두 `pip install -r requirements.txt`다(`libs/agents-sdk/core/policy_sandbox/code_executor.py:103` 등) |
| 권한·승인 모델 | 없다. `permission`, `approval`이 소스에 나오지 않는다 |
| 컨텍스트 관리(요약, 압축) | 없다. `summariz`로 걸리는 것은 메타데이터 목록을 줄이는 함수다(`agents-job-scouter/core/metadata.py:16`) |
| 모델 공급자 | OpenAI 호환 API(`chat.completions.create`)와 자체 gRPC 추론 서버. Anthropic/Claude와 MCP는 소스에 없다 |
| 사람이 쓰는 대화형 인터페이스 | 없다. 서버형 서비스다 |

있는 것은 LLM으로 작업 DAG를 만들고 실행하는 플래너·컨트롤러, DSL 워크플로, MongoDB 작업 큐, 레지스트리, 위임 프록시, LLM이 만든 Python의 실행이다. 이 SDK의 "에이전트"는 LLM에게 계획을 짜게 하고 미리 등록한 함수와 워크플로를 실행하는 서비스다.

LLM 백엔드는 추상화했지만(1·2장), 기존 에이전트 하니스(Claude Code, Codex, goose)를 런타임으로 끼우는 층은 없다. 같은 문제를 Buzz는 ACP 뒤로 런타임을 숨겨서(`buzz.md`, `acp.md`), xsm은 런타임을 소유하지 않고 사람이 띄운 세션에 붙어서 푼다.

코드에서 함께 확인한 것:

- **실행 격리가 없다.** 문서는 "Sandboxed code execution"이라고 하지만, 생성 코드는 같은 프로세스의 `exec`로 실행한다. `libs/agent-workflows/agent_workflows/controller.py:185`는 `__builtins__`를 비운 전역으로, `libs/codegen-sdk/agents_codegen/generator.py:175`는 모듈 네임스페이스에서 그대로 실행한다. 같은 프로세스에서 builtins를 비우는 것은 우회 방법이 잘 알려져 있어 보안 경계가 아니다.
- **같은 파일을 서비스마다 복사했다.** `policy_sandbox/code_executor.py`가 6곳에 있다. `deployer`, `agents-executor`, `libs/agents-sdk`의 세 사본은 MD5가 같았다(나머지 셋은 비교하지 않았다).

## 6. xsm에 대한 시사점

| 판단 | 내용 |
|---|---|
| 해당 없음 | 대상이 SDK로 새로 만드는 에이전트다. 확인한 네 페이지에 Claude, Codex, MCP, 터미널 세션에 대한 언급이 없다 |
| 해당 없음 | 참여하려면 에이전트를 이 SDK로 다시 만들어야 한다. 이미 쓰는 Claude Code·Codex를 데려올 길이 없다(5장) |
| 해당 없음 | NATS, MongoDB, Redis, Kubernetes를 전제로 한다. 상주 런타임 없이 도는 xsm(ADR-0003)과 반대이고, ADR-0014의 보드 후보로도 Buzz보다 무겁다 |
| 참고 | 위임 방식(입찰, 투표, 플래너 라우팅)을 나란히 둔다. ADR-0012(다음 노드는 누가 고르나)의 선택지 비교에 레퍼런스로 쓸 수 있다 |
| 참고 | 발신자별 rate limit을 수신 경로의 첫 단계에 둔다. ADR-0005 critic이 지적한 멘션 폭주 대책, ADR-0014 B·C의 멘션 호출 상한과 같은 방향이다 |
