# Orca 벤치마킹 — CLI·스킬 표면 (2026-09-23)

`docs/references/orca.md`(2026-09-19, 소스 코드 분석)를 잇는 조사다. 그 노트는 Orca 저장소의
TypeScript 구현을 읽었고, 이 문서는 **설치된 바이너리가 스스로 내놓는 명령 표면과 버전 일치
가이드**를 읽었다. 앱 버전 `1.4.184`, 런타임 `ready`.

기존 노트와 겹치는 것(메시지 타입, federation 테이블, mailbox-pointer, DB 스키마)은 다시 쓰지
않는다. 그 뒤로 달라졌거나 노트가 다루지 않은 것만 적는다.

## 무엇을 읽어서 썼는가

읽은 파일:

- `~/.agents/skills/orca-cli/SKILL.md` (79행, 디스커버리 스텁)
- `~/.agents/skills/orchestration/SKILL.md` (99행, 스텁 + 사용자가 덧붙인 실측 함정 절)
- `~/.agents/skills/` 전체 목록 — orca 계열은 `orca-cli`, `orca-linear`, `orchestration`,
  `computer-use` 넷. `~/.claude/plugins/marketplaces/*/skills/`와 `~/.orca/`에는 orca 스킬이 없다.
- `/Users/jaesolshin/Documents/GitHub/cross-session-messaging/` 의 `INTENT.md`,
  `docs/adr/README.md`, ADR 0002·0003·0005·0006·0010·0012, `docs/xsm/PROTOCOL.md`,
  `skills/xsm/SKILL.md`, `xsm/cli.py`(파서 정의 1257–1490행)
- `docs/references/orca.md` (646행)

실행한 명령(모두 읽기 전용):

```
orca --help                      # 전체 명령 목록
orca skills list
orca skills get orchestration    # 406행, 버전 일치 가이드
orca skills get orca-cli         # 379행, 버전 일치 가이드
orca orchestration --help
orca orchestration {run-create,worker-start,worker-list,worker-read,check,task-create,gate-create,send} --help
orca status --json
orca orchestration run-list --json
orca worktree ps --json
orca automations list --json
```

상태를 바꾸는 명령은 하나도 실행하지 않았다. 아래에서 "가이드"는 `orca skills get` 출력이고,
행 번호는 그 출력 기준이다(사본: `$CLAUDE_JOB_DIR/tmp/orch.md`, `cli.md`).

---

## 요약 다섯 줄

1. Orca는 **자동 코디네이터 루프를 스스로 폐기했다**(`coordinator-start`/`stop`/`run`/`run-stop`은
   "retired scheduler commands", 가이드 283행). 지금의 Run은 "네임스페이스이자 홈 인박스일 뿐,
   워커를 배치하거나 스케줄링하지 않는다"(`run-create --help`). ADR-0012가 여는 "다음 노드는 누가
   고르는가"에 대해 Orca가 도달한 답은 "에이전트가 고른다, 런타임은 뷰만 준다"이다.
2. xsm에 없는 것 가운데 제약 안에서 가장 싸게 가져올 수 있는 것은 **워커 출력 읽기**,
   **수신 측 블로킹 대기**, **과제 종결의 명시적 outcome** 셋이다. 셋 다 일회성 명령이고
   상주 프로세스를 요구하지 않는다.
3. Orca의 조정은 전부 **앱 런타임에 대한 RPC**다(가이드 52행). 앱이 떠 있어야 하고, 실험 기능
   토글이 켜져 있어야 한다(가이드 51행). xsm이 피해야 할 구조는 이것이고 이미 피하고 있다.
4. Orca가 사람 승인을 다루는 방식 하나는 그대로 베낄 값이 있다. 아티팩트 공개는 데스크톱 설정에서
   사람이 켜야 하고, **CLI나 RPC로 켜는 길이 없으며**, 거부는 타입 있는 코드
   (`artifact_sharing_disabled`)와 "재시도하지 말라"는 지시로 돌아온다(cli 가이드 236–248행).
   xsm의 `approve`/`grant`도 같은 원칙인데, 거부 응답이 그만큼 명시적이지 않다.
