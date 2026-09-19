# 스파이크 S3·S6·S7 실험 결과

- 실행일: 2026-09-19 18:00~18:25 (KST)
- 환경: macOS, Claude Code 2.1.278, Codex CLI 0.155.1
- 원칙: `docs/plan/README.md` 5장의 공통 실험 원칙을 따랐다.
  - 매번 전용 세션을 새로 띄웠다(tmux, `env -i`).
  - 사용자 설정 파일은 수정하지 않았다. 실험 후 `~/.codex/config.toml`, `~/.codex/hooks.json`, `~/.claude-3/settings.json`, `~/.claude-4/settings.json`의 수정 시각이 모두 실험 이전인 것을 확인했다.
  - 훅은 실험 세션에만 걸었다. Claude는 `--settings`, Codex는 프로젝트 로컬 `.codex/hooks.json`과 `--dangerously-bypass-hook-trust`를 썼다.
- 도구:
  - 훅 덤프: `/tmp/xsm-spike/hooks/dump.py`
  - S6 훅: `/tmp/xsm-spike/hooks/codex_mesh_ups.py`
  - 이름 해석 시제품: `tools/mesh_resolve.py`
  - 프레임 발신: `tools/spike_s1_send.py`

## 요약

| 스파이크 | 결과 | 설계에 주는 영향 |
|---|---|---|
| S3 훅 기반 레지스트리 | **확인(조건부).** 두 런타임 모두 훅으로 필요한 값을 얻는다. 단, 강제 종료 시 종료 훅은 실행되지 않는다 | 레지스트리는 종료 훅에 의존하지 말고 조회할 때 생존 판정을 해야 한다. Codex HOME은 `transcript_path`에서 유도한다 |
| S6 Codex 발신 표시 훅 | **확인(한계 있음).** 큐 입력에도 훅이 실행되고, 경고 문맥은 `developer` 메시지로 들어간다. 차단하면 메시지가 **어디에도 남지 않는다** | 보류·사람 검토를 하려면 훅이 차단한 메시지를 직접 저장해야 한다 |
| S7 이름과 네임스페이스 | **ADR-0008 B안 규칙이 실제 데이터에서 동작.** 예상 밖 사실 두 가지: Claude의 이름 고유성 강제가 원격 플래그로 **꺼져 있고**, Codex 이름 전송은 thread가 많으면 **항상 거부된다** | 같은 홈 안의 중복도 실제로 생기므로 `이름 [ref]` 지정이 필수다. Codex는 메시가 이름을 UUID로 직접 풀어 보내야 한다 |

## S3: 훅 기반 레지스트리

### Claude (수신 세션 `~/.claude-4`, `--settings`로 SessionStart·UserPromptSubmit·SessionEnd 덤프 훅)

| 확인 항목 | 결과 |
|---|---|
| `SessionStart` 시점에 레지스트리 레코드와 inbox 소켓이 있는가 | **있다.** 훅 실행 순간과 3초 뒤 모두 레코드(`30067.json`)와 소켓(`/tmp/cc-socks/30067.sock`)이 존재했다 |
| 훅 환경변수 | `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE=1`. 훅의 부모 프로세스는 claude 본체다 |
| `SessionStart` 입력 필드 | `cwd`, `hook_event_name`, `model`, `scratchpad_dir`, `session_id`, `session_title`, `source`, `transcript_path`. **`permission_mode`는 없다** |
| `UserPromptSubmit` 입력 필드 | 위 필드 + `permission_mode`, `prompt`, `prompt_id` |
| 모드 변경 반영 | shift+tab으로 바꾸자 `permission_mode`가 `default` → `acceptEdits` → `plan`으로 따라 바뀌었다 |
| 피어 메시지에도 `UserPromptSubmit`이 실행되는가 | **실행된다.** `prompt`에는 "Another Claude session…" 머리말이 붙기 전의 **원래 봉투 문자열**(`<cross-session-message from=… from-mode="prompting">…`)이 들어온다 |
| 정상 종료(`/exit`) | `SessionEnd` 훅이 실행됐다(`reason: "prompt_input_exit"`). 훅 시점에 레코드는 이미 지워졌고, 레코드와 키 파일이 남지 않았다 |
| 강제 종료(`kill -9`) | **`SessionEnd` 훅이 실행되지 않았다.** 레코드(`34637.json`), 키 파일, 소켓이 모두 남았다. 생존 판정(PID 없음, 소켓 `ConnectionRefusedError`)으로 `stale` 판정이 정확히 났다. 남은 테스트 파일은 삭제했다 |

### Codex (`CODEX_HOME=~/.codex` 기본, 프로젝트 로컬 `.codex/hooks.json` + `--dangerously-bypass-hook-trust`)

