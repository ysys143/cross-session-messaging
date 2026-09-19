# 적대적 리뷰 R2: 발견·레지스트리·범위·보안

## 발견 목록

| ID | 유형 | 심각도 | 대상(문서:줄 또는 절) | 문제 | 원본 증거(경로:줄) |
|---|---|---|---|---|---|
| 1 | WRONG | high | docs/references/buzz.md:1 | 인용: `crates/buzz-relay/src/architecture.md`는 존재하지 않음. 실제는 루트의 `ARCHITECTURE.md` | /tmp/xsm-refs/buzz/ARCHITECTURE.md 존재 확인, crates/buzz-relay/src/ 디렉토리에 architecture.md 없음 |
| 2 | WRONG | medium | docs/references/buzz.md:74 | 인용: `VISION_MESH.md:1-54` — 파일은 53줄인데 범위를 54까지로 표기 (1줄 초과) | `wc -l /tmp/xsm-refs/buzz/VISION_MESH.md` = 53 줄 |
| 3 | WRONG | medium | docs/references/buzz.md:74 | 인용: `VISION_REMOTE_AGENTS.md:1-74` — 파일은 73줄인데 범위를 74까지로 표기 (1줄 초과) | `wc -l /tmp/xsm-refs/buzz/VISION_REMOTE_AGENTS.md` = 73 줄 |
| 4 | WRONG | high | docs/references/orca.md:159-162 | 주장: "Hook이 메시지 spool 디렉터리 정기 poll". 그러나 docs/references/README.md 4절의 정오표에서 근거 없다고 지적했고, orca.md 자체의 3a절(114-128)에서는 실제 wakeup이 "Mailbox-Pointer (로컬 터미널 주입)"라고 설명 | orca.md 114-128절, docs/references/README.md 4절 정오표 |
| 5 | UNSUPPORTED | medium | docs/plan/README.md:8-21 | 목표 G1-G8이 INTENT.md에서 유래했다고 표기했으나, INTENT.md에는 명시적으로 "G1, G2, ... G8"이라는 레이블이 없음. docs/plan/README.md에서 첫 처음 정의됨 | INTENT.md:1-58 검색 결과, G1-G8 라벨 없음 |
| 6 | UNSUPPORTED | medium | docs/plan/README.md:23-29 | 제약 C1, C2가 INTENT.md에서 유래했다고 표기했으나, INTENT.md에는 "제약"이라는 섹션이 없음. docs/plan/README.md에서 새로 정의됨 | INTENT.md 전체 검사 결과, 제약 섹션 없음 |
| 7 | GAP | high | docs/references/README.md:4절, docs/references/orca.md:625-631 | Orca의 "각 에이전트별 hook 구현" 코드가 확인 못함이라고 기록했으나, 실제로 orca 저장소에서 `src/main/agent-hooks/`를 검사했을 때 테스트·유틸리티 파일만 발견되고 managed script 구현은 없는 것으로 보임. 이것이 실제 구현 부재인지 다른 위치인지 불명 | /tmp/xsm-refs/orca/src/main/agent-hooks/ 디렉토리 내 구현 파일 부재, docs/references/orca.md 확인 못함 섹션 |
| 8 | UNSUPPORTED | medium | docs/references/README.md:75-76절 | 주장: "Orca가 `codex app-server`를 직접 띄워 `thread/start`·`thread/resume`으로 스레드를 열고 `turn/start`로 메시지를 넣는다" — 근거로 제시한 파일들이 실제로 이 기능을 구현하는지 검증 필요. orca 소스 검사에서는 구현이 확인되었지만(orca.md 164-171절), 실제 작동은 **확인 못 함** | docs/references/orca.md 내용은 찾을 수 있으나 런타임 검증 없음 |
| 9 | GAP | medium | docs/adr/0001-session-registry.md:49, docs/adr/0002-delivery-and-wakeup.md:55, etc. | ADR 0001-0007이 모두 "Proposed" 상태이고 "토론 기록" 테이블이 비어있음. ADR 페이지가 토론 라운드의 결과가 아니라 초안 상태임이 명시되어 있는데, 계획 문서(docs/plan/README.md)에서 이들을 "근거"로 참고한다고 명시한 부분이 있는지 확인 필요 | docs/adr/README.md:26-32절, 각 ADR 상태 필드 |
| 10 | UNSUPPORTED | low | docs/references/README.md:73절 | 주장: "Codex 레지스트리가 `codex agents`로 공유 app-server 데몬에서 세션을 조회한다" — 스파이크 S2에서 이것이 실제 실행 중 TUI 세션에 닿는지 확인한다고 했으나, 현재 기록에서 S2 결과를 찾을 수 없음 | docs/plan/README.md 5장, S2 스파이크 결과 미기록 |
| 11 | GAP | high | docs/plan/README.md:130-136 | 스파이크 S1-S4가 "가정 검증 실험"이라 하면서 부작용 관리, 테스트 전용 세션 사용을 명시했으나, 이들 스파이크의 **결과**가 문서에 기록되지 않은 것으로 보임. P1 단계는 "완료"라고 표기되지 않음 | docs/plan/README.md:2-4절에서 "스파이크(5장)...이 남았다"고 기록 |
| 12 | WRONG | medium | docs/references/README.md:4절 표 | 작성일이 2026-09-19인데, 각 워커의 커밋 해시는 다음과 같음: orca `1ef94739` (2026-09-19), herdr `3f2a6e7` (2026-09-18), moai-adk `2213871` (2026-09-10), buzz `4e65148` (2026-09-18), agora 코드 미공개. agora에 대해 "arXiv 2609.18094 v1"만 명시되고 커밋 정보 없음이 맞다면, 이것이 다른 소스들과 비교해 부족한 정보인지 명시 필요 | docs/references/README.md:12-18절 |
| 13 | UNSUPPORTED | medium | docs/list-agents-cross-session-messaging.md:45 | 주장: "생존 판정은 PID와 시작 시각 비교, 소켓 프로브로 한다" — 실제 코드에서 이 세 가지 메커니즘이 모두 사용되는지, 우선순위가 무엇인지 검증 필요. chunk-6kcckmy2.js에서만 확인되며 다른 청크와의 조합 방식이 명시되지 않음 | docs/list-agents-cross-session-messaging.md:45절, 부록 A의 근거 E01-E45 |
| 14 | UNSUPPORTED | low | docs/references/README.md:2.3절 | 주장: "Claude Code에는 수신 정책 설정이 이미 있다" — `crossSessionInbound`와 `isolatePeerMachines`가 설명되었으나, 이들 설정이 실제로 사용자가 접근 가능하고 문서화된 공개 기능인지, 아니면 내부 구현 상세인지 확인 필요 | docs/list-agents-cross-session-messaging.md 9-13절에서 근거 청크 언급, 공개 문서 참고 필요 |