5. Orca의 계약 마이그레이션 절은 48행짜리다(가이드 54–101행). 프로토콜을 버전 넘길 때 에이전트가
   읽어야 할 규칙이 어디까지 불어나는지 보여 주는 경고다. xsm은 `PROTOCOL.md` §7과
   `tests/vectors.json`으로 그 경계를 이미 좁혀 두었다.

---

## 1. Orca가 제공하는 조정 기능과 그 메커니즘

메커니즘 열은 문서와 도움말에서 **확인된 것만** 적는다. 소스 수준 구현은 기존 노트
(`docs/references/orca.md` 3·7·9절)를 본다.

| 기능 | 명령 | 메커니즘(확인된 범위) |
|---|---|---|
| Run(네임스페이스·코디네이터 인박스) | `run-create`, `run-use`, `run-current`, `run-list`, `run-show` | 런타임의 SQLite. `run-list --json`이 `home_database: "this_database"`, `coordinator_handle: term_…`, `coordinator_pane_key`, `consumer_generation: 1`을 돌려준다. "스케줄링도 배치도 하지 않는다"(`run-create --help` Notes) |
| 메시지·스레드 | `send`, `check`, `reply`, `inbox` | 앱 런타임 RPC(가이드 52행). `--thread-id`, `--priority`, `--payload` 지원. 타입: `status`, `dispatch`, `worker_done`, `merge_ready`, `escalation`, `handoff`, `question`, `decision_gate`, `heartbeat`(가이드 151행) |
| FIFO 배치 + ack | `check [--ack <delivery_id>]` | 코디네이터 `check`는 **가장 오래된 미확인 배치**(최대 50건)를 돌려주고, `--ack`가 올 때까지 같은 배치를 그대로 재생한다(가이드 139행). `--peek`/`--all`은 소비하지 않는 읽기 |
| 블로킹 수신 대기 | `check --wait --types … --timeout-ms …` | 폴링 대신 블로킹. 15초마다 stderr에 JSON keepalive 줄을 뱉어 호출자가 프로세스 생존을 안다(`check --help`). 타임아웃은 실패가 아니라 체크포인트(가이드 146행) |
| 블로킹 ask-reply | `ask --question … [--options csv] [--timeout-ms]`, `ask --resume <msg_id>`, `reply --id` | 워커가 `ask`로 막히고 코디네이터가 `reply`. 타임아웃이나 끊김은 질문을 **pending으로 남기고**, 다시 묻지 말고 원래 message id로 `--resume` 한다(가이드 148행). Windows 패키지 환경에서는 commit/resume 2단계에 런처 종료 코드 `75`(가이드 73행) |
| 그룹 주소 | `send --to @all\|@idle\|@claude\|@codex\|@worktree:<id>` | 확인된 것은 주소 문법과 "lifecycle 메시지는 그룹으로 보내지 말 것"이라는 규칙(가이드 150·152–153행). 팬아웃 구현 방식은 도움말에 없다 — **확인 못 함** |
| 태스크 DAG | `task-create --spec [--deps <json_array>] [--parent]`, `task-list [--ready] [--brief]`, `task-update` | 상태 여섯: `pending`, `ready`, `dispatched`, `completed`, `failed`, `blocked`(가이드 170행). `task-list --ready`를 "코디네이터의 외부 기억"으로 쓰고 의존 사슬은 3~4단계를 넘기지 말라고 명시(가이드 390행) |
| 태스크 디스패치 | `dispatch --task --to <handle> [--inject]`, `dispatch-show` | `--inject`는 과제 spec에 **생명주기 프리앰블**을 얹어 에이전트 CLI 입력으로 밀어 넣는다. 대상이 맨 셸이면 `--inject` 없이 디스패치만 하고 프롬프트는 `terminal send`로(가이드 174–175행). 한 태스크에서 3회 연속 실패하면 디스패치 컨텍스트가 **서킷 브레이크**되고 태스크가 failed가 된다(가이드 176행) |
| 워커 생명주기 | `worker-start`, `worker-show`, `worker-read`, `worker-stop`, `worker-abandon`, `worker-release`, `worker-retain`, `worker-list` | `worker-start`가 워크트리·터미널·준비대기·디스패치를 하나로 합성한다. `--agent`, `--model`, `--effort`, `--setup`, `--retry-of <dispatch_id>`, `--on <saved-environment>`. 회수는 상태 기계다: `active`/`reclaimable`/`retained`/`release_pending`/`release_unknown`/`released`(`worker-list --help`) |
| 완료 신고 | `send --type worker_done --task-id --dispatch-id --outcome succeeded\|failed --files-modified <csv> [--report-path]` | 유효한 `worker_done`이 태스크와 디스패치를 **자동으로 완료 처리**한다. 그래서 뒤에 `task-update`를 붙이지 말라고 못 박는다(가이드 154행). `--outcome`은 필수이고, 실패를 본문으로만 표현하는 것을 금지한다(`send --help` Notes) |
| 워커 출력 읽기 | `worker-read --dispatch <id> [--source auto\|transcript\|terminal] [--cursor]` | 기본 `auto`는 훅이 보고한 Codex/Claude/Grok 트랜스크립트를 **증명 가능할 때** 쓰고, 아니면 터미널 출력을 `source: "terminal"`과 타입 있는 `fallbackReason`으로 돌려준다. 커서는 소스에 고정되며 `source_changed`면 새로 읽는다 |
| 결정 게이트 | `gate-create --task --question [--options]`, `gate-resolve --id --resolution`, `gate-list` | 태스크를 막는 게이트. 워커의 `ask`와 **역할이 다르다**: `ask`는 워커→코디네이터 질문, 게이트는 코디네이터가 관리하는 DAG 결정이다(가이드 281행). 해결자는 코디네이터(에이전트)다 |
| 코디네이터 루프 | `coordinator-start`, `coordinator-stop` | **폐기됨.** 아무 효과 없이 현재 스킬의 복구 안내만 돌려준다(가이드 283행) |
| 핸드오프(소유권 이전) | `worktree create --agent --prompt`, `terminal send` | 오케스트레이션 명령을 **쓰지 말라**고 규정한다. `task-create`조차 금지다 — 태스크 행이 필요하면 그것은 감독 요청이라는 뜻(가이드 295행, cli 가이드 62행) |
| 터미널 제어 | `terminal list/show/read/send/wait/create/split/switch/close/stop/rename` | 런타임이 소유한 PTY. `wait --for exit\|tui-idle`, `read --cursor/--limit` 페이징. 핸들은 런타임 스코프이고 `terminal_handle_stale`이면 재획득(cli 가이드 205행) |
| 워크트리 | `worktree create/list/show/set/rm/ps/current` | id는 `<repoId>::<worktreePath>` 두 부분 주소(cli 가이드 97행). 계보(`--parent-worktree`/`--no-parent`)와 git base(`--base-branch`)는 **별개 축**이라고 반복해 경고한다(가이드 330행) |
| 사람이 보는 진행 | `worktree set --comment`, `--workspace-status todo\|in-progress\|in-review\|completed`, `task-create --task-title/--display-name` | 워크트리 카드의 짧은 상태 텍스트. `worktree ps --json`에 `workspaceStatus`, `comment`, `lastActivityAt`이 실제로 들어 있다 |
| 아티팩트 | `artifacts share/update/unshare/list/delete` | 사인인된 Orca 계정으로 HTML/Markdown 게시. **공개 게시는 기본 꺼짐이고 사람만 켠다**(데스크톱 Settings → Artifacts). CLI·RPC로 켜는 길이 없다. 거부는 `artifact_sharing_disabled`이고 파일을 읽기 **전에** 검사한다(cli 가이드 236–248행) |
| 스킬 공유 | `skills list/get/install/update` | 가이드 본문을 **바이너리가 직접 서빙**한다. 디스크의 SKILL.md는 "discovery stub, not the usage guide"이고 명령·플래그를 일부러 적지 않는다 — 바이너리와 드리프트가 날 수 없게 하려는 것 |
| federation(원격) | `worker-start --on <saved-environment>`, `environment add --pairing-code`, `send --to dispatch:<id>` | Run과 Task는 **홈 서버에 권위**로 남고, 이후 명령은 Dispatch ID로 라우팅되므로 `--on`을 다시 쓰지 않는다(가이드 215행). 원격에서 `current`·`new-child`는 모호해서 **의도적으로 무효**이고 정확한 원격 셀렉터나 `new-top-level`+원격 repo 셀렉터를 써야 한다(가이드 225행). `status --json`의 capabilities에 `orchestration.federation.v1`, `…federation-control-mail.v1`, `…federation-lifecycle-settlement.v1` |
| 자동화(스케줄) | `automations create --trigger daily\|hourly\|weekdays\|weekly\|<cron>\|<RRULE> --prompt --provider --repo\|--workspace` | 정해진 시각에 프롬프트를 워크트리에서 돌린다. 스케줄러 주체는 앱 런타임 — 도움말에는 그 이상 없다. `automations list --json`은 이 기계에서 빈 배열 |

