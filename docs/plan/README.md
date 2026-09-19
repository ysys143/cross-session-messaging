# 실행 계획: 에이전트 세션 간 메시징 (cross-session messaging 확장)

- 상태: 골격(scaffold). 레퍼런스 조사, 적대적 리뷰(`docs/reviews/`), 보강 조사(`docs/references/supplement/`)를 마쳤다. 확정·변경 사항은 `docs/references/README.md` 0.5절에 있다. 스파이크(5장)와 ADR 토론(6장)이 남았다.
- 원본 의도: `INTENT.md`
- 선행 분석: `docs/list-agents-cross-session-messaging.md` (Claude Code 2.1.278 내부 구조)
- 결정 기록: `docs/adr/` (ADR은 토론 라운드를 거쳐 확정)

## 1. 목표와 제약

G1~G8과 C1·C2는 INTENT.md에 있는 라벨이 아니다. 이 계획이 INTENT.md를 해석해 붙인 것이다. 오른쪽 열이 해석의 출처다.

### 목표

| ID | 목표 | INTENT.md 근거 |
|---|---|---|
| G1 | 서로 다른 `CLAUDE_CONFIG_DIR`(`~/.claude`, `~/.claude-3` …)의 Claude 세션이 서로를 발견하고 메시지를 보낸다 | "#### claude code의 /list-agents 와 cross-session messaging을 확장" |
| G2 | Codex↔Codex, Claude↔Codex 메시징. `CODEX_HOME`·`CLAUDE_CONFIG_DIR`과 무관하게 동작 | "#### codex에서 messaging 구현 및 agent runtime 확장" |
| G3 | 수신 세션을 **직접 깨워서(invoke/wakeup)** 일을 시키고, 결과를 보고받고, 중간에 개입한다 | 같은 절 첫 항목 |
| G4 | SSH로 연결된 다른 머신의 세션과 통신 (이상적 목표) | 같은 절 |
| G5 | 프로젝트별로 통신 가능한 세션 범위를 제한 (ADR로 결정) | 같은 절 |
| G6 | 사람과 에이전트가 함께 보는 1:N·N:N 채널-스레드 기록 | "추가1)" |
| G7 | 공동 문서 편집에서 충돌 없이, 사본 증식이나 무한 잠금·대기 없이 협업 | "추가1)" 마지막 항목 |
| G8 | Swarm·Graph Engineering에서 이 채널을 쓰는 방식 | "추가2)" |

### 제약

- **C1. 별도 런타임에 의존하지 않는다.** 사용자는 평소처럼 `claude`와 `codex`를 직접 실행하고, 메시징은 그 세션들 위에서 추가 런타임 없이 동작해야 한다. Orca 같은 기성 멀티 하네스 안에서 띄운 세션도 그대로 참여할 수 있어야 한다(공존). 2026-09-20 사용자 확인: "금지가 초점이 아니라, 별도의 런타임에 의존하게 만드는 것을 해결하고자 했던 것임. 당연히 기성 멀티 하네스는 그대로 사용 가능해야 하고."
  - 훅, MCP 서버, 스킬, 백그라운드 서비스처럼 "옆에서 붙는" 구성 요소가 이 제약에 어긋나는지는 ADR-0003에서 정한다.
- **C2. 에이전트 CLI를 수정하지 않는다.** 공식 확장 지점과, 이미 존재하는 프로토콜을 호환되게 쓰는 방식만 사용한다.
  - 공식 확장 지점: 훅, MCP, 플러그인, 설정
  - 기존 프로토콜의 예: Claude inbox 소켓, Codex app-server

## 2. 현재 알고 있는 사실

### 2.1 Claude Code (직접 분석함)

근거: `docs/list-agents-cross-session-messaging.md`.

- 세션 레지스트리는 `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`이다. **프로필 격리의 원인이 바로 이것이다.**
  - 레코드에는 `messagingSocketPath`, `sessionId`, `cwd`, `name`, `status`, `procStart`가 들어 있다.
