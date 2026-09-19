# T8 — 세션 발견, 발신 표시, 세션 명명과 네임스페이스

- 작성일: 2026-09-19
- 작성: 코디네이터(이 세션)가 원본에서 직접 확인했다. Orca 워커 조사가 아니다.
- 원본:
  - Claude Code 2.1.278 추출 모듈. 경로 약칭 `B/` = 추출 디렉터리의 `chunk-*.js`, minify된 함수 이름 기준.
  - Codex `rust-v0.155.1`. 경로 약칭 `C/` = `/tmp/xsm-refs/codex/codex-rs`.
  - 실측: `../../spikes/S1-S2-results.md`
- 관련 ADR: 0001(발견), 0002(전달·발신 표시), 0008(이름·네임스페이스)

## 요약

1. **발견**: 이름이나 주소로 찾는 범위가 두 런타임 모두 자기 홈 안이다.
   - Claude는 설정 폴더(CONFIG_DIR)마다 **살아 있는 세션**의 파일 레지스트리를 둔다.
   - Codex는 "지금 실행 중인 세션" 목록이 없고, CODEX_HOME마다 **저장된 thread** DB만 있다.
   - 두 런타임 모두 세션 훅이 세션 id를 준다. Claude 훅은 권한 모드도 준다.
2. **발신 표시**: 두 런타임이 비대칭이다.
   - Claude에는 네이티브 봉투(`<cross-session-message …>`)와 수신 정책이 있고, S1에서 실측했다.
   - Codex 큐 메시지에는 발신 표시가 없다(S2 실측). 대신 `UserPromptSubmit` 훅이 입력 직전에 문맥을 덧붙이거나 차단할 수 있다.
3. **이름과 충돌**: 두 런타임 모두 `/rename`이 있지만 충돌 규칙이 반대다.
   - Claude는 자기 설정 폴더 안의 살아 있는 세션끼리 **쓸 때** 고유성을 강제한다.
   - Codex는 고유성을 강제하지 않고 **읽을 때** 모호하면 오류를 낸다.
   - 홈 사이에는 어떤 규칙도 없다.

## 1. 세션 발견

### 1.1 Claude Code

| 사실 | 근거 |
|---|---|
| 레지스트리는 `<CONFIG_DIR>/sessions/<pid>.json`이다. 발견은 자기 CONFIG_DIR만 읽는다 | `B/chunk-cyg1gqsq.js` `Vj()`; `../../list-agents-cross-session-messaging.md` 6장 |
| 레코드 필드: `pid`, `sessionId`, `cwd`, `messagingSocketPath`, `name`, `nameSource`, `nameSince`, `formerNames`, `status`, `procStart`, `kind`, `peerFeatures`. **권한 모드는 없다** | 실측 레코드(`~/.claude-3/sessions/73960.json`), `B/chunk-6kcckmy2.js` `Y()` |
| 생존 판정: PID와 `procStart`로 재사용 여부를 확인하고, 소켓에 250ms 연결을 시도한다 | `B/chunk-6kcckmy2.js` `Q()`, `q()` |
| 레코드는 세션이 종료되면 지워진다 | S1 실측(테스트 세션 종료 후 `~/.claude-4/sessions/`에 원래 레코드만 남음) |
| 훅 공통 입력: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `agent_id`, `agent_type`, `effort` | `B/chunk-t877rbgv.js` 훅 입력 생성부(`session_id:e.id,transcript_path:Lm(e.id),cwd:n,…,permission_mode:r`) |
| 실험 세션에만 훅을 거는 방법: `--settings '<json>'`(flagSettings) | `claude --help`; S1에서 `--settings`로 `crossSessionInbound`를 준 선례 |

### 1.2 Codex

| 사실 | 근거 |
|---|---|
| "실행 중인 세션" 파일 레지스트리가 없다. thread는 `$CODEX_HOME/state_5.sqlite`의 `threads` 테이블(`id`, `cwd`, `name`, `rollout_path`, `cli_version` …)에 영구 저장된다 | 로컬 스키마 읽기 전용 확인; S2 |
| 큐 전달에는 **CODEX_HOME과 thread id**가 필요하다. 다른 HOME에서 보내면 즉시 오류가 난다 | S2c (`no rollout found for thread id … -32603`) |
| 꺼진 thread에도 큐에 넣을 수 있고, thread가 다시 로드되면 전달된다 | `C/ext/queue/src/service.rs` watcher는 로드된 thread만 깨움(T1); S2d(대기 후 전달) |
| `UserPromptSubmit` 훅 입력: prompt, session·turn id, cwd, transcript path, model, `permission_mode` | T1 4절 (`C/core/src/hook_runtime.rs:669-686`) |
| 훅 파일: 설정 계층마다 그 폴더의 `hooks.json`을 읽는다. 신뢰된 프로젝트의 `.codex/`가 계층이 되면 실험 세션에만 훅을 걸 수 있다. **훅 신뢰 승인 절차는 확인하지 않았다** | `C/hooks/src/engine/discovery.rs:128-150,339-343`; 폴더 신뢰 창 문구 "Trusting the directory allows project-local config, hooks, and exec policies to load" (S2 실측) |