| 확인 항목 | 결과 |
|---|---|
| 프로젝트 로컬 훅이 사용자 설정 수정 없이 실행되는가 | **실행된다.** `--dangerously-bypass-hook-trust`를 쓰면 TUI에 "Enabled hooks may run without review for this invocation" 경고가 뜨고, 신뢰 기록은 남지 않는다. 이 옵션 없이는 훅이 신뢰되지 않아 실행되지 않는다(`codex-rs/hooks/src/engine/discovery.rs:714-718,794-811`) |
| `SessionStart` 실행 시점 | 앱 시작 때가 아니라 **첫 프롬프트로 thread가 생길 때** 실행됐다 |
| `SessionStart` 입력 필드 | `cwd`, `hook_event_name`, `model`, `permission_mode`, `session_id`, `source`, `transcript_path`. Claude와 달리 **`SessionStart`에도 `permission_mode`가 있다** |
| `UserPromptSubmit` 입력 필드 | 위 필드 + `prompt`, `turn_id` |
| 권한 모드 값 | YOLO 모드에서 `bypassPermissions`. Claude와 같은 이름이다 |
| 훅의 `session_id` = 큐의 thread id인가 | **같다.** `state_5.sqlite`의 `threads.id`와 일치했다 |
| 훅 환경변수 | **비어 있다.** 기본 HOME을 쓰는 세션에는 `CODEX_HOME`이 없고 thread id 환경변수도 없다. 훅의 부모 프로세스는 codex 본체다 |
| CODEX_HOME 유도 | `transcript_path` = `/Users/…/.codex/sessions/2026/09/19/rollout-…-<thread id>.jsonl`. **`sessions/` 앞부분이 CODEX_HOME이다** |
| 큐 입력에도 `UserPromptSubmit`이 실행되는가 | **실행된다.** 새 `turn_id`가 붙고 `prompt`에 큐 메시지 원문이 들어온다 |

## S6: Codex 수신 측 발신 표시 훅

`UserPromptSubmit` 훅 `codex_mesh_ups.py`는 본문 헤더 `[agent-mesh v1 from="…" ref=… kind=… mode=… id=…]`를 파싱한다.
- 수신 세션 모드(`bypassPermissions` → bypass, 그 외 → prompting)와 같으면 `additionalContext`로 Claude와 같은 취지의 경고를 붙인다.
- 다르면 `decision: "block"`을 낸다.
- 헤더가 없으면 아무것도 출력하지 않는다.

| # | 입력 | 훅 판정 | 관찰 |
|---|---|---|---|
| T6d | 사람이 TUI에 직접 입력(헤더 없음) | 개입 없음 | 정상 처리 |
| T6a | 큐, 헤더 `mode=bypass`(수신자와 같음) | `additionalContext` | 모델 응답: "ACK 6A — s6-sender 에이전트 세션이 보냈으며, 사용자가 직접 입력한 메시지가 아닙니다." 대화 기록(rollout)에 경고가 **`role: "developer"` 메시지**로 저장됐다 |
| T6b | 큐, 헤더 `mode=prompting`(수신자와 다름) | `block` | TUI에 "Blocked by hook" + 사유가 표시됐다. 모델 턴은 실행되지 않았다. **큐 항목은 이미 소비돼 사라졌고(pending 0), 대화 기록에도 본문과 차단 사실이 남지 않았다** |
| T6c | 큐, 헤더 없음 | 개입 없음 | 일반 입력으로 처리됐다(`ACK 6C`) |

해석:
- Codex에서도 "사용자 입력이 아니다"라는 표시를 Claude와 같은 수준으로 재구성할 수 있다. `developer` 역할로 들어가므로 사용자 메시지보다 우선순위가 높은 문맥이 된다.
- **차단은 보류가 아니다.** 메시지가 사라지므로, 사람이 검토하는 보류를 원하면 훅이 차단한 메시지를 메시 저장소에 기록하고 사용자에게 알려야 한다. Claude 네이티브에는 보류 대화상자가 있는 것과 대조된다.
- 헤더와 모드는 위조할 수 있다(같은 uid). 인증이 아니다.

## S7: 세션 이름과 네임스페이스

### Codex (같은 CODEX_HOME)

| # | 실험 | 결과 |
|---|---|---|
| c | thread 두 개에 `/rename xsm-dup` | 둘 다 `xsm-dup`으로 저장됐다(고유성 강제 없음). `codex queue --thread xsm-dup` → "Multiple sessions match 'xsm-dup' (including … and …); use a session UUID to disambiguate." 큐에 들어가지 않았다 |
| d | 두 번째를 `XSM-DUP`으로 바꾸고 `--thread XSM-DUP` | 대소문자를 구분해 두 번째 thread 하나만 일치했다. 그런데 **"Cannot verify a unique session label across server pages; matching session UUID: …"로 거부됐다.** 이 `~/.codex`의 활성 thread는 2,386개다. 한 페이지(100개)를 넘으면 고유성을 확인할 수 없어 이름 전송을 거부한다. c에서 "Multiple"이 나온 것은 두 thread가 첫 페이지에 있었기 때문이다 |
| f | `/rename xsm@dup` | `@`가 허용된다. `threads.name` = `xsm@dup` |