- inbox는 세션별 유닉스 소켓 `/tmp/cc-socks/<pid>.sock`이다. **모든 프로필이 같은 디렉터리를 쓴다.**
- 프로토콜은 줄 단위 JSON이다.
  - 인증 줄: `{"type":"auth","token":…}` (선택)
  - 메시지 줄: `{"type":"user","message":{"role":"user","content":…},"priority":"now|next|later"}`
- macOS/Linux에서는 인증 줄이 **선택 사항**이다. 소켓 권한 0600(같은 uid)이 보안 경계다.
- 실측: 소켓에 외부 프로세스가 JSON 두 줄을 쓰면 세션의 대화 입력으로 도착한다. 도착 시 머리말은 "Another Claude session sent a message:"다.
- 추론: 다른 프로필의 세션에도 소켓에 직접 쓰면 전달될 가능성이 높다. **아직 실험하지 않았다.** → 스파이크 S1.

### 2.2 Codex CLI 0.155.1 (로컬 확인)

- `codex queue --thread <UUID|이름> --message <TEXT>`: "Queue a message for an existing session". **Codex 쪽 inbox·wakeup의 1순위 후보다.**
- `codex agents`: "Browse all agent sessions on the shared local app-server daemon". 세션 발견 수단의 후보다.
- `codex app-server daemon|proxy`가 있다. 제어 소켓은 `$CODEX_HOME/app-server-control/app-server-control.sock`이다. `~/.codex/ipc/ipc.sock`은 TUI가 IDE 문맥을 가져오는 별도 IPC다(4바이트 길이 + JSON). 처음에 이것을 제어 소켓으로 잘못 적었다(리뷰 R1-01, `codex-rs/app-server-transport/src/transport/mod.rs:56-72`).
- `--remote ws://host:port | wss:// | unix://PATH`와 `--remote-auth-token-env`가 있다. 원격 연결 경로의 후보다(G4).
- `~/.codex/hooks.json`에 `SessionStart`, `UserPromptSubmit` 훅이 있다. Orca가 이미 여기에 훅을 걸고 있다.
- 확인 못 함:
  - 대화형 TUI 세션이 공유 app-server 데몬에 붙어 `queue`로 도달 가능한지
  - `CODEX_HOME`이 다르면 데몬과 소켓이 분리되는지

  → 스파이크 S2.

### 2.3 레퍼런스 조사 결과 (완료)

종합: `docs/references/README.md`. 계획에 직접 영향을 주는 사실은 다음과 같다.

- **Claude 네이티브 수신 정책**: `crossSessionInbound`(accept/hold/refuse)와 `isolatePeerMachines`가 있다.
  - 값이 없으면 보낸 세션과 받는 세션의 권한 모드 계열이 같을 때만 자동 전달한다.
  - 저장소 설정은 더 엄격하게만 바꿀 수 있다.
  - 이것이 G5(범위)의 출발점이고, S1의 결과에 영향을 준다(종합 2.3절).
- **wakeup 방식은 네 가지다.**
  - 네이티브 inbox: Claude
  - PTY 포인터 주입: orca. idle일 때만 "메시지 있음" 한 줄을 넣고, 본문은 에이전트가 가져간다.
  - PTY 전체 주입: herdr
  - MCP poll: moai. idle 세션을 깨우지 못한다.
  - 런타임 없이 wakeup까지 되는 것은 Claude 네이티브뿐이다(종합 2.1절).
