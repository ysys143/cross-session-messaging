# ADR-0004: 통신 범위(scope) 모델

- 상태: Proposed
- 관련 목표: G5
- 작성일: 2026-09-19

## 질문

무분별한 확장을 막기 위해, 어떤 세션이 어떤 세션을 보고 메시지를 보낼 수 있는지를 어떤 단위로 제한하는가?

## 맥락

- Claude의 현재 범위는 '같은 CONFIG_DIR + 같은 uid'다. 프로젝트 개념은 없다.
- Orca는 Run/워크트리/그룹 주소(`@worktree:<id>`, `@claude` 등)를 쓴다(orchestration 가이드, `docs/references/orca.md` 6절).
- INTENT.md는 이것을 확정 사항이 아니라 토론 대상으로 명시한다.
- Claude Code에는 수신 정책 설정이 이미 있다(`docs/references/README.md` 2.3절).
  - `crossSessionInbound`: `accept`/`hold`/`refuse`. 값이 없으면 권한 모드가 같을 때만 자동 전달한다.
  - 우선순위: 관리 정책 > 플래그 > 사용자 설정. 저장소 설정은 더 엄격하게만 바꿀 수 있다.
  - `isolatePeerMachines`: 다른 머신으로 보낼 때 매번 승인을 받는다.
  - 두 설정은 공식 문서(`code.claude.com/docs/en/settings`)에 있는 공개 기능이다.
  - 모드 동등성에 쓰는 `from-mode`는 발신자가 봉투에 스스로 적는 값이다. 인증되지 않으므로 범위 제어의 보안 근거로 쓸 수 없다.

- 범위 정책을 한 번의 핸드셰이크로 "신뢰 관계"로 굳히고 그 관계가 유효할 때만 메시지를 허용하는 방식은 ADR-0009에서 다룬다(실험 설계 `docs/spikes/S8-handshake-plan.md`).
- **비교 실측(S9)**: cc-peer default front는 `~/.claude/sessions`에 있는 Claude 세션을 그 세션의 동의 없이 메시에 올렸다. 범위 정책은 메시지 시점뿐 아니라 세션 편입 시점에도 적용돼야 한다. 같은 실험에서 cc-peer가 기본값으로 `from-mode=bypass`를 주장해, Claude 세션이 아닌 node 프로세스의 메시지가 bypass 수신 세션에서 곧바로 실행됐다.

## 선택지

### A. 프로젝트 정책 파일

- 방식: 저장소에 `.agent-mesh.toml` 같은 허용 목록을 둔다
- 장점: 저장소와 함께 버전 관리
- 단점: 저장소 밖의 세션(홈 디렉터리 작업 등) 처리가 애매함

### B. 명명된 그룹

- 방식: 세션이 시작할 때 그룹에 가입하고, 같은 그룹 안에서만 통신한다
- 장점: 프로젝트 경계와 독립적
- 단점: 가입 절차가 필요

### D. Claude 네이티브 정책 확장

- 방식: 수신 측 정책은 `crossSessionInbound` 의미(accept/hold/refuse, 모드 동등성, 저장소는 강화만)를 그대로 쓴다. Codex 수신 어댑터에도 같은 규칙을 적용한다. 송신 측 범위(누구에게 보일지)만 새로 정의한다
- 장점: 사용자가 이미 아는 설정 체계와 일관된다. Claude 쪽은 추가 구현이 없다
- 단점: 비공개 기능의 의미에 의존한다. 송신 측 범위는 여전히 별도 설계가 필요하다

### C. 제한 없음 + 수신 측 거부

- 방식: 모두 보이되 수신 측 정책으로 거부한다
- 장점: 단순함
- 단점: 목록이 폭증하고 기본값이 느슨함

## 근거

- 레퍼런스: `docs/references/README.md` 2.3절. Claude 네이티브 `crossSessionInbound`/`isolatePeerMachines`
- 스파이크: `docs/plan/README.md` 5장

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 1-보충(S9) | 코디네이터 | 범위 검사를 세션 편입(레지스트리 노출) 시점과 메시지 시점 두 곳에 둔다 | `docs/spikes/S9-results.md` E2·E3 | 자동 편입은 편리하지만 원격 gateway와 결합하면 노출 범위를 넓힌다. 편입도 정책 대상으로 둔다 |

## 결정

아직 없음. 토론 라운드를 거친 뒤 사용자가 결정한다.
