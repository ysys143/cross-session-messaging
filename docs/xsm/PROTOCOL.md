# xsm 프로토콜 v1

다른 언어로 다시 구현하더라도 같은 동작이 나오도록, 와이어 형식과 상태 파일과 판정 규칙을 여기에 고정한다. 적합성 기준은 문장이 아니라 `tests/vectors.json`이다. 어떤 구현이든 그 벡터를 통과하면 xsm v1 구현이다.

```bash
python3 -m unittest discover -s tests      # 테스트 51건(벡터 35건 포함)
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
| `reply-to` | 아니오 | 답장 대상 `id` |

값에 공백이 있으면 큰따옴표로 감싼다. 알 수 없는 필드는 무시한다(앞으로 늘어날 수 있다).

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
- `readonly database` 오류는 샌드박스 발신이다.

## 3. 상태 파일

모두 `XSM_HOME`(기본 `~/.xsm`) 아래의 JSON이다. 원자적으로 쓴다(임시 파일 + rename).

| 경로 | 스키마 |
|---|---|
| `config.json` | `{"strict_peers": bool, "same_repo_scope": bool, "scopes": [{"id": str, "members": [{"runtime": str?, "home": str?, "cwd": glob?}]}]}` |
| `homes.json` | `[{"path": str, "runtime": "claude"\|"codex", "alias": str}]` |
| `sessions/<runtime>-<session-id>.json` | `{"runtime", "home", "alias", "session_id", "pid", "lstart", "cwd", "ref", "updated", "permission_mode"?, "name"?}` |
| `ledger/<msg-id>.json` | `{"id", "status": "queued", "t", "kind", "scope", "from": {...}, "to": {...}, "preview"}` |
| `ledger/<msg-id>.recv.json` | `{"id", "decision": "delivered"\|"held"\|"blocked", "reason", "t", "receiver": {...}}` |
| `held/<밀리초>.json` | `{"t", "reason", "runtime", "receiver", "id"?, "from"?, "scope"?, "body"}` |
| `decisions.jsonl` | 한 줄에 판정 하나. 최소 `{"t", "decision", "reason"}` |

규칙:

- 포인터는 **훅만** 쓴다. 홈 목록을 glob으로 추측하지 않는다. 대상 목록은 `homes.json`과 포인터에 적힌 홈의 합집합이다.
- 이름과 소켓은 포인터에 캐시하지 않고 조회 시 런타임의 원본에서 읽는다. Claude는 `<홈>/sessions/<pid>.json`, Codex는 `<홈>/state_5.sqlite`의 `threads` 표다.
- `decisions.jsonl`에 줄이 없다는 것은 곧 훅이 실행되지 않았다는 뜻이다. 판정은 성공·실패·예외를 가리지 않고 한 줄씩 남긴다.

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

### 4.3 생존 판정

| 런타임 | 판정 |
|---|---|
| Claude | pid 생존 + `ps lstart` 일치 + inbox 소켓 연결 성공 |
| Codex | pid 생존 + `ps lstart` 일치 |

- 종료 훅에 의존하지 않는다. 강제 종료 시 `SessionEnd`는 실행되지 않는다(S3).
- **주의:** Claude가 자기 레코드에 쓰는 `procStart`는 UTC이고 `ps lstart`는 로컬 시간이다. 두 값을 직접 비교하면 안 된다. 우리가 등록한 포인터의 `lstart`만 `ps` 출력과 비교한다.

## 5. 판정

### 5.1 수신 게이트

`UserPromptSubmit`에서 순서대로 본다. `SessionStart`는 등록만 하고 절대 차단하지 않는다.

1. 봉투도 헤더도 없다 → **아무것도 출력하지 않는다**(사람 입력).
2. 헤더가 없다 → `strict_peers`가 참이면 **차단**, 거짓이면 통과.
3. 발신자 `ref`가 레지스트리에 없다 → **차단**.
4. 발신자와 수신자가 공통 scope에 없다 → **차단**.
5. 지금 계산한 scope가 헤더의 `scope`와 다르다 → **차단**(보낸 뒤 정책이 바뀐 경우).
6. 그 밖에는 **통과**시키고 발신 표시 문맥을 붙인다.

차단할 때는 **본문을 `held/`에 먼저 저장한 뒤** 차단한다. Codex는 차단하면 큐 항목이 사라지기 때문이다(S6). 저장에 실패하면 차단하지 않고 경고 문맥을 붙여 통과시킨다. 차단·통과 모두 `id`가 있으면 영수증을 쓴다.

출력 형식:

| 판정 | 출력 |
|---|---|
| 통과 | `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<발신 표시>"}}` |
| 차단 | `{"decision": "block", "reason": "xsm: …", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": true}}` (Claude), Codex는 `suppressOriginalPrompt` 없이 |
| 사람 입력 | 출력 없음 |

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

### 5.4 발신 사전 검사

수신 측과 같은 검사를 먼저 한다. 순서대로 하나라도 걸리면 보내지 않는다.

1. 발신 세션이 등록돼 있다.
2. 대상이 해석된다(모호하면 거부).
3. 대상이 등록돼 있고 살아 있다.
4. 자기 자신이 아니다.
5. 공통 scope가 있다.
6. 네이티브 예보가 `refuse`가 아니다.

통과하면 원장에 `queued`를 쓰고 전송한다. **전송 성공은 전달이 아니다.** `delivered`는 수신 측 영수증으로만 정해진다.

## 6. 결과값과 종료 코드

| 결과 | 뜻 | 종료 코드 |
|---|---|---|
| `delivered` | 수신 훅이 기록했다 | 0 |
| `sent-unconfirmed` | 큐에 들어갔고 확인이 없다 | 3 |
| `held` / `blocked` | 수신 게이트가 막았다 | 2 |
| `refused` | 발신 단계에서 거부했다 | 2 |
| `error` | 전달 경로가 실패했다 | 4 |

`list`, `who`, `ledger`, `held`, `doctor`는 성공하면 0, 등록되지 않은 세션에서의 `who`는 2다.

## 7. 버전과 호환

- 헤더의 `v1`이 형식 버전이다. 모르는 필드는 무시하고, 모르는 버전은 "우리 메시지가 아닌 피어 메시지"로 다룬다(5.1절 2번 규칙이 적용된다).
- 상태 파일에 새 필드를 더하는 것은 호환된다. 필드를 지우거나 뜻을 바꾸면 v2다.
- `tests/vectors.json`의 `version`이 벡터 형식의 버전이다.

## 8. 다시 구현할 때

1. `tests/vectors.json`을 읽고 여섯 절(parse, build, scope, resolve, gate, native_forecast)을 각자의 구현에 연결한다. 게이트 벡터는 훅 실행 파일을 하위 프로세스로 돌려 stdin/stdout으로 검사한다.
2. 상태 파일 경로와 스키마(3절)를 그대로 쓴다. 두 구현이 같은 `XSM_HOME`을 공유해도 되어야 한다.
3. 훅은 예외를 밖으로 내보내지 않고, 종료 코드는 항상 0이며, 판정을 반드시 한 줄 기록한다.
4. 외부 의존을 기본값으로 쓰지 않는다. 네트워크 접속도, 상주 프로세스도 없다.