- **Codex 연동**: 경로는 세 가지다. S2가 여전히 결정적이다.
  - **Orca TUI(PTY)**: Orca 터미널에서 띄운 Codex TUI에 입력창으로 전달한다. 이번 조사의 Codex 워커 2개가 이 경로로 작업을 받고 `worker_done`으로 답했다(실측).
  - **Orca 구조화된 세션(app-server)**: Orca가 `codex app-server`를 직접 띄워 stdio JSON-RPC로 연결한다. `thread/start`·`thread/resume`으로 스레드를 열고 `turn/start`로 메시지를 넣는다(`orca/src/main/codex/codex-app-server-connection.ts:52-67`, `codex-structured-thread-open.ts:49-87`, `codex-structured-turn-start.ts`). Codex는 턴이 진행 중일 때 온 `turn/start`를 진행 중인 턴에 합쳐 버린다. 그래서 Orca는 턴이 끝난 뒤에만 보낸다(`orca/src/main/runtime/orchestration/structured-session-pointer-delivery.ts:80-95`).
  - **moai**: `codex app-server`를 새로 띄워 thread를 재개한다.
  - 공통점: 셋 다 **전달하는 쪽이 세션 프로세스를 띄웠거나 소유한 경우**다.
  - **보강 조사 결과(T1)**: 사용자가 직접 띄운 Codex TUI에 닿는 **네이티브 경로가 있다.** `codex queue --thread <id>`가 `$CODEX_HOME/queue_1.sqlite`에 쓰면, TUI 안의 app-server watcher가 10초마다 변경을 감지하고 로드된 idle thread의 턴을 시작한다(`codex-rs/ext/queue/src/service.rs:89-96`).
    - 한계: 대상의 CODEX_HOME을 알아야 한다. Interrupted는 자동으로 깨우지 않는다. 진행 중인 턴에 끼어들지 못한다. 발신자 origin 필드가 없다.
    - 실측은 아직 없다(S2).
  - **금지안**: 별도 app-server로 사용자 TUI의 thread를 resume해 메시지를 넣으면 안 된다. Codex는 thread에 자체 잠금이 없다(Orca 개발자 설명, `orca/src/main/native-chat/structured-agent-session-history-adoption.ts:78-112`).
- **발견**: 레퍼런스 모두 "자기가 띄운 세션"이나 "스스로 등록한 세션"만 발견한다. 세션 훅으로 CONFIG_DIR 밖에 등록하는 방식(herdr 훅 + moai 레지스트리의 조합)이 G1·G2의 유력안이다.
- **채널-스레드와 문서 협업**:
  - buzz: 사람용 UI가 강하다. 운영 부담은 PostgreSQL + Redis + 릴레이다.
  - agora: append-only Git DAG이고 정본은 도출한다. 코드는 미공개다.
  - 이 둘이 G6·G7의 양 끝 선택지다.

## 3. 목표 아키텍처 초안 (가설)

> 이 초안은 ADR-0001의 선택지 A(공유 디렉터리)와 ADR-0002의 선택지 A(네이티브 inbox)를 **가정해서** 그린 것이다. 두 ADR은 모두 Proposed이고 결정되지 않았다. 경로 `~/.agent-mesh/…`와 명령 `mesh send`는 설명용 가칭이다(리뷰 R5-F1).

```
          ┌──────────── 공유 레지스트리 (CONFIG_DIR 독립) ────────────┐
          │  ~/.agent-mesh/sessions/<host>/<agent>-<id>.json          │  ← ADR-0001
          └────────▲──────────────────▲──────────────────▲───────────┘
                   │ 등록/갱신(훅)     │                  │
   Claude(~/.claude) Claude(~/.claude-3)   Codex(~/.codex)   Codex(~/.codex-2)
        │ inbox: /tmp/cc-socks/*.sock        │ inbox: app-server / codex queue
        └──────────── 전달 어댑터 (agent별) ──┘                              ← ADR-0002
                   │
   원격: ssh 포워딩 또는 ws 릴레이 (G4)                                      ← ADR-0007
   범위: 프로젝트 정책 파일 (G5)                                             ← ADR-0004
   기록: 채널-스레드 저장소 (G6), 문서 협업 규약 (G7)                        ← ADR-0005, 0006
```

구성 요소:

1. **공유 레지스트리.** 각 세션이 시작할 때 훅(`SessionStart`)이 자기 주소를 CONFIG_DIR 밖의 공용 위치에 기록한다.
   - Claude: `CLAUDE_CODE_MESSAGING_SOCKET`과 세션 레코드를 읽어 옮긴다.
   - Codex: thread id와 **CODEX_HOME 경로**를 기록한다. 큐 경로는 주소가 아니라 대상 HOME의 `queue_1.sqlite`를 쓰기 때문이다(T1, 리뷰 R1-04).
