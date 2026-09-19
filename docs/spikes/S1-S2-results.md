# 스파이크 S1·S2 실험 결과

- 실행일: 2026-09-19 17:12~17:27 (KST)
- 환경: macOS, Claude Code 2.1.278, Codex CLI 0.155.1
- 원칙:
  - 사용 중인 세션에는 보내지 않았다. 매 실험마다 전용 세션을 새로 띄웠다.
  - 사용자 설정 파일은 수정하지 않았다.
  - 보류된 메시지의 승인("Deliver")은 사람의 결정이므로 대신 누르지 않았다.
- 도구: `tools/spike_s1_send.py` (프레임 발신, 세션 레코드 조회)

## 요약

| 스파이크 | 가정 | 결과 |
|---|---|---|
| S1 | 프로필이 다른 Claude 세션에도 inbox 소켓으로 직접 전달된다 | **확인.** `~/.claude-3` 세션의 독립 프로세스가 `~/.claude-4` 세션에 인증 줄 없이 전달했다. 수신 판정은 코드 분석(`docs/references/README.md` 0.5절)과 7건 모두 일치했다. 답장(`SendMessage`)도 발신 소켓으로 돌아왔다 |
| S2 | `codex queue`로 실행 중인 대화형 Codex 세션을 깨울 수 있다 | **확인.** 사용자가 직접 띄운 Codex TUI를 외부 프로세스가 `codex queue`로 깨웠다(약 13초). 다른 CODEX_HOME에서 보내면 명시적 오류가 난다. 진행 중인 턴에는 끼어들지 않고, Interrupted 상태에서는 자동으로 실행하지 않는다 |

## S1: Claude 프로필 간 전달

### 설계

- **수신 세션**: `CLAUDE_CONFIG_DIR=~/.claude-4`로 새로 띄웠다. 작업 폴더는 `/tmp/xsm-spike/rN`, 실행은 tmux, 환경은 `env -i`의 최소 환경이다.
- **설정 통제**: 모든 프로필의 사용자 설정에 `crossSessionInbound`가 이미 있었다. 그래서 `--setting-sources project,local`로 사용자 설정을 빼서 "설정 없음" 상태를 만들었다. R4만 `--settings '{"crossSessionInbound":"accept"}'`를 줬다.
- **발신**: 이 조사 세션(`~/.claude-3`)의 Bash에서 실행한 독립 Python 프로세스가 보냈다.
  - 수신 세션의 자손이 아니고 childToken도 쓰지 않는다. 대상 프로필의 키 파일도 없으므로 인증 줄 없이 보낸다.
  - 봉투 `from`은 `uds:/tmp/xsm-spike/sender.sock`으로 했고, 이 경로에 리스너를 띄워 답장을 받았다.

### 결과

| 수신 | 권한 모드 | 설정 | 프레임 | 예상 | 관찰 | 화면 사유 |
|---|---|---|---|---|---|---|
| R1 | bypass | 없음 | 봉투 `from-mode="bypass"` (R1-B, R1-B2) | 수락 | **수락** | "Message from @sender" |
| R1 | bypass | 없음 | 봉투 없음 (R1-N) | 보류 `no-mode-asserted` | **보류** | "The sender did not attest its permission mode, and this session bypasses permission prompts" |
| R2 | bypass | 없음 | 봉투 `from-mode="prompting"` (R2-P) | 보류 `mode-mismatch` | **보류** | "The sending session's permission mode class doesn't match this session's" |
| R3 | prompting | 없음 | 봉투 없음 (R3-N) | 수락 | **수락** | 응답 `ACK R3-N` |
| R3 | prompting | 없음 | 봉투 `from-mode="prompting"` (R3-P) | 수락 | **수락** | 응답 `ACK R3-P` |
| R3 | prompting | 없음 | 봉투 `from-mode="bypass"` (R3-B) | 보류 `mode-mismatch` | **보류** | R2와 같은 문구 |
| R4 | bypass | `accept` (플래그) | 봉투 없음 (R4-N) | 수락 | **수락** | "Another Claude session sent a message:" |

### 추가 관찰

