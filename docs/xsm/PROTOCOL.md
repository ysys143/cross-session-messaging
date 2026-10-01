# xsm 프로토콜 v1

다른 언어로 다시 구현하더라도 같은 동작이 나오도록, 와이어 형식과 상태 파일과 판정 규칙을 여기에 고정한다. 적합성 기준은 문장이 아니라 `tests/vectors.json`이다. 어떤 구현이든 그 벡터를 통과하면 xsm v1 구현이다.

```bash
python3 -m unittest discover -s tests      # 테스트 53건(벡터 35건 포함)
```

동작을 바꿀 때는 **코드와 벡터를 같은 커밋에서 함께** 바꾼다. 코드만 바꾸면 벡터가 깨지며, 그것이 이 파일의 목적이다.

## 1. 와이어 형식

메시지는 두 겹이다. 바깥은 **런타임의 것**이고 안쪽은 **우리 것**이다.

```
<cross-session-message from="uds:/tmp/cc-socks/1234.sock" from-session="<세션 id>" from-name="<이름>@<홈별칭>" from-mode="bypass|prompting">
[xsm v1 id=<msg-id> from="<이름>@<홈별칭>" ref=<6자리> scope="<scope id>" kind=note|task|reply reply-to=<msg-id>]
<본문>
</cross-session-message>
```

### 1.1 봉투

- 봉투 태그와 속성 순서는 Claude가 쓰는 것을 그대로 따른다: `from`, `from-session`, `hop-chain`, `from-name`, `from-mode`. 모두 선택 사항이다.
- Claude는 봉투를 네이티브로 해석해 수신 화면에 발신자와 회신 주소를 보여 준다. 봉투가 없으면 모델이 답장할 주소를 모른다(S1 관찰 2). **발신 어댑터는 항상 봉투를 붙인다.**
- `from-mode`는 발신자의 자기 주장이다. Claude의 수신 판정이 이 값을 쓰므로(5.3절) 구현은 **레지스트리에 기록된 실제 권한 모드**에서 채우고, 모드를 모르면 속성을 생략한다. 값은 두 부류뿐이다: `bypassPermissions` → `bypass`, 그 밖의 모든 모드 → `prompting`.
- 본문에 `<`, `>`, `&`를 넣으면 런타임이 봉투를 다시 쓰면서 형태가 바뀔 수 있다. 벡터는 이 문자를 쓰지 않는다.

### 1.2 헤더

한 줄이고, 여는 대괄호로 시작해 닫는 대괄호로 끝난다. 그다음 줄부터 본문이다.

```
[xsm v1 <field>=<value> ...]
```

| 필드 | 필수 | 뜻 |
|---|---|---|
| `id` | 예 | 16자리 16진수. 영수증과 스레드의 키 |
| `from` | 예 | `이름@홈별칭`. 사람이 읽는 표시용 |
| `ref` | 예 | 발신 세션의 6자리 지문(4절). **판정은 이름이 아니라 이 값으로 한다** |
| `scope` | 예 | 발신 시점에 성립한 scope id. 수신 측이 다시 계산해 비교한다 |
| `kind` | 예 | `note`, `task`, `reply` |
| `outcome` | 아니오 | `succeeded` \| `failed`. 과제를 끝맺는 `reply`에만 붙는다. `kind` 바로 뒤, 선택 필드 중 맨 앞에 온다 |
| `reply-to` | 아니오 | 답장 대상 `id` |

값에 공백이 있으면 큰따옴표로 감싼다. 알 수 없는 필드는 무시한다(앞으로 늘어날 수 있다).

`outcome`은 **보낼 때 엄격하고 받을 때 관대하다**. 만드는 쪽은 아는 값 두 개만 쓰고 그 밖의 값은 거부한다. 읽는 쪽은 모르는 값을 "없는 것"으로 접고 메시지는 그대로 전달한다. 나중 판에서 값이 늘어도 이 판이 메시지를 떨어뜨리지 않기 위해서다. 없을 때는 필드 자체가 빠지므로, 이 필드를 모르는 수신자에게 헤더는 예전과 글자 하나까지 같다.

### 1.3 분류

수신 측은 들어온 입력을 셋 중 하나로 본다.

| 분류 | 조건 | 뜻 |
|---|---|---|
| 사람 입력 | 봉투도 헤더도 없다 | 사용자가 직접 친 것으로 다룬다 |
| 남의 피어 메시지 | 봉투는 있고 헤더가 없다 | 다른 도구(cc-peer 등)나 직접 주입 |
| xsm 메시지 | 헤더가 있다 | 우리 것. 봉투는 있을 수도 없을 수도 있다(Codex 큐 경로) |

**알려진 한계.** 봉투 없이 들어온 피어 메시지는 훅 입력에서 사람 입력과 구분되지 않는다. 훅 입력 필드가 같고, 훅 실행 시점에는 대화 기록에도 아직 출처가 없다(S8-g2). v1은 이것을 막지 않는다.

## 2. 전달 경로

구현은 런타임의 네이티브 경로만 쓴다. 자체 전송 계층을 만들지 않는다.

### 2.1 Claude

`<세션 pid>.sock`(기본 `/tmp/cc-socks/`)에 **한 줄 JSON**을 쓴다.

```json
{"type": "user",
 "msg_id": "<msg-id>",
 "priority": "next",
 "from": "uds:<발신자 소켓 경로>",
 "message": {"role": "user", "content": "<1절의 봉투 전체>"}}
```

- `from`은 선택이지만, 없으면 수신 측이 회신 주소를 모른다.
- 연결이 `EPERM`이면 발신 세션이 샌드박스 안이다(S4). 구현은 이를 `sandbox-blocked`로 구분해 보고한다.

### 2.2 Codex

```
CODEX_HOME=<대상 홈> codex queue --thread <thread-uuid> --message <봉투 전체>
```

- **항상 UUID로 지정한다.** 이름 조회는 스레드가 100개를 넘으면 거부된다(S7).
- 대상 TUI가 스레드를 로드한 유휴 상태면 약 10초 안에 턴이 시작된다. 진행 중인 턴에는 끼어들지 않는다(S2).
- **첫 프롬프트 전 스레드로 보내기.** `codex queue`는 rollout이 없는 스레드를 `no rollout found`로 거부한다. 이 오류일 때만 xsm은 `codex queue`가 쓸 행을 대기열 DB(`queue_<n>.sqlite`의 `queued_items`)에 직접 쓴다: `id`(UUIDv7), `thread_id`, `payload_json = {"UserInput": {"content": [{"type": "text", "text": <봉투>, "text_elements": []}], "client_id": <UUIDv7>}}`, `queue_order = 그 스레드 최대값 + 1`, `created_at_ms = updated_at_ms = 지금`. DB 트리거가 리비전을 올리고 TUI가 가져간다(실측 8~14초). 열 구성이 이와 다르면 쓰지 않고 `codex-internal-changed`로 실패한다. Codex 내부 형식에 기대는 유일한 곳이다(ADR-0002 부록).
- **Esc로 멈춘 스레드 깨우기(2026-09-28).** Codex 대기열은 Interrupted 스레드를 건너뛴다. 그래서 xsm은 대기열에 넣은 직후 그 홈의 app-server 데몬 제어 소켓(`$CODEX_HOME/app-server-control/app-server-control.sock`)에 WebSocket(`GET /rpc`, 업그레이드)으로 붙어 JSON-RPC로 `initialize`(`capabilities.experimentalApi: true`) → `initialized` 알림 → `thread/queue/start {threadId, queuedSubmissionId}`를 보낸다. 항목 id는 `codex queue` 출력(``Queued message `<id>` for thread `<thread>` ``)에서, 직접 쓴 행이면 그 행의 `id`에서 얻는다. 결과는 넷이다. `started`(`result.turn`이 온다: 지금 턴이 시작됐다), `busy`(`thread already has an active or pending turn`: 항목은 남고 턴이 끝나면 평소대로 간다), `not-loaded`(`resume the thread before starting a queued message`: 데몬에 스레드가 없다), `unavailable`(소켓 없음, 연결·핸드셰이크 실패, 3초 초과, 모르는 응답). `started`만 발신 결과 문구를 바꾸고, 상태는 어느 쪽이든 `sent-unconfirmed`다. `thread/resume`과 `turn/start`는 부르지 않는다. `codex_wake: false`(config.json)나 `XSM_NO_CODEX_WAKE=1`이면 묻지 않는다(ADR-0002 부록).
- **기다렸다 받기.** `xsm inbox --wait <초>`는 사본이 하나라도 생길 때까지 막았다가 **생기는 즉시** 돌려준다. 상한 600초(사람을 기다리는 승인 한도와 같은 값)이고 넘기면 조이며 그 사실을 stderr로 알린다. 이 상한이 "대기는 데몬이 아니다"를 코드로 못 박는 지점이다. 15초마다 stderr로 살아 있음을 알리고(조용한 프로세스는 에이전트가 죽인다), 만료돼도 종료 코드는 **0**에 출력은 `(no messages waiting)`이다 — §6의 코드는 메시지 하나의 전달 결과이지 "아무것도 오지 않았다"가 아니다. 대기 루프는 세기만 하고 절대 꺼내지 않는다(꺼내기는 rename 선점이라 기다리던 메시지를 삼킨다). MCP `xsm_inbox`의 `wait`는 상한 60초다. MCP 서버가 요청을 한 번에 하나씩 읽으므로 더 길게 막으면 클라이언트에는 서버가 죽은 것으로 보인다.
- **턴 중 수신(`xsm inbox`).** 발신 측은 대기열에 넣기 전에 봉투 사본을 `inbox/<thread-uuid>/<id>.json`에
  둔다(대기열 전송이 실패하면 지운다). Codex 세션은 턴 도중 `xsm inbox`나 MCP `xsm_inbox`로 사본을 꺼낸다.
  꺼내는 순간 훅과 같은 검사(`receive.check`)를 거치고 같은 영수증을 쓴다. 발신 측은 `codex queue`가
  출력한 대기열 항목 id와 홈을 사본에 적어 두고(`queued_id`, `codex_home`), 사본을 꺼낸 쪽이 데몬 제어
  소켓의 `thread/queue/delete {threadId, queuedSubmissionId}`로 대기열 사본을 거둔다. 거두지 못하면(데몬 없음,
  샌드박스 셸, 이미 시작됨) 나중에 대기열 사본이 훅에 도착하고, 영수증이 이미 있으므로 보관하지 않고
  거부한다. Codex에서 훅 거부는 그 대기열 항목을 소비하지만(S6) 화면에 "Blocked by hook" 카드와 짧은 턴
  항목을 메시지마다 남긴다. 그래서 거두기가 기본이고 거부는 그 뒤의 안전망이다(이슈 #7, Codex 0.159.2에서
  삭제 응답 `{"deleted": true|false}`와 삭제 뒤 턴 종료 시 전달되지 않음을 실측, 2026-09-30). 반대로 훅이 먼저 받으면 사본을 지운다. 사본을 꺼낼 때는 rename으로 선점하므로 두 경로가
  동시에 읽어도 한 번만 넘긴다. Codex 세션이 부르는 xsm 명령과 MCP 도구는 대기 중인 사본 수를 알린다
  (명령은 stderr). 근거: S10 collab4에서 Codex 워커가 `sleep` 폴링으로 턴을 끝내지 않아 15분 동안 받은
  메시지 6건을 하나도 읽지 못했다.
- `readonly database` 오류는 샌드박스 발신이다. 발신 셸이 샌드박스 안이면(`CODEX_SANDBOX`, 또는 xsm이 Claude 워커 설정 `env`에 넣는 `XSM_SANDBOXED=1`) `codex queue`를 시도하지 않고 바로 거부하며 MCP `xsm_send`를 안내한다. `codex queue`는 상태 DB 쓰기와 내장 app server 기동이 필요해 두 샌드박스 모두에서 실패한다(S10 orch1 실측: 워커마다 1회씩 헛시도).

## 3. 상태 파일

모두 `XSM_HOME`(기본 `~/.xsm`) 아래의 JSON이다. 원자적으로 쓴다(임시 파일 + rename).

