# 적대적 리뷰 R5: 계획 정합성 및 G8 커버리지

리뷰 대상: 계획(docs/plan/README.md), ADR(docs/adr/*.md), 참고자료(영상 요약, 레퍼런스)

검토일: 2026-09-19

## 발견 목록

| ID | 유형 | 심각도 | 대상(문서:줄) | 문제 | 원본 증거(경로:줄) |
|---|---|---|---|---|---|
| F1 | UNSUPPORTED | high | plan/README.md:85-99 | 아키텍처 초안이 아직 결정되지 않은 ADR-0001의 선택지 A(공유 디렉터리 `~/.agent-mesh/sessions/`)를 마치 결정된 것처럼 구체화했다. ADR 상태가 "Proposed"인데 초안에 반영됨 | adr/0001-session-registry.md:1,47-49 ("상태: Proposed" / "아직 없음. 토론 라운드를 거친 뒤 사용자가 결정한다") |
| F2 | UNSUPPORTED | high | plan/README.md:130-137 (스파이크 S1) | S1의 가정("프로필이 다른 Claude 세션에도 inbox 소켓으로 직접 전달된다")은 "실험" 항목이지만 실제로는 실험하지 않음. 부록에서 "다른 세션의 대화에 메시지가 들어가므로 하지 않았다"고 명시 | list-agents-cross-session-messaging.md:부록 B ("다른 설정 디렉터리의 세션에 실제로 보내는 실험. 다른 세션의 대화에 메시지가 들어가므로 하지 않았다") |
| F3 | UNSUPPORTED | high | plan/README.md:134 (스파이크 S2) | S2("codex queue로 실행 중인 대화형 Codex 세션을 깰 수 있다")의 결과가 명시되지 않음. 계획 54~58줄에서 "확인 못 함"으로 표기 | plan/README.md:54-58 ("확인 못 함: 대화형 TUI 세션이 공유 app-server 데몬에 붙어 `queue`로 도달 가능한지" / "CODEX_HOME이 다르면 데몬과 소켓이 분리되는지") |
| F4 | UNSUPPORTED | medium | plan/README.md:135-137 (스파이크 S3, S4) | S3("훅만으로 공유 레지스트리를 유지할 수 있다")와 S4("Codex 세션에서 Claude inbox로 보낼 수 있다")의 결과가 계획 문서에 없음. 계획에는 "부작용 관리" 항목만 있고 실제 수행 결과 없음 | plan/README.md:135-137 (표: S3, S4 행) |
| F5 | GAP | high | plan/README.md:127 (P8) | G8(Swarm·Graph Engineering에서 채널 사용 방식)이 "P8: G8 Swarm·Graph 패턴 정리"로만 있고, 구체적인 요구사항이 추출되지 않음. INTENT.md 추가2) 참고자료(영상 2개)에서 도출 가능한 요구사항 미기술 | INTENT.md:28-43 (추가2), 영상 요약 내용 |
| F6 | UNSUPPORTED | high | plan/README.md:127 (P8 선행) | P8이 "P6"을 선행으로 두고 있으나, P6(G6·G7: 채널-스레드와 문서 협업 규약)이 "P0"을 선행으로 함. G8의 구체적 요구사항이 P6과 독립적인지, 아니면 P6 결정 후에야 정의 가능한지 불명확 | plan/README.md:122-127 (단계표) |
| F7 | GAP | high | references/README.md / plan/README.md | G8 도출 자료 중 X 게시물(https://x.com/kirillk_web3/status/2087619214915826155)은 Jina Reader로 접근 불가. 따라서 X 게시물의 내용을 근거할 수 없음 | 시도: `curl -s "https://r.jina.ai/https://x.com/kirillk_web3/…"` 결과 403 AbuseAlleviationError |
| F8 | WRONG | medium | references/README.md:148 | orca.md 3절의 "Agent Hook이 메시지 spool 디렉터리를 정기 poll"은 근거가 없다고 정정되었으나, 정정 내용이 orca.md에 반영되지 않음(아직 존재하는 것처럼 읽힐 수 있음) | references/README.md:148 정정 사항; orca.md는 원문 그대로 |
| F9 | UNSUPPORTED | low | references/README.md:151 (buzz.md 정정) | buzz.md 인용 오류: 파일 존재하지 않음, 줄 범위 초과. 계획 문서에서 buzz를 G6·G7 선택지로 다루고 있는데 보고서 신뢰도가 저하됨 | references/README.md:146-151 |
| F10 | GAP | medium | adr/*.md | 모든 ADR이 "Proposed" 상태이고 "토론 기록" 섹션이 비어 있음. 계획이 P2에서 "ADR-0001~0004 토론 라운드와 확정"을 예정했으나 현재 단계가 명확하지 않음 | adr/0001,0002,0003,0004,0005,0006,0007.md (상태 필드 및 토론 기록 테이블) |

## 보강 조사 질문

### G8 커버리지 확인 (최우선)

1. **G8 요구사항 도출** (F5 해결)
   - **질문**: 영상 2개(OpenAI 멀티에이전트, Lauren Tan 검증 스킬)에서 "메시징 채널·스웜·그래프 구조"에 대한 구체적 요구사항이 무엇인가?
   - **확인처**: 
     - OpenAI 요약: "에이전트에 간단한 도구(다른 에이전트로 메시지 전송 등)만 부여하고 자율적으로 협업" (줄 35), "primitive messaging" (줄 33)
     - Lauren Tan 요약: "에이전트 각각에 정체성 부여" (줄 56), "비개발자들이 쉽게 에이전트로 업무 수행" (줄 56) → 이것이 G6(1:N·N:N 채널)과의 관계는?
   - **원본 위치**: 
     - 영상 요약 1: /Users/jaesolshin/.local/share/open-scribe/transcript/OpenAI_researcher_on_agent_swarms_&_recursive_self-improvement_summary.txt (줄 33~39)
     - 영상 요약 2: /Users/jaesolshin/.local/share/open-scribe/transcript/한영자막_Cursor_핵심_개발자_Lauren_Tan_…_summary.txt (줄 56)

2. **X 게시물 접근성** (F7 해결)
   - **질문**: X 게시물은 현재 접근 불가(403). 해당 게시물이 G8 정의에 필수적인가, 아니면 영상 요약 2개만으로 충분한가?
   - **대안**: 해당 게시물을 다른 방법(사용자 제공, 아카이브 서비스 등)으로 확보하거나, "접근 불가"를 명시적으로 기록

### 스파이크 실행 상태 확인

3. **S1 실험 설계 재검토** (F2 해결)
   - **질문**: 부록 B에서 "다른 세션의 대화에 메시지가 들어가므로 하지 않았다"는 것은 성공을 가정한 것 아닌가? 모드 동등성 규칙 때문에 보류될 가능성은?
   - **확인처**: list-agents-cross-session-messaging.md:9.2, 9.4 (권한 모드 기본값), references/README.md:2.3 (crossSessionInbound 규칙)
   - **보완 실험**: S1에서 `from-mode` 필드를 다양하게(또는 부재 상태로) 보내서 모드 동등성이 실제로 작동하는지 확인

4. **S2, S3, S4 상태 명확화** (F3, F4 해결)
   - **질문**: S2~S4가 아직 수행되지 않았는가, 아니면 결과를 문서화하지 않았는가?
   - **확인처**: plan/README.md:132-137 (스파이크 표)
   - **필요 조치**: 스파이크별로 (a) 수행 여부, (b) 결과, (c) 막힌 부분(있으면)을 명시

### 아키텍처 결정 상태 명확화

5. **ADR 결정 프로세스** (F1, F10 해결)
   - **질문**: 계획 3장의 아키텍처 초안에 `~/.agent-mesh/sessions/`가 구체화된 이유는? ADR-0001이 선택지 A를 이미 채택했는가, 아니면 초안일 뿐인가?
   - **확인처**: adr/0001-session-registry.md (결정 섹션, 현재 비어 있음)
   - **필요 조치**: plan/README.md 3장을 "가설"/"선택지 A 기준 가정 초안" 등으로 명시하거나, ADR 결정을 반영하도록 갱신

6. **ADR 토론 기록 현황** (F10 해결)
   - **질문**: ADR-0001~0007의 "토론 기록" 섹션이 모두 비어 있음. 실제로 토론이 있었는가, 아니면 예정인가?
   - **확인처**: adr/0001-0007.md (모두 "아직 없음" 상태)
   - **필요 조치**: 토론 예정 일정, 참가자, 또는 토론 기록 링크 명시

### 참고자료 신뢰도 개선

7. **Buzz 보고서 정정** (F9 해결)
   - **질문**: references/buzz.md의 인용 오류가 있는데, 이 오류가 ADR-0005(채널-스레드 저장소)의 buzz 선택지를 영향을 주는가?
   - **확인처**: references/README.md:146-151 (정정 사항)
   - **필요 조치**: buzz.md 원문 수정 또는 buzz 검토 의견서 추가

## 이 리뷰에서 확인 못 한 것

- **X 게시물 내용**: Jina Reader 접근 불가 → 게시물의 실제 내용을 근거할 수 없음 (F7)
- **S2, S3, S4 실험 결과**: 계획 문서에 기록되지 않았으나, 따로 실험 보고서가 있을 가능성 → 확인 필요 (F3, F4)
- **ADR-0001 토론 진행 상황**: "Proposed"는 현재 단계인지 진행 중인지 → 부외자는 확인 불가
- **P8의 실제 작업 범위**: "Swarm·Graph 패턴 정리"가 코드 구현인지 문서 작성인지 → 계획 문서에 명시 부족
- **Claude의 추가 버전 변화**: 2.1.278 이후 inbox 프로토콜 변경 여부 (비공개 API 의존 위험 F9 관련)
