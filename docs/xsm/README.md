# xsm v0.1 — 세션 간 메시징

서로 다른 프로필(`~/.claude`, `~/.claude-3`, …)과 런타임(Claude Code, Codex)의 세션이 서로를 찾고 메시지를 주고받게 한다. 상주 프로세스도, 서버도, 외부 연결도 없다. 훅이 포인터를 남기고, 일회성 CLI가 각 런타임의 네이티브 경로로 전달하며, 모든 상태는 `~/.xsm`의 파일이다.

설계 근거는 `docs/adr/`의 결정 초안과 `docs/spikes/`의 실측이다. 이 문서는 쓰는 방법만 다룬다. 형식과 판정 규칙의 명세는 `PROTOCOL.md`, 실사용 점검 절차는 `TESTPLAN.md`에 있다.

## 설치

```bash
python3 -m xsm install --claude-home ~/.claude-3 --claude-home ~/.claude-4 --codex-home ~/.codex --dry-run
python3 -m xsm install --claude-home ~/.claude-3 --claude-home ~/.claude-4 --codex-home ~/.codex
python3 -m xsm doctor
```

- 설치는 **병합**이다. 우리가 넣는 훅 명령 끝에는 `#xsm-hook` 표식이 붙고, 설치·제거는 그 표식이 붙은 항목만 건드린다. Orca, cctrace, 사용자 훅은 그대로 남는다.
- 매번 `<파일>.xsm-backup-<시각>` 백업을 남기고, 쓴 뒤 다시 파싱해 깨졌으면 백업으로 되돌린다.
- 훅 명령에는 **설치 시점의 파이썬 절대 경로**가 박힌다. 훅이 뜨지 못하면 검문이 통째로 열리기 때문이다(S8-g2).
- Codex는 첫 세션에서 훅 신뢰를 한 번 승인해야 한다. 승인 전에는 훅이 실행되지 않는다. 설치기는 안내만 하고 우회 옵션을 쓰지 않는다.

```bash
python3 -m xsm install --claude-home ~/.claude-4 --python 3.13       # uv python find로 해석
python3 -m xsm install --claude-home ~/.claude-4 --python /opt/homebrew/bin/python3.13
```

**`uv run`을 훅에 쓰지 않는 이유.** 버전을 고정하려면 `uv python find <버전>`이 알려 준 **경로**를 박으면 된다. 훅 명령에 `uv run`을 넣으면 매 프롬프트마다 런처가 하나 더 끼고, PATH에서 uv를 찾아야 하고, 인터프리터가 없으면 내려받기까지 시도한다(훅 제한 시간 10초). xsm은 표준 라이브러리만 쓰므로 의존성 해석에서 얻을 것도 없다. 측정: 고정 인터프리터 0.04초, `uv run --no-project` 첫 실행 0.16초·이후 0.04초. CLI를 uv로 실행하는 것은 자유다(`uv run --no-project python -m xsm list`), `bin/xsm`은 `XSM_PYTHON`을 따른다.

제거:

```bash
python3 -m xsm uninstall --claude-home ~/.claude-3
```

## 세션 안에서 (슬래시 명령)

설치가 홈에 명령 파일을 넣는다. 세션에서 그대로 쓴다.

| 명령 | 하는 일 |
|---|---|
| `/xsm-list` | 지금 메시지를 보낼 수 있는 세션 목록 |
| `/xsm-who` | 이 세션의 주소 |
| `/xsm-send <대상> <내용>` | 보내고 결과를 그대로 보고 |
| `/xsm-inbox` | 최근 메시지와 보류된 것 |
| `/xsm-doctor` | 설치 상태와 알려진 한계 |

**모델 호출 비용.** 방법마다 다르다(2026-09-21 실측).

