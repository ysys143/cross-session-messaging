# ADR-0001: 세션 레지스트리 위치와 형식

- 상태: Accepted
- 관련 목표: G1, G2
- 작성일: 2026-09-19

## 질문

서로 다른 CONFIG_DIR의 Claude·Codex 세션을 하나의 목록으로 발견하려면, 세션 레코드를 어디에 어떤 형식으로 두어야 하는가?

## 맥락

- Claude Code는 `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`에 레코드를 둔다. 이것이 프로필 간 격리의 원인이다(`docs/list-agents-cross-session-messaging.md` 6.1절).
- Claude의 inbox 소켓은 프로필과 무관하게 `/tmp/cc-socks/`에 모인다. 다만 소켓만으로는 이름, cwd, sessionId를 알 수 없다(같은 문서 6.2절).
- Codex에는 `codex agents`(공유 app-server 데몬의 세션 목록)가 있다(`docs/plan/README.md` 2.2절).

- (T8) Codex에는 "실행 중인 세션" 파일 레지스트리가 없다. thread는 `$CODEX_HOME/state_5.sqlite`의 `threads`에 영구 저장되고, 큐 전달에는 CODEX_HOME과 thread id가 필요하다(S2c: 다른 HOME이면 즉시 오류).
- (T8) 세션 훅이 id를 준다. Claude 훅 공통 입력은 `session_id`, `transcript_path`, `cwd`, `permission_mode`이고, Codex `UserPromptSubmit` 입력에도 session id와 `permission_mode`가 있다. Claude 레지스트리 레코드에는 권한 모드가 없다.
- (T8) 이름은 실행 중에 `/rename`으로 바뀐다. 공용 레지스트리에 이름을 복사하면 낡는다.
- 상세: `docs/references/supplement/T8-discovery-sender-naming.md` 1절. 이름·네임스페이스는 ADR-0008에서 다룬다.
- **실측(S3, `docs/spikes/S3-S6-S7-results.md`)**:
  - Claude `SessionStart` 시점에 레코드와 소켓이 이미 있고, 훅 환경에 `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CONFIG_DIR`이 있다. `permission_mode`는 `SessionStart`에 없고 `UserPromptSubmit`에 있으며, 모드를 바꾸면 따라 바뀐다.
  - Codex `SessionStart`는 첫 프롬프트로 thread가 생길 때 실행된다. 입력에 `permission_mode`가 있다. 훅의 `session_id`는 큐의 thread id와 같다. 기본 HOME이면 훅 환경에 `CODEX_HOME`이 없으므로 `transcript_path`(`$CODEX_HOME/sessions/…`)에서 유도한다.
  - 강제 종료 시 `SessionEnd` 훅은 실행되지 않고 레코드·키·소켓이 남는다. 생존 판정(PID + 소켓 연결)으로 정확히 걸러진다. **레지스트리는 종료 훅에 의존하지 말고 조회 시 생존 판정을 해야 한다.**
  - Codex 프로젝트 로컬 훅은 신뢰가 없으면 실행되지 않는다. 사용자 설정 없이 실행하려면 `--dangerously-bypass-hook-trust`가 필요하다. 실제 배포에서는 사용자가 훅을 한 번 신뢰해야 한다(ADR-0003).
- **비교 실측(S9, agent-comms·cc-peer)**: 메모리 전용 레지스트리의 실패 방식을 확인했다.
  - 코디네이터가 kill -9로 죽으면 누구도 포트를 다시 열지 않았고, 새 노드가 별도 메시를 만들어 분할됐다. 정상 종료 때만 인계된다(약 192ms).
  - 메시 전체가 내려가면 room과 기록이 사라지고, 크래시한 노드의 잠금 파일이 남는다.
  - cc-peer 로스터와 전송 키 조회는 `$HOME/.claude/sessions`만 보므로 `~/.claude-3`·`~/.claude-4` 세션은 보이지 않는다. pid를 직접 지정해도 실패한다.
  - agent-comms는 신원을 `(harness, cwd)` 슬롯에 묶는다. 같은 폴더의 두 번째 Codex 세션은 임시 ID를 받고, 이 ID는 재시작하면 바뀐다. 메시 기록에는 CODEX_HOME이 없다.