## 보강 조사 질문

### 우선순위 1: 설계 결정에 영향

1. **ADR 토론 상태 명확화**
   - 질문: docs/plan/README.md에서 "ADR-0001~0004 토론 라운드와 확정"(122줄)이 P2 단계라고 했는데, 현재 모든 ADR이 Proposed 상태인 이유는? 토론이 진행되지 않았는가, 아니면 진행 중인가?
   - 원본: docs/adr/0001-session-registry.md~0007-remote-transport-and-trust.md의 상태 필드

2. **스파이크 S1-S4 결과 문서화**
   - 질문: docs/plan/README.md 5장에서 S1-S4 스파이크를 정의했으나, 이들의 실행 결과와 검증 결과가 어디에 기록되어 있는가? P0 완료 섹션에서는 "레퍼런스 조사는 완료됐고"라고만 했는데, S1-S4는?
   - 원본: docs/plan/README.md:3-5절 의도 vs. 실제 진행 상황

3. **Codex queue 테스트 결과 (S2)**
   - 질문: docs/references/README.md 2.1절 표에서 "실행 중인 TUI 세션에 닿는지는 **확인 못 함**(스파이크 S2)"이라고 명시했는데, S2 스파이크가 실제로 수행되었는가? 수행되었다면 어떤 결과인가?
   - 원본: docs/references/README.md:53절, docs/plan/README.md:133-134절

### 우선순위 2: 근거-주장 정합성