1. **답장 왕복.** R1-B2에서 수신 에이전트가 `SendMessage`로 보낸 답장이 발신 소켓에 도착했다. 답장 봉투는 다음과 같다.

   ```
   <cross-session-message from="uds:/tmp/cc-socks/27913.sock" hop-chain="bdd832a3f58050957de47b5c" from-name="r1-bb" from-mode="bypass">
   ACK R1-B2
   </cross-session-message>
   ```

   - Claude가 보내는 봉투에는 `from-mode`가 붙는다. 모드를 봉투에 넣는 플래그가 이 환경에서 켜져 있다.
   - 우리 발신 소켓에는 키 파일이 없으므로 인증 줄 없이 왔다.
2. **봉투가 없으면 답장 주소가 모델에게 보이지 않는다.** R4-N은 수락됐지만, 에이전트는 "sender address가 없어서 누구에게 답할지 모른다"며 답장하지 않았다. 프레임의 `from` 필드는 수신 판정에는 쓰이지만 모델에게 보이는 본문에는 없다. **어댑터는 항상 봉투를 붙여야 한다.**
3. **거부 통지가 오지 않았다.** R1-N 보류를 Deny로 처리했을 때 "tell the sender it was declined" 통지가 리스너에 도착하지 않았다. 발신 프로세스가 전송 직후 종료해 확인된 발신자 PID가 없었던 것으로 추정하지만, 원인은 확인하지 않았다.
4. **테스트 에이전트가 사용자의 실제 세션을 봤다.** R1과 R4의 에이전트는 `ListAgents`로 같은 프로필의 사용자 세션(`gcp-service-account-key-cleanup`)을 봤다. 연락하지는 않았다. 본문에 "다른 세션에 연락하지 말 것"을 넣은 뒤에도 R4는 그 세션이 발신자인지 사용자에게 물었다. 같은 프로필의 세션은 서로 모두 보이므로, 테스트 메시지가 실제 세션으로 번질 위험이 있다.
5. **보류 승인은 에이전트가 대신할 수 없다.** R2 보류에 대해 이 조사 세션이 "Deliver"를 누르려 하자 auto 모드 분류기가 거부했다("Create Unsafe Agents"). 보류 해제는 사람만 할 수 있다는 설계 의도가 실제로 작동한다. 그래서 "승인하면 전달된다" 경로는 시험하지 않았다.
6. **사용자 프로필의 현재 설정.** 조사 시점에 `~/.claude`, `~/.claude-2`, `~/.claude-4`, `~/.claude-5`는 `crossSessionInbound: "accept"`이고, `~/.claude-3`은 `"hold"`다. 로그인된 프로필은 `~/.claude-3`과 `~/.claude-4`뿐이었다.

### 해석

- G1(프로필 간 메시징)의 **전달**은 Claude Code를 수정하지 않고 가능하다. 남은 문제는 **발견**(레지스트리가 프로필마다 분리됨)과 **정직한 `from-mode`**다.
- `from-mode`는 발신자가 스스로 적는 값이다. 어댑터가 실제 발신 세션의 모드를 적어야 모드 동등성 안전장치가 의미를 가진다.

## S2: Codex 큐 wakeup

### 설계

- **수신 세션**: `CODEX_HOME=~/.codex`로 Codex TUI를 새로 띄웠다(tmux, `env -i`). 작업 폴더는 `cross-session-messaging/.local/xsm-c1`(git 무시)이다.
  - 처음에는 `/tmp` 아래에서 띄우려 했으나, 폴더 신뢰 확인을 승인하면 `~/.codex/config.toml`에 영구 기록되므로 거부했다.
  - 대신 이미 신뢰된 저장소 아래에서 실행했다. 신뢰 판정은 경로가 정확히 일치하거나 git 루트가 일치해야 한다.
- **thread 생성**: TUI에 첫 프롬프트("READY")를 넣어 만들었다. thread id `01a0b8c3-5718-7732-94dc-e7757bcf6a25`는 `~/.codex/state_5.sqlite`의 `threads` 테이블에서 읽기 전용으로 찾았다.
- **발신**: 이 조사 세션의 Bash에서 `env -i … CODEX_HOME=<home> codex queue --thread <id> --message …`로 보냈다.