확인 못 한 것: 그룹 주소의 팬아웃 구현, 자동화 스케줄러의 구동 주체와 앱이 꺼졌을 때의 동작,
`consumer_generation`의 현재 의미(기존 노트 10절이 소스에서 읽은 fence와 같은 것인지),
Run 간 격리의 실제 강도.

---

## 2. xsm에 없는 것 중 가져올 만한 것

각 항목에 (a) Orca가 그것으로 푸는 문제, (b) 없어서 지금 불편한 것, (c) 제약 안의 구현 형태,
(d) ADR을 적는다. (c)는 전부 **상주 프로세스 없이, 런타임 자체 수단으로** 되는 형태만 적었다.

### 2.1 워커 출력 읽기

- (a) `worker-read --dispatch <id>`가 훅이 보고한 트랜스크립트나 터미널 출력을 커서 페이징으로
  돌려준다. 코디네이터가 워커를 릴리스한 뒤에도 읽을 수 있다(가이드 389행).
- (b) xsm에서 워커가 무엇을 하고 있는지 보려면 사람이 `xsm attach`로 tmux 패널에 가야 한다
  (`skills/xsm/SKILL.md:163`). 부모 세션은 워커가 보고를 보낼 때까지 아무것도 모른다. 워커가
  막혔는지 일하는 중인지 구분할 수단이 없다 — Orca 스킬에 사용자가 직접 적어 둔 함정
  (`~/.agents/skills/orchestration/SKILL.md:78-99`, 워커가 `dispatched`로 영원히 남은 사고)이
  바로 이것을 못 보는 데서 왔다.
