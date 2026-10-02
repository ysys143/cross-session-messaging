# ACP 조사

ACP라는 이름의 프로토콜이 둘 있다. **Agent Client Protocol**은 에디터와 코딩 에이전트 사이를 표준화하고, **Agent Communication Protocol**은 에이전트 서비스끼리 REST로 상호운용하게 한다. xsm과 비교하면 앞쪽은 워커 실행 방식의 참고 대상이고, 뒤쪽은 A2A와 같은 층이다. xsm과의 비교는 `xsm-a2a-agent-comms.md` 5장에 있다.

- 작성일: 2026-10-02
- 출처(모두 2026-10-02 Jina Reader로 조회):
  - Agent Client Protocol: `https://agentclientprotocol.com`의 `overview/introduction`, `protocol/overview`, `protocol/session-setup`, `protocol/prompt-turn`
  - Agent Communication Protocol: `https://agentcommunicationprotocol.dev` 첫 화면
- 방법: 공식 문서 열람만 했다. SDK 설치, 에이전트 실행, Claude Code·Codex의 ACP 지원 여부 확인은 하지 않았다.

## 1. Agent Client Protocol (Zed 주도)

공식 소개는 "LSP가 언어 서버 연동을 표준화했듯 에디터와 에이전트 사이를 표준화한다"이다. 에디터마다 에이전트별 연동을 만들고 에이전트마다 에디터별 API를 맞추는 비용을 없애는 것이 목적이다. 사람은 주로 에디터 안에 있고, 특정 작업에 에이전트를 부른다는 구도를 전제한다.

### 1.1 역할과 전송

- **Client:** 사용자와 에이전트 사이의 인터페이스. 보통 에디터나 IDE이고, 환경과 사용자 상호작용, 자원 접근을 관리한다.
- **Agent:** 생성형 AI로 코드를 자율 수정하는 프로그램. 보통 Client의 하위 프로세스로 돈다.
- **로컬:** 하위 프로세스와 stdio로 JSON-RPC 2.0을 주고받는다.
- **원격:** 클라우드나 별도 인프라의 에이전트와 HTTP나 WebSocket으로 통신한다.
- **메시지 종류:** 응답을 기대하는 메서드와 응답 없는 알림 두 가지다. 양쪽 모두 상대가 부를 메서드를 노출한다.

### 1.2 세션

- `session/new`: 새 세션을 만든다. 에이전트가 붙을 MCP 서버 목록을 함께 넘긴다.
- `session/load`: `initialize` 응답에 `loadSession`이 있을 때만 쓴다. 대화 기록을 `session/update` 알림으로 재생한 뒤 응답한다. 재시작 뒤 이어 쓰기와, 다른 Client 인스턴스 사이의 세션 공유를 위한 기능이다.
- `session/resume`: `sessionCapabilities.resume`이 있을 때만 쓴다. 기록 재생 없이 문맥을 복원하고 MCP 서버에 다시 붙는다.
- `sessionCapabilities.additionalDirectories`: 세션의 파일 시스템 루트를 넓힌다.

### 1.3 턴

- Client가 `session/prompt`를 보내면 턴이 시작된다.
- 에이전트는 진행 내용과 도구 호출 상태를 `session/update` 알림으로 보낸다.
- 남은 도구 호출이 없으면 에이전트가 `StopReason`과 함께 `session/prompt`에 응답해 턴을 끝낸다. 에이전트는 언제든 턴을 멈출 수 있다.
- Client는 `session/cancel` 알림으로 언제든 턴을 끊을 수 있다. 끝나지 않은 도구 호출을 `cancelled`로 표시하고, 에이전트는 응답 전에 남은 업데이트를 보낼 수 있다.

### 1.4 권한과 Client 기능

- 에이전트는 도구를 실행하기 전에 `session/request_permission`으로 Client(사람)에게 승인을 받을 수 있다.
- 파일 읽기·쓰기와 터미널 실행(출력 조회, 종료 대기, kill, release)은 Client가 capability로 제공한다.

### 1.5 형식과 확장

- 가능한 곳은 MCP의 JSON 표현을 재사용하고, diff 표시처럼 코딩 UX에 필요한 타입을 따로 둔다. 사람이 읽는 텍스트의 기본 형식은 Markdown이다.
- 경로는 모두 절대 경로, 줄 번호는 1부터다.
- 확장: `_meta` 필드, `_`로 시작하는 사용자 정의 메서드, 초기화 때 capability 광고.
- 호환성 시험 도구(Test Compatibility Kit)가 있다.

### 1.6 다른 레퍼런스와의 관계

- buzz의 `buzz-acp`가 이 프로토콜로 에이전트를 붙이는 하니스다(`buzz.md` 135행).
- Client가 에이전트 프로세스를 소유하는 구조라 C1(진입점 래퍼 금지)과 반대편이다. 이 점에서 herdr·Orca와 같은 부류다.

## 2. Agent Communication Protocol (IBM BeeAI)

- 프레임워크와 팀, 인프라로 갈라진 에이전트를 표준 RESTful API로 잇는 상호운용 프로토콜이다.
- 지원 범위: 모든 모달리티, 동기·비동기 통신, 스트리밍, 상태 유지·비유지 패턴, 온라인·오프라인 발견, 장기 작업.
- 내부 구현과 무관하게 최소 명세만 요구한다. BeeAI, LangChain, CrewAI, 직접 만든 코드 어느 것으로 만든 에이전트든 붙을 수 있다고 설명한다.
- Linux Foundation 아래 오픈 표준이고, BeeAI가 참조 구현이다.
- 2025년에 A2A로 합류했다고 알려져 있으나, 조회한 첫 화면에는 그 언급이 없어 확인하지 못했다.

## 3. 확인하지 못한 것

- Claude Code와 Codex가 Agent Client Protocol 에이전트로 네이티브 동작하는지, 어댑터가 필요한지.
- Agent Communication Protocol의 A2A 합류 여부와 현재 유지 상태.