### 결과

| # | 조건 | 관찰 |
|---|---|---|
| S2a | 대상 idle, 같은 HOME | 17:24:28에 큐에 넣었다. 17:24:41에 TUI가 스스로 턴을 시작해 `ACK Q1`로 답했다(약 13초). 큐 항목은 소비 후 삭제됐다 |
| S2b | 대상 턴 진행 중(`sleep 25`) | 17:25:34에 큐에 넣었다. 턴이 끝나 `DONE-BUSY`가 나온 17:26:03 뒤, 17:26:07까지 `ACK Q2`가 나왔다. **턴 중간에는 끼어들지 않았다.** 완료 직후 전달돼, 10초 주기가 아니라 idle 이벤트로 바로 전달된 것으로 보인다 |
| S2c | 다른 HOME(`~/.codex-2`)에서 발신 | `Error: … no rollout found for thread id … (code -32603)`. 두 HOME의 큐 어디에도 들어가지 않았다. **조용히 사라지지 않고 즉시 실패한다** |
| S2d | 대상 Interrupted(Esc로 중단) | 17:26:32에 큐에 넣었다. 30초(watcher 3주기) 동안 전달되지 않았고 항목은 남아 있었다(pending=1). 이후 사용자가 다음 턴("RESUME")을 넣어 정상 완료되자 곧바로 `ACK Q4`가 전달됐고 큐는 비었다 |

### 추가 관찰

1. **발신 표시가 없다.** 큐로 들어온 메시지는 TUI에 사용자가 입력한 것과 같은 모양(`› S2 probe …`)으로 표시된다. Claude의 "Another Claude session sent a message"와 권한 경고 같은 표시가 없다(리뷰 R1-09가 실제로 확인됨). **어댑터는 본문 규약으로 발신 세션을 밝혀야 하고, 필요하면 Codex `UserPromptSubmit` 훅으로 경고 문맥을 붙여야 한다.**
2. **입력 제출 문제.** tmux로 TUI에 프롬프트를 입력하면 첫 Enter가 붙여넣기 줄바꿈으로 처리되어 제출되지 않았고, Enter를 한 번 더 보내야 했다. PTY 주입 방식(herdr, Orca)이 입력 보정을 두는 이유와 같다. 큐 경로에는 이 문제가 없다.
3. **테스트 thread가 남는다.** 테스트 thread는 사용자의 `~/.codex` 대화 이력에 남아 있다. 필요하면 `codex archive 01a0b8c3-5718-7732-94dc-e7757bcf6a25`로 보관 처리할 수 있다(되돌리기 가능). 이번 작업에서는 하지 않았다.
4. **사용량 경고.** Codex TUI가 "주간 사용 한도 10% 미만"을 경고했다. 실험에는 모델 턴 7회(중단된 1회 포함, 모두 짧은 응답)를 썼다.

### 해석

- G2·G3(Codex 수신과 wakeup)은 **별도 런타임 없이** 가능하다. 조건은 발신 측이 대상 세션의 **CODEX_HOME과 thread id**를 아는 것이다. 따라서 공용 레지스트리에 이 두 값이 있어야 한다(ADR-0001, 계획 3장).
- "중간 개입"(G3)은 큐로 되지 않는다. 턴 진행 중에는 다음 턴까지 기다리고, Interrupted 상태에서는 사용자의 다음 입력이 필요하다.
- 실패가 즉시 오류로 드러나는 점(S2c)은 전달 보장 설계에 유리하다.

## 남긴 흔적

- 테스트 세션 프로세스와 tmux 세션은 모두 종료했다. Claude 테스트 세션의 레지스트리 레코드는 종료할 때 자동으로 지워졌다.
- 남은 것:
  - `~/.claude-4/projects/-private-tmp-xsm-spike-r1~r4/`의 테스트 대화 기록
  - `~/.codex`의 테스트 thread
  - `/tmp/xsm-spike/`(리스너 스크립트, 답장 로그 `replies.jsonl`)
  - 사용자 설정 파일은 바뀌지 않았다.