- (c) 워커는 tmux 창에서 돈다(ADR-0010 결정, `xsm/cli.py`의 `attach`). `tmux capture-pane -p -t
  <워커 타깃>`은 일회성이고 상주 프로세스가 없다. `xsm workers read <이름> [--lines N]`으로
  감싸면 된다. Orca처럼 소스를 라벨링할 필요는 없다 — 소스가 하나뿐이다. **미검증**: 워커 창
  타깃을 기록에서 되찾는 경로가 현재 `attach`에만 있는지 확인이 필요하다.
- (d) ADR-0010(워커 경계), ADR-0011(관측).

### 2.2 수신 측 블로킹 대기

- (a) `check --wait --types worker_done,escalation,question --timeout-ms`가 sleep 폴링을 없앤다.
  15초 keepalive로 호출자가 프로세스 생존을 안다.
- (b) xsm에는 **보내는 쪽** 대기만 있다(`send --wait`, `status --wait`, `xsm/cli.py:1407,1417`).
  받는 쪽에는 없다. `xsm inbox`는 플래그가 하나도 없는 일회성 조회다(`xsm/cli.py:1412-1413`).
  PROTOCOL.md:94-95가 기록한 사고가 정확히 이 부재다 — "S10 collab4에서 Codex 워커가 `sleep`
  폴링으로 턴을 끝내지 않아 15분 동안 받은 메시지 6건을 하나도 읽지 못했다."
- (c) `xsm inbox --wait <초> [--kind task,reply]`. 파일 하나(`inbox/<thread-uuid>/`)를 보는
  일회성 명령이 반환될 때까지 블로킹할 뿐이고, 명령이 끝나면 프로세스도 끝난다. 상주가 아니다.
  keepalive를 stderr로 흘리는 Orca의 처리는 그대로 베낄 값이 있다 — 에이전트가 "멈춘 것 아닌가"
  하고 중단하는 일을 막는다.
