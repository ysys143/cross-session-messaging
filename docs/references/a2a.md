# A2A 조사

A2A(Agent2Agent)는 서로 다른 프레임워크로 만든, 내부가 감춰진(opaque) 에이전트 시스템끼리 작업을 맡기고 결과를 주고받게 하는 개방형 프로토콜이다. 에이전트는 서버(A2A Server)로 노출되고, 상대는 Agent Card로 능력을 알아낸 뒤 Task 단위로 일을 맡긴다. 사람이 띄운 터미널 세션에 붙는 xsm과는 출발점이 다르다. xsm과의 비교는 `xsm-a2a-agent-comms.md` 1장과 4장에 있다.

- 작성일: 2026-10-02
- 출처: 명세 원문 `https://a2a-protocol.org/latest/specification/` (Latest Released Version 1.0.0, 2026-10-02 Jina Reader로 조회). 아래 절 번호는 명세의 절 번호다.
- 방법: 명세 열람만 했다. SDK(Python, Go, Java, JS, Rust 등) 설치나 실제 서버 간 통신은 하지 않았다.
- 거버넌스: 조회 당시 사이트 상단 배너가 "A2A joins the Agentic AI Foundation" 블로그 글을 알렸다. 공지 본문은 읽지 않았다.

## 1. 목표와 원칙 (1.1, 1.2절)

- 목표: 이질적 에이전트 시스템 간 상호운용, 작업 위임과 문맥 교환, 동적 능력 발견, 동기·스트리밍·비동기 푸시를 아우르는 상호작용, 엔터프라이즈 보안, 장기 작업과 사람 개입(human-in-the-loop).
- 원칙: 기존 표준 재사용(HTTP, JSON-RPC 2.0, Server-Sent Events), 엔터프라이즈 대응(인증·인가·추적·모니터링), Async First, 모달리티 무관, **Opaque Execution**(내부 사고·계획·도구 구현을 공유하지 않고 선언된 능력과 교환된 정보로만 협업).
- 명세는 세 층이다: 데이터 모델(Task, Message, AgentCard, Part, Artifact, Extension), 추상 연산, 프로토콜 바인딩(JSON-RPC, gRPC, HTTP/REST, 사용자 정의).

## 2. 핵심 개념 (2.2절, 4.1절)

| 개념 | 내용 |
|---|---|
| A2A Client | 사용자나 다른 시스템을 대신해 요청을 보내는 앱 또는 에이전트 |
| A2A Server | A2A 엔드포인트를 노출하고 작업을 처리하는 에이전트 |
| Agent Card | 서버가 공개하는 JSON 메타데이터. 신원, 능력, skill, 엔드포인트, 인증 요구 |
| Message | 한 번의 발화. `role`(user/agent)과 하나 이상의 Part. `messageId`는 작성자가 만든다. `contextId`, `taskId`, `referenceTaskIds`로 문맥과 연결 |
| Task | 기본 작업 단위. 서버가 ID를 만들고 상태 수명주기를 가진다 |
| Part | 내용의 최소 단위. `text`, `raw`(바이트), `url`, `data`(JSON) 중 정확히 하나와 `mediaType`, `filename` |
| Artifact | 작업 결과물(문서, 이미지, 구조화 데이터). Part로 구성 |
| Context | 관련 Task와 Message를 묶는 선택적 식별자(`contextId`) |
| Extension | 핵심 명세 밖의 기능. Agent Card에 선언하고 `required`로 강제할 수 있다 |

### 2.1 Task 상태 (4.1.3절)

| 상태 | 성격 |
|---|---|
| `SUBMITTED` | 접수됨 |
| `WORKING` | 처리 중 |
| `INPUT_REQUIRED` | 중단 상태. 추가 사용자 입력이 필요하다 |
| `AUTH_REQUIRED` | 중단 상태. 인증·인가가 필요하다 |
| `COMPLETED`, `FAILED`, `CANCELED`, `REJECTED` | 종료 상태. 이후 메시지를 받지 않는다(`UnsupportedOperationError`) |

`REJECTED`는 에이전트가 처음이든 도중이든 작업을 하지 않기로 했을 때 쓴다.

## 3. 연산 (3.1절)

- 메시지: `SendMessage`, `SendStreamingMessage`
- Task: `GetTask`, `ListTasks`(cursor 페이지네이션, `contextId` 필터), `CancelTask`, `SubscribeToTask`
- 푸시 설정: `Create/Get/List/DeleteTaskPushNotificationConfig`
- 발견: `GetExtendedAgentCard`(인증 뒤 더 자세한 카드)

`SendMessage`는 기본적으로 Task가 종료 상태나 중단 상태가 될 때까지 기다리고, `return_immediately`면 바로 진행 중 Task를 돌려준다. 응답은 Task일 수도 있고 Message일 수도 있다(3.3.3절).

### 3.1 다중 턴 (3.4절)