2. **전달 어댑터.** 수신 측 종류에 따라 전달 방식이 다르다.
   - Claude: inbox 소켓에 줄 단위 JSON을 쓴다. 본문은 `<cross-session-message from="…" from-mode="bypass|prompting">` 봉투로 감싼다. 봉투가 없으면 bypass 수신자에게는 보류된다(`docs/references/README.md` 0.5절).
   - Codex: `CODEX_HOME=<대상> codex queue --thread <id> --message …`. 봉투에 발신자 표시가 없으므로 본문 규약으로 표시하고, 대상 세션의 `UserPromptSubmit` 훅으로 경고 문맥을 붙이는 안을 검토한다(T1 4절).
   - 발신 측 인터페이스는 CLI 하나(가칭 `mesh send`)와 MCP 도구로 제공한다. 어느 에이전트에서든 호출할 수 있게 하기 위해서다.
3. **발견 인터페이스.** `mesh list`가 공유 레지스트리를 읽는다. 생존 판정은 PID와 시작 시각 비교, 소켓 프로브로 한다. Claude가 쓰는 방식과 같다.
4. **채널-스레드 기록.** 1:1 즉시 전달(mailbox)과 분리한다. 후보는 append-only 로그(jsonl/sqlite), buzz, 외부 메신저다.

이 초안은 레퍼런스 조사와 스파이크 결과에 따라 바뀐다. 특히 공유 레지스트리 대신 Orca 같은 기존 런타임의 레지스트리를 재사용하는 안도 ADR-0001에서 비교한다.

## 4. 작업 흐름과 단계

| 단계 | 내용 | 완료 기준 | 선행 |
|---|---|---|---|
| P0 | 레퍼런스 조사 5건 + 종합 | `docs/references/*.md` 5개와 종합 문서 | 완료 (`docs/references/README.md`) |
| P1 스파이크 | S1–S4 실험으로 핵심 가정 검증 | 각 스파이크의 결과 기록 | P0와 병행 가능 |
| P2 | ADR-0001~0004 토론 라운드와 확정 | ADR 상태가 Accepted | P0, P1 |
| P3 | G1: Claude 프로필 간 발견·전달 | 서로 다른 CONFIG_DIR의 Claude 두 세션 사이에서 왕복 메시지 | P2 |
| P4 | G2·G3: Codex 수신·발신, Claude↔Codex | Claude가 Codex에 작업을 지시하고 결과를 보고받음 | P2, S2 |
| P5 | G5: 범위 정책 | 정책 밖의 세션은 목록·전달에서 제외 | P3 |
| P6 | G6·G7: 채널-스레드와 문서 협업 규약 | ADR-0005·0006 확정 후 최소 구현 | P0 |
| P7 | G4: 원격 머신 | ssh 너머 세션과 왕복 메시지 | P3, P4 |
| P8 | G8: Swarm·Graph 패턴 정리 | R-G8-01~10을 계획과 ADR에 배분 | 요구사항 도출 완료(`docs/references/supplement/T7-g8-swarm-graph.md`). 기록·검증 관련 요구는 P6(채널)에, 정체성·메시지 도구 관련 요구는 P3·P4(전달)에 합류 |

## 5. 스파이크 (가정 검증 실험)

상태:
- **S1 완료(확인)**, **S2 완료(확인)**. 결과는 `docs/spikes/S1-S2-results.md`에 있다.
- **S3·S6·S7 완료.** 결과는 `docs/spikes/S3-S6-S7-results.md`에 있다.
  - S3: 조건부 확인. 강제 종료 시 종료 훅이 없다.
  - S6: 확인. 단, 차단된 메시지는 사라진다.
  - S7: B안 규칙 동작. Claude 고유성 플래그 꺼짐, Codex 이름 전송은 페이지 초과 시 거부.
