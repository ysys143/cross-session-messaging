# 스파이크 S4·S5 실험 결과

- 실행일: 2026-09-19 18:24~18:40 (KST)
- 환경: macOS, Claude Code 2.1.278, Codex CLI 0.155.1, OpenSSH(localhost 루프백과 원격 호스트 jaesol-macmini)
- 원칙: `docs/plan/README.md` 5장의 공통 실험 원칙을 따랐다.
  - 매번 전용 세션을 새로 띄웠다(tmux, `env -i`).
  - 사용자 설정 파일은 수정하지 않았다(실험 후 `~/.codex/config.toml`, `~/.claude-4/settings.json` 수정 시각이 실험 이전임을 확인).
  - Codex 훅은 프로젝트 로컬 `.codex/hooks.json`에 두고 `--dangerously-bypass-hook-trust`로 실행했다.
- **S5는 두 단계로 실행했다.** 먼저 SSH 루프백(`ssh localhost`)으로 S5-1·S5-6을 확인했고, 사용자 확인을 받은 뒤 원격 호스트 jaesol-macmini에서 S5-2·S5-3을 실행했다.
- 도구:
  - `tools/spike_s1_send.py`: 이번에 `--from`(빈 문자열이면 생략), `--name`, `--body` 옵션을 추가했다.
  - `/tmp/xsm-spike/s4_send_to_claude.sh`, `/tmp/xsm-spike/s4_reply_to_codex.sh`, `/tmp/xsm-spike/s5_recv.py`

## 요약

| 스파이크 | 결과 | 설계에 주는 영향 |
|---|---|---|
| S4 Codex↔Claude | **확인.** Codex → Claude → Codex 왕복이 성공했고, 양쪽 발신 표시가 모두 작동했다. 단, **샌드박스가 켜진 Codex는 셸로 어느 방향도 보낼 수 없다** | 발신 도구는 샌드박스 밖에서 실행돼야 한다. 셸 명령이 아니라 MCP 도구나 신뢰된 훅으로 제공해야 한다 |
| S5 원격(루프백 + jaesol-macmini) | **부분 확인.** 원격 실험(S5-2, S5-3)에서 답장 주소의 머신 간 오배송과 `isolatePeerMachines` 무력화를 실측했다(아래). SSH 포워딩 소켓으로 Claude에 전달되고, `ssh 호스트 'codex queue …'`로 Codex가 깨어난다. 그러나 **수신 측이 보는 발신자는 `sshd-session`**이고, Claude는 이를 **로컬 피어로 취급**해 `isolatePeerMachines` 원격 승인이 적용되지 않는다 | 원격 어댑터는 원격임을 스스로 표시하고 정책을 보존해야 한다. Codex 원격 전달은 SSH 원격 실행이 가장 단순하다 |

## S4: Codex ↔ Claude 전달

### 설계

- **Claude 수신 세션**: `~/.claude-4`, bypass, `--setting-sources project,local`, 이름 `xsm-s4-claude`
- **Codex 세션**: 사용자 기본값인 YOLO 모드. 프로젝트 로컬 훅은 S3 덤프 훅과 S6 발신 표시 훅
- **전송 경로**: 두 방향의 명령을 스크립트로 고정했다. 모델이 명령을 조립하는 능력이 아니라 전송 경로를 시험하기 위해서다.
  - Codex → Claude: `spike_s1_send.py`로 봉투 `<cross-session-message from-name="xsm-s4-codex" from-mode="bypass">`를 붙인다. `from`은 비운다(Codex에는 답장 소켓이 없음). 본문에 "답장은 `sh /tmp/xsm-spike/s4_reply_to_codex.sh`로"라고 적는다.
  - Claude → Codex: `codex queue --thread <tid> --message '[agent-mesh v1 from="xsm-s4-claude" … mode=bypass id=s4-reply] …'`

### 결과

| 단계 | 관찰 |
|---|---|
| Codex(YOLO)가 셸로 Claude 소켓에 전송 | 성공 |
| Claude 수신 | "Message from @xsm-s4-codex"로 표시됐다. 봉투의 `from-name`이 발신자 표시로 쓰였다 |
| Claude가 답장 스크립트 실행 | 에이전트가 **스크립트를 먼저 읽고 내용을 확인한 뒤** 한 번 실행했다. `codex queue`가 성공했다 |
| Codex 수신 | 헤더가 붙은 답장이 도착했고, S6 훅이 `additionalContext`를 붙였다. 응답 `ACK S4-REPLY` |
| 샌드박스 Codex(`-s workspace-write -a never`) → Claude 소켓 | **실패**: `PermissionError: [Errno 1] Operation not permitted` (`socket.connect`) |
| 샌드박스 Codex → `codex queue` | **실패**: "attempt to write a readonly database" (`~/.codex/state_5.sqlite` 쓰기 차단) |
| 샌드박스 Codex 시작 시 훅 | 훅 신뢰 옵션 없이 시작하자 "Hooks need review — 3 hooks are new or changed. Hooks can run outside the sandbox after you trust them."가 떴다. 기본 선택 "Continue without trusting (hooks won't run)"을 골랐고, 신뢰 기록은 남지 않았다 |