| 경로 | 스키마 |
|---|---|
| `mcp/<pid>.json` | `{"pid", "ppid", "lstart", "started", "cwd"}`: 실행 중인 xsm MCP 서버의 비콘(§4.3). 서버가 끝나면 지우고, 죽은 pid의 비콘은 읽을 때 정리한다 |
| `bounces/<session-id>/<ms>.json` | `{"t", "reason", "held", "to": {name, alias, ref, runtime, cwd}, "connect_dir"?, "preview"}`: 받는 쪽 게이트가 보관한 네이티브 메시지를, 소켓으로 식별된 보낸 세션에 알리는 쪽지(이슈 #8). 보낸 세션의 다음 `UserPromptSubmit`(막히지 않은 것)과 xsm CLI·MCP 결과가 한 번 보여 주고 지운다. 메시지가 아니므로 게이트를 지나지 않고 다시 반송되지 않는다 |
| `inbox/<thread-uuid>/<id>.json` | `{"id", "t", "content", "queued_id"?, "codex_home"?}`: Codex 대상 메시지의 봉투 사본(§2.2). 어느 경로로든 넘겨지면 지우고, 읽히지 않은 사본은 세션 포인터 보존 기간이 지나면 정리한다 |
| `config.json` | `{"strict_peers": bool, "same_repo_scope": bool, "retention_days": number, "ledger_retention_days": number, "telemetry_retention_days": number, "scopes": [{"id": str, "members": [{"runtime": str?, "home": str?, "cwd": glob?, "root": path?}]}], "reaches": [{"ref": str, "root": path, "t": number, "by": str, "runtime": str, "home": path, "session_id": str, "pid": number, "lstart": str?}], "links": [{"a": path, "b": path, "t": number, "by": str}]}`. `root`는 `xsm join`이 쓰는 구성원으로, 그 폴더와 그 아래 전부와 맞는다. `links`는 `xsm link`가 쓴다(§5.3.1) |
| `asked/<ref>.pending.json` | `{"verb", "target", "here"?, "cwd", "t", "session_id", "verdict": str\|null, "verdict_t"?, "shown"?: bool, "shown_t"?}`, 모드 0600. 사람이 정할 일을 동의 없이 실행한 에이전트의 요청. 그 세션에 사람이 입력한 **가장 최근** 메시지가 `verdict`로 붙고(새 메시지가 덮어쓰며 `verdict_t`가 갱신되고 `shown`은 거짓이 된다), 같은 명령을 다시 실행하면 먼저 에이전트에게 보여 주고(`shown`), 그다음 실행이 한 번 쓰고 지운다. 쓰거나 만료되면 옆의 `.lock`과 함께 지우고, 아무도 돌아오지 않은 것은 정리가 쓸어낸다(§5.3.3) |
| `asked/<ref>.json` | `{"verb": "link"\|"join"\|"leave"\|"reach", "args": str, "cwd", "t", "session_id", "runtime"}`, 모드 0600. 사람이 세션에 직접 입력한 xsm 명령으로, 그 세션의 동의다(§5.3.3). 한 번 쓰면 지우고, 10분이 지난 것은 정리가 지운다 |
| `interpreter` | `{"path": str, "version": str}`. 훅이 실행될 인터프리터 절대 경로. `xsm install --python`이 쓴다 |
| `homes.json` | `[{"path": str, "runtime": "claude"\|"codex", "alias": str}]` |
| `sessions/<runtime>-<session-id>.json` | `{"runtime", "home", "alias", "session_id", "pid", "lstart", "cwd", "ref", "updated", "permission_mode"?, "name"?, "ended_at"?, "end_reason"?, "mcp_pid"?, "mcp_lstart"?, "app_server"?}`. `app_server`는 Codex 기록의 pid가 TUI가 아니라 그 홈의 app-server 데몬일 때 `true`, 아닐 때 `false`다. 키가 없는 기록은 이 필드 이전의 것이라 pid가 산 동안 판정할 때 `ps`로 한 번 물어본다(§4.3) |
| `ledger/<msg-id>.json` | `{"id", "status": "queued", "t", "kind", "scope", "from": {...}, "to": {...}, "preview", "outcome"?, "closed_t"?, "closed_by"?}`. `outcome`은 그 과제를 끝맺는 답장이 도착했을 때 붙는다. `status`와 **별도 키**다 — 조회 시 영수증의 판정이 `status`를 덮어쓰므로 같은 키에 두면 사라진다 |
| `ledger/<msg-id>.recv.json` | `{"id", "decision": "delivered"\|"held"\|"blocked", "reason", "t", "receiver": {...}, "outcome"?}` |
| `attempts/<key>.json` | `{"key", "text"(200자), "cwd", "first_t", "tries": [{"t", "worker", "task_id", "ended_t"?, "outcome"?, "why"?}]}`. `key`는 `sha256(정규화한 과제 문장 + "\|" + realpath(cwd))`의 앞 12자다. 같은 문장이라도 폴더가 다르면 다른 일이다. 최근 20건만 남긴다 |
| `held/<밀리초>.json` | `{"t", "reason", "runtime", "receiver", "id"?, "from"?, "scope"?, "body"}` |
| `decisions.jsonl` | 한 줄에 판정 하나. 최소 `{"t", "decision", "reason"}` |

규칙:

- 포인터는 **훅만** 쓴다. 홈 목록을 glob으로 추측하지 않는다. 대상 목록은 `homes.json`과 포인터에 적힌 홈의 합집합이다.
- 이름과 소켓은 포인터에 캐시하지 않고 조회 시 런타임의 원본에서 읽는다. Claude는 `<홈>/sessions/<pid>.json`, Codex는 `<홈>/state_5.sqlite`의 `threads` 표다.
- **Codex 대신 등록.** 훅 신뢰가 확인된 Codex 홈에서는, CLI(`list`, `send`)가 아래 방법으로 찾은 열린 스레드를 직접 포인터로 기록한다(`adopted: true`). 그 홈에 xsm을 설치하고 훅을 신뢰한 것이 동의다. 이후 그 스레드의 훅이 처음 돌면 포인터를 다시 쓰고 `adopted`를 지운다. 훅 경로에서는 하지 않는다(프로세스 표를 읽는 데 수백 ms가 든다). 프롬프트도 `/rename`도 없는 Codex에는 스레드가 없으므로 주소를 줄 수 없다.
- **열려 있지만 등록되지 않은 Codex 스레드.** Codex는 첫 프롬프트 때 훅을 돌리므로, 띄우고 `/rename`만 한 세션은 레지스트리에 없다. 구현은 실행 중인 `codex` 프로세스(작업 폴더, 시작 시각, `resume <id>` 인자)와 `<CODEX_HOME>/state_5.sqlite`의 `threads`를 맞춰 프로세스마다 열린 스레드 하나를 찾고, 대상 이름이 그것과 맞으면 "없다" 대신 "열려 있지만 등록되지 않았다"와 이유를 돌려준다. 이유는 둘 중 하나다. 훅이 신뢰되지 않았다(`config.toml`의 `[hooks.state."<hooks.json 절대 경로>:<이벤트>:<그룹>:<훅>"]`에 `trusted_hash`가 없다), 또는 아직 프롬프트가 없다(스레드의 `rollout_path`가 없다).
- `decisions.jsonl`에 줄이 없다는 것은 곧 훅이 실행되지 않았다는 뜻이다. 판정은 성공·실패·예외를 가리지 않고 한 줄씩 남긴다.

### 3.1 설치의 멱등성

`install`은 여러 번 실행해도 같은 상태가 된다.

- 같은 인터프리터로 다시 설치하면 파일을 쓰지 않는다. 백업도 새로 만들지 않는다.
- 표식이 붙은 훅 항목이 손으로 중복됐거나 낡은 경로를 가리키고 있으면 하나로 정리한다.
- 표식이 없는 항목은 개수와 순서를 그대로 둔다.
- `--dry-run`은 아무것도 쓰지 않는다. 인터프리터 고정 파일도 건드리지 않는다.
- `--python` 없이 설치하면 이전에 고정한 인터프리터를 그대로 쓴다. 바꿀 때만 명시한다.
- 설정 파일을 다시 쓸 때 원래 들여쓰기를 유지한다. 설치 후 제거하면 파일이 바이트 단위로 원래대로 돌아온다.

## 4. 주소와 생존

### 4.1 ref

```
ref = sha256("<runtime>:<홈의 realpath>:<session-id>")[:6]
```

이름은 바뀌고 중복될 수 있으므로, 헤더와 판정은 `ref`를 쓴다.

### 4.2 해석 규칙

입력 형태는 `이름`, `이름@홈별칭`, `이름 [ref]`, `ref:<6자리>`, `claude:<세션id>`, `codex:<스레드id>`다.

1. 이름 비교는 대소문자와 공백·하이픈·밑줄을 무시한다.
2. 마지막 `@` 뒤는 **알려진 홈 별칭일 때만** 한정자다. 아니면 이름의 일부다(Codex 이름에는 `@`가 들어갈 수 있다).
3. 살아 있는 후보를 먼저 본다. 살아 있는 후보가 둘 이상이면 **거부하고 후보를 보여 준다.** 자동 선택하지 않는다.
4. 살아 있는 후보가 없고 멈춘 후보만 있으면 `offline-only`다.
5. `ref:`, `claude:`, `codex:`는 상태와 상관없이 그 기록을 찾는다. `ref`는 24비트라 겹칠 수 있다(합성 시도 약 1만 1천 번 안에 충돌, 2026-09-28). `ref:`에 맞는 기록이 둘 이상이면 `ambiguous`로 거부하고 각 후보의 `<runtime>:<session_id>` 주소를 보여 준다.
6. 보내기는 해석이 끝난 뒤 주소 형태와 상관없이 대상이 `ended`/`stale`이면 "… is not running (<state>)"와 재개 방법으로 거부한다. 어댑터는 부르지 않는다. `live`와 `unknown`(생존을 확인할 수 없는 세션)은 보낸다. 다른 기계로 보내는 `…@<peer>`는 이 판정 전에 원격으로 넘어간다.

### 4.3 생존 판정

| 런타임 | 판정 |
|---|---|
| Claude | pid 생존 + `ps lstart` 일치 + inbox 소켓 연결 성공 |
| Codex | pid 생존 + `ps lstart` 일치 + **xsm MCP 서버 생존**(`mcp_pid`, 있을 때) |
| Codex, 데몬이 호스트하는 스레드(`app_server`) | **데몬의 `thread/loaded/list`에 스레드가 있다.** 데몬이 답하지 않으면 pid 생존 + `ps lstart` 일치(비콘은 보지 않는다) |

- 수신 세션은 자기 pid를 `CLAUDE_CODE_MESSAGING_SOCKET`(경로에 pid가 들어 있다)이나 조상 프로세스 탐색으로 얻고, 둘 다 실패하면 같은 `session_id`로 이미 남아 있는 포인터에서 되찾는다. 그래도 알 수 없으면 5.1절 3번 규칙이 적용된다.
- 종료 훅에 의존하지 않는다. 강제 종료 시 `SessionEnd`는 실행되지 않는다(S3).
- **주의:** Claude가 자기 레코드에 쓰는 `procStart`는 UTC이고 `ps lstart`는 로컬 시간이다. 두 값을 직접 비교하면 안 된다. 우리가 등록한 포인터의 `lstart`만 `ps` 출력과 비교한다.


**Codex 스레드 교체.** 한 Codex TUI 프로세스는 `/new`나 resume으로 스레드를 바꾼다. 옛 스레드는 그 프로세스 안에서 약 60초 뒤 `Shutdown`되고, 그 뒤로는 자기 대기열을 읽지 않는다(실측 2026-09-23: 살아 있는 pid 앞으로 보낸 메시지 2건이 영원히 `queued`). Codex는 스레드가 열릴 때마다 MCP 서버를 새로 띄우고 닫힐 때 죽이므로, **xsm MCP 서버의 pid가 곧 스레드의 생존이다.** MCP 서버는 시작할 때 비콘(`mcp/<pid>.json`: pid, ppid, lstart, started, cwd)을 쓰고 끝날 때 지운다. Codex 훅은 등록할 때 자기 Codex pid 아래 가장 최근 비콘의 pid를 `mcp_pid`로 적는다. 그 프로세스가 죽었으면 기록은 pid가 살아 있어도 `ended`(`end_reason: thread_replaced`)다. 비콘이 없으면(MCP 서버 미설치, adopt) pid 판정만 쓴다.

**데몬이 호스트하는 Codex 스레드.** 0.157부터 Codex TUI는 기본으로 `CODEX_HOME`마다 하나인 app-server 데몬의 클라이언트다. 그러면 훅과 MCP 서버가 TUI가 아니라 데몬 아래에서 돈다. 조상 탐색이 찾는 `codex`는 데몬이므로 그 홈의 모든 스레드가 같은 pid를 적고, 멈춘 스레드도 데몬이나 다른 TUI가 사는 한 `live`로 보였다(issue #5; 실측 2026-09-29: 한 데몬 pid에 스레드 14개). 모든 스레드의 MCP 서버가 데몬의 자식이라 "pid 아래 가장 최근 비콘"도 마지막에 열린 스레드의 것이다(실측: 두 번째 TUI의 비콘이 첫 스레드 기록에 적혀, 열려 있는 첫 스레드가 `ended`로 읽혔다). 그래서 등록할 때 pid가 공유 데몬(`codex app-server --listen unix:// --managed-daemon`, `--managed-daemon`이 있어야 한다. 앱·IDE의 stdio app-server는 데몬이 아니다)이면 기록에 `app_server: true`를, 아니면 `false`를 적고(키가 없는 옛 기록은 pid가 살아 있는 동안 판정 때마다 `ps`로 같은 질문을 한다), 생존은 데몬 제어 소켓(`$CODEX_HOME/app-server-control/app-server-control.sock`)의 `thread/loaded/list`로 판정한다. 목록에 있으면 `live`, 없으면 `ended`(`end_reason: thread_unloaded`)다. 데몬은 스레드를 잡은 마지막 TUI가 끝나고 약 60초 뒤 그 스레드를 내려놓고(그 순간 MCP 서버도 끝난다), 데몬이 스스로 업데이트해 pid가 바뀌어도 TUI가 다시 붙으면 스레드를 다시 올린다(실측 2026-09-29, 0.158/0.159). 소켓이 없거나 샌드박스가 연결을 거부하거나 시간 안에 답이 없으면 아무것도 말하지 않은 것으로 보고 pid 판정으로 넘어간다. 제한 시간은 CLI에서 1초이고, 프롬프트마다 도는 수신 훅에서는 0.2초다(훅 제한 10초 안에 응답하지 않는 홈이 여럿이어도 들어오게 한다). 답은 홈마다 2초, 답을 못 받은 실패는 30초 동안 캐시하므로 응답 없는 홈은 30초 창마다 한 번의 제한 시간만 든다(2026-09-29 검토).

**첫 프롬프트 전의 Codex 스레드.** Codex는 첫 프롬프트나 `/rename` 전에는 스레드 행도 rollout도 쓰지 않고 훅도 부르지 않는다. 그래도 로그 DB(`logs_<n>.sqlite`)에는 TUI가 스레드를 연 지 몇 초 안에 `process_uuid = pid:<pid>:…`와 스레드 id가 함께 찍힌다. xsm은 어떤 기록도 가리키지 않는 MCP 비콘마다 그 부모 pid의 로그에서 비콘 시작 시각에 가장 가까이 처음 나타난 스레드(±30초)를 찾아 그 스레드를 **adopt**한다(`adopted`, `unprompted`, `mcp_pid`). 신뢰된 훅이 설치된 홈만 대상이다. 이름은 붙지 않았으므로 `codex-<id 끝 6자>`다(UUIDv7은 앞자리가 시각이라 몇 분 사이 연 스레드끼리 앞 8자가 같았다). 로그에서 id를 찾지 못한 비콘은 `codex-<pid>@codex [-] (… no prompt yet …)`로만 보인다.

한 Claude 프로세스는 `/clear`나 `--resume`으로 세션 id를 바꾼다. 그래서 같은 pid의 기록이 여럿 생긴다. 네이티브 기록(`<홈>/sessions/<pid>.json`)의 `sessionId`와 기록의 id가 다르면, 그 기록은 프로세스가 살아 있어도 `ended`(`end_reason: superseded`)로 본다.

**지우기.** `xsm list clear`는 기준 폴더와 범위가 맞는 레코드 가운데 `ended`/`stale`인 것의 포인터 파일을 지운다. `-a`면 범위를 따지지 않는다. `live`와 `unknown`은 지우지 않는다. 원장과 보류 기록은 건드리지 않는다.

**목록 범위.** `xsm list`는 기본으로 살아 있는 세션 가운데 기준 폴더와 범위가 맞는 것만 보여 준다. 기준 폴더는 `--dir`, 없으면 부른 세션의 cwd, 없으면 셸의 cwd다. 범위가 맞는다는 것은 같은 기본 프로젝트이거나 가입한 이름 붙인 프로젝트를 공유한다는 뜻이다. 자기 자신은 늘 보인다. `-a`/`--all`은 모든 프로젝트, 그리고 멈춘 세션과 미등록 세션까지 보여 준다.
### 4.4 세션 수명 주기

| 상태 | 뜻 | 어떻게 정해지나 |
|---|---|---|
| `live` | 메시지를 받을 수 있다 | pid 생존 + `ps lstart` 일치 + (Claude) 소켓 연결 |
| `ended` | 정상 종료했다 | `SessionEnd` 훅이 `ended_at`과 `end_reason`을 기록했고 프로세스가 없다. xsm이 멈춘 워커(`end_reason: worker-stopped`)는 기록된 pid가 살아 있어도 `ended`다. 데몬이 내려놓은 Codex 스레드(`thread_unloaded`)도 `ended`다 |
| `stale` | 인사 없이 사라졌다 | 프로세스가 없는데 종료 기록이 없다. 강제 종료, 터미널 닫힘, 충돌 |
| `unknown` | 판단할 근거가 없다 | pid를 모르는 Codex 세션. 죽은 것으로 취급하지 않는다 |

Codex에는 `SessionEnd`가 없으므로 멈춘 Codex 세션은 `stale`이다. 예외는 위의 `thread_replaced`, `thread_unloaded`, `worker-stopped`다.

**재개.** `claude --resume <세션 id>`는 **같은 세션 id**로 돌아온다(2026-09-21 실측: pid는 바뀌고 ref와 이름은 그대로). 그래서 SessionStart 훅이 같은 포인터를 다시 쓰고, 이때 종료 기록을 지워 `live`로 되돌린다. 주소와 ref가 끊기지 않는다.

**멈춘 상대에게 보내기.** 보내지 않고 거부한다(Claude는 받을 소켓이 없다). 거부 이유에 어떻게 멈췄는지와 재개 명령을 싣는다.

```
refused: only stopped sessions match 'life-b'
  life-b@claude-4 [ac63ed] exited cleanly (prompt_input_exit); resume it with: CLAUDE_CONFIG_DIR=… claude --resume <id>
```

**원장.** `queued`로 남은 메시지의 대상이 더 이상 `live`가 아니면 `undelivered`로 표시한다. 원장 파일은 고치지 않고 표시할 때 판단한다. 원격 대상(`to.runtime: remote`)은 이 기계 레지스트리에 없으므로 이 판단에서 빼고 `queued`로 두며, 영수증은 상대에게 있으니 `xsm status <id>`로 묻는다는 메모를 붙인다(issue #4).

**보관 기간.** 상주 프로세스가 없으므로, 세션이 시작할 때(`SessionStart` 훅)와 CLI를 실행할 때 기회가 되면 정리한다. 최대 한 시간에 한 번이다(`<XSM_HOME>/last-prune`의 수정 시각).

| 대상 | 기본 보관 | 설정 키 | 이유 |
|---|---|---|---|
| 멈춘 세션의 포인터 | 7일 | `retention_days` | 재개하면 같은 주소가 돌아오므로 바로 지우지 않는다. 기준 시각은 `ended_at`, 없으면 마지막 `updated` |
| 원장과 영수증 | 30일 | `ledger_retention_days` | "그 메시지가 도착했나"에 답할 만큼 |
| 보류된 본문 | 30일 | `ledger_retention_days` | 같음 |
| 판정 요청(`asked/`) | 창을 넘기면 | (고정: `TTL` 10분, `ASK_MAX` 30분) | 사람의 말을 1000자까지 담는다. 판정 파일과 `.lock`은 쓰이거나 만료되면 그때 지우고, 아무도 돌아오지 않은 것(판정 파일 없는 `.lock`, 10분이 지난 입력 동의 포함)을 이 정리가 지운다(§5.3.3) |
| 스팬과 메트릭 포인트 | 7일 | `telemetry_retention_days`(`0`이면 무한) | 사고 직후에 보는 기록이다. 머리에서만, **이미 내보낸 줄만** 지우고 `otlp-cursor.json`을 그만큼 내린다. 커서가 닿지 않은 줄은 남긴다(ADR-0011) |
`live`인 포인터는 기간과 무관하게 지우지 않는다. `xsm prune --dry-run`으로 무엇이 지워질지 먼저 볼 수 있다.

## 5. 판정

### 5.1 수신 게이트

`UserPromptSubmit`에서 순서대로 본다. `SessionStart`는 등록만 하고 절대 차단하지 않는다.

1. 봉투도 헤더도 없다 → **아무것도 출력하지 않는다**(사람 입력).
2. 헤더가 없다(Claude 자체의 피어 메시지) → `strict_peers`가 참이면 **차단**한다. 수신 세션이 Claude가 아니면 **차단**한다. 봉투의 `from`이 이 기계의 `uds:` 소켓이면 발신자와 범위에 상관없이 **통과**시키고 아무것도 출력하지 않는다(Claude 자신의 안내가 그대로 보인다). 같은 기계·같은 사용자이므로 범위 검사는 지킬 것이 없고 Claude가 허락한 연결만 끊었다(ADR-0013, 2026-10-01 개정). 다만 사람이 `deny`로 막은 세션(수신 세션, 또는 소켓을 가진 살아 있는 세션 하나)이면 **차단**한다. 소켓을 가진 살아 있는 세션 하나가 있으면 판정 기록에 그 이름을 적는다. `from`이 이 기계의 `uds:` 소켓이 아니면(Remote Control, 클라우드, 다른 기계) **차단**한다. Claude의 네이티브 봉투에는 세션 id가 없다. 이 경로에서 보관한 메시지(지금은 사실상 `strict_peers`)의 발신자가 살아 있는 세션 하나로 식별되면, 그 세션 앞으로 `bounces/`에 쪽지를 남긴다(이슈 #8). 범위 검사는 xsm 헤더가 있는 메시지에만 적용되고, `xsm send`는 보내기 전에 범위 밖을 거부한다.
3. 수신 세션이 자기 자신을 식별하지 못한다(세션 환경변수가 없고 기존 포인터도 없다) → **차단**. 범위를 검사할 수 없는 상태에서 통과시키면 그 세션이 열린 문이 된다.
4. 발신자 `ref`가 레지스트리에 없다 → **차단**. 봉투의 `from`이 `uds:<socket>`인데 헤더의 발신자가 Claude 세션이 아니거나, 그 세션의 소켓이 봉투의 소켓과 다르다 → **차단**. Claude가 채우는 봉투가 실제 경로이고 헤더는 발신자가 쓰는 글이기 때문이다(ADR-0013). 어느 한쪽에 소켓이 없으면 비교하지 않는다.
5. 발신자가 살아 있지 않다(`live`나 `unknown`이 아니다) → **차단**. 멈춘 세션의 포인터는 며칠 남으므로, 그 이름이 지금 메시지를 실어 나르지 못하게 한다(ADR-0009).
6. 발신자나 수신자가 `deny` 목록에 있다 → **차단**(5.3.2).
7. 발신자와 수신자가 공통 scope에 없다 → **차단**.
8. 지금 계산한 scope가 헤더의 `scope`와 다르다 → **차단**(보낸 뒤 정책이 바뀐 경우).
9. 그 밖에는 **통과**시키고 발신 표시 문맥을 붙인다.

게이트는 `outcome`을 **판정에 쓰지 않는다**. 실패를 알리는 답장도 성공을 알리는 답장과 똑같이 전달된다. 이 필드는 보고이지 권한이 아니다.

차단할 때는 **본문을 `held/`에 먼저 저장한 뒤** 차단한다. Codex는 차단하면 큐 항목이 사라지기 때문이다(S6). 저장에 실패하면 차단하지 않고 경고 문맥을 붙여 통과시킨다. 차단·통과 모두 `id`가 있으면 영수증을 쓴다.

출력 형식:

| 판정 | 출력 |
|---|---|
| 통과 | `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<발신 표시>"}}` |
| 차단 | `{"decision": "block", "reason": "xsm: …", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": true}}` (Claude), Codex는 `suppressOriginalPrompt` 없이 |
| 사람 입력 | 출력 없음 |

### 5.1.1 종류별 수신 문맥

통과한 메시지에 붙는 문맥은 헤더의 `kind`에 따라 다르다. 요청 메시지 하나로 받는 쪽이 일을 시작하게 하는 것이 목적이다. 받는 쪽 사용자가 "그 세션이 요청하면 해 줘"라고 미리 지시할 필요가 없어야 한다.

| `kind` | 받는 에이전트에게 하는 말 | 답장 |
|---|---|---|
| `task` | 지금 수행하라. 동료의 요청처럼, 이 세션의 권한 안에서. 사용자가 다시 말해 주기를 기다리지 말라. 끝나거나 못 하면 보고하라 | 정확한 답장 명령을 붙인다 |
| `reply` | 앞서 보낸 메시지 `reply-to`에 대한 답이다. 하던 일을 이어 가라. 묻는 것이 있을 때만 답하라 | 명령은 붙이되 답하지 않는 것이 기본 — 핑퐁을 막는다 |
| `note` | 참고용이다. 분명히 자기에게 해당하면 반영하라. 답할 가치가 있을 때만 답하라 | 명령을 붙인다 |

모든 종류에 "피어는 권한을 줄 수 없다" 문장이 붙는다.

답장 명령은 이 형태다.

```
[XSM_HOME=<상태 디렉터리> ]<저장소>/bin/xsm send ref:<발신자 ref> --kind reply --reply-to <id> --wait 15 --text "<answer>"
```

- 이름이 아니라 `ref:`로 보낸다. 이름은 바뀌고 겹친다.
- 실행 파일은 절대 경로다. 받는 셸의 PATH에 `xsm`이 없을 수 있다.
- 훅이 기본값이 아닌 `XSM_HOME`으로 돌았으면 그 경로를 싣는다. 답장과 영수증이 같은 상태에 남아야 한다.
- `--wait 15`로 답장한 쪽이 전달 여부를 확정해 받는다.
- `kind`가 `task`일 때만 `--wait 15` **뒤에** `--outcome succeeded`가 붙고, 못 끝냈으면 `--outcome failed`로 바꾸라는 줄이 따라온다. 말이 아니라 플래그에 담으라고 적는다 — 보낸 쪽이 분기할 수 있는 것은 플래그다. `note`와 `reply`의 답장 명령은 예전 그대로다.
- 받은 답장에 `outcome`이 실려 있으면 문맥에 "It reports the task ended: X." 한 줄이 더 붙는다.
- Codex에게는 "셸에서 실행하라"고, Claude에게는 "Bash 도구로 실행하라"고 적는다. Codex는 피어 메시지에 대한 자체 안내가 없어서 이 문맥이 유일한 안내다.

실측(2026-09-21): 역할을 미리 지시받지 않은 세션이 `task`를 받아 요청된 확인을 모두 하고 추가 경계 조건까지 시험한 뒤, 붙어 온 답장 명령을 그대로 실행했다. 요청한 쪽은 `reply`를 받고 다시 답하지 않았다.

### 5.2 고장 시 규칙

훅이 예외로 끝나면 런타임은 이를 비차단 오류로 처리하고 프롬프트를 그대로 실행한다(S8-g2). 따라서 구현은 모든 예외를 잡고 **입력 종류에 따라 갈라진다.**

- 봉투나 헤더가 보이면(원문 문자열 검사 포함) → `block`.
- 그렇지 않으면 → 출력 없음. **사람이 자기 세션에서 쫓겨나면 안 된다.**
- 두 경우 모두 `decisions.jsonl`에 남긴다. 종료 코드는 항상 0이다.

### 5.3 네이티브 판정 예보

Claude는 우리 훅보다 **먼저** 자체 판정을 한다. 구현은 보내기 전에 이를 예측해 알린다.

| 수신자 `crossSessionInbound` | 모드 부류 | 예보 |
|---|---|---|
| `accept` | 무관 | accept |
| `hold` 또는 `refuse` | 무관 | 그 값 |
| 없음 | 양쪽 같음 | accept |
| 없음 | 다름 | hold |
| 없음 | 한쪽이라도 미상 | unknown |

`refuse`면 보내지 않고 거부한다. 예보는 수신 홈의 **사용자 설정 파일**만 읽으므로 `--settings`나 프로젝트 설정으로 뜬 세션과는 어긋날 수 있다. 예보이지 판정이 아니다.

### 5.3.1 프로젝트 가입

`xsm join <이름>`은 호출한 세션의 프로젝트 폴더(git 루트, 없으면 그 폴더)를 `id`가 `<이름>`인 scope의 `{"root": …}` 구성원으로 추가한다. `xsm leave`는 그 구성원을 지우고, 구성원이 남지 않으면 scope를 지운다.

- 기본 프로젝트가 먼저다. 모든 세션은 시작한 디렉터리의 프로젝트(`repo:<저장소 이름>`, 저장소가 아니면 `dir:<폴더 이름>`)에 속하고, 두 세션의 기본 프로젝트가 같으면 이름 붙인 프로젝트와 관계없이 그 scope를 쓴다. 이름 붙인 프로젝트는 기본 프로젝트에 더해지는 중복 가입이다. 같은 저장소인지는 `git rev-parse --git-common-dir`(모든 linked worktree가 함께 쓰는 `.git`)로 판정하고, 저장소 이름은 그 `.git`을 담은 메인 체크아웃 폴더 이름이다. 그래서 Orca나 `claude --worktree`가 만든 worktree와 메인 체크아웃은 같은 기본 프로젝트이고 판정 사유는 `same git repository (linked worktree)`다. 같은 원격의 별도 clone은 `.git`이 달라 다른 저장소다. link·reach·join의 폴더는 지금처럼 각 worktree의 toplevel이다(2026-10-01).
- 두 세션은 **각자의 폴더가 같은 프로젝트의 구성원일 때만** 그 scope를 공유한다. 한쪽의 가입만으로는 열리지 않는다.
- 이름은 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`이다. `root` 구성원이 없는 손으로 쓴 scope와 이름이 겹치면 가입을 거부한다.
- 가입이 한쪽뿐이라 거부될 때 이유 문구에 가입하지 않은 폴더와 필요한 명령을 붙인다.
- **link.** `config.json`의 `links: [{"a", "b", "t", "by"}]`. `a`와 `b`는 두 프로젝트 폴더(각각 git 루트, 없으면 그 폴더)다. 한 세션의 시작 폴더가 한쪽 아래이고 다른 세션의 시작 폴더가 다른 쪽 아래이면(방향은 가리지 않는다) 두 세션은 `link:<이름>+<이름>` scope로 통한다. 이름은 두 루트의 basename이고, 순서는 두 루트의 전체 경로를 정렬한 순서라 양쪽이 같은 id를 계산한다. 이 판정은 기본 프로젝트와 명시 scope 다음, 워커와 reach 앞에 온다. 링크는 세션에 묶이지 않고 `xsm unlink`로 지울 때까지 남는다. 한쪽에서 한 번이면 된다(사용자 결정, 2026-09-28). 넓히는 일이라 사람만 만든다: CLI `xsm link <폴더>`는 사람이 터미널에서 직접 쳤거나(`human_terminal()`) 그 세션에 맞는 입력 동의(§5.3.3)가 있을 때만, 세션에서는 MCP `xsm_link`가 입력 동의를 쓰고, 없으면 elicitation으로 묻는다. 좁히는 `xsm unlink <폴더>`와 `drop: true`는 누구나 한다. `xsm link`의 `--dir`는 사람이 터미널에서 직접 칠 때만 받는다. `xsm unlink --dir`는 누구나 쓸 수 있다. 범위 밖 거부 문구는 `/xsm link <루트>`를 먼저 권한다.
- **reach.** `config.json`의 `reaches: [{"ref", "root", "t", "by", "runtime", "home", "session_id", "pid", "lstart"}]`. 허락받은 세션과, 시작 폴더가 `root`(대상 폴더의 git 루트, 없으면 그 폴더) 아래인 세션들은 공통 scope가 없어도 `reach:<ref>` scope로 양방향 통한다. 허락받은 세션은 `ref`가 아니라 **신원과 실행 세대**로 맞춘다: `runtime`·`home`·`session_id`가 같고, 그 세션의 현재 기록의 `pid`·`lstart`가 허락할 때 적어 둔 값과 같아야 한다. `ref`는 겹칠 수 있고, `claude --resume`은 같은 session id를 새 pid로 되살리기 때문이다(2026-09-28). 그래서 재개한 세션에는 reach가 다시 열리지 않고, 다시 허락받아야 한다. 세대 필드가 없는 옛 행은 누구에게도 적용되지 않으며, 같은 세션이 같은 폴더를 다시 허락받으면 새 행으로 바뀐다. MCP `xsm_reach`는 `ref`만이 아니라 호출한 세션의 기록 전체를 `add_reach`/`drop_reach`에 넘겨 허락과 거두기를 그 세션의 신원과 세대에 묶는다(2026-09-28). 넘겨받은 기록에 `session_id`나 `lstart`가 없어 `ref`로 찾아야 할 때만, 살아 있는 기록 둘이 그 `ref`를 가지면 거부한다. 그 세션의 같은 프로젝트 동료나 `root` 밖 세션에는 열리지 않는다. 넓히는 일이라 사람만 허락한다: CLI `xsm reach <폴더> --session <ref>`는 `human_terminal()`이거나 호출한 세션 자신의 입력 동의(§5.3.3)가 있을 때만, 세션에서는 MCP `xsm_reach`가 입력 동의를 쓰고, 없으면 elicitation으로 묻는다. 거두기(`--drop`, `drop: true`)는 누구나 한다. 그 세션의 `SessionEnd` 훅이 그 세션의 reach를 모두 지운다. 포인터는 `runtime`과 session id로만 이름 붙으므로, 훅이 밝힌 홈(`CLAUDE_CONFIG_DIR`/`CODEX_HOME`, 없으면 transcript 경로)이 포인터의 `home`과 다르면 그 인사는 포인터를 `ended`로 적지도, reach를 지우지도 않고 `decisions.jsonl`에 `end-ignored`로만 남는다. 복사한 홈에 같은 스레드 id가 있을 때 다른 홈의 인사가 돌고 있는 세션을 끝내고 reach를 지웠다(2026-09-28). 훅이 홈을 밝히지 않으면 기본 폴더를 추측하지 않고 예전처럼 적는다. 인사 없이 끝났거나 새 세대로 재개됐으면(그 세대가 `live`/`unknown`으로 돌고 있지 않으면) 정리(`xsm prune`, 매시간)가 지운다. 범위 밖 거부 문구 끝에 이 방법을 붙인다.
- 워커와 그 워커를 띄운 세션(워커 기록의 `ref`와 `parent_ref`)은 공통 scope가 없어도 `worker:<워커 이름>` scope로 통한다. 범위 밖 폴더의 워커는 사람이 허가했을 때만 뜨므로(5.5), 이 짝에 대한 동의는 이미 있다. 워커와 그 폴더의 다른 세션, 부모와 그 폴더의 다른 세션은 위 규칙을 그대로 따른다.
- 가입과 탈퇴는 사용자의 결정이고 xsm이 강제한다(ADR-0009). CLI `join`/`leave`는 `human_terminal()`이거나 입력 동의(§5.3.3)가 있을 때만 동작한다. 세션에서는 MCP `xsm_join`이 입력 동의를 쓰고, 없으면 elicitation(`allow`/`deny`)으로 묻고, 허용될 때만 가입한다. `join`, `leave`, `post`, `link`의 `--dir`는 사람이 터미널에서 직접 칠 때만 받는다.

### 5.3.2 세션 차단

`config.json`의 `deny`는 세션 ref 목록이다. 발신 사전 검사에서는 발신자나 대상이 목록에 있으면 거부한다. 수신 게이트에서는 발신자나 수신자가 목록에 있으면 막는다. `xsm block <ref>`는 누구나 추가할 수 있고, `xsm unblock <ref>`는 사람이 터미널에서 치거나, 에이전트가 사용자에게 물어 받은 답(판정, 두 단계, §5.3.3)이 있을 때만 된다. 막힌 쪽으로 보내려다 거부될 때의 문구는 사용자에게 물어 `xsm unblock <ref>`를 실행하라고 알려 준다.

### 5.3.3 입력한 명령이 곧 동의

사람이 자기 세션에 직접 입력한 `/xsm link <폴더>`(Codex `$xsm link <폴더>`)는 그 자체로 동의다(사용자 결정, 2026-09-28). 두 저장소를 잇는 데 양쪽의 `join`과 양쪽의 폼이 필요했고, Codex는 approval_policy "never"에서 폼을 보여 주지 않고 거절하므로 테스터가 막혔다.

- **기록, Claude.** `UserPromptExpansion` 훅만 쓴다. 이 이벤트는 사람이 슬래시 명령을 직접 입력했을 때만 오고, 피어 메시지는 평문으로 와서 명령이 실행되지 않으므로 오지 않는다(Claude Code 2.1.283과 hooks 문서, 2026-09-28). `expansion_type`이 `slash_command`, `command_name`이 `xsm` 또는 `xsm:xsm`, `command_args`가 `link`·`join`·`leave`·`reach` 중 하나와 대상 인자로 시작할 때만 `asked/<ref>.json`을 쓴다. 세션은 그 session id의 포인터로 찾는다. 이 훅은 아무것도 출력하지 않아 확장을 바꾸거나 막지 않는다. Claude의 `UserPromptSubmit`에서는 기록하지 않는다: 래퍼 없이 inbox 소켓에 쓴 피어 메시지가 사람이 입력한 "/xsm link x"와 같은 필드로 온다. 그래서 `xsm install`과 플러그인 `hooks/hooks.json`은 Claude에 `UserPromptExpansion`을 등록한다. 이 이벤트가 없는 옛 설치는 입력 동의를 적지 못해 폼으로만 묻는다. 직접 설치했으면 `xsm install --refresh`, 플러그인이면 `/plugin update xsm@xsm`으로 갱신한 뒤 새 세션을 연다.
- **기록, Codex.** `UserPromptSubmit` 훅이, 프롬프트가 피어가 아니고(`envelope.parse`의 `peer`가 거짓), `<cross-session-message`나 `[xsm v1`을 담지 않았으며, 앞 공백 다음이 `$xsm`과 위 동사 하나, 그리고 대상 인자로 시작할 때만 쓴다. 프롬프트는 바꾸지 않는다.
- 두 경우 모두 `args`는 그 줄의 나머지 원문이다. 기록이 실패해도 훅은 아무 말 없이 통과시킨다. xsm 워커(`XSM_WORKER`)에서는 기록하지 않는다. xsm이 그 창에 글자를 쳐 넣기 때문이다. 이 파일은 훅만 쓴다. 피어 메시지나 도구 호출로는 만들어지지 않는다.
- **사용.** 기록 후 10분 동안 한 번 쓴다. 쓰는 쪽(CLI `link`/`join`/`leave`/`reach`, MCP `xsm_link`/`xsm_join`/`xsm_reach`)은 동사가 같고, 대상이 같을 때만 쓴다. `link`와 `reach`의 대상은 세션 폴더 기준으로 푼 정규 프로젝트 루트로 비교하고, `join`/`leave`는 이름 그대로 비교한다. `link`는 연결하는 이쪽 폴더도 입력한 세션의 프로젝트여야 한다. `session_id`가 기록과 다르면(24비트 ref 충돌) 쓰지 않는다. 맞지 않는 요청은 동의를 소모하지 않는다.
- **남는 한계.** Codex에서는 `codex queue` 항목(xsm이 Codex에 전달하는 길)도 사람의 입력과 같은 원문으로 `UserPromptSubmit`에 오므로, 봉투·헤더 없이 `$xsm link …`로 시작하는 항목을 넣을 수 있는 같은 OS 사용자의 프로세스는 동의를 위조할 수 있다. 이것은 xsm의 기존 신뢰 경계 안이다: 경계는 uid이고 동의 기록은 보안 장치가 아니다(ADR-0009). 두 런타임 모두, 세션의 입력창에 글자를 칠 수 있는 것(그 창에 대한 `tmux send-keys` 등)은 그 세션의 폼에 답할 수 있듯 동의도 만들 수 있다.

- **AskUserQuestion의 답(2026-10-01).** 에이전트가 Claude Code의 AskUserQuestion으로 물으면 사람이 고른 답은 `UserPromptSubmit`이 아니라 도구 결과로 돌아온다. xsm이 그것을 보지 못해 에이전트가 같은 질문을 채팅에 입력해 달라고 되물었다(실측). 그래서 Claude에는 `PostToolUse` 훅을 `matcher: "AskUserQuestion"`으로 등록한다. 플러그인 `hooks/hooks.json`과 `xsm install`이 쓰는 `settings.json` 모두에 넣고, 다른 도구의 결과는 훅까지 오지 않는다. 훅 입력의 `tool_response`는 도구 결과다: `{"questions": [...], "answers": {<질문 원문>: <고른 선택지 라벨, 또는 Other에 쓴 말>}, "annotations": {<질문>: {"notes"?, "preview"?}}}`. 공식 hooks 문서는 PostToolUse의 `tool_response`가 도구마다 다르다고만 하고 이 도구의 필드를 적지 않으므로, 실제 transcript의 도구 결과(`toolUseResult`)에서 읽었다. 이 도구는 다지선다에서 여러 개를 고르면 라벨을 쉼표로 이어 한 문자열로 준다. 그 세션에 신선한 요청이 있을 때만 `answers`를 질문마다 `"<질문>" -> "<답>"`으로 이어(질문은 200자까지, 사람이 적은 노트가 있으면 ` (notes: …)`를 붙이고, `preview`는 버린다) 판정으로 남기며, 이어서는 타자한 답과 똑같다: 가장 최근이 이기고 두 단계를 거친다. 요청이 없거나 만료됐으면 아무것도 쓰지 않는다. `tool_input.answers`는 훅이 채우는 값이라 쓰지 않는다. `tool_response`가 문자열이거나 텍스트 블록 배열이면 그 텍스트를 그대로 남긴다. 이 훅은 아무것도 출력하지 않고, 내부 오류가 나도 프롬프트 결정을 출력하지 않는다. 이 이벤트나 matcher가 없는 옛 플러그인은 `xsm doctor`와 `xsm install --refresh`가 `PostToolUse(AskUserQuestion)`이 없다고 알리고, 직접 설치는 doctor의 install 줄에 `PostToolUse:add`가 나온다. 거부 문구는 Claude 에이전트에게만 질문 도구로 물어도 된다고 알린다(Codex의 질문 도구 답은 xsm이 듣지 않는다).
- **물어보고 실행(판정, 2026-10-01).** 동의 없이 에이전트가 사람이 정할 일(CLI `link`/`join`/`leave`/`reach`, 그리고 `unblock`, `approve`, `attempts clear`, `frameworks ignore`, `--grant` 없는 `spawn`/`remote add`, `post --tag decision`, `doc add --tag endorsed`)을 실행하면, 요청을 `asked/<ref>.pending.json`에 적고 "사용자에게 쉬운 말로 물어보라. 답은 판정으로 남는다. 같은 명령을 다시 실행하면 그 답을 보여 준다"로 거부한다(종료 코드 2). 사람에게 명령 입력을 요구하지 않는다(사용자 결정: 묻는 것도 실행하는 것도 에이전트의 일이다). 요청을 남길 수 없으면(등록된 세션이 아님, `XSM_WORKER`, 상태 폴더에 쓸 수 없음) 답을 보관하겠다고 약속하지 않고 그렇다고 말한다. MCP 폼 도구가 있으면 그것을 쓰라고 하고, 없으면 여기서는 정할 수 없다고 사용자에게 알리라고 한다.
  - **판정이 되는 입력.** 그 세션의 `UserPromptSubmit`이 사람이 입력한 메시지를 판정으로 붙인다. 제외하는 것: 피어 메시지와 봉투를 담은 입력, `/xsm`·`$xsm` 명령, 그 밖의 슬래시 명령(`/clear`는 답이 아니다), 하니스가 넣은 텍스트. 하니스 텍스트는 `<task-notification`, `<system-reminder`, `<command-…`, `<local-command…`, `<bash-…`, `<user-prompt-submit-hook`, `<cross-session-message`로 시작하는 입력과, 소문자-하이픈 태그로 시작하고 그 닫는 태그를 담은 입력이다(백그라운드 작업 완료 알림이 판정으로 저장된 사례, 2026-10-01). 경로(`/tmp/x`)는 슬래시 명령이 아니다.
  - **가장 최근 답이 이긴다.** 판정은 요청 뒤 사람이 입력한 마지막 메시지다. 되묻는 말("이거 지우면 다른 기록도 지워져?")이 첫 메시지이고 "응"이 그다음일 수 있으므로 첫 메시지를 고정하지 않는다. 새 메시지는 `verdict_t`를 갱신하고 이미 보여 준 표시를 지운다. 신선도는 답에서 센다: 답이 없는 요청은 만든 지 10분 뒤에, 답이 있으면 가장 최근 답 10분 뒤에 만료된다. 다만 요청을 만든 때부터 30분(`ASK_MAX`)이 지나면 답이 아무리 최근이어도 사라진다. 사람이 계속 말을 거는 동안 요청이 몇 시간씩 살아 있다가, 상관없는 뒤 메시지가 판정으로 보이는 일을 막는다(2026-10-01).
  - **두 단계.** 답이 있은 뒤 첫 재실행은 진행하지 않는다. 종료 코드 2로 거부하며 "your user replied: "<원문>". If that is a yes to <무엇>, run this same command again to go ahead; if it is a no or a question, do not: answer them, and their next message replaces it."를 출력하고 판정을 `shown`으로 표시한다. 그다음 재실행이 진행하고 요청을 지운다(한 번만 쓴다). 그다음 재실행은 보여 준 지 1초(`SHOW_DELAY`, `shown_t`) 뒤여야 한다. `xsm unblock X || xsm unblock X`처럼 한 줄에서 두 번 돌려도 에이전트가 읽기 전에 진행하지 못하고, 이른 재실행은 답을 다시 보여 줄 뿐이다. 보여 준 뒤 새 답이 오면 다시 보여 주어야 진행한다. 판정 파일을 읽고 쓰는 곳(훅의 `note_verdict`, CLI의 `take_or_request`·`take_verdict`)은 옆의 `.lock` 파일에 `flock`을 걸어 한 번에 하나만 돈다. 잠금이 없을 때는 보여 주는 쓰기 직전에 온 새 답이 사라지고 옛 "응"이 통과하는 경주가 있었다(400회 중 323회 측정, 2026-10-01). 판정 가져오기와 요청 기록은 한 번의 잠금 안에서 한다(`take_or_request`): 따로 하면 그 사이에 온 답이 보이지 않은 채 요청에 덮어써졌다. 잠금은 `LOCK_NB`로 `LOCK_WAIT`(5초)까지 기다리고, 그 뒤에는 쓸 수 없는 폴더에서처럼 잠금 없이 진행한다. 판정을 쓰거나 요청이 만료되면 판정 파일과 `.lock`을 지운다. 잠금을 쥔 채로 지우고, 그 잠금을 기다리던 쪽은 잠금을 얻은 뒤 fd가 경로의 파일과 같은 inode인지 보고, 아니면 새 파일에서 다시 잠근다. 아무도 돌아오지 않은 파일(창을 넘긴 판정 파일, 판정 파일 없는 `.lock`, 10분이 지난 입력 동의)은 정리(`xsm prune`, 매시간)가 쓸어내며, 쓰는 중인 것은 잠금을 기다리지 않고 건너뛴다. 판정 파일은 사람의 말을 1000자까지 담으므로 만료된 채 남아 있지 않게 하려는 것이다. 상태 폴더에 쓸 수 없으면(보여 주는 쓰기와 쓰는 단계의 삭제 포함) 트레이스백 없이 답을 보관할 수 없다고 거부한다(`cannot_keep`). xsm이 문장을 해석하지 않으므로 질문이나 "아니"가 곧 예가 되지 않게 하려는 장치다.
  - **기록.** 진행할 때 그 원문을 출력과 `decisions.jsonl`(`event: consent`)에, link·reach 기록의 `by`에 남긴다. xsm은 문장을 해석하지 않는다. 예/아니오는 에이전트가 읽고 정하고, 근거는 사람의 원문으로 남는다. MCP 도구가 폼을 띄울 수 없거나 답을 받지 못하면 같은 CLI 경로를 안내한다. 신뢰 경계는 입력 동의와 같다.

### 5.4 발신 사전 검사

수신 측과 같은 검사를 먼저 한다. 순서대로 하나라도 걸리면 보내지 않는다.

1. 발신 세션이 등록돼 있다.
2. 대상이 해석된다(모호하면 거부).
3. 대상이 등록돼 있고 살아 있다.
4. 자기 자신이 아니다.
5. 공통 scope가 있다.
6. 네이티브 예보가 `refuse`가 아니다.

통과하면 원장에 `queued`를 쓰고 전송한다. **전송 성공은 전달이 아니다.** `delivered`는 수신 측 영수증으로만 정해진다.

### 5.5 워커

`xsm spawn`이 띄운 세션이다. 등록된 뒤에는 다른 세션과 똑같이 주소가 붙고 검문을 받는다. 기록은 `workers/<이름>.json`과 `workers/<이름>/`(워커 전용 `settings.json`)이다.

- **실행 위치.** Orca·herdr 패널(`ORCA_TERMINAL_HANDLE`·`ORCA_PANE_KEY`, `HERDR_PANE_ID`·`HERDR_ENV`) 안이면 `spawn`과 `stop`은 거부한다. 단, 사람이 `xsm frameworks ignore <이름>`으로 켠 프레임워크(`config.json`의 `ignore_frameworks`)는 예외다. 살아 있는 tmux 패널(`$TMUX_PANE`을 `tmux display`로 확인) 안이면 그 옆 분할 패널에서 TUI로 띄운다. 그 밖에는 백그라운드 tmux 세션에서 TUI로 띄운다. tmux가 없으면 거부한다.
- **백그라운드 워커가 묻지 않고 하는 일(한 선언).** 규칙이 흩어져 있어 하루에 세 번 넓혀졌으므로(그때마다 워커가 10분씩 사람을 기다렸다) `workers.WORKER_POLICY` 한 곳에서 선언하고 두 런타임을 거기서 구성한다. `xsm workers --policy`가 그대로 출력한다.

  | 항목 | 규칙 |
  |---|---|
  | shell | 모든 셸 명령. 모두 OS 샌드박스 안에서 돌기 때문이다 |
  | read | 어디서든 읽기(`Read`·`Glob`·`Grep`; Codex의 `workspace-write`도 읽기는 전부 허용한다) |
  | write | 작업 폴더 안, 그리고 xsm 저장소만 |
  | reach | 피어에게 보내기: 샌드박스 밖에서 도는 xsm MCP 도구 |
  | ask | 그 밖의 모든 것은 사람에게. Claude는 `PermissionRequest` 훅으로, Codex는 훅이 없으므로 거부로 |

  Claude 워커 허용 도구는 `workers.CLAUDE_WORKER_TOOLS`(`Bash`, `Monitor`, `Read`, `Glob`, `Grep`)와 `workers.CLAUDE_WORKER_MCP`(`xsm_send`, `xsm_post`, `xsm_channel`, `xsm_inbox`)다. Codex 워커는 `-s workspace-write -a never`에 xsm 저장소 쓰기와 MCP 승인이 붙는다.
- **환경.** 워커에는 `XSM_WORKER=<이름>`이 붙고, 부모의 `CLAUDE_CODE_SESSION_ID`·`CLAUDE_CODE_MESSAGING_SOCKET`과 프레임워크 패널 변수는 지운다. Claude 워커는 패널이든 백그라운드든 `--permission-mode default`로 뜨고, `--settings`로 `crossSessionInbound: accept`를 받고, 보고용으로 `xsm send`만 미리 허용된다(`--allowedTools Bash(<xsm> send:*)`). `spawn`·`stop`·`install` 같은 다른 xsm 명령은 여전히 승인을 거친다.
- **백그라운드.** tmux 밖에서 부르거나 `--background`면 분리된 tmux 세션 `xsm-workers`에 창을 열고(없으면 `new-session -d`, 있으면 `new-window -d`) 그 안에서 실제 TUI를 띄운다. 전달은 다른 세션과 같다(Claude 인박스 소켓, `codex queue`). 워커는 `claude -p`나 `codex exec`로 띄우지 않는다(2026-09-22 결정, ADR-0010 부록).
- **TUI Codex.** `codex -s workspace-write -a on-request`로 띄운다. 사용자 설정이 전체 접근이어도 패널에서 승인을 묻게 하기 위해서다. 패널에서 띄운 뒤 `/rename <이름>`을 입력해 스레드를 만들고, 그 이름과 시작 시각 이후 생성으로 스레드를 찾아 등록한다. 같은 폴더의 가장 최근 스레드로 추정하지 않는다(그렇게 했다가 이전 워커의 스레드로 잘못 등록된 것을 실측했다).
- **승인.** 백그라운드 Claude 워커는 자기 `--settings`의 `PermissionRequest` 훅으로 대기 절차를 탄다. 백그라운드 Codex 워커는 `-a never`라 묻지 않는다. Codex 홈에는 `PermissionRequest` 훅을 설치하지 않으며, 이전 버전이 넣은 xsm 그룹은 다음 설치 때 지운다. 훅은 `XSM_WORKER`가 없으면 출력하지 않는다. 워커면 `approvals/<id>.json`(`status: pending`)을 쓰고, 부모에게 `note`로 알리고, `approved`/`denied`가 될 때까지 기다린다. 한도(`approval_timeout`, 기본 600초)를 넘기면 `denied`다. 출력은 `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}` 또는 `behavior: deny`와 `message`. 훅 내부 오류 때는 아무것도 출력하지 않아 런타임 기본값을 따른다. `approve`는 에이전트 표식(`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CODEX_THREAD_ID`, `CODEX_SANDBOX*`)이 없고, 표준 입력이 TTY이며, `/dev/tty`를 열 수 있을 때만 동작한다. 그리고 `/dev/tty`에서 `yes`를 받아야 한다. 터미널이 아닌 곳(에이전트)에서는 사용자에게 물어 받은 답이 있을 때만 승인한다. 처음 실행하면 무엇을 물을지(워커, 요청 id, 요약)를 알려 주고 요청을 남긴다. 사용자의 가장 최근 메시지가 판정이 되고, 다시 실행하면 그 답을 보여 주며(진행하지 않는다), 그다음 실행이 승인하면서 그 원문을 `answered_by`에 남긴다(두 단계, §5.3.3; 이슈 #9, 2026-10-01). 이 경로는 그 워커를 시작한 세션(`parent_ref`)만 쓴다. MCP `xsm_approve`와 같다. 사람이 터미널에서 칠 때는 어느 요청이든 승인한다. `deny`는 조건이 없다. **이것은 보안 경계가 아니다.** 같은 사용자로 도는 에이전트는 pty를 할당하거나(`script`), 환경을 지우거나, 승인 파일을 직접 쓸 수 있다. 이 검사가 막는 것은 에이전트가 습관적으로 또는 피어의 지시를 따라 승인하는 경우까지다. 패널 워커의 승인 창은 패널에서 사람이 답하므로 이 훅이 관여하지 않는다. 워커가 사라진 대기 요청은 목록을 볼 때 `denied`로 닫힌다. Codex 워커의 대기 한도는 훅 한도(660초)보다 30초 짧게 잘린다.
- **화면 읽기.** `xsm workers read <이름> [--lines N]`이 워커 pane의 tmux 화면을 글자로 낸다. 기본 80줄, 상한 2000줄이고 tmux가 덧대는 꼬리 빈 줄은 잘라낸다. 읽기 전용이므로 프레임워크 거부(Orca·herdr)를 받지 않는다 — ADR-0003이 막는 것은 워커를 띄우고 끝내는 일이다. 워커나 pane 기록이 없으면 거부(2), pane이 이미 죽어 캡처가 비면 `(no screen: the tmux pane is gone)`과 함께 성공(0)이다. 멈춘 워커의 마지막 화면을 보는 것이 이 명령의 주된 쓰임이기 때문이다.
- **깊이.** 워커 기록에 `depth`(최상위 세션이 띄운 워커가 1)와 `max_depth`(그 워커 아래에 적용되는 한도)가 있다. 부른 쪽이 워커인지는 `XSM_WORKER`, 없으면 부른 세션의 session_id로 워커 기록을 찾아 정한다. 최상위 세션의 한도는 `--max-depth`, 없으면 `XSM_MAX_DEPTH`, 없으면 config `max_depth`(기본 1)이다. 워커의 한도는 자기 기록의 `max_depth`이고, `--max-depth`는 그보다 작거나 같을 때만 받는다. 새 워커의 깊이가 한도를 넘으면 `spawn`을 거부한다.
- **놀지 않는 워커.** `PermissionRequest` 대기가 시작되면 `_notify_parent`가 부모에게 `kind=task`를 보낸다. 내용은 `xsm_approve`를 id와 함께 부르라는 지시다. 거부되거나 시간이 지나면 결과를 `note`로 보낸다. MCP `xsm_approve`는 부모 세션(`parent_ref`)만 부를 수 있다. 이 도구는 대기 요청을 elicitation(`allow`/`deny`)으로 보이고, 답을 `approvals/<id>.json`에 `via: mcp-elicitation`으로 쓴다. 이 경로에서는 TTY 검사를 하지 않는다. 양식 자체가 사람이 답했다는 확인이기 때문이다. 워커가 받는 `task`의 수신 문맥에는 두 가지가 붙는다. 하나는 권한 핑계로 끝내지 말고 필요한 권한을 적어 보고하며 한 것만 말하라는 규칙이고, 다른 하나는 작업 폴더다.
- **위험한 옵션.** `full_access`와 `trust_hooks`는 부른 쪽이 사람(`human_terminal()`)이 아니면 허가를 요구한다. 허가는 `grants/<id>.json`에 저장되며 `{asked_by, runtime, cwd, options, expires}`로 묶인다. `spawn`은 파일을 `.used`로 rename해서 한 번만 쓰고, 세션·런타임·폴더가 다르거나, 옵션을 다 덮지 못하거나, 만료됐으면 거부한다. 허가는 MCP `xsm_grant`(elicitation으로 `deny`/`allow once`를 묻고 결과를 채널에 `decision`으로 남긴다)나, `--grant` 없이 실행한 `spawn`/`remote add`의 판정 경로가 만든다. 후자는 무엇을 물을지(`workers.describe_grant`) 알려 주고 요청을 남긴다. 사용자의 가장 최근 메시지가 판정이 되고, 같은 명령을 다시 실행하면 그 답을 보여 주며(진행하지 않는다), 그다음 실행이 그 판정을 사유(`answer: "verdict: …"`)로 한 일회용 허가를 만들어 곧바로 쓴다(두 단계, §5.3.3; 이슈 #9, 2026-10-01). `trust_hooks`는 Codex 워커에만 된다.
- **동시 수.** 새 워커를 띄우기 전에 부른 세션(`parent_ref`)의 워커 가운데 `gone`이 아닌 것을 센다. `XSM_MAX_WORKERS`, 없으면 config `max_workers`(기본 4) 이상이면 거부한다. 세기 전에 남은 워커를 먼저 정리한다.
- **남은 워커.** 워커의 `parent_ref`에 해당하는 레코드가 없거나 `ended`/`stale`이면 그 워커는 남은 것이다. 만든 지 120초가 지났는데 자기 프로세스가 없는 워커도 같다. 이런 워커는 `stop`으로 정리한다. 확인 시점은 셋이다. (1) 부모의 SessionEnd 훅: 부모 프로세스가 아직 살아 있으므로, 분리된 `xsm reap --after-pid <부모 pid>`가 그 프로세스가 끝나길(최대 120초) 기다렸다가 정리한다. (2) `workers`와 `spawn`. (3) 매시간 정리. 훅 경로에서는 늘 분리된 프로세스로 한다.
- **종료.** `stop`은 pid와 시작 시각이 기록과 같을 때만 신호를 보낸다(프로세스 그룹에 SIGTERM, 5초 뒤 SIGKILL). 패널 워커는 `tmux kill-pane`. 대기 중인 승인은 `denied`로 닫고, 워커 기록과 폴더를 지운다. 세션 포인터는 지우지 않고 `ended`(`end_reason: worker-stopped`)로 적는다. 포인터가 없는 Codex 스레드는 아무도 등록하지 않은 스레드로 보여, 같은 폴더에 열린 다른 TUI에 adopt되어 그 pid로 `live`가 됐다(issue #5). 멈춘 포인터는 다른 멈춘 포인터처럼 보관 기간(§4.4)이 지나면 정리가 지운다. 원장은 보존 기간 규칙을 따른다. `once`는 `--task`가 있어야 쓸 수 있다. 과제 id는 보내기 전에 워커 기록에 저장한다. 그 과제(`task_id`)에 대한 `reply`가 부모(`parent_ref`)의 훅을 통과하면, 훅이 분리된 프로세스로 `xsm stop --internal`을 띄운다. 훅 안에서 종료를 기다리지 않기 위해서다. `task_id`가 없으면 어떤 답장으로도 멈추지 않는다.

- **재시도 계보와 연속 실패 차단.** `--task`가 있는 `spawn`은 그 과제의 계보(`attempts/<key>.json`)를 먼저 본다. 마지막 성공 이후 실패가 `max_task_attempts`(설정, `XSM_MAX_TASK_ATTEMPTS`, 기본 3)에 이르면 거부한다. 거부 메시지에는 안정적인 토큰 `task-attempts-exhausted`와 시도 이력이 들어가고, **새 종료 코드를 만들지 않는다**(거부는 2다). 검사는 폴더가 정해진 직후, 워커 디렉터리·tmux 창·프로세스가 생기기 전에 한다.
  - 계보의 시작은 `spawn` 호출이 아니라 **과제가 실제로 나가는 순간**이다. 뜨지도 못한 워커는 시도가 아니다.
  - 끝맺음: 답장이 오면 그 `outcome`으로, 답장 없이 `stop`되면 `failed`("stopped without answering")로 닫는다. `finish`는 멱등이라 먼저 닫은 쪽이 이긴다 — 답장 뒤에 따라오는 `stop`이 답장을 덮어쓰지 못한다.
  - 세는 규칙: `failed` +1, `succeeded`면 0으로, **`outcome`이 없으면 세지도 리셋하지도 않는다.** 본문을 읽어 성패를 추측하지 않기 때문이다.
  - `--retry-of <과제 id>`를 주면 그 과제의 계보에 붙는다. 문장을 고쳐 쓴 재시도도 한 계보다. 계보만 잇고 모델·폴더 같은 배치는 상속하지 않는다.
  - 해제는 `xsm attempts clear <key>`이고 **사람이** 정한다. 사람이 터미널에서 치거나, 에이전트가 무엇이 실패했는지 말하고 물어 받은 답(판정, 두 단계 §5.3.3)으로 한다. MCP 도구도, `--force-retry` 같은 우회 플래그도 없다. 원래는 "예스가 나올 때까지 다시 묻는 경로"를 막으려고 터미널에만 열었다. 2026-10-01 사용자 결정(사람에게 명령을 치게 하지 않는다)으로 판정 경로를 열었다. 다시 물을 수는 있지만 매번 사람이 실제로 답해야 하고, 그 답은 원문으로 남는다. `xsm attempts`(목록)와 `xsm attempts show <key>`는 누구나 본다.
  - 보존은 `attempt_retention_days`(기본 7일). 한도는 몰아치기를 막으려는 것이지 실패를 영구 기록하려는 것이 아니다.
  - **새 타이머도 상주 주체도 없다.** 계보는 이미 도는 두 경로(`spawn`, 답장 또는 `stop`)에서만 움직인다. **`outcome`은 이 판정을 바꾸지 않는다.** `failed`라고 답해도 답한 것이므로 `once` 워커는 멈춘다. 같은 일을 다시 맡길지는 사람의 판단이다. 과제의 성패는 워커 기록이 아니라 부모가 이미 id를 쥐고 있는 원장 항목(`ledger/<과제 id>.json`의 `outcome`)에 남는다 — 워커 기록은 몇 초 뒤 지워진다.

### 5.6 채널과 MCP 서버

채널 레코드는 `channels/<key>/<YYYY-MM>.jsonl`에 한 줄씩 저장된다. 한 레코드는 `O_APPEND`로 연 파일에 `write()` 한 번으로 쓴다. 16개 프로세스가 동시에 60KB까지 써도 섞이거나 사라지지 않았다(실측). 그래서 잠금은 쓰지 않는다. 채널 파일은 정리 대상이 아니다.

- **key.** 기본 프로젝트면 `<범위 id의 안전한 형태>-<루트 경로 sha256 앞 6자>`다. 이름이 같은 두 저장소가 채널을 공유하지 않게 하기 위해서다. 이름 붙인 프로젝트면 `project-<이름>`이다.
- **레코드.** `{"id", "t", "channel", "author", "tag", "text", "reply_to", "root", "approved"?}`. `author`는 `{"kind": "human", "name", "via"?, "asked_by"?}` 또는 `{"kind": "agent", "name", "alias", "ref", "runtime"}`이다. `approved`는 `{"question", "answer", "options"}`이다. 본문은 최대 32,000자다.
- **작성자 판정.** `workers.human_terminal()`(에이전트 표식 없음, 표준 입력이 TTY, `/dev/tty` 열림)이 참이면 사람이다. 아니면 등록된 세션이다. 둘 다 아니면 거부한다.
- **`decision`.** 사람만 쓸 수 있다. 에이전트는 MCP `xsm_decide`(양식)로 기록하거나, `xsm post --tag decision "…"`을 실행한다. 후자는 사용자에게 물으라고 답하고, 사용자의 가장 최근 메시지를 판정으로 받아 다시 실행하면 보여 주고, 그다음 실행이 작성자 `{"kind": "human", "via": "verdict", "verdict": <원문>, "asked_by": <ref>}`로 남긴다. 문서의 `endorsed`도 같다(노드의 `approved: "verdict: …"`).
- **MCP 서버**(`hooks/xsm-mcp.py`, 줄 단위 JSON-RPC over stdio). 초기화 때 클라이언트의 `capabilities.elicitation`을 기록한다. 서버가 섬기는 세션은 가장 가까운 `claude`/`codex` 조상 프로세스의 살아 있는 레지스트리 기록이다.
  - **호출자 판정.** Claude는 세션마다 MCP 서버가 하나이므로 조상 pid의 살아 있는 기록 중 가장 최근 것이다. Codex는 다르다. 한 `CODEX_HOME`의 스레드는 모두 데몬 아래에서 돌아 pid가 같고, MCP 서버는 스레드마다 하나씩 뜨지만 전부 데몬의 자식이다(실측 2026-09-29, 0.158, TUI 둘). 그래서 pid와 "가장 최근 기록"으로는 다른 스레드의 이름으로 서명할 수 있고, 실제로 첫 TUI의 `xsm_channel`이 둘째 스레드의 폴더로 풀렸다. Codex 0.158은 모든 `tools/call`의 `params._meta.threadId`에 호출한 스레드를 싣는다(`sessionId`와 `x-codex-turn-metadata.thread_id`도 같은 값이고, `initialize`의 `clientInfo`, 서버 환경변수에는 스레드 정보가 없다). 서버는 호출마다 이 값을 읽어, 조상 pid가 같고 `session_id`가 그 값인 살아 있는 Codex 기록을 그 호출의 발신자로 쓴다. 그런 기록이 없으면(하위 에이전트의 스레드, 훅이 아직 등록하지 않은 스레드) 거부하지 않고 **그 자리에서 연결한다**(사용자 결정 2026-09-30: 쉬운 연결이 우선이고 우발적 연결 실패는 막아야 한다). 후보 홈(서버의 `CODEX_HOME` 환경변수, 같은 pid 기록들의 홈, 선언된 Codex 홈, `~/.codex` 순) 중 상태 DB(`state_5.sqlite`, 읽기 전용)의 `threads`에 그 id가 있는 첫 홈이 그 스레드의 홈이다. 그 홈에 xsm이 설치돼 있고 두 훅(SessionStart, UserPromptSubmit)이 신뢰돼 있으면(`adopt_open_codex`와 같은 동의) `adopted: true` 기록을 만들어 쓴다. cwd와 이름은 그 DB에서, pid는 데몬의 것(`app_server`)이다. 하위 에이전트 스레드(`source`가 `{"subagent":{"thread_spawn":{"parent_thread_id":…}}}`)는 수명이 분 단위이고 한 홈에 수백~천 개씩 쌓이므로(실측: 간선 1693개) 등록하지 않고, 같은 pid에 살아 있는 부모 기록이 있으면 부모로 서명한다. 부모를 알 수 없거나 등록돼 있지 않으면 그 스레드를 자기 이름으로 등록한다. 홈이 신뢰되지 않았거나 어느 상태 DB에도 없으면 같은 pid의 **가장 최근에 갱신된 살아 있는 기록**으로 서명한다. 스레드 id가 없는데 같은 pid에 살아 있는 Codex 기록이 둘 이상이면(스레드를 실어 보내지 않는 옛 Codex)도 같은 규칙으로 가장 최근 기록이다(0.4.7 이전과 같고, 거부하지 않는다). 식별할 기록이 전혀 없을 때만 거부한다. 스레드 id는 Codex 기록이 있는 pid에서만 쓰며 Claude 경로는 그대로다.
  - **도구.** `xsm_post`(`decision` 태그는 받지 않음), `xsm_channel`, `xsm_decide`.
  - **`xsm_decide`의 흐름.** 서버가 `elicitation/create`를 보낸다. 요청 내용은 `message`와 `requestedSchema.properties.answer`(선택지가 있으면 `enum`)다. `action: accept`와 빈 값이 아닌 답이 돌아와야만 레코드를 쓴다. 이때 작성자는 `{"kind": "human", "via": "mcp-elicitation", "asked_by": <세션 ref>}`다. 거절, 취소, elicitation 미지원이면 아무것도 쓰지 않는다. MCP 사양에서 `decline`은 사람의 명시적 거절이고 `cancel`은 고르지 않고 닫은 것이다(2025-06-18). 폼을 보여 주는 것으로 알려진 클라이언트(Codex가 아니고 `clientInfo.name`을 밝힌 쪽)의 `decline`은 사용자의 아니오로 읽어 결과를 "your user declined … do not ask again"으로 돌려주고 에이전트가 다시 묻지 않게 한다(2026-10-01). `xsm_approve`는 이때 요청을 `deny`로 답한다. `cancel`은 "did not answer"(닫힘)이라 에이전트가 말로 다시 물을 수 있다. Codex는 approval_policy "never"에서 폼을 보여 주지 않고 `decline`을 보내므로 사람의 거절과 구별되지 않고, 이름 없는 클라이언트도 그렇게 읽어 둘 다 "did not answer"로 남긴다.
- **등록.** 설치기가 `claude mcp add --scope user xsm -- <고정 인터프리터> <repo>/hooks/xsm-mcp.py`(Codex는 `codex mcp add xsm -- …`)를 실행한다. `mcp get`으로 확인해, 명령이 같으면 건너뛰고 다르면 지우고 다시 등록한다. `uninstall`은 `mcp remove`를 실행한다.

### 5.7 공동 문서(노드)

문서 `<path>`의 노드는 `<path>.nodes/<id>.md`다. 머리는 `---`로 감싼 `key: value` 줄이다(`id`, `t`, `author`, `author_kind`, `tags`, `parents`, 선택적으로 `approved`). 본문은 그 아래에 온다. `id`는 `sha256("t|author|tags|parents|body")`의 앞 12자다. 노드는 `O_CREAT|O_EXCL`로 만들므로 이미 있는 노드를 덮어쓰지 않는다. 부모는 이미 있는 노드여야 한다. `endorsed`는 `author_kind: human`일 때만 쓸 수 있다. 사람 판정은 채널과 같고, MCP `xsm_doc_endorse`의 elicitation도 사람으로 인정한다. `render`의 기준 노드는 가장 최근의 `endorsed`, 없으면 가장 최근의 `report`다. 여기에 끝 노드(자식이 없는 노드)와, `verification` 자식이 없는 `hypothesis`를 덧붙인다.

`xsm doc next <문서>`는 그 두 집합의 **합집합**을 한 목록으로 낸다. 이미 있는 두 뷰를 합칠 뿐 새 판단을 들이지 않기 위해서다. 규칙:

- **순위를 매기지 않는다.** 순서는 `read()`가 준 순서 그대로다(시각, 같으면 id). 무엇을 순위 신호로 삼느냐가 ADR-0012가 아직 답하지 않은 질문이므로, 여기서 한 신호를 고르면 그 선택지가 사고로 채택된다. `--json`은 `"order": "created"`와 `"assigns": false`를 실어 이 중립을 계약으로 드러낸다. `score` 키는 없다.
- **배정하지 않는다.** 누가 무엇을 맡을지 정하지 않고, 목록을 보는 것이 맡는 것이 아니다.
- `wip` 태그가 붙은 노드는 후보에서 뺀다(선언은 기여가 아니다). 그 부모에는 "누가 하고 있다고 말했다" 한 줄이 붙지만 **후보 집합에서 빼지도, 순서를 바꾸지도 않는다.**
- 기존 뷰(`leaves`, `log`, `render`)는 한 줄도 바뀌지 않는다. `wip` 노드는 거기서 여전히 보통 노드다(자식이 있으므로 부모는 `leaves`에서 빠진다). 이것이 `wip`을 채택한 것이 아님을 보이는 증거다.
- MCP에 노출하지 않는다. 읽기만 하므로 샌드박스에서도 `bin/xsm doc next`로 된다.

### 5.8 원격(두 방향 SSH)

- **짝.** `config.json`의 `remotes`에 `{peer, host, local_project, remote_project}`를 둔다. `peer`는 상대가 스스로 알린 호스트 이름(`XSM_HOSTNAME`, 기본 `hostname`의 첫 부분)이다. `host`는 SSH로 닿는 이름이다.
- **키.** 각 기계는 `remote/id_ed25519`를 가진다. 상대의 공개키는 `authorized_keys`에 `command="<고정 인터프리터> <repo>/hooks/xsm-remote.py <peer>",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding <키> xsm-remote:<peer>`로 들어간다. `remote remove`는 `xsm-remote:<peer>`로 끝나는 줄만 지운다.
- **요청.** 표준 입력의 JSON 한 줄이다. `op`는 `ping`, `ping-back`, `sessions`, `send`, `status`, `unpair` 중 하나다. `send`는 `{id, target, body, kind, reply_to, wait, project, sender}`를 싣는다. 받는 쪽은 다섯 가지를 확인한 뒤 로컬 경로로 전달한다. `project == 짝의 remote_project`인지, 대상이 해석되는지, 대상이 `ended`/`stale`이 아닌지(로컬 보내기의 6번과 같은 판정, 거부 문구 "… is not running (<state>)", 2026-09-28 추가: 전에는 멈춘 세션의 대기열에 넣고 상대에게 `queued`라 답했다), 대상이 짝의 `local_project`에 속하는지, 차단되지 않았는지다. 봉투는 `origin=<peer>`, `scope="remote:<peer>"`, 회신 주소 없음으로 다시 만든다. 이때 `remote/inbound-<id>.json`에 `{id, peer}`를 남기고, `wait`가 있으면 영수증을 기다려 상태를 돌려준다. 이 기계 원장에 이미 있는데 그 peer의 inbound로 기록되지 않은 id는 `refused`("message id … is already in use here")다. 같은 peer가 같은 id를 다시 보냈고 영수증이 이미 있으면 다시 전달하지 않고 그 판정을 `duplicate: true`와 함께 돌려준다. 전달 경로가 실패하면 원장에 `error`를 쓴다(2026-09-29, issue #4).
- **발신 쪽 실패 분류(2026-09-29, issue #4).** 답이 오지 않은 호출은 요청이 상대에 닿았는지로 나눈다. 짝이 없음, ssh 실행 불가, ssh가 종료 코드 255로 표준 출력 없이 로그인 전 오류(`Could not resolve hostname`, `connect to host`, `Connection refused`, `No route to host`, `Network is unreachable`, `Permission denied (`, `Host key verification failed`, `kex_exchange_identification`, `during banner exchange`)를 낸 경우는 강제 명령이 돌지 않았으므로 원장에 `error`다. 시간 초과, 로그인 뒤 끊긴 연결(255라도 위 문구가 없으면), 읽을 수 없는 응답은 상대가 이미 큐에 넣었을 수 있으므로 원장에 `unknown`이다. 상대가 답했지만 받지 않았으면(`ok: false`) `refused` 또는 `error`로 적는다.
- **상태 조회.** `status`는 `{id, wait?}`를 싣는다. 받는 쪽은 그 id가 **그 peer의 inbound로 기록됐고** 원장 scope가 `remote:<peer>`일 때만 `{ok: true, status, reason, target}`을 답한다. 그 밖의 id(다른 peer의 메시지, 이 기계의 로컬 메시지, 경로 문자가 든 id)는 모두 `{ok: true, status: null}`이다. 본문과 미리보기는 싣지 않는다. `wait`는 30초까지 영수증을 기다린다. 발신 쪽 `xsm status <id>`는 scope가 `remote:`이고 상태가 `queued`/`unknown`이면 이 op로 묻고 결과를 원장에 쓴다: `delivered`/`held`/`blocked`는 영수증, 상대의 `error`는 `error`, 상대의 `queued`는 `unknown`을 `queued`로 되돌림, `null`은 로컬이 `unknown`이고 원장의 `settle_after`(끊긴 호출의 제한 시간 + 60초)가 지났을 때만 `error`다. 문구는 "상대에 기록이 없다, 호출의 제한 시간이 지난 뒤이므로 도착하지 않았을 가능성이 크다, 같은 id로 다시 보내는 것은 언제나 안전하고 새 id는 중복 위험이 있다"이다. 장시간 망 분리 뒤 늦게 도착하는 요청은 배제할 수 없으므로 "안전하다"고 단정하지 않는다(2026-09-29 검토). 옛 버전 peer는 `status` op를 몰라 `{ok: false, error: "unknown op 'status'"}`로 답한다. 이 경우 원장은 `unknown` 그대로 두고 "peer의 xsm이 너무 오래돼 답하지 못한다, 업데이트하라"는 note만 붙인다. `status`의 `wait`는 30초, `send`의 `wait`는 300초로 잘리고 NaN·무한대·음수는 0이다. 그 전에는 끊긴 호출이 상대에서 아직 돌고 있을 수 있으므로 `unknown`에 두고 언제 다시 물을지 알린다. 너무 일찍 `error`로 확정하면 사람이 새 id로 다시 보내 과제가 두 번 돈다.
- **다시 보내기.** 같은 id로 다시 보내는 것은 안전하다. 받는 쪽이 영수증 있는 id를 다시 전달하지 않고, 영수증 전이라 대기열에 둘이 들어가도 수신 게이트가 `ledger.received(id)`로 두 번째를 버린다. 새 id로 다시 보내면 별개 메시지라 과제가 두 번 돌 수 있다. `unknown`이면 먼저 `xsm status <id>`로 확인한다. 같은 id로 보내는 명령은 `xsm send <대상> --text … --resend <id>`(MCP `xsm_send`의 `resend`)다(2026-09-29). 발신 쪽이 다음을 모두 확인하고 하나라도 어긋나면 `refused`로 거부하며 아무것도 보내지 않는다: id가 이 기계 원장에 있고 `from.ref`가 호출한 세션이며, 상태가 `queued`/`unknown`/`error`이고(영수증이 있으면 `delivered`/`held`/`blocked`이므로 거부), 대상이 같고(로컬은 같은 `ref`, 원격은 같은 `spec@peer`), `kind`가 같고, 본문 앞 200자가 원장의 `preview`와 같다(원장에는 미리보기만 있어 본문은 다시 넘겨야 한다). 원장은 `from.session_id`를 적지 않으므로 "같은 발신자"는 `ref`로만 판단한다. 다시 보낸 항목은 `t`를 처음 값으로 두고 `resent_t`와 `resends`(횟수)를 적으며, 앞 시도의 `error`/`settle_after`는 지운다. `reply_to`는 원장에 없어 검사하지 않는다.
- **게이트.** 헤더에 `origin`이 있으면 5.1의 4~8번 대신 다음을 본다. 짝이 있는지, 이 기계의 수신기가 그 id를 그 peer로 기록했는지, scope가 `remote:<peer>`인지, 수신 세션이 짝의 `local_project`에 속하는지, 차단되지 않았는지다.
- **주소.** `…@<peer>`의 마지막 `@` 뒤가 짝지은 peer면 원격으로 보낸다. 답장 명령은 `ref:<ref>@<origin>`이다.

## 6. 결과값과 종료 코드

| 결과 | 뜻 | 종료 코드 |
|---|---|---|
| `delivered` | 수신 훅이 기록했다 | 0 |
| `sent-unconfirmed` | 큐에 들어갔고 확인이 없다 | 3 |
| `unknown` | 원격 호출의 답을 잃었다. 상대에 닿았는지 모른다(`xsm status <id>`로 묻는다) | 3 |
| `held` / `blocked` | 수신 게이트가 막았다 | 2 |
| `refused` | 발신 단계에서 거부했다 | 2 |
| `error` | 전달 경로가 실패했다 | 4 |

**`delivered`는 영수증이지 내용 무결성 검사가 아니다.** 본문에 `<cross-session-message ...>` 태그가 중첩되어 있으면 Claude Code는 수신 시 이를 `<\cross-session-message ...>`와 `<\/cross-session-message>`로 바꾼다(0.4.7, Claude Code 2.1.284에서 Claude에서 Claude로 보내 측정했다. 본문이 2바이트 늘었고 나머지는 바이트 단위로 같았다). xsm은 봉투를 만들 때와 읽을 때 본문을 그대로 두며(`tests/test_envelope_nested.py`), 변환은 Claude 쪽에서 일어나고 xsm은 되돌리지 않는다(xsm 훅보다 앞인지 뒤인지는 측정하지 않았다). Codex 수신자는 바이트를 그대로 받는다(이슈 #3 보고). 코드 블록이나 파일 경로로 보내면 온전히 전해지는지는 측정하지 않았다.

`list`, `who`, `ledger`, `held`, `doctor`는 성공하면 0, 등록되지 않은 세션에서의 `who`는 2다.

**`outcome`은 종료 코드를 새로 만들지 않는다.** 위 표는 메시지 하나의 전달 결과를 말하고, 과제의 성패는 그와 다른 층이다. 실패를 알리는 답장이 전달되면 `delivered`(0)다. 과제의 성패는 `xsm status <과제 id>`가 전달 상태 뒤에 `(task failed)`로, `xsm ledger`가 `[task failed]`로 보여 준다. `--kind reply`가 아닌데 `--outcome`을 주면 보내기 전에 거부한다(2).

## 7. 버전과 호환

- 헤더의 `v1`이 형식 버전이다. 모르는 필드는 무시하고, 모르는 버전은 "우리 메시지가 아닌 피어 메시지"로 다룬다(5.1절 2번 규칙이 적용된다).
- `outcome`은 v1 안에서의 추가다. 선택 필드이고 기본은 생략이므로, 이 필드를 모르는 구현과 아는 구현이 섞여 있어도 양쪽 다 오늘과 같이 동작한다. 모르는 구현은 필드를 무시하고, 아는 구현은 필드가 없는 답장을 "성패를 말하지 않은 답장"으로 읽는다. 본문을 읽어 성패를 추측하지 않는다.
- 상태 파일에 새 필드를 더하는 것은 호환된다. 필드를 지우거나 뜻을 바꾸면 v2다.
- `tests/vectors.json`의 `version`이 벡터 형식의 버전이다.

## 8. 다시 구현할 때

1. `tests/vectors.json`을 읽고 여섯 절(parse, build, scope, resolve, gate, native_forecast)을 각자의 구현에 연결한다. 게이트 벡터는 훅 실행 파일을 하위 프로세스로 돌려 stdin/stdout으로 검사한다.
2. 상태 파일 경로와 스키마(3절)를 그대로 쓴다. 두 구현이 같은 `XSM_HOME`을 공유해도 되어야 한다.
3. 훅은 예외를 밖으로 내보내지 않고, 종료 코드는 항상 0이며, 판정을 반드시 한 줄 기록한다.
4. 외부 의존을 기본값으로 쓰지 않는다. 네트워크 접속도, 상주 프로세스도 없다.