- **S4 완료, S5 부분 완료(SSH 루프백).** 결과는 `docs/spikes/S4-S5-results.md`에 있다.
  - S4: Codex↔Claude 왕복 확인. 샌드박스가 켜진 Codex는 셸로 보낼 수 없다.
  - S5: 루프백으로 S5-1·S5-6, jaesol-macmini로 S5-2·S5-3을 확인했다. 원격 `from` 경로는 받는 머신의 같은 경로로 오배송되고, SSH 터널은 `isolatePeerMachines`를 무력화한다. S5-4(Orca), S5-5(NAT·buzz)는 남았다.
- S3, S6, S7의 근거와 미확인 항목은 `docs/references/supplement/T8-discovery-sender-naming.md` 4절에 있다.

### 공통 실험 원칙 (S1·S2에서 얻음)

- **세션**: 매번 전용 세션을 새로 띄운다. tmux와 `env -i`의 최소 환경을 쓴다. 사용 중인 세션에는 보내지 않는다.
- **도구**: 프레임 발신과 레코드 조회에는 `tools/spike_s1_send.py`를 쓴다.
- **사용자 설정을 수정하지 않는다.**
  - Claude: 설정 없음 상태는 `--setting-sources project,local`, 설정과 훅은 `--settings '<json>'`으로 실험 세션에만 준다.
  - Codex: 이미 신뢰된 저장소 아래의 git 무시 폴더(`.local/`)에서 실행한다. 폴더 신뢰를 승인하면 `~/.codex/config.toml`에 영구 기록되므로 승인하지 않는다. 훅은 프로젝트 로컬 `.codex/hooks.json`으로 건다(신뢰 승인 절차는 S3에서 확인).
- **사람의 결정을 대신하지 않는다.** 보류 메시지 승인("Deliver") 같은 결정은 자동 모드 분류기가 막는다(S1). 그 경로는 사용자가 직접 확인한다.
- **메시지 본문**에 "다른 세션에 연락하지 말 것"을 넣는다. 같은 프로필의 사용자 세션이 목록에 보이기 때문이다(S1).
- **Codex 사용량**: 주간 한도 경고가 있었다(S2). 턴 수를 최소로 설계한다.

### 스파이크 목록