**결론**: thread가 많은 사용자에게 **Codex의 이름 기반 큐 전송은 사실상 쓸 수 없다.** 메시 계층이 이름을 UUID로 직접 풀어 보내야 한다.

### Claude

| # | 실험 | 결과 |
|---|---|---|
| a | 같은 `~/.claude-4`에서 두 세션을 `--name xsm-dup`으로 시작 | **둘 다 `xsm-dup`을 유지했다**(`nameSource: "user"`). 재검사 시간(3초)을 넘겨도 바뀌지 않았다 |
| a-원인 | GrowthBook 캐시 확인(`~/.claude-3/.claude.json`, `~/.claude-4/.claude.json`의 `cachedGrowthBookFeatures`) | **`tengu_session_name_uniqueness: False`**(코드 기본값은 true). 서버 설정으로 고유성 강제가 꺼져 있다. 같은 캐시에서 `tengu_harbor_kite: True`, `tengu_harbor_kite_mode_emit: True` |
| b | 다른 `~/.claude-3`에서 `--name xsm-dup` | 유지됐다. 세션 A의 `/list-agents`에는 같은 폴더의 B만 보이고 C는 보이지 않았다. A 자신은 `xsm-dup [1879d7]`처럼 ref와 함께 표시됐다 |
| e | 이름을 ref로 소켓 주소에 확정한 뒤 대상이 `/rename xsm-renamed`, 그다음 그 주소로 전송 | **정상 도착.** 레코드에 `formerNames: [{name: "xsm-dup", until: …}]`가 남았다 |

### 이름 해석 시제품 (`tools/mesh_resolve.py`, ADR-0008 B안 규칙, 읽기 전용)

실험 중 이 머신에는 `xsm-dup`이 세 홈에 걸쳐 있었다: `~/.claude-3` 1개, `~/.claude-4` 2개, `~/.codex` 1개(꺼짐).

| 입력 | 결과 |
|---|---|
| `xsm-dup` | 후보 4개(살아 있는 Claude 3, 꺼진 Codex 1) → **모호함** |
| `XSM-Dup` | 정규화로 `xsm-dup`과 같게 취급 → 모호함 |
| `xsm-dup@claude-4` | 같은 홈에 2개 → **한정해도 모호함** |
| `xsm-dup@claude-3` | 1개로 해석 |
| `xsm-dup@codex` | 꺼진 thread지만 한정했으므로 해석 |
| `xsm@dup` | 꺼진 후보뿐이라 맨 이름으로는 not-found |
| `xsm@dup@codex` | 마지막 `@` 뒤가 알려진 별칭 → 한정자로 해석 → 1개 |
| (e 이후) `xsm-dup@claude-4` | B가 이름을 바꾼 뒤 A 하나로 해석 |

해석:
- 한정 이름(`@홈별칭`)과 "마지막 `@`만 한정자" 규칙이 실제 데이터에서 의도대로 동작했다.
- Claude 고유성 강제가 꺼져 있으므로 같은 홈 안에서도 중복이 생긴다. **`이름 [ref]` 지정을 주소 문법에 넣어야 한다**(시제품에는 아직 없음).
- 맨 이름이 꺼진 후보만 가질 때 "not-found"보다 "꺼진 세션만 일치, `@홈`으로 지정하라"는 안내가 낫다.

## 남긴 흔적

- 테스트 프로세스와 tmux 세션은 모두 종료했다. Claude 테스트 세션의 레지스트리 레코드는 없다(강제 종료로 남은 1건은 PID가 죽은 것을 확인한 뒤 삭제했다). 테스트 폴더 `.local/xsm-c1`(프로젝트 로컬 훅 포함)은 삭제했다.
- 남은 것:
  - `~/.claude-3/projects/`, `~/.claude-4/projects/`의 테스트 대화 기록(`-private-tmp-xsm-spike-*`)
  - `~/.codex`의 테스트 thread 4개(`01a0b8c3…`, `01a0b8ef…`, `01a0b8f1…`(`xsm-dup`), `01a0b8f3…`(`xsm@dup`)). 큐 대기는 0건이다. 필요하면 `codex archive <id>`로 보관 처리할 수 있다.
  - `/tmp/xsm-spike/`(훅 스크립트와 로그)
- 모델 사용량: Claude 짧은 턴 5회(OK1~3, S3-PEER, S7E), Codex 짧은 턴 6회(READY, QH, READY6, 6A, 6C, READY7). 6B는 훅이 차단했고 7D는 전송이 거부돼 턴이 없었다
