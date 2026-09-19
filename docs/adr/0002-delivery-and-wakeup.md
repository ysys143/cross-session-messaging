# ADR-0002: 에이전트별 전달·wakeup 방식

- 상태: Proposed
- 관련 목표: G2, G3
- 작성일: 2026-09-19

## 질문

수신 세션이 idle이든 작업 중이든 메시지를 받아 처리하게 하려면, 에이전트 종류별로 어떤 전달 경로를 쓸 것인가?

## 맥락

- Claude: inbox 소켓에 줄 단위 JSON을 쓰면 대화 큐에 들어간다. `priority` 값은 `now`/`next`/`later`다. 턴 도중에 오면 "while you were working" 형태로 전달된다(실측, 같은 문서 9.4절, 12절, 13절).
- Codex: `codex queue --thread --message`는 `$CODEX_HOME/queue_1.sqlite`에 쓰고, 실행 중인 TUI의 watcher(10초)가 로드된 idle thread를 깨운다. 코드 경로를 확인했고 S2에서 실측했다(유휴 TUI 깨우기 성공, 진행 중 턴 개입 불가, Interrupted 상태에서는 대기)(`docs/references/supplement/T1-codex-queue.md`).
- 별도 app-server로 같은 thread를 resume하는 방식은 두 writer 분기 위험 때문에 배제한다(`docs/references/supplement/T3-orca-structured-sessions.md`).
- Claude 수신 판정은 봉투의 `from-mode`(발신자 자기 주장)를 쓴다. 봉투가 없으면 bypass 수신자는 보류한다(`docs/references/README.md` 0.5절).
- 터미널 입력 주입(PTY)은 에이전트와 무관하게 쓸 수 있다. herdr는 프롬프트 전체를 주입하고(`herdr/src/app/api/agents.rs:130-215`), orca는 idle일 때만 "메시지 있음" 포인터 한 줄을 주입한 뒤 본문을 CLI로 가져가게 한다(`orca/src/main/runtime/orchestration/formatter.ts:112-122`). 두 방식 모두 터미널을 소유하는 런타임을 전제한다.

- (S1) Claude는 봉투가 없으면 수락돼도 모델이 답장 주소를 모른다. Claude가 보내는 봉투에는 `from-mode`, `from-name`, `hop-chain`이 붙는다.
- (S2, T8) Codex 큐 메시지에는 발신 표시가 없다. 큐 요청 필드가 `thread_id`, `input`, `client_user_message_id`뿐이다. 대신 `UserPromptSubmit` 훅이 `additionalContext`를 덧붙이거나 `decision: "block"`으로 차단할 수 있다(`codex-rs/hooks/src/schema.rs:428-448`).
- 상세: `docs/references/supplement/T8-discovery-sender-naming.md` 2절.
- **실측(S3·S6, `docs/spikes/S3-S6-S7-results.md`)**:
  - Claude: 피어 메시지에도 `UserPromptSubmit` 훅이 실행되고, `prompt`에 원래 봉투 문자열이 들어온다.
  - **(S4)** Codex↔Claude 왕복이 네이티브 수단만으로 성공했다. 단, 샌드박스가 켜진 Codex(`-s workspace-write`)는 셸로 Claude 소켓에 연결하지 못하고(`EPERM`) `codex queue`도 실패한다(`~/.codex/state_5.sqlite` 쓰기 차단). **발신 도구는 샌드박스 밖에서 실행되는 경로(MCP 도구 또는 신뢰된 훅)로 제공해야 한다.** MCP 서버가 샌드박스 밖에서 도는지는 미확인이다.
  - Codex: 큐 입력에도 `UserPromptSubmit` 훅이 실행된다. `additionalContext`는 대화 기록에 `developer` 역할 메시지로 들어가고, 모델이 "사용자 입력이 아니다"를 인지했다. `decision: "block"`을 내면 TUI에 "Blocked by hook"이 뜨지만, **큐 항목은 이미 소비됐고 대화 기록에도 남지 않는다.** 보류와 사람 검토를 원하면 훅이 차단한 메시지를 직접 저장해야 한다.
- **비교 실측(S9, agent-comms)**:
  - 훅 기반 전달(PostToolUse·Stop·UserPromptSubmit `asyncRewake`)은 완전 유휴 Claude를 깨우지 못했다. 이벤트는 파일에 쌓였다가 사람의 프롬프트로 턴이 열린 뒤에야 전달됐다.
  - 유휴 깨우기에 쓰이는 채널 푸시는 `--dangerously-load-development-channels`가 필요하다(미확인).
  - Codex bridge는 도구 응답에 덧붙이는 방식뿐이어서 유휴 Codex에 45초 동안 아무 변화가 없었고, `codex queue`는 쓰지 않는다.
  - 수신자가 크래시한 동안 보낸 메시지는 발신 측에 "Sent"로 남았지만 재시작 후에도 재생되지 않았다. "쓰기 성공"과 "전달·읽음"을 구분해야 한다.

## 선택지

### A. 네이티브 inbox 우선

- 방식: Claude는 소켓, Codex는 `codex queue`를 쓴다(app-server resume은 15행 이유로 제외)
- 장점: 에이전트의 큐잉·권한 경고를 그대로 활용
- 단점: 에이전트마다 어댑터가 필요하고 비공개 프로토콜 변화에 취약

### B. PTY 주입

- 방식: 터미널 멀티플렉서나 Orca terminal send로 입력을 주입한다
- 장점: 모든 CLI에 적용 가능
- 단점: 사용자 입력과 충돌하고, 메시지가 사람 입력으로 보임(권한 경고 없음)

### D. PTY 포인터 + CLI pull