- (d) ADR-0002(전달·wakeup), ADR-0010(워커).

### 2.3 과제 종결의 명시적 outcome

- (a) `worker_done`은 `--outcome succeeded|failed`가 필수이고, 유효한 신고가 태스크와 디스패치를
  자동으로 종결한다. "실패를 본문으로만 표현하지 말 것"이 명시 규칙이다(`send --help` Notes).
- (b) xsm의 `--once`는 `task_id`에 대한 **아무 `reply`**나 오면 워커를 멈춘다
  (`PROTOCOL.md:332`). 성공과 실패를 구분하지 않는다. 부모는 본문을 읽어야만 안다. 원장에도
  남지 않는다.
- (c) 헤더에 선택 필드 하나를 더한다: `outcome=succeeded|failed`. PROTOCOL.md §1.2가 "알 수 없는
  필드는 무시한다"로 이미 열려 있으므로 v1 호환이다. 5.1.1의 `task` 수신 문맥이 붙여 주는 답장
  명령에 `--outcome`을 끼워 넣으면 워커가 따로 배울 것이 없다. 원장 영수증에 그대로 실린다.
- (d) ADR-0010(워커 종료), ADR-0011(원장·텔레메트리), ADR-0012(그래프에서의 결과 판정).

### 2.4 후보를 출력만 하는 "ready 뷰"

- (a) `task-list --ready`. 코디네이터는 이것을 "외부 기억"으로 쓰고, 런타임은 고르지 않는다
  (가이드 390행 + `run-create --help`의 "never schedules or places workers").
- (b) xsm의 `doc leaves()`와 `unverified()`는 이미 frontier와 미검증 가설 큐다
  (ADR-0012 맥락 절, `doc.py:122-125`). 그러나 **무엇을 먼저 할지 정렬하는 근거가 없고**,
  누가 무엇을 하는 중인지 보이지 않는다(`wip` 태그는 허용될 뿐 어느 뷰도 다루지 않는다).
- (c) `xsm doc next`가 후보를 정렬해 출력만 한다. 배정하지 않는다. ADR-0012가 이미 "DAG를 읽어
  후보를 출력만 하는 일회성 명령은 C1을 위반하지 않는다"로 결론지어 두었다. Orca의 기여는
  **런타임이 고르지 않는다는 선택 자체가 유지 가능하다는 증거**다 — Orca는 자동 코디네이터를
  만들었다가 폐기했다(가이드 283행).
- (d) ADR-0012(정면), ADR-0006(문서 노드).

### 2.5 재시도 계보와 연속 실패 차단

- (a) `worker-start --retry-of <dispatch_id>`가 재시도를 이전 시도에 묶되 배치는 **상속하지
  않는다**(가이드 267행). 한 태스크에서 3회 연속 실패하면 서킷 브레이크(가이드 176행).
- (b) xsm에는 시도(attempt)라는 개념이 없다. 워커가 실패하면 부모는 같은 과제로 다시 spawn하고,
  몇 번째인지 아무 데도 남지 않는다. 같은 과제로 워커를 무한히 태울 수 있다.
- (c) 워커 기록(`workers/<이름>.json`)에 `retry_of`와 `attempt`를 더하고, `spawn`이 같은
  `task_id`의 실패 시도를 세어 한도(예: 3)를 넘으면 거부한다. 파일만 읽는다.
- (d) ADR-0010.

### 2.6 사람 승인 거부의 타입과 "재시도하지 말라"

- (a) 아티팩트 게이트: 거부는 `artifact_sharing_disabled` 코드 + 복구 절차 + "Do not retry — the
  answer will not change until a human acts"이다. 그리고 **파일을 읽기 전에** 검사해서 거부
  비용을 한 왕복으로 줄인다.
- (b) xsm의 `approve`/`grant`는 원칙이 같지만(ADR-0009 계보, `human_terminal()`), 거부가
  에이전트에게 "다시 시도하면 될지도 모른다"로 읽힐 여지가 있다. `SKILL.md:190-197`은 산문으로
  "네가 답하지 말라"를 반복하는데, 이것은 기계가 읽는 코드가 아니다.