## 선택지

### A. 공유 디렉터리 레지스트리

- 방식: 훅이 `~/.agent-mesh/sessions/`처럼 CONFIG_DIR 밖의 경로에 레코드를 쓴다
- 장점: 단순함. 에이전트 종류와 무관
- 단점: 훅 등록을 프로필마다 해야 함. 비정상 종료 시 잔여 레코드

### B. 기존 레지스트리 연합

- 방식: 각 CONFIG_DIR의 `sessions/`와 `codex agents`를 모두 읽어 합친다
- 장점: 새 상태를 만들지 않음
- 단점: CONFIG_DIR 목록을 알아야 함. 비공개 형식에 의존

### D. 혼합: Claude는 연합, Codex와 보충 정보는 훅

- 방식:
  - Claude 세션은 알려진 CONFIG_DIR들의 `sessions/`를 읽기 전용으로 합친다(B).
  - Codex 세션은 `SessionStart` 훅이 공용 레지스트리에 `{codex_home, thread_id, cwd, pid, 시작시각, transcript_path}`를 쓴다(A).
  - Claude 세션의 `config_dir`와 `permission_mode`는 훅이 보충한다.
  - 공용 레지스트리는 포인터(홈, id)만 가지고, 이름은 조회할 때 원본에서 읽는다.
- 장점: Claude 쪽에 새 상태를 만들지 않는다. Codex의 부족한 부분만 채운다. 이름이 낡지 않는다.
- 단점: 훅 설치가 필요하다(ADR-0003). Claude 네이티브 레코드 형식에 의존한다.

### C. 기존 런타임 재사용

- 방식: Orca 같은 런타임의 세션·터미널 레지스트리를 쓴다
- 장점: 이미 동작함
- 단점: 해당 런타임 밖에서 띄운 세션은 보이지 않음. C1과 충돌 가능

## 근거

- 레퍼런스: `docs/references/README.md` 2.2절(발견), 3장. herdr 훅 보고 방식과 moai 프로젝트 로컬 레지스트리가 선택지 A의 선례다
- 스파이크: `docs/plan/README.md` 5장

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 3-구현 | 코디네이터 | A′로 xsm v0.1을 구현했다. 실사용 시험에서 드러난 것을 반영했다 | `docs/xsm/TRIAL-2026-09-21.md`, `docs/xsm/TESTPLAN.md` 4·6장 | 환경변수는 중첩 실행에 새므로 런타임 판별은 입력 필드가 먼저다. 한 Claude 프로세스가 `/clear`·재개로 id를 바꿔 같은 pid 기록이 여럿 생긴다(실측) |
| 3-사용자 | 사용자 | 계획 승인. 상태는 `~/.xsm`에 둔다 | 2026-09-20 계획 승인 | 종료된 세션 정책을 요청(2026-09-21) |
| 2-응답 | 코디네이터 | 반론을 받아들여 D 대신 A′(대칭 포인터 레지스트리)를 제안한다 | 반론 2·5·6 | 등록을 옵트인 동의로 삼으면 ADR-0004(편입 시점 범위 검사)와 S9 결론 6이 함께 해결된다. 훅이 없는 세션이 보이지 않는 비용은 받아들인다 |
| 2 | 반론 검토자(critic, opus) | D 수정 필요. 홈 목록을 glob으로 추측하고(`tools/mesh/common.py:117`), 런타임을 경로 문자열로 판별하며(`mesh_ups.py:68`), Codex 생존을 PID로만 본다. 훅 없는 Claude 세션은 모드가 미상이라 발견은 되지만 제대로 보낼 수 없다. 두 런타임 모두 훅 등록 포인터만 쓰는 A′를 권고 | S2d, S3, S8-g, S9 결론 6, T1 85·107행 | 비기본 CODEX_HOME, Codex `/new`·resume 후 `SessionStart`, 데몬 모드 TUI의 PID는 미확인 |
| 1-보충(S9) | 코디네이터 | D 유지. 레지스트리는 모든 CONFIG_DIR·CODEX_HOME을 열거하고, 신원은 폴더가 아니라 세션 단위로 둔다 | `docs/spikes/S9-results.md` E3·E4·E6 | 메모리 복제 메시는 발견이 빠르지만 크래시 복구와 영속성이 없다는 점이 실측됐다. 파일 레지스트리는 조회 시 생존 판정으로 같은 문제를 피한다(S3) |
| 1 | 코디네이터(Claude, 이 조사 세션) | D를 제안한다 | T8 1.3절; S1(다른 프로필 전달 성공), S2(CODEX_HOME·thread id 필수) | 훅 시점 문제(`SessionStart` 때 소켓 레코드가 있는지)는 S3로 확인이 필요하다 |