- 방식: 수신 세션이 idle일 때 "메시지 N건, `mesh check` 실행" 한 줄만 입력하고, 본문은 에이전트가 CLI로 가져간다(orca 방식)
- 장점: 본문이 입력창을 거치지 않고, 어떤 CLI에든 적용된다
- 단점: 터미널에 입력할 수단(멀티플렉서, 런타임)이 필요해 C1과 긴장 관계다. 사용자가 입력 중일 때 섞일 수 있다(이번 조사에서 실제 관찰)

### E. 발신 표시 규약 (A와 함께 쓰는 하위 결정)

- 방식:
  - Claude 수신: 네이티브 봉투를 항상 붙이고, `from-mode`에는 발신 세션의 실제 모드를 적는다. 발신자가 Claude면 `from`에 자기 소켓 주소를 적고, Codex면 비운 채 본문에 회신 방법을 적는다.
  - Codex 수신: 본문 맨 앞에 규약 헤더 `[agent-mesh v1 from="…" ref=… kind=… mode=… id=…]`를 두고, 수신 세션의 `UserPromptSubmit` 훅이 경고 문맥(`additionalContext`)을 붙이고 모드 동등성을 적용한다.
- 장점: 두 런타임에서 "사용자 입력이 아니다"라는 의미를 같은 수준으로 전달한다. Claude 쪽은 네이티브 규칙을 그대로 쓴다.
- 단점: 같은 uid면 위조할 수 있어 인증이 아니다. Codex 훅 차단은 큐에서 이미 꺼낸 뒤의 결정이라 보류나 재전송이 없을 수 있다(S6).

### C. MCP 폴링

- 방식: 각 세션에 MCP 도구 `inbox_check`를 두고 에이전트가 가져간다
- 장점: 공식 확장 지점
- 단점: idle 세션을 깨우지 못함(G3 미충족)

## 근거

- 레퍼런스: `docs/references/README.md` 2.1절(wakeup 방식 비교), 2.7절
- 스파이크: `docs/plan/README.md` 5장

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 2-사용자 | 사용자 | Codex는 턴 경계 전달로 충분하다 | 2026-09-20 질의 응답 | PTY 주입(D)은 채택하지 않는다 |
| 2-응답 | 코디네이터 | 반론을 받아들여 A+E에 전달 원장(L)을 필수로 붙이고, Codex 발신 범위와 G3를 좁혀 명시한다. D는 채택하지 않는다 | 반론 1~6 | "Sent는 전달이 아니다"(S9 E6-3)와 "Codex 차단은 소멸"(S6)은 문서가 아니라 설계로 막아야 한다 |
| 2 | 반론 검토자(critic, opus) | A+E를 그대로 채택하는 데 반대. 전달 보장, 샌드박스 Codex의 발신 경로, 중간 개입이 결정에서 빠졌다. Codex 훅 실측은 모두 `--dangerously-bypass-hook-trust`로 했다. 문안 14행(낡음)과 36행(15행과 모순) 오류 | S2b·S2d, S4(`EPERM`, MCP 미확인), S6, S9 E6-3 | 사용자가 Codex의 턴 경계 전달을 G3 충족으로 받아들일지는 사용자 결정 사항 |
| 1-보충(S9) | 코디네이터 | A 유지. 유휴 깨우기는 네이티브 경로(Claude inbox, `codex queue`)만 가능하다는 점이 다른 구현에서도 확인됐다 | `docs/spikes/S9-results.md` E1·E4·E6 | 훅은 전달이 아니라 표시·검문(S6·S8)에 쓰는 것이 맞다. 전달 확인은 발신 결과가 아니라 수신 측 기록으로 판단해야 한다 |
| 1 | 코디네이터(Claude, 이 조사 세션) | A(네이티브 우선: Claude inbox, Codex queue) + E(발신 표시 규약)를 제안한다. 별도 app-server resume은 배제 | S1, S2 실측; T8 2.3절; T3(두 writer 분기) | Codex는 진행 중인 턴에 끼어들 수 없어 G3의 "중간 개입"을 충족하지 못한다. 이 공백을 받아들일지, PTY 포인터(D)를 보조로 둘지 토론이 필요하다 |

## 결정

### 결정 초안 (2라운드 후, 사용자 확인 대기)

- **A+E+L.** 전달은 네이티브 경로만 쓴다: Claude는 inbox 소켓, Codex는 `codex queue`. app-server resume은 쓰지 않는다(근거는 Orca 주석이며 Codex 실측은 없다).
- **L(전달 원장)**: 발신 도구는 `queued`로 기록한다. `delivered`는 수신 측 `UserPromptSubmit` 훅의 기록으로만 판정한다. 발신 결과 "Sent"나 종료 코드 0을 전달로 보지 않는다.
- Codex 수신 훅이 `block`할 때는 먼저 held 저장소에 본문을 기록한다. 기록에 실패하면 차단하지 않고 경고 문맥을 붙여 통과시킨다.
- 샌드박스(`workspace-write`) Codex가 MCP 도구로 발신하는 것이 실측되기 전까지, Codex 발신 범위는 "샌드박스 밖 실행 또는 신뢰된 훅 경로"로 한정한다.
- G3: Codex의 중간 개입은 지원하지 않는다. 전달 시점은 현재 턴이 끝난 뒤다. Interrupted 대상에는 전송 시 경고를 돌려준다. PTY 주입(D)은 채택하지 않는다.

결정 전 확인: (1) 샌드박스 Codex의 MCP 발신(S10 후보), (2) `--dangerously-bypass-hook-trust` 없이 사용자가 신뢰를 승인한 상태에서 Codex 훅 동작 재확인. 사용자 확인(2026-09-20): Codex의 턴 경계 전달로 G3를 충족한 것으로 본다.


아직 없음. 토론 라운드를 거친 뒤 사용자가 결정한다.