- (c) `approvals`/`grants` 거부 응답에 안정적인 `reason` 코드와 "사람이 답할 때까지 결과가
  바뀌지 않는다"는 한 문장을 넣는다. 벡터(`tests/vectors.json`)로 고정할 수 있는 종류의 변경이다.
- (d) ADR-0009(Superseded지만 계보), ADR-0010(승인 절).

### 2.7 그룹 주소

- (a) `@all`, `@idle`, `@claude`, `@codex`, `@worktree:<id>` — 한 번에 여러 세션을 깨운다.
  단 lifecycle 메시지는 그룹으로 보내는 것이 금지다.
- (b) xsm은 한 번에 한 세션이다. scope 안 전원에게 짧은 공지를 하려면 `xsm list` 후 반복해야
  한다. 채널(`xsm post`)은 **기록**이지 깨우기가 아니다 — 아무도 읽지 않을 수 있다.
- (c) `xsm send @scope --kind note`가 현재 scope의 live 세션에 같은 메시지를 각각 보낸다.
  전달은 기존 경로 그대로이고 원장에 N건이 남는다. `kind=task`는 금지한다(Orca가 lifecycle을
  막는 것과 같은 이유이고, xsm에서는 권한 확산이 더 큰 문제다).
- (d) ADR-0004(통신 범위), ADR-0005(채널과의 역할 분담을 여기서 다시 확인해야 한다).

### 2.8 원격에서 "홈이 권위, 이후는 id로 라우팅"

- (a) `--on <environment>`은 워커를 띄울 때 한 번만 쓰고, 이후 명령은 Dispatch ID로 라우팅된다.
  Run과 Task는 홈 서버에 권위로 남는다(가이드 215행).
- (b) xsm의 원격은 주소에 매번 `@peer`가 붙는다(`PROTOCOL.md:357`). 상대 세션과 여러 번
  주고받으면 매번 peer를 다시 적어야 하고, 잘못 적으면 로컬로 샌다.
- (c) 원격 대상을 한 번 해석한 뒤 `ref:<ref>@<peer>`를 그대로 쓰는 것이 이미 답장 명령의 규약이다
  (`PROTOCOL.md:357`). 부족한 것은 발신 측 편의가 아니라 **한 대화 안에서 peer를 고정하는 기록**이다.
  우선순위는 낮다. 이득이 크지 않고 0007이 Accepted로 닫혀 있다.
- (d) ADR-0007.

### 2.9 스킬 가이드를 바이너리가 서빙하기

- (a) 디스크의 SKILL.md는 스텁이고 명령·플래그를 일부러 안 적는다. 실제 가이드는
  `orca skills get <name>`이 바이너리에서 낸다. 캐시된 사본에서 플래그를 추측하지 말라고 명시.
- (b) xsm은 스킬이 저장소 안에 있고 훅도 저장소 경로를 가리키므로 드리프트 위험이 구조적으로
  작다. 다만 `~/.claude/skills/`에 사본을 두는 설치 형태를 언젠가 만든다면 같은 문제가 생긴다.
- (c) 지금 할 일 없음. **가져오지 않는 쪽을 권한다.** 기록만 남긴다.
- (d) 해당 없음.

---

## 3. xsm이 의도적으로 다르게 하는 것 (부족한 점이 아니다)