| ID | 가정 | 실험 | 판정 기준 | 상태 |
|---|---|---|---|---|
| S1 | 프로필이 다른 Claude 세션에도 inbox 소켓으로 직접 전달된다 | 수신자 모드 × 봉투 × `crossSessionInbound` 행렬 | `docs/references/README.md` 0.5절 판정 순서와 일치 | **완료(확인)** |
| S2 | `codex queue`로 실행 중인 대화형 Codex 세션을 깨울 수 있다 | idle, busy, 다른 HOME, Interrupted | 턴 시작 여부와 시각, 오류 문구 | **완료(확인)** |
| S3 | 훅만으로 공용 레지스트리를 유지할 수 있다 | (a) Claude 실험 세션에 `--settings`로 `SessionStart`·`UserPromptSubmit`·`SessionEnd` 훅을 걸어 입력 JSON과 환경변수를 파일로 덤프한다. (b) Codex 실험 세션에 프로젝트 로컬 `.codex/hooks.json`으로 같은 덤프 훅을 건다. (c) 비정상 종료(kill -9) 후 남은 레코드가 생존 판정으로 걸러지는지 본다 | Claude: `SessionStart` 시점에 레지스트리 레코드와 inbox 소켓이 있는가, 훅 환경에 `CLAUDE_CODE_MESSAGING_SOCKET`이 있는가, `permission_mode` 값과 shift+tab 후 `UserPromptSubmit`의 갱신. Codex: 훅 입력의 session id가 queue의 thread id와 같은가, `CODEX_HOME` 환경변수 유무(기본 HOME일 때), `transcript_path`로 HOME을 유도할 수 있는가, 프로젝트 로컬 훅에 신뢰 승인이 필요한가 | **완료(조건부 확인)** |
| S4 | Codex 세션에서 Claude inbox로, Claude에서 Codex 큐로 보낼 수 있다 | Codex 실험 세션이 셸로 `tools/spike_s1_send.py`를 실행해 Claude 실험 세션에 봉투(`from-mode`=Codex 세션 모드)를 붙여 보낸다. 반대로 Claude 실험 세션이 `CODEX_HOME=… codex queue`를 실행한다 | 양방향 전달. Codex 샌드박스가 소켓 쓰기를 허용하는지. Claude 수신 측 보류 판정이 Codex 모드 표기와 맞는지 | **완료(확인, 샌드박스 제약)** |
| S5 | SSH 너머 전달이 피어 신원·회신 주소·머신 격리를 보존한다 | `docs/references/supplement/T4-remote-transport.md` 5절의 절차 | 같은 문서 기준 | **부분 완료(루프백 + jaesol-macmini)**. S5-4·S5-5 남음 |
| S6 | Codex 수신 측에서 발신 표시를 훅으로 재구성할 수 있다 | Codex 실험 세션에 `UserPromptSubmit` 훅을 건다. 훅은 본문 헤더 `[agent-mesh v1 …]`를 파싱해 (a) `additionalContext`로 경고 문맥을 붙이고 (b) 모드가 다르면 `decision: "block"`을 낸다. 큐로 헤더 있는 메시지와 없는 메시지를 보낸다 | (a) 큐로 들어온 입력에도 훅이 실행되고 모델 응답에 문맥이 반영되는가, (b) block된 큐 메시지가 큐에 남는지·사라지는지·사용자 화면에 어떻게 보이는지, (c) 사람이 직접 입력한 프롬프트에는 훅이 개입하지 않는가(헤더 없음) | **완료(확인, 한계 있음)** |
| S7 | 이름 기반 주소가 홈·런타임 간 충돌 규칙(ADR-0008 B)으로 해석된다 | (a) 같은 CONFIG_DIR에서 Claude 실험 세션 두 개에 같은 이름을 `/rename`해 양보와 새 이름 형식을 관찰한다. (b) 서로 다른 CONFIG_DIR의 Claude 세션 두 개에 같은 이름을 준다. (c) 같은 CODEX_HOME의 Codex thread 두 개에 같은 이름을 주고 `codex queue --thread <이름>`의 모호성 오류를 본다. (d) 대소문자만 다른 Codex 이름 두 개. (e) 전송 직전에 대상이 `/rename`했을 때 id 확정 방식이 오배송을 막는지. (f) Codex 이름에 `@`를 넣을 수 있는지 | (a) 새 이름 형식과 `nameSource: "collision"`, (b) 두 이름이 모두 유지되고 네이티브 `SendMessage`로는 다른 홈을 찾지 못하는가, (c) 오류 문구, (d) 네이티브는 구분하고 메시 정규화는 모호성 오류, (f) `@` 허용 여부에 따른 한정자 해석 규칙 확정 | **완료(확인)** |
| S8 | 사용자가 미리 정한 범위 안에서 핸드셰이크로 신뢰 관계(pair)를 수립한 뒤에만 메시지를 허용할 수 있다 | `docs/spikes/S8-handshake-plan.md` 4절의 S8-a~S8-j: 범위 안 자동 수립, 범위 밖 거부, 1회 승인, 오배송 방어, 재시작·PID 재사용, 같은 uid 위조 경계, Claude 네이티브 정책과 훅의 순서, 철회·만료, 원격 pair 지문 고정, 정책 변경 | 같은 문서 4절의 판정 기준. 특히 S8-f(위조 경계)로 "로컬 핸드셰이크는 동의 기록이지 보안이 아니다"를 실측으로 확인 | **완료(확인)**. `docs/spikes/S8-results.md` 보강 S8-c·S8-g2 완료(`docs/spikes/S8c-g2-results.md`) |
| S9 | agent-comms(ExaDev)·cc-peer 방식의 실측 비교 (E1~E6) | `docs/references/agent-comms.md` 4절의 제안. E1 Claude bridge가 개발 채널 플래그 없이 훅만으로 idle 세션을 깨우는가, E2 cc-peer default front의 기본 `fromMode="bypass"`가 bypass·prompting 수신자에게 각각 어떻게 판정되는가, E3 `~/.claude-3`·`~/.claude-4` 세션이 로스터에서 빠지는가(`sessionsDir()`가 `~/.claude/sessions` 고정), E4 `~/.codex`·`~/.codex-2` bridge가 같은 메시에 들어오는가와 `codex queue` 결합, E5 `mesh_listen`·`gateway_trust`로 jaesol-macmini 왕복, E6 코디네이터 kill -9 복구와 기록 소멸 | 전용 세션과 임시 HOME으로 실행해 `~/.agents/bus` 생성과 사용자 설정 변경을 피한다 | **완료(E1~E6)**. E5는 tailnet 한정·허브 끔으로 실행. E1 개발 채널 대조군과 E4c는 미실행. 코디네이터가 기본값으로 제3자 허브(`wss://mesh.exadev.io/`)에 접속한다는 사실을 발견했다. 결과 `docs/spikes/S9-results.md` |