### 1.3 설계 제안 (ADR-0001 1라운드 입장)

- **Claude**: 새 레지스트리를 만들지 않는다. 알려진 CONFIG_DIR들의 `sessions/`를 읽기 전용으로 합친다(ADR-0001 선택지 B).
- **Codex**: `SessionStart` 훅이 공용 레지스트리(`~/.agent-mesh/sessions/`, 가칭)에 `{agent, codex_home, thread_id, cwd, pid, 시작시각, transcript_path}`를 기록한다. `codex_home`은 훅 환경의 `CODEX_HOME`에서 읽고, 없으면 `transcript_path`에서 유도한다.
- **Claude 보충 레코드**: 훅이 `config_dir`와 `permission_mode`를 기록한다. 권한 모드는 바뀔 수 있으므로(shift+tab) `UserPromptSubmit`마다 갱신한다.
- **포인터만 저장한다**: 공용 레지스트리는 원본을 가리키는 값(홈, id)만 가진다. 이름은 조회할 때 원본(Claude 레코드 `name`, Codex `threads.name`)에서 읽는다. `/rename`은 실행 중에 언제든 바뀌기 때문이다.

## 2. 발신 표시

### 2.1 Claude 수신: 네이티브 봉투

| 사실 | 근거 |
|---|---|
| 봉투: `<cross-session-message from="…" from-session="…" hop-chain="…" from-name="…" from-mode="bypass\|prompting">\n본문\n</cross-session-message>`. 속성 순서가 고정이고, 파서가 다시 렌더링해 원문과 같을 때만 인정한다 | `B/chunk-cyg1gqsq.js` `TG()`, `pQe()`, `Nhe()` |
| `from-mode`는 발신자가 스스로 적는 값이고 수신 정책의 입력이다 | `B/chunk-9mrd94qp.js` `k()`; S1 7건 실측 |
| 봉투가 없으면 수락돼도 모델이 답장 주소를 모른다 | S1 R4-N |
| Claude가 보내는 봉투에는 `from-mode`, `from-name`, `hop-chain`이 붙는다 | S1 R1-B2 답장 원문 |

### 2.2 Codex 수신: 표시 없음, 훅으로 보완

| 사실 | 근거 |
|---|---|
| 큐 요청 필드는 `thread_id`, `input`, `client_user_message_id`뿐이다. 발신자 필드가 없다 | `C/app-server-protocol/src/protocol/v2/thread.rs:878-885` (T1) |
| 큐 메시지는 TUI에 사용자 입력과 같은 모양으로 표시된다 | S2 실측 |
| `UserPromptSubmit` 훅 출력: `hookSpecificOutput.additionalContext`로 문맥을 덧붙이거나 `decision: "block"`으로 차단한다 | `C/hooks/src/schema.rs:428-448` |
| 훅 차단은 이미 큐에서 꺼낸 입력에 대한 결정이다. 큐에 남겨 두는 보류나 발신자 통지는 자동으로 제공되지 않는다 | T1 4절 (`C/ext/queue/src/service.rs:439-448`) |

### 2.3 설계 제안 (ADR-0002 1라운드 입장)

- **Claude로 보낼 때**: 봉투를 **항상** 붙인다.
  - `from-mode`에는 발신 세션의 실제 모드를 적는다. 값은 공용 레지스트리의 훅 기록에서 가져온다.
  - `from`: 발신자가 Claude면 자기 소켓 주소를 적어 네이티브 답장 왕복을 쓴다. 발신자가 Codex면 비우고, 본문에 회신 방법(`mesh send --to <이름>`)을 적는다.
- **Codex로 보낼 때**: 본문 맨 앞에 규약 헤더를 둔다.

  ```
  [agent-mesh v1 from="<이름>" ref=<ref> kind=claude|codex mode=bypass|prompting id=<uuid>]
  ```

  수신 세션의 `UserPromptSubmit` 훅이 헤더를 알아보고, Claude의 권한 경고와 같은 취지의 `additionalContext`를 붙인다. 수신 정책(모드 동등성)은 같은 훅에서 적용한다.
- **한계**: 같은 uid의 프로세스는 헤더와 `from-mode`를 위조할 수 있다. 이 표시는 실수 방지와 맥락 제공이지 인증이 아니다. Claude 네이티브 규칙도 같은 수준이다. 인증은 원격(G4) 확장 때 서명으로 다룬다.

## 3. 세션 명명과 네임스페이스

### 3.1 네이티브 규칙 비교