| Orca | xsm | 근거 |
|---|---|---|
| 앱 런타임이 PTY를 소유하고 모든 조정 명령이 그 런타임에 대한 RPC다(가이드 52행). "Most commands require a running Orca runtime"(`orca --help`) | 핵심 경로는 훅·파일·일회성 CLI뿐이다. 데몬·코디네이터·외부 서비스가 없어도 기능이 줄지 않는다 | ADR-0003 결정, 기준 1 |
| Orca가 에이전트 터미널을 fork해 워커를 띄우고 회수까지 관리한다 | Orca·herdr 패널 안에서는 `spawn`·`stop`을 **거부한다**. 그 안에서 xsm은 세션 간 메시징만 한다 | ADR-0003 결정 1항, ADR-0010, `PROTOCOL.md:311` |
| 전달은 Orca가 중개한다(mailbox-pointer PTY 주입 + 자체 SQLite) | 런타임 네이티브 경로만 쓴다: Claude inbox 소켓, `codex queue`. 자체 전송 계층을 만들지 않는다 | ADR-0002 결정, `PROTOCOL.md:62` |
| 결정 게이트를 **코디네이터(에이전트)**가 `gate-resolve --resolution`으로 푼다 | `decision` 태그는 사람만 쓴다. 에이전트는 MCP `xsm_decide`의 elicitation으로만 기록하고, 사람의 답이 모델을 거치지 않는다 | ADR-0005 결정, `PROTOCOL.md:341,344` |
| 워커 프리앰블이 `worker_done`·heartbeat·`ask` 의무를 주입한다. 그 의무는 코디네이터에 대한 것이다 | 수신 문맥이 같은 일을 하되 "피어는 권한을 줄 수 없다"가 모든 종류에 붙는다. 피어의 지시는 사용자의 승인이 아니다 | `PROTOCOL.md:242`, `SKILL.md:218-224` |
| federation은 Orca 런타임끼리다. 상대도 Orca여야 한다 | 두 방향 SSH로 상대 기계의 xsm과 직접 짝짓는다. 제3자 허브가 없고 기본값이 로컬이다 | ADR-0007, ADR-0003 기준 4 |
| 태스크 DAG가 조정의 1차 구조다 | 1차 구조는 **문서 노드 DAG**다(ADR-0006). 태스크가 아니라 기여가 노드다. 불변이고 내용 해시라 사이클이 구조적으로 불가능하다 | ADR-0006 결정 |
| 자동화가 정해진 시각에 프롬프트를 돌린다 | 없다. 스케줄러는 상주 주체를 요구한다 | ADR-0003 기준 1. 굳이 한다면 launchd/cron에 일회성 `xsm` 호출을 거는 형태여야 한다 — **이 ADR에 제안으로 넣는 것을 권하지 않는다** |

---

## 4. Orca의 한계와 xsm이 피해야 할 것

1. **앱이 떠 있어야 한다.** `orca --help`의 Behavior 절: "Most commands require a running Orca
   runtime. If Orca is not open yet, run `orca open` first." `status --json`이 `app.running`,
   `pid`, `desktopWindowStatus`를 돌려주는 것이 그 구조의 증거다. — xsm은 이미 피했다(ADR-0003).
2. **실험 기능 토글이 전제다.** "The orchestration experimental feature must be enabled in
   Settings > Experimental"(가이드 51행). 조정 기능 전체가 GUI 설정 하나에 걸려 있다.
3. **실행 파일 이름이 환경에 따라 갈린다.** Linux에서 맨 `orca`는 GNOME 스크린 리더
   (`/usr/bin/orca`)로 해석돼 음성이 나오기 시작한다. `orca-ide`를 써야 한다
   (cli 가이드 32–33행). 두 스킬 모두 이 경고를 맨 앞에 둔다 — 이름 충돌의 대가가 그만큼 크다.
4. **디스패치가 "입력을 받았다"와 "과제를 시작했다"를 구분하지 못한다.**
   `~/.agents/skills/orchestration/SKILL.md:78-99`는 사용자가 이 기계에서 실측한 함정을 적어
   두었다: `worker-start`가 `stage: "input_accepted"`를 돌려줘도 프롬프트가 입력창에 타이핑만
   되고 제출되지 않아 워커가 영원히 `dispatched`로 남는다 — 2026-09-11 세션에서 연속 두 번.
   해법은 `terminal read`로 tail을 읽고 엔터만 따로 보내는 것이다. **터미널 텍스트를 관측
   수단으로 쓰는 구조의 값이다.** xsm은 전달 확인을 수신 훅의 영수증으로만 판정하므로
   (`PROTOCOL.md:305` "전송 성공은 전달이 아니다") 같은 실패 양식이 없다. 이 선택을 유지한다.