| 방법 | 모델 호출 | 쓰는 법 |
|---|---|---|
| 상태줄 | 0회. 항상 보인다 | `xsm install --claude-home <홈> --statusline`. 화면 아래에 `xsm 2 peers · as builder · 1 held` 식으로 뜬다 |
| 셸 모드 | 0회 | 세션 입력창에 `! xsm list`, `! xsm who`. 출력은 대화에 남지만 응답 턴이 생기지 않는다(대화 기록에 `bash-input`/`bash-stdout`만 있다). `xsm`이 PATH에 있어야 한다 |
| 슬래시 명령 | 1회(세션 모델) | `/xsm-list` 등. 출력을 넣고 모델이 한 줄만 덧붙이게 지시한다 |

명령 파일에 `model:`을 적어 저렴한 모델로 돌리려 했지만 이 버전에서는 무시됐다(`haiku` 별칭과 전체 ID 모두 세션 모델로 실행). 그래서 지정하지 않는다. 상태줄은 홈마다 하나뿐이라, 이미 설정돼 있으면 덮어쓰지 않는다.

읽기만 하는 명령은 출력을 그대로 세션에 넣는다. `/xsm-send`는 인자를 셸 문자열로 이어 붙이지 않고 모델이 인자로 넘기게 해, 본문에 따옴표나 특수문자가 있어도 안전하다.

## 쓰기

```bash
xsm list                          # 주소를 지정할 수 있는 세션
xsm who                           # 지금 세션의 신원
xsm send "reviewer@claude-4" --text "..." --wait 20
xsm status <msg-id>
xsm ledger
xsm held list | xsm held show <id>
xsm doctor | xsm selftest
```

주소는 `이름`, `이름@홈별칭`, `이름 [ref]`, `ref:xxxxxx`, `claude:<세션id>`, `codex:<스레드id>`다. 이름이 여러 세션과 맞으면 **거부하고 후보를 보여 준다**. 이름은 고유하지 않고, 조용한 오배송보다 오류가 낫기 때문이다(ADR-0008, S7).

## 이름과 주소

`--name`은 선택 사항이다. 주지 않아도 세션에는 이름이 있고 주소로 쓸 수 있다. 출처는 셋이다.

| 출처 | 예 | 성질 |
|---|---|---|
| `user` | `--name builder`, `/rename` | 사람이 고른 이름 |
| `derived` | `ws-99`, `graduate-school-path-7b` | 작업 폴더 이름 + 짧은 접미사. 자동 |
| `auto` | `gcp-service-account-key-cleanup` | 대화 내용에서 생성. 자동 |

xsm은 이름을 캐시하지 않고 조회할 때마다 런타임의 원본에서 읽는다. 그래서 `/rename`으로 이름을 바꿔도 곧바로 반영된다. 반면 `ref`는 `(런타임, 홈, 세션 id)`의 해시라서 **이름이 바뀌어도 그대로다**(실측: `ws-99` → `renamed-later`, ref `10b151` 유지). 메시지 헤더가 발신자를 `ref`로 싣는 이유이고, 어딘가에 적어 둘 주소라면 `ref:`를 쓰는 편이 안전하다.

`xsm who`는 이름이 사람이 고른 것이 아닐 때 그 사실과 ref를 함께 알려 준다. Codex 스레드는 첫 메시지에서 제목이 만들어지거나 이름이 없을 수 있고, 없으면 `codex-<앞 8자>`로 표시된다.

## 범위

기본값은 **같은 저장소 자동 허용**이다. 두 세션의 작업 폴더가 같은 git 저장소 아래면 설정 없이 통신한다. git 저장소가 아니면 같은 폴더일 때만 허용한다.

저장소를 넘어 통신하려면 `~/.xsm/config.json`에 적는다.

```json
{
  "strict_peers": true,
  "same_repo_scope": true,
  "scopes": [
    {"id": "review", "members": [
      {"runtime": "claude", "home": "claude-4", "cwd": "~/work/app/**"},
      {"runtime": "codex",  "home": "codex",    "cwd": "~/work/app/**"}
    ]}
  ]
}
```