## 결정

**A′를 택한다**(사용자의 계획 승인 2026-09-20, xsm v0.1로 구현).

- 훅이 `{runtime, home, session_id, pid, lstart, cwd}`를 `~/.xsm/sessions/`에 기록한다. 이름·소켓·스레드 상태는 조회할 때 원본에서 읽는다.
- 홈 목록은 훅 기록의 홈과 `homes.json`의 합집합이다. glob으로 추측하지 않는다.
- 런타임은 훅 입력 필드, 대화 기록 위치, 환경변수 순서로 판별한다. 비기본 CODEX_HOME은 대화 기록 경로에서 얻는다.
- **생존 판정.** Claude는 pid·`lstart`·소켓과 네이티브 기록의 현재 세션 id로 판정한다. id가 다르면 대체된 것으로 보고 `ended`로 둔다. Codex는 pid·`lstart`로 판정한다.
- **훅이 없는 세션.** "미등록"으로 표시하고 보내지 않는다. 예외가 하나 있다. 훅이 신뢰된 Codex 홈에서는 CLI가 열린 스레드를 대신 등록한다. 설치와 신뢰가 곧 동의이기 때문이다. 새 TUI는 시작한 뒤 생긴 스레드만 받는다.
- **종료된 세션.** 아래 초안 그대로 구현했다(`ended`/`stale`, 포인터 7일·기록 30일, 재개 명령 안내).

기각한 것: D(모든 홈을 glob으로 열거하는 파일 레지스트리). 2라운드 반론 2·5·6 때문이다.

남은 미확인: Codex `/new` 뒤 SessionStart 재실행 여부, 데몬 모드 TUI에서 훅의 부모 pid.


### 2라운드 결정 초안 (기록)

- **A′: 대칭 포인터 레지스트리.** Claude와 Codex 모두 훅이 `{runtime, home, session_id, pid, lstart}`만 기록한다. 이름·소켓·thread 상태는 조회 시 원본에서 읽는다.
- 홈 목록은 훅 레코드에 적힌 `home`과 사용자가 명시한 홈 목록 파일의 합집합이다. glob으로 추측하지 않는다.
- 런타임은 경로 문자열이 아니라 훅 입력의 필드로 판별한다.
- 조회 시 생존 판정: Claude는 PID·`lstart`·소켓 연결, Codex는 PID·`lstart`와 thread 상태(not-loaded·Interrupted는 "대기됨"으로 알림).
- 훅 레코드가 없는 세션은 "미등록"으로만 표시하고 전송 대상에서 뺀다.
- `permission_mode`는 첫 `UserPromptSubmit` 전까지 "미상"이다.

종료된 세션(2026-09-21 추가, 실측 기반):
- 포인터는 종료해도 지우지 않는다. `claude --resume`이 같은 세션 id로 돌아오므로(pid만 바뀜, ref·이름 유지) 포인터가 곧 재개 후의 주소다.
- `SessionEnd` 훅으로 정상 종료(`ended`)와 흔적 없는 소멸(`stale`)을 구분한다. Codex는 `SessionEnd`가 없어 항상 `stale`이다.
- 멈춘 세션의 포인터는 7일, 원장과 보류 본문은 30일 보관하고, 세션 시작과 CLI 실행 때 한 시간에 한 번까지 정리한다. 상주 프로세스는 두지 않는다.
- 멈춘 상대에게는 보내지 않고, 재개 명령을 함께 돌려준다.

결정 전 확인(S10 후보, Codex 턴 필요): 비기본 CODEX_HOME의 홈 유도, Codex `/new`·resume 후 `SessionStart` 재실행, 데몬 모드 TUI에서 훅의 부모 PID.