5. **계약 마이그레이션 비용.** 가이드 54–101행이 전부 legacy authority label 규칙이다
   (`[LEGACY COMPATIBILITY]`, `[LEGACY RECOVERY REPLAY — MAY HAVE BEEN SEEN]`,
   `[LEGACY READ-ONLY]`, takeover, Windows 두 단계 commit/resume, 종료 코드 75, WSL 런치 토큰).
   에이전트가 메시지 하나를 처리하기 전에 읽어야 할 규칙이 48행이다. 호환성 층이 조정 자체보다
   길어진 상태. xsm은 `PROTOCOL.md` §7이 "필드 추가는 호환, 삭제·의미 변경은 v2"로 잘라 두었고
   적합성 기준이 `tests/vectors.json`이다. 이 경계를 흐리지 않는 것이 이 항목의 교훈이다.
6. **자동 코디네이터 루프는 유지되지 않았다.** `coordinator-start`/`coordinator-stop`/`run`/
   `run-stop`이 "retired scheduler commands"로 남아 아무 효과 없이 복구 안내만 낸다
   (가이드 283행). 자동 스케줄링을 만들었다가 접었다는 뜻이다. ADR-0012가 "런타임이 다음 노드를
   고르게 할 것인가"를 저울질할 때 이 사실을 근거로 써야 한다.
7. **원격에서 상대적 셀렉터가 무효다.** `current`와 `new-child`는 서버 간에 모호해서 의도적으로
   거부된다(가이드 225행). 원격을 붙이면 주소 문법이 갈라진다는 일반적 교훈. xsm의
   `…@<peer>` 규약도 같은 함정을 안고 있으므로, 원격 주소에서 상대적 표현을 허용하지 않는
   현재 설계를 유지해야 한다.
8. **아티팩트 공유는 기계가 켤 수 없다.** 이것은 한계가 아니라 배울 점이다(2.6).

---

## 5. 우선순위 — 효과 대비 구현 비용

| 순위 | 항목 | 효과 | 비용 | 제안을 넣을 ADR |
<!-- 구현 상태는 각 행 맨 앞에 [구현] 으로 적는다 (2026-09-23) -->
|---|---|---|---|---|
| 1 [구현] | **워커 출력 읽기** `xsm workers read` (2.1) | 부모가 워커의 상태를 처음으로 볼 수 있다. Orca에서 실제로 사고를 낸 실패 양식(워커가 조용히 멈춤)을 xsm에서 진단 가능하게 만든다 | 낮음. `tmux capture-pane` 한 번 + 기록에서 타깃 조회 | ADR-0010(부록), ADR-0011 |
| 2 [구현] | **수신 측 블로킹 대기** `xsm inbox --wait` (2.2) | 이미 발생한 사고를 직접 막는다 — 15분 동안 메시지 6건을 놓친 S10 collab4. sleep 폴링을 없앤다 | 낮음. 파일 감시 루프 하나, 프로세스는 반환과 함께 종료 | ADR-0002, ADR-0010 |
| 3 [구현] | **outcome 필드** (2.3) | 성공·실패가 원장과 `--once` 판정에 들어온다. 재시도 판단(5순위)의 전제다 | 낮음. 헤더 선택 필드 하나 + 벡터. v1 호환 | ADR-0010, ADR-0011 |
| 4 [구현] | **ready 뷰** `xsm doc next` (2.4) | ADR-0012가 던진 질문에 실행 가능한 형태를 준다. Orca가 자동 코디네이터를 폐기했다는 사실이 "뷰만 준다"는 선택을 뒷받침한다 | 중간. 정렬 근거를 정해야 한다(agora 식 2는 임베딩 없이 계산되고 `doc.py`의 `children()`·`author`·태그로 바로 나온다 — ADR-0012 맥락 절) | **ADR-0012**(정면) |
| 5 [구현] | **재시도 계보와 연속 실패 차단** (2.5) | 같은 과제로 워커를 무한히 태우는 것을 막는다. 3번 항목에 의존한다 | 중간. 워커 기록 스키마 확장 + `spawn` 게이트 | ADR-0010 |

들어가지 않은 것과 이유: 그룹 주소(2.7)는 ADR-0004·0005의 역할 분담을 다시 열어야 해서 비용이
순위 밖이다. 원격 라우팅(2.8)은 ADR-0007이 Accepted로 닫혀 있고 이득이 작다. 자동화는 3절에
적은 대로 제안하지 않는다.