- `contextId`: 에이전트가 만들거나 클라이언트 값을 받아들인다. 받아들일 수 없으면 오류로 거절하고 새로 만들지 않는다. 클라이언트에게는 불투명한 값이다.
- `taskId`: 서버만 만든다. 클라이언트가 보내는 `taskId`는 기존 Task를 가리켜야 하고, `contextId`와 어긋나면 거절된다.
- `INPUT_REQUIRED`가 되면 클라이언트는 같은 `taskId`·`contextId`로 새 메시지를 보내 이어 간다.
- 후속 요청은 `referenceTaskIds`로 관련 Task를 명시한다.

### 3.2 진행 보고 (3.5절)

| 방식 | 연산 | 특징 |
|---|---|---|
| Polling | `GetTask` | 단순하다. 지연이 크다 |
| Streaming | `SendStreamingMessage`, `SubscribeToTask` | 지속 연결. 첫 이벤트는 Task 객체이고, 이어서 `TaskStatusUpdateEvent`·`TaskArtifactUpdateEvent`. 종료 상태에서 스트림이 닫힌다 |
| Push (webhook) | 푸시 설정 4종 | 에이전트가 클라이언트 URL로 HTTP POST. 클라이언트가 HTTP로 도달 가능해야 한다 |

선택 기능은 Agent Card의 `capabilities`(`streaming`, `pushNotifications`, `extendedAgentCard`, `extensions`)에 선언해야 쓸 수 있고, 선언하지 않은 기능을 부르면 정해진 오류를 돌려준다(3.3.4절).

## 4. 발견: Agent Card (4.4절, 8장, 14.3절)

- 위치: `https://{server_domain}/.well-known/agent-card.json`(IANA well-known URI 등록), 레지스트리·카탈로그, 직접 설정.
- 필수 필드: `name`, `description`, `supportedInterfaces`(선호 순서, 첫 항목이 우선), `version`, `capabilities`, `defaultInputModes`, `defaultOutputModes`, `skills`.
- 선택 필드: `provider`, `documentationUrl`, `securitySchemes`, `securityRequirements`, `signatures`, `iconUrl`.
- 서명: JWS(`AgentCardSignature`)로 서명할 수 있고, 서명이 있으면 클라이언트가 검증해야 한다(SHOULD). 카드에는 민감한 자격 증명이나 내부 구현을 넣지 않는다.

## 5. 인증과 인가 (4.5절, 7장)

- 보안 방식: API 키, HTTP 인증, OAuth 2.0(Authorization Code, Client Credentials, Device Code), OpenID Connect, mTLS. Agent Card에 선언한다.
- **작업 중 인가(7.6절):** 에이전트가 도중에 OAuth 토큰이나 파괴적 작업 전 사람의 승인이 필요하면 Task를 `AUTH_REQUIRED`로 바꾸고 이유를 상태 메시지에 적는다.
  - 자격 증명은 기본적으로 대역 외(HTTPS 등)로 받는다.
  - 클라이언트는 직접 해결하거나, 다른 사람·에이전트에 맡기거나, 자기 Task를 `AUTH_REQUIRED`로 바꿔 위로 넘길 수 있다(위임 사슬).
  - `AUTH_REQUIRED` 전이 자체를 어떤 작업의 인가로 취급하면 안 된다. 한 번 받은 인가가 이후 메시지까지 인가한다고 가정해서도 안 된다. 인가의 범위·유효기간·철회는 구현이나 확장이 정한다.
- 버전: 클라이언트는 요청마다 `A2A-Version` 헤더(Major.Minor)를 보낸다. 헤더가 없으면 0.3으로 간주한다(3.6절).

## 6. xsm에 주는 시사점

| 판단 | 내용 |
|---|---|
| 차용 후보 | Task 상태 어휘. 특히 중단 상태 `INPUT_REQUIRED`·`AUTH_REQUIRED`와 종료 상태 `REJECTED`. 워커 보고와 ADR-0012(누가 다음 노드를 고르나)에서 "답장 메시지" 대신 쓸 수 있다 |
| 차용 후보 | Agent Card식 자기 선언. `xsm list`의 세션 메타데이터(할 수 있는 일, 받는 입력 형식)를 넓힐 때 참고 |
| 차용 후보 | `contextId`/`taskId`/`referenceTaskIds`의 규칙. 채널 스레드와 요청·답장 연결을 명시하는 데 쓸 수 있다 |
| 같은 방향 | 7.6.4절의 "한 번의 승인은 그 일에만 쓴다"는 규칙은 xsm 동의 처리(한 번 승인에 쓴 말은 소진된다, #11)와 같은 원칙이다 |
| 구현하지 않음 | 전송 프로토콜 자체. A2A는 에이전트마다 HTTP 서버를 전제하고 깨우기 개념이 없다. 사람이 띄운 TUI에 붙는 xsm의 문제를 풀지 않는다 |

moai-adk도 필드명만 A2A에서 빌리고 전송 프로토콜은 구현하지 않았다(`moai-adk.md` 51행).