범위는 **보낼 때와 받을 때 두 번** 검사한다. 보낼 때 걸리면 메시지는 아예 나가지 않고, 받을 때 걸리면 본문을 보류 저장소에 남기고 차단한다.

## 전달 판정

- `xsm send`의 성공은 전달이 아니다. 발신은 원장에 `queued`로 남고, **수신 세션의 훅이 영수증을 쓸 때만** `delivered`가 된다.
- 수신자가 꺼져 있으면 `sent-unconfirmed`로 끝난다. 이 상태를 전달로 읽지 않는다.
- Codex는 진행 중인 턴에 끼어들지 않는다. 전달 시점은 현재 턴이 끝난 뒤이거나, 스레드가 유휴 상태면 약 10초 안이다.

## 알려진 한계

| 한계 | 내용 |
|---|---|
| 봉투 없는 입력 | xsm 봉투 없이 들어온 피어 메시지는 훅 입력에서 사람 입력과 구분되지 않아 검문을 통과한다(S8-g2). 모든 피어 메시지를 멈추려면 Claude 설정의 `crossSessionInbound`를 `"hold"`로 두어야 하는데, 그러면 정상적인 xsm 메시지도 함께 보류된다. |
| 보안이 아니라 동의 기록 | 같은 사용자 권한으로 도는 에이전트는 `~/.xsm`과 설정 파일을 직접 고칠 수 있다(S8-c). 검문은 범위와 동의를 기록하는 장치다. |
| 샌드박스 Codex | 샌드박스가 켜진 Codex는 셸에서 소켓과 큐 양쪽에 접근하지 못한다(S4). 발신은 샌드박스 밖 실행이나 신뢰된 훅 경로에서만 된다. |
| 권한 모드가 다른 상대 | Claude는 발신자와 수신자의 권한 모드 부류가 다르면 **우리 훅보다 먼저** 네이티브로 보류한다(S1, S8-g). 예를 들어 bypass로 도는 Codex가 auto로 도는 Claude에 보내면 수신 화면에 보류 창이 뜨고, 발신 측 결과는 `sent-unconfirmed`로 남는다. 해결은 셋 중 하나다. (1) 수신 세션이 `crossSessionInbound: "accept"`를 쓴다(실측: 같은 조건에서 보류 → 전달로 바뀐다). (2) 두 세션의 권한 모드 부류를 맞춘다. (3) 사람이 보류 창에서 승인한다. `xsm send`는 보내기 전에 이 판정을 예측해 결과 문구에 적고, `xsm list`는 보류될 상대에 `would be held`를 표시한다. 예측은 수신 홈의 **사용자 설정 파일**만 읽으므로 `--settings`로 다르게 띄운 세션에서는 어긋날 수 있다. 참고로 이 키는 프로젝트 설정(`.claude/settings*.json`)에서는 적용되지 않는다(2026-09-21 실측). |
| 원격 | 이 버전은 로컬 전용이다. SSH 너머 전달은 ADR-0007을 결정한 뒤에 다룬다. |
| 기록 채널 | 대화 기록·스레드 저장소는 아직 없다(ADR-0005, 0006). |

## 상태 파일

```
~/.xsm/
  config.json          범위와 표시 설정 (사용자가 편집)
  homes.json           선언된 홈 목록. 훅 레코드의 홈과 합집합으로 쓴다
  sessions/*.json      훅이 남긴 세션 포인터
  ledger/*.json        보낸 기록과 수신 영수증
  held/*.json          검문이 막은 메시지 본문
  decisions.jsonl      훅 판정 감사 기록
```

`XSM_HOME`으로 위치를 옮길 수 있다. 홈 목록을 glob으로 추측하지 않으므로, 비표준 경로의 프로필은 `xsm homes add`로 선언하거나 그 홈에서 훅이 한 번 돌면 자동으로 등록된다.
