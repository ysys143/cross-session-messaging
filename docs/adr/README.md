# Architecture Decision Records

`INTENT.md`의 요구에 따라, ADR은 **여러 차례의 토론 라운드**를 거쳐 확정한다.

## 상태

| 상태 | 의미 |
|---|---|
| Proposed | 질문과 선택지를 정리한 상태. 토론 전 |
| Discussing | 토론 라운드 진행 중 |
| Accepted | 결정됨. 결정과 근거가 기록됨 |
| Superseded | 다른 ADR로 대체됨 |
| Deferred | 사용자가 범위 밖으로 미룸. 다시 열 조건을 결정 절에 적는다 |

## 토론 라운드 절차

1. **Round 0 (근거 수집)**: 레퍼런스 조사(`docs/references/`)와 스파이크(`docs/plan/README.md` 5장) 결과를 ADR의 "근거" 절에 링크한다.
2. **Round 1..N (토론)**: 참가자마다 입장, 근거, 반론을 "토론 기록" 표에 한 줄씩 남긴다. 참가자는 사람, Claude 세션, Codex 세션이다.
   - 각 라운드는 이전 라운드의 반론에 답해야 한다.
   - 새 근거가 없으면 같은 주장을 반복하지 않는다.
3. **라운드 종료 조건**: 다음 중 하나가 성립하면 라운드를 끝내고 결정 단계로 간다.
   - 참가자 전원이 새 근거 없이 입장을 반복한다.
   - 결정을 가르는 사실이 스파이크나 조사로 확정됐다.
   - 3라운드를 넘겼다. 이때 남은 쟁점을 "미해결"로 적고 사용자에게 넘긴다.
4. **결정**: 사용자가 결정한다. 에이전트의 합의는 권고일 뿐 결정이 아니다. 결정 절에 선택지, 이유, 기각한 선택지와 그 이유를 적고 상태를 Accepted로 바꾼다.

## 목록

| ADR | 제목 | 상태 | 관련 목표 |
|---|---|---|---|
| [0001](0001-session-registry.md) | 세션 레지스트리 위치와 형식 | Accepted | G1, G2 |
| [0002](0002-delivery-and-wakeup.md) | 에이전트별 전달·wakeup 방식 | Accepted | G2, G3 |
| [0003](0003-no-wrapper-runtime-boundary.md) | 진입점 래퍼 금지의 경계 | Accepted | C1 |
| [0004](0004-communication-scope.md) | 통신 범위(scope) 모델 | Accepted | G5 |
| [0005](0005-channel-thread-store.md) | 채널-스레드 기록 저장소 | Accepted | G6 |
| [0006](0006-shared-document-editing.md) | 공동 문서 편집 규약 | Discussing | G7 |
| [0007](0007-remote-transport-and-trust.md) | 원격 통신과 신뢰 모델 | Discussing | G4 |
| [0008](0008-session-naming-and-namespace.md) | 세션 이름, 주소, 네임스페이스 | Accepted | G1, G2, G5 |
| [0009](0009-session-trust-handshake.md) | 세션 간 신뢰 수립(핸드셰이크) | Discussing | G1, G2, G4, G5 |
| [0010](0010-workers.md) | 워커를 띄우고 끝내는 일의 경계 | Accepted | C1 |

새 ADR은 [TEMPLATE.md](TEMPLATE.md)를 복사해 만든다.