### 해석

- Claude↔Codex 교차 메시징은 두 런타임의 네이티브 수단(Claude inbox, Codex 큐)만으로 된다.
- **샌드박스가 결정적 제약이다.** 기본 설정의 Codex는 셸로 Claude 소켓에 연결할 수도, 큐 DB에 쓸 수도 없다. 발신 도구는 샌드박스 밖에서 실행되는 경로로 제공해야 한다.
  - 후보 1: MCP 서버 도구. Codex가 MCP 서버를 샌드박스 밖 프로세스로 실행하는지는 확인 못 함 → 후속 확인 필요.
  - 후보 2: 신뢰된 훅. "Hooks can run outside the sandbox after you trust them"이라는 화면 문구가 근거다.
- **훅 방식은 사용자의 1회 신뢰 승인이 전제다.** 이것이 ADR-0003(런타임 금지 경계)에서 허용할 부착 방식의 실제 비용이다.
- Claude의 bypass 에이전트도 받은 지시의 스크립트를 먼저 읽고 실행했다. 피어 요청을 검토하는 동작이 관찰됐다.

## S5: 원격 (SSH 루프백, 이어서 jaesol-macmini)

### S5-1 전송 관측: 수신 측이 보는 발신자

자격 정보 기록 수신기(`s5_recv.py`, macOS `LOCAL_PEERPID`·`LOCAL_PEERCRED`)에 두 방식으로 연결했다.

| 연결 | 수신 측이 본 peer | uid |
|---|---|---|
| 직접 연결 | 실제 발신 프로세스(pid 66537, `Python`) | 501 |
| `ssh -L <로컬 소켓>:<수신 소켓> localhost`로 포워딩 | **`sshd-session`(pid 65901)** | 501 |

SSH 포워딩을 거치면 원래 발신자의 PID와 프로세스 신원은 수신 측에서 사라지고, SSH 서버 쪽 프로세스만 보인다. uid는 SSH 로그인 계정의 uid다. T4의 예측과 같다.

### S5 Claude 전달

- 같은 방식으로 Claude 테스트 세션(bypass)의 inbox 소켓을 포워딩했다.
- 봉투 `from-name="xsm-s5-remote" from-mode="bypass"`, `from` 없음으로 보냈다.
- **결과: 수락됐다**(`ACK S5`).
- 수신 측은 이 메시지를 **로컬 uds 피어**로 취급한다. 봉투의 발신자 표시(`from-name`)는 발신자 자기 주장이고, 커널이 보는 peer는 `sshd-session`이다.
- `isolatePeerMachines`의 원격 승인 분기는 bridge/cloud 목적지에 걸리는 것이라, **SSH 포워딩 원격 전송에는 적용되지 않는다.** 원격임을 알리는 표시가 없어지는 것이다.

### S5-6 Codex 원격 큐

- `ssh localhost "CODEX_HOME=\$HOME/.codex codex queue --thread <tid> --message '[agent-mesh v1 …] …'"`로 보냈다.
- **결과: 대상 Codex 세션이 깨어나 `ACK S5-Q`로 답했고, S6 훅이 헤더를 인식해 경고 문맥을 붙였다.**
- 소켓 포워딩 없이 SSH 원격 실행만으로 된다. 인증은 SSH 키가 담당한다.

### S5-2·S5-3 실제 원격 (jaesol-macmini, 18:36~18:40, 사용자 확인 후 실행)

**환경 제약과 배치**
- macmini(macOS 26.6.2, Claude Code 2.1.278, Codex 0.154.0)에서는 SSH 세션의 Claude 프로필이 모두 `loggedIn: false`였다. 인증 정보가 로그인 키체인에 있어 비대화식 SSH에서는 읽히지 않는 것으로 보인다. 키체인 잠금 해제는 하지 않았다.
- 그래서 역할을 이렇게 나눴다.
  - **A (macmini)**: 발신자와 답장 수신기(자격 정보 기록). Python과 SSH만 쓴다.
  - **B (이 머신)**: Claude 테스트 세션(`~/.claude-4`, bypass).
- **연결**
  - A → B: 이 머신에서 `ssh -R /tmp/xsm-spike-remote/to-B.sock:<B inbox> jaesol-macmini`로 macmini에 B로 가는 소켓을 만들었다.
  - 미끼: 이 머신에 **macmini의 A와 같은 경로**(`/tmp/xsm-spike-remote/A.sock`)로 미끼 수신기를 두었다.