4. **Orca hook poll 메커니즘**
   - 질문: docs/references/orca.md 159-162에서 "Hook이 메시지 spool 디렉터리 정기 poll"이라고 주장했는데, 실제로 orca.md 3a절(114-128)과 docs/references/README.md 정오표는 PTY 포인터 주입이 실제 메커니즘이라고 설명한다. 어느 것이 맞는가? 아니면 둘 다 존재하는가?
   - 원본: docs/references/orca.md 159-162절 vs. 114-128절, /tmp/xsm-refs/orca/src/main/runtime/orchestration/mailbox-pointer-delivery.ts

5. **Buzz 파일 경로와 범위**
   - 질문: docs/references/buzz.md에서 인용한 `crates/buzz-relay/src/architecture.md:1-54`와 `VISION_MESH.md:1-54`, `VISION_REMOTE_AGENTS.md:1-74`는 모두 실제 파일 위치나 길이와 불일치한다. 왜 이런 오류가 발생했는가? 조사 워커의 검증 실패인가, 아니면 보고서 작성 후 소스 구조 변화인가?
   - 원본: /tmp/xsm-refs/buzz/ 파일 확인

6. **G1-G8, C1-C2 출처 명확화**
   - 질문: docs/plan/README.md에서 목표 G1-G8과 제약 C1-C2가 INTENT.md에서 유래했다고 표기했으나, INTENT.md에는 이런 라벨이 없다. 이들을 docs/plan/README.md에서 처음 체계화한 것인가? 그렇다면 인용을 수정하거나 "파생 정의"라고 명시해야 한다.
   - 원본: INTENT.md 전체, docs/plan/README.md:8-29절

### 우선순위 3: 확인 못한 부분의 범위 명시

7. **Orca 에이전트 hook 구현 위치**
   - 질문: docs/references/README.md 4절에서 "orca.md의 '각 에이전트별 hook 구현'은 **확인 못 함**"이라고 했는데, 실제로 /tmp/xsm-refs/orca/src/main/agent-hooks/ 디렉터리를 검사했을 때 테스트 파일만 발견되었다. 이것은 구현이 없다는 뜻인가, 아니면 다른 위치에 있다는 뜻인가?
   - 원본: /tmp/xsm-refs/orca/src/main/agent-hooks/ 디렉터리, docs/references/orca.md 625-631절

8. **Claude Code 설정 공개 여부**
   - 질문: docs/references/README.md 2.3절에서 `crossSessionInbound`와 `isolatePeerMachines`을 Claude Code의 수신 정책으로 설명했으나, 이들이 공식 문서화되고 사용자가 설정할 수 있는 공개 기능인가? 내부 구현 상세만 유출된 것은 아닌가?
   - 원본: Claude Code 2.1.278 바이너리 근거(부록 A: E01-E45), Claude 공식 문서

## 이 리뷰에서 확인 못 한 것

1. **스파이크 S1-S4의 실행 여부와 결과** — 계획 문서에는 정의되어 있지만 결과 기록을 찾을 수 없음. 이들이 실제로 수행되었는지, 수행되었다면 어떤 결과인지는 다른 문서나 세션에 기록되어 있을 수 있음.

2. **ADR 토론 라운드의 진행 상황** — 현재 모든 ADR이 Proposed 상태이고 토론 기록 테이블이 비어 있다. 이것이 토론이 아직 시작되지 않았다는 뜻인지, 아니면 문서가 아직 갱신되지 않았다는 뜻인지 불명.

3. **Claude Code와 Codex의 실제 메시징 작동** — 문서들은 정적 코드 조사만 수행했으며, 실제 세션 간 메시징이 작동하는지, 크로스 프로필 메시징이 가능한지는 **확인 못 함**이라고 명시됨.

4. **Orca 원격 federation의 실제 작동** — /tmp/xsm-refs/orca의 코드에서 federation 구조는 확인되었으나, 실제 SSH 원격 머신 간 메시징이 작동하는지는 확인 못 함.

5. **Moai-adk의 다중 프로필 지원** — moai-adk.md에서 "서로 다른 프로필이라도 MCP가 동일 프로젝트 루트에 접근하면 같은 등록·우편함을 사용한다"고 추론했으나, 실제 다중 프로필 테스트는 수행하지 않음.

6. **Buzz relay의 실제 에이전트 연동** — buzz의 구조는 문서화되어 있으나, 실제로 Claude Code/Codex 에이전트가 Buzz relay와 통신하는 구현은 이번 조사에 포함되지 않음.