## 6. ADR 목록

`docs/adr/README.md` 참고. 모두 Proposed 상태다.

| ADR | 질문 |
|---|---|
| 0001 | 세션 레지스트리를 어디에 두는가 (공유 디렉터리, 기존 런타임 재사용, 에이전트별 레지스트리 연합) |
| 0002 | 에이전트 종류별 전달·wakeup 방식 |
| 0003 | "진입점 래퍼 런타임 금지"의 경계. 훅, MCP, 백그라운드 서비스 중 무엇을 허용하는가 |
| 0004 | 통신 범위(scope) 모델 |
| 0005 | 채널-스레드 기록 저장소 (buzz, 외부 메신저, md/sqlite 규약) |
| 0006 | 공동 문서 편집의 충돌·잠금 규약 |
| 0007 | 원격 머신 통신과 신뢰 모델 |
| 0008 | 세션 이름, 주소, 네임스페이스 (홈·런타임 간 이름 해석과 충돌) |
| 0009 | 세션 간 신뢰 수립(핸드셰이크): 범위 안에서 한 번 신뢰를 수립한 뒤 메시지 허용 |

## 7. 위험

- **비공개 프로토콜 의존.** Claude inbox 프로토콜과 레지스트리 형식은 공개 API가 아니다. 버전마다 바뀔 수 있다. 확인된 변화 신호는 다음과 같다.
  - `peerProtocol: 1` 필드
  - `tengu_session_stable_address` 플래그
  - 대응: `tools/find_evidence.py` 같은 버전별 검증 스크립트를 CI 성격의 점검으로 둔다.
- **보안.** 같은 uid면 어느 세션에든 메시지를 넣을 수 있다. 범위를 넓히면(프로필 간, 원격) 공격면도 넓어진다. 수신 측의 권한 경고 문구와 auto 모드 규칙("피어 메시지는 사용자 의도가 아니다")은 유지된다. 그러나 원격 확장에는 별도의 인증이 필요하다(ADR-0007).
- **루프와 폭주.** Claude에는 수신 측 rate limit과 hop-chain 루프 차단이 있다. 우리 어댑터가 다른 에이전트로 중계할 때는 같은 보호 장치를 직접 구현해야 한다.

## 8. 미결 질문 (사용자 결정 필요)

- Q1. 진입점 래퍼 금지 제약이 백그라운드 데몬(세션을 감싸지 않고 옆에서 도는 서비스)도 금지하는가? → ADR-0003
- Q2. 원격 머신 지원(G4)은 이번 범위에 들어가는가, 후순위인가?
- Q3. 채널-스레드(G6)의 1차 사용자는 사람인가 에이전트인가? 사람이 1차 사용자라면 UI가 있는 buzz나 메신저가 유리하고, 에이전트라면 파일 규약이 유리하다.
