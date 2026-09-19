# 적대적 리뷰 종합

- Orca Run: `run_d6197677205f`. 리뷰어 5명(R1·R4는 Codex, R2·R3·R5는 Claude). 모두 `worker_done`으로 보고했다.
- 대상: `docs/references/`, `docs/plan/`, `docs/adr/`, `docs/list-agents-cross-session-messaging.md`
- 개별 보고서: `R1-wakeup-codex.md`, `R2-discovery-scope-security.md`, `R3-channel-concurrency.md`, `R4-remote-orchestration.md`, `R5-plan-g8.md`

## 1. 코디네이터 검증 결과

리뷰어의 발견도 검증 대상이다. 코디네이터가 원본과 대조해 다음과 같이 처리했다.

| 처리 | 발견 | 근거 |
|---|---|---|
| 직접 확인, 수용 | R1-02: Codex `thread/queue/add`와 공유 SQLite 큐, 10초 watcher | `/tmp/xsm-refs/codex/codex-rs/ext/queue/src/service.rs:89-96` (tag `rust-v0.155.1`, 커밋 `be2951ea`) |
| 기각 | R2-7: "Orca `src/main/agent-hooks/`에는 테스트 파일만 있다" | `managed-agent-hook-registry.ts`, `managed-hook-script-refresh.ts` 등 구현 파일이 있다. R1-07이 그중 `hook-stdin-contract.ts:124-163`을 인용했다 |
| 기각 | R2-12: agora에 커밋 정보가 없다는 것을 WRONG으로 분류 | agora는 코드가 공개되지 않았으므로 커밋이 없는 것이 맞다 |
| 중복 병합 | buzz 인용 오류: R2-1~3, R3-F1~F3, R5-F9 | 종합 정오표에 이미 있던 내용 |
| 중복 병합 | orca 훅 spool 설명: R2-4, R1-07, R5-F8 | R1-07이 방향을 바로잡았다. 훅은 **이벤트를 spool에 기록**하고, 메일 wakeup은 포인터 전달부가 한다 |
| 중복 병합 | 스파이크 미실행과 ADR 토론 공백: R2-9·11, R3-F11, R5-F2~F4·F10 | 계획 문서의 상태 표기로 해결한다(3장) |

## 2. 설계 판단을 바꾸는 발견 (high)

| 주제 | 발견 | 바뀌는 판단 |
|---|---|---|
| Codex wakeup | R1-02, R1-03, R1-04, R1-09, R1-10 | "런타임 없이 wakeup되는 것은 Claude뿐"이라는 결론이 무너진다. `codex queue`가 공유 SQLite 큐로 idle thread를 깨우는 구현 경로가 있다. 한계도 있다. 대상 HOME을 식별해야 하고, Interrupted 상태는 자동으로 깨우지 않으며, 봉투에 에이전트 발신 표시가 없다 |
| Codex 제어 소켓 | R1-01 | `~/.codex/ipc/ipc.sock`은 IDE 문맥용이다. 제어 소켓은 `$CODEX_HOME/app-server-control/app-server-control.sock`이다 |
| Claude 수신 정책 | R1-05, R1-06 | 모드 동등성 규칙에 selfSent, fromMode 부재, mode-unknown 분기가 있다. 이전의 자기 소켓 실험은 selfSent 예외에 해당했을 수 있어 대표성이 없다. "같은 설정 디렉터리가 필요하다"는 결론은 발견에 대해서는 맞지만 전달에 대해서는 근거가 없다 |
| Orca 구조화 세션 | R1-08, R4-06, R4-07 | Orca는 Claude(Agent SDK)와 Codex(app-server)를 PTY 없이 소유 세션으로 돌린다. 외부 세션의 **이력**은 채택하지만, 실행 중인 외부 TUI를 인수하지는 않는다. 같은 thread를 두 writer가 쓰면 분기할 위험이 있다 |
| 원격 | R4-01, R4-02, R4-05, R4-08, R4-09, R4-10 | Orca federation은 SSH가 아니라 페어링된 WebSocket 위에서 공개키 유도 공유키와 기기 토큰을 쓴다. buzz mesh는 릴레이를 끄고 직접 IP로 연결하며 Redis로 소유권을 판정한다. Codex `--remote`는 TUI를 원격 app-server에 붙이는 옵션이다. SSH 포워딩안은 Claude의 PID·uid 검증과 회신 주소를 보존하지 못할 수 있다 |
| 채널(G6) | R3-F4, R3-F5, R3-F6, R4-03, R4-04 | buzz 최소 자체 호스팅 구성과 Claude/Codex 연동 지점이 조사되지 않았다. mesh 크레이트 부재 주장은 틀렸다 |
| 문서 협업(G7) | R3-F8, R3-F9 | Git 브랜치+PR, CRDT/OT, 절 단위 소유권+병합 선택지가 검토되지 않았다 |
| G8 | R5-F5, R5-F7 | 요구사항이 전혀 도출되지 않았다. X 게시물은 Jina Reader에서 403이다 |
| 계획 | R5-F1, R5-F6, R2-5, R2-6 | 3장 아키텍처 초안이 ADR-0001 A안을 이미 결정된 것처럼 썼다. G1~G8과 C1·C2는 INTENT.md를 해석해 붙인 라벨인데 출처처럼 보인다 |

## 3. 보강 조사 배정

같은 Run에서 Task로 배정한다. 결과는 `docs/references/supplement/`와 해당 개별 보고서의 정정 절로 들어간다.

| Task | 내용 | 해소 대상 |
|---|---|---|
| T1 | Codex 큐 경로 정밀 추적과 실험 설계 | R1-01~04, R1-09, R1-10, R2-10 |
| T2 | Claude 수신 정책 정확한 의미, 공개 문서 여부, 이전 실험의 유효성 | R1-05, R1-06, R2-13, R2-14 |
| T3 | Orca 구조화 세션, 채택, 훅 spool 정정 | R1-07, R1-08, R4-06, R4-07, R2-4 |
| T4 | 원격 전송과 신뢰 모델 재조사 | R4-01~05, R4-08~10 |
| T5 | buzz를 채널로 쓰는 최소 구성과 연동, buzz.md 정정 | R3-F4~F6, R4-03, R4-04, 인용 오류 |
| T6 | G7 공동 문서 편집 선택지 확장과 agora 전문 대조 | R3-F7~F9 |
| T7 | G8 요구사항 도출 | R5-F5, R5-F7 |

계획·ADR의 편집 사항(R5-F1, R5-F6, R5-F10, R2-5, R2-6, R3-F11)은 보강 조사가 끝난 뒤 코디네이터가 반영한다.

## 4. 처리 결과 (2026-09-19)

- 보강 조사 T1~T7이 모두 끝났다. 정정 Task 5건(T2-fix, T3-fix, T5-fix, T6-fix, T7-fix)을 포함해 Run의 Task 17개가 모두 `completed`다.
- 결론 변경은 `../references/README.md` 0.5절에 모았다.
- 계획·ADR 편집을 반영했다: 라벨 출처 표기, 아키텍처 초안의 가설 표시, 스파이크 상태와 절차, P8 선행, ADR 라운드 종료 조건, ADR-0002·0004·0005·0006·0007 맥락과 선택지.
- 해소되지 않은 것은 모두 **실측이 필요한 항목**이다: S1(Claude 프로필 간 전달), S2(Codex 큐 wakeup), S3(훅 레지스트리), S4(Codex→Claude), S5(원격). 절차는 `../plan/README.md` 5장에 있다.