| 경우 | 봉투 `from` | 결과 |
|---|---|---|
| (i) 순진한 주소 | `uds:/tmp/xsm-spike-remote/A.sock` (macmini의 경로) | **오배송.** B의 `SendMessage` 답장이 macmini의 A가 아니라 **이 머신의 같은 경로에 있던 미끼**에 도착했다(미끼가 본 peer: B의 claude 프로세스). B는 "xsm-s5-A-macmini에게 보냈다"고 보고했다. **오류 없이 조용히 잘못 갔다** |
| (ii) 명시적 매핑 | `uds:/tmp/xsm-spike-remote/map-A.sock` (이 머신에서 `ssh -L`로 macmini의 A에 연결되는 경로) | **정확히 도착.** macmini의 A가 받았다(peer: `sshd-session`). 미끼에는 새 기록이 없다. 단, B의 답장 봉투에는 `from="uds:/tmp/cc-socks/<B pid>.sock"`(이 머신 경로)이 적혀, A가 다시 답하려면 반대 방향 매핑도 필요하다 |
| (iii) 머신 격리 | (ii)와 같음. B2에 `--settings '{"isolatePeerMachines":true}'` | **격리 정책이 적용되지 않았다.** B2는 다른 머신으로 가는 답장을 승인 요청 없이 보냈고, macmini의 A가 받았다. Claude는 `uds:` 경로를 로컬로 판단한다. `--settings`로 준 값이 적용됐는지는 UI로 확인할 수 없었다(`/config`에 노출되지 않는 키) |

**해석**
- `uds:` 주소에는 머신 정보가 없다. 머신을 넘으면 받는 쪽 머신의 **같은 경로에 있는 임의의 프로세스**에게 답장이 갈 수 있다. 원격 어댑터가 `from`에 원격 경로를 그대로 적으면 **보안 결함**이 된다.
- 올바른 방식은 둘 중 하나다.
  - 수신 머신 기준으로 유효한 주소로 **매핑**한다(양방향 모두 필요).
  - `from`을 비우고 본문 규약으로 회신 방법을 적는다(`from-name="이름@호스트"` + "`ssh 호스트 …`로 회신").
- SSH 터널 원격 전송은 Claude의 `isolatePeerMachines`를 무력화한다. 원격 어댑터는 수신 측에서 원격 메시지를 식별하고 정책(예: 원격이면 hold)을 **스스로 적용해야** 한다.

### 루프백과 이번 원격 실험으로 확인하지 못한 것

| 단계 | 이유 |
|---|---|
| S5-4 Orca 재페어링·철회 | 전용 Orca fixture가 필요하다 |
| S5-5 NAT·buzz mesh | 실제 네트워크 구성이 필요하다 |

### 해석

- **Claude 원격 전달(소켓 포워딩)**: 기술적으로 되지만, 원격 발신을 로컬 피어로 보이게 만든다. 원격 어댑터는 두 가지를 해야 한다.
  - 원격 여부를 봉투나 본문 규약에 명시한다(예: `from-name="이름@호스트"`).
  - 수신 측 정책(예: 원격이면 항상 hold)을 직접 적용한다.
  - 수신 측 커널 peer 검증이 원래 발신자를 증명하지 못한다는 점을 전제로 해야 한다.
- **Codex 원격 전달**: `ssh 호스트 codex queue`가 가장 단순하고, 소켓 포워딩의 신원 문제도 없다. 대상 호스트의 CODEX_HOME과 thread id를 알아야 한다.
- 두 런타임 모두 **"SSH로 원격 명령을 실행해 그 머신에서 로컬 전달을 한다"**는 방식이 가장 일관된다. 이렇게 하면 원격 머신에서의 발신자는 그 머신의 SSH 세션 프로세스가 된다.

## 남긴 흔적

- 테스트 프로세스, tmux 세션, SSH 터널(-L/-R), 수신기와 미끼는 모두 종료했다. Claude 테스트 세션의 레지스트리 레코드는 없다. 테스트 폴더 `.local/xsm-c4`는 삭제했다. macmini의 `/tmp/xsm-spike-remote`와 수신기 프로세스도 삭제·종료했다. macmini에서는 파일 생성과 프로세스 실행만 했고 설정은 건드리지 않았다.
- 남은 것:
  - `~/.claude-4/projects/-private-tmp-xsm-spike-s4c`, `-s5b1`, `-s5b2`(테스트 대화 기록)
  - `~/.codex`의 테스트 thread 2개(`01a0b8fb…`, `01a0b8fd…`). 큐 대기는 0건이다.
  - `/tmp/xsm-spike/`
- 모델 사용량:
  - Claude: 짧은 턴 5회(S4 수신, S5 루프백 수신, S5-NAIVE, S5-MAPPED, S5-ISOLATE)
  - Codex: 짧은 턴 6회(READY, 전송 지시, S4-REPLY 수신, 샌드박스 전송 지시 2회, S5-Q 수신)