| | Claude Code | Codex |
|---|---|---|
| 이름 설정 | `/rename`. 레지스트리의 `name`에 기록하고 `nameSource: "user"`로 표시한다. 이전 이름은 `formerNames`에 남는다 | `/rename` → `thread/name/set` → `threads.name` (`C/tui/src/slash_command.rs:30,96`, `C/app-server-protocol/src/protocol/common.rs:611-614`) |
| 기본 라벨 | cwd에서 파생(`nameSource: "derived"`) | 첫 메시지 미리보기(`C/tui/src/named_session_lookup.rs` `display_label`) |
| 조회 범위 | 자기 CONFIG_DIR의 살아 있는 세션(`listAllLiveSessions`) | 자기 CODEX_HOME의 모든 활성 thread(꺼진 것 포함) |
| 고유성 | 코드상 강제한다. 플래그 `tengu_session_name_uniqueness`(코드 기본 true), `B/chunk-67bmqm0d.js` `Rht()`. **실측(S7): 이 계정은 서버 설정이 False라 강제하지 않았다** | 강제하지 않는다. 설정 시 정규화 후 저장(`C/app-server/src/request_processors/thread_processor.rs:1819-1850`) |
| 충돌 처리 | `/rename`한 쪽이 양보해 새 이름을 받는다. 시작 시 충돌이면 이름을 먼저 얻은 세션이 유지한다(`B()`, `nameSince` 비교). 양보한 세션은 `nameSource: "collision"` | 조회 시 오류: "Multiple sessions match '{name}' … use a session UUID to disambiguate." 페이지를 넘어 고유성을 확인할 수 없으면 별도 오류 |
| 비교 | `Tr()`: NFKC 정규화, 제어문자 제거, trim, 소문자화, 공백→`-` (`B/chunk-cyg1gqsq.js`) | 정확 일치(대소문자 구분, `display_label(&thread) != name`) |
| 구분자 | `이름 [ref]`, ref = sha256 앞 6자리(`p1()`, `od=6`) | UUID |
| 금지 문자 | `@`가 들어간 이름은 주소로 쓸 수 있는 이름으로 인정하지 않는다(`jEt()` → `qg()`의 `!e.includes("@")`) | 확인 못 함 |
| 다른 홈에서 호출 | 불가 | 불가(S2c) |

### 3.2 홈 사이의 충돌

두 런타임 모두 홈 사이 규칙이 없다. `~/.claude`의 `reviewer`, `~/.claude-4`의 `reviewer`, Codex의 `reviewer`가 정당하게 공존할 수 있다. 메시 계층이 다른 홈 세션의 이름을 바꿀 수단도 없다. Claude의 양보 로직은 자기 CONFIG_DIR 안에서만 돈다. 따라서 **쓸 때 막는 방식은 불가능하고, 읽을 때 막는 방식만 가능하다.**

### 3.3 설계 제안 (ADR-0008 1라운드 입장)

1. **전역 고유성은 강제하지 않는다.**
2. **한정 이름**: `이름@홈별칭`(예: `reviewer@claude-4`, `reviewer@codex`).
   - 홈 별칭은 설정 폴더 이름에서 파생한다.
   - Claude 이름에는 `@`가 들어갈 수 없으므로 충돌하지 않는다. Codex 이름에는 들어갈 수 있으므로, 마지막 `@` 뒤가 알려진 별칭과 일치할 때만 한정자로 해석한다.
3. **맨 이름은 메시 전체에서 유일할 때만 푼다.** 여럿이면 후보 목록(한정 이름, ref, 생존 상태)과 함께 오류를 낸다.
4. **비교는 Claude의 `Tr()` 정규화로 통일한다.** Codex에서 네이티브로 다른 `Reviewer`와 `reviewer`는 메시에서 모호한 이름이 되어 오류가 난다. 오류가 오배송보다 안전하다.
5. **살아 있는 세션을 우선한다.** 꺼진 Codex thread는 한정 이름이나 `--include-offline`으로만 대상이 된다.
6. **ref를 고정한다**: `sha256(runtime:home:session_id)` 앞 6자리. 전송 시점에 이름을 id로 확정하고 이후에는 id로 보낸다.
7. **범위(G5) 필터를 이름 해석보다 먼저 적용한다.**

## 4. 확인 못 한 것

> 2026-09-19 갱신: 아래 항목 대부분을 S3·S6·S7로 실측했다. 결과는 `../../spikes/S3-S6-S7-results.md`에 있다. 남은 것: Claude `/rename` 충돌 시 새 이름 형식(고유성 플래그가 꺼져 있어 관찰 불가), Codex `normalize_thread_name` 세부.

- Claude `SessionStart` 훅 실행 시점에 세션 레코드와 inbox 소켓이 이미 있는지, 훅 프로세스 환경에 `CLAUDE_CODE_MESSAGING_SOCKET`이 있는지 → S3
- Codex 훅 환경에 `CODEX_HOME`이 있는지. 기본 HOME을 쓰는 세션은 환경변수가 비어 있을 수 있다 → S3
- Codex 프로젝트 로컬 `.codex/hooks.json`의 훅 신뢰 승인 절차 → S3, S6
- Codex `UserPromptSubmit`의 `additionalContext`가 큐로 들어온 입력에도 똑같이 적용되는지, `block`된 큐 메시지가 어떻게 되는지 → S6
- Claude `/rename` 충돌 시 새 이름의 실제 형식(`O()`의 접미사 규칙), Codex 이름 정규화(`normalize_thread_name`)의 세부 → S7
- 홈 사이 같은 이름에서의 실제 동작(네이티브 `SendMessage`가 다른 홈 세션을 찾지 못하는 것) → S7
