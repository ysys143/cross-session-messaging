# xsm v0.1 — 세션 간 메시징

서로 다른 프로필(`~/.claude`, `~/.claude-3`, …)과 런타임(Claude Code, Codex)의 세션이 서로를 찾고 메시지를 주고받게 한다. 상주 프로세스도, 서버도, 외부 연결도 없다. 훅이 포인터를 남기고, 일회성 CLI가 각 런타임의 네이티브 경로로 전달하며, 모든 상태는 `~/.xsm`의 파일이다.

설계 근거는 `docs/adr/`의 결정 초안과 `docs/spikes/`의 실측이다. 이 문서는 쓰는 방법만 다룬다. 형식과 판정 규칙의 명세는 `PROTOCOL.md`, 실사용 점검 절차는 `TESTPLAN.md`에 있다. 첫 실사용 시험 결과는 `TRIAL-2026-09-21.md`에 있다.

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
- Codex 훅은 첫 프롬프트부터 돈다. 그래서 훅이 신뢰된 홈에서는 `xsm list`·`xsm send`가 열린 Codex 스레드를 대신 등록한다. `/rename`만 한 세션에도 바로 보낼 수 있고, 그 메시지가 첫 프롬프트가 된다. 프롬프트도 `/rename`도 없는 Codex는 스레드가 없어서 주소가 없다.

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

Codex에는 슬래시 명령이 없어서, 설치는 같은 명령을 이름이 같은 스킬(`skills/xsm-list/` 등)로 넣는다.
Codex에서는 `/xsm-list` 대신 `$xsm-list`로 부른다. Claude의 `!`명령`` 선실행이 Codex에는 없으므로
스킬 본문은 모델에게 명령을 실행해 출력을 그대로 옮기라고 지시한다. 모델이 스스로 부르지 않도록
`agents/openai.yaml`에 `allow_implicit_invocation: false`를 둔다.

| 명령 | 하는 일 |
|---|---|
| `/xsm-list` | 지금 메시지를 보낼 수 있는 세션 목록 |
| `/xsm-who` | 이 세션의 주소 |
| `/xsm-send <대상> <내용>` | 보내고 결과를 그대로 보고 |
| `/xsm-inbox` | 최근 메시지와 보류된 것 |
| `/xsm-doctor` | 설치 상태와 알려진 한계 |
| `/xsm-join <이름>` | 이 저장소를 xsm 프로젝트에 가입. 다른 저장소와 통신하려면 양쪽이 같은 이름으로 가입한다 |
| `/xsm-leave <이름>` | 프로젝트에서 탈퇴 |
| `/xsm-projects` | 프로젝트와 가입한 폴더 |

**모델 호출 비용.** 방법마다 다르다(2026-09-21 실측).

| 방법 | 모델 호출 | 쓰는 법 |
|---|---|---|
| 상태줄 | 0회. 항상 보인다 | `xsm install --claude-home <홈> --statusline`. 화면 아래에 `xsm 2 peers · as builder · 1 held` 식으로 뜬다 |
| 셸 모드 | 0회 | 세션 입력창에 `! xsm list`, `! xsm who`. 출력은 대화에 남지만 응답 턴이 생기지 않는다(대화 기록에 `bash-input`/`bash-stdout`만 있다). `xsm`이 PATH에 있어야 한다 |
| 슬래시 명령 | 1회(세션 모델), 사고 없음 | `/xsm-list` 등. 모델은 명령 출력을 그대로 옮겨 적기만 한다 |

**슬래시 명령의 추론을 줄인 방법.** 화면에는 명령에 끼워 넣은 출력이 보이지 않고 모델의 응답만 보이므로, 모델이 출력을 옮겨 적어야 한다. 그래서 (1) 지시에서 조건 판단을 모두 없애고 "표시 명령이다, 결정할 것이 없다, 표시된 블록을 그대로 복사하라"만 남겼고, (2) 옮겨 적을 분량을 줄이려고 CLI에 `--compact` 출력(정렬 공백 없음, 홈은 `~`, 미등록·정지 세션 제외)을 만들었다. 새 세션에서 처음 실행한 값이다(2026-09-21).

| 명령 | 처음 | 조건 제거 + 간결 출력 |
|---|---|---|
| `/xsm-who` | 290토큰 · 사고 2회 · 5.8초 | 44토큰 · 사고 0 · 1.3초 |
| `/xsm-list` | 88토큰 · 2.3초, 목록 대신 요약만 보임 | 125토큰 · 사고 0 · 1.8초, 목록이 보임 |
| `/xsm-inbox` | 68토큰 · 2.5초, 요약만 보임 | 112토큰 · 사고 0 · 1.7초 |
| `/xsm-send` | 373토큰 · 4.4초, 결과 뒤에 설명 | 275토큰 · 사고 0 · 3.6초, 결과만 |

명령 파일에 `model:`을 적어 저렴한 모델로 돌리려 했지만 이 버전에서는 무시됐다(`haiku` 별칭과 전체 ID 모두 세션 모델로 실행). 그래서 지정하지 않는다. 상태줄은 홈마다 하나뿐이라, 이미 설정돼 있으면 덮어쓰지 않는다.

읽기만 하는 명령은 출력을 그대로 세션에 넣는다. `/xsm-send`는 인자를 셸 문자열로 이어 붙이지 않고 모델이 인자로 넘기게 해, 본문에 따옴표나 특수문자가 있어도 안전하다.

## 설치 두 가지

| 길 | 대상 | 갱신 | 특징 |
|---|---|---|---|
| 플러그인 (`/plugin install xsm@xsm`) | Claude Code | `plugin.json`의 `version` | 훅·명령·스킬·MCP·`bin/`이 한 번에. 끄면 훅도 꺼짐. 명령은 `/xsm:list` |
| `xsm install` | Codex, 플러그인 안 쓰는 Claude 홈 | `xsm install --refresh` | 설정 파일에 훅을 병합하고 명령·스킬을 복사. 명령은 `/xsm-list`, Codex는 `$xsm-list` |

한 Claude 홈에 둘 다 두면 훅이 두 번 돈다. 두 번째 수신 게이트가 자기 영수증을 보고 중복으로 거부하므로
메시지가 사라진다. `xsm install`은 플러그인이 있는 홈을 거부한다. `xsm doctor`가 어느 홈이 어느 길인지,
복사본이 낡았는지, 지금 무엇이 막혀 있는지 보여 준다.

### eval 스위트

`evals/`에 케이스 세 개가 있다. 각각 이 세션들이 반복해서 틀렸던 것을 고정한다: 세션 목록과 전송 문법,
샌드박스에서 Codex로 보낼 때의 MCP 경로, 아직 프롬프트가 없는 Codex 스레드.

```bash
git archive HEAD | tar -x -C /tmp/xsm-clean && cp -R evals /tmp/xsm-clean/
cd /tmp/xsm-clean && claude plugin eval . --runs 1 --model haiku --judge-model haiku --trust-plugin
```

깨끗한 사본에서 돌리는 이유: eval은 폴더 전체를 플러그인으로 싣는데, 작업 중인 저장소에는 git이 무시하는
파일(`.omc/`의 하드 링크 등)이 있고 eval은 하드 링크가 있으면 케이스를 거부한다.

실측 2026-09-23: 스킬 **설명문**만 보고 답한 모델이 `/xsm send <session-id> <message>`라는 없는 문법을
지어냈다(세 케이스 0점). 설명문에 실제 명령 모양을 넣자 세 케이스 모두 1.00이 됐고, 플러그인 없는
대조군은 0.00이다(Δ +1.00). 스킬 설명문은 모델이 본문을 열기 전에 보는 유일한 것이므로 명령 모양을 담는다.

### 플러그인을 고칠 때 알아야 할 것 (실측 2026-09-23)

- **MCP 서버는 플러그인 루트의 `.mcp.json`에서만 읽는다.** `plugin.json`에 `mcpServers`를 직접 쓰거나 다른
  경로를 가리키면 `claude plugin details`가 `MCP servers (0)`으로 센다. 그래서 이 저장소 루트에 `.mcp.json`이
  있다. 대가: Claude Code는 같은 파일을 **프로젝트 MCP 설정**으로도 읽으므로, 이 저장소를 연 세션은 xsm MCP
  서버를 쓸지 한 번 묻는다. 기여자만 겪는 비용이다.
- **설치본은 버전별 캐시 사본이다.** 저장소를 고쳐도 `plugin.json`의 `version`이 그대로면
  `claude plugin update`가 "already at the latest version"이라고 답한다. 고쳤으면 버전을 올린다.
- **검증은 `claude plugin validate . --strict`**, 구성 요소와 토큰 비용은 `claude plugin details xsm`.
- `claude mcp list`를 세션 밖에서 돌리면 `Missing environment variables: CLAUDE_PLUGIN_ROOT` 경고가 붙는다.
  세션 안에서는 그 변수가 채워지므로 서버는 정상 연결된다.

## 쓰기

```bash
xsm list                          # 이 폴더와 주고받을 수 있는 살아 있는 세션 (-a: 모든 프로젝트, 멈춘 세션 포함)
xsm who                           # 지금 세션의 신원
xsm send "reviewer@claude-4" --text "..." --wait 20
xsm status <msg-id>
xsm inbox                         # Codex 세션: 턴 도중 도착한 메시지를 지금 읽는다
                                  # (샌드박스 셸에서 Codex 상대 send는 바로 거부하고 MCP xsm_send를 안내한다)
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

저장소를 넘어 통신하려면 **양쪽 저장소가 같은 이름의 xsm 프로젝트에 가입**한다. 각 저장소의 세션에서 한 번씩 실행한다.

```
/xsm-join demo          # 셸에서는 xsm join demo
/xsm-projects           # 프로젝트와 가입한 폴더
/xsm-leave demo
```

- 모든 세션은 시작한 디렉터리의 프로젝트에 **기본으로 속한다**(`repo:<저장소>`, 저장소가 아니면 `dir:<폴더>`). 이름 붙인 프로젝트는 여기에 **추가로** 가입하는 것이라, 같은 저장소 세션끼리는 가입 뒤에도 기본 프로젝트로 통신한다. `/xsm-projects`가 둘 다 보여 준다.
- 가입 단위는 세션이 아니라 **폴더**다. git 저장소 안이면 저장소 루트, 아니면 그 폴더다. 가입한 뒤에는 그 폴더 아래에서 여는 모든 세션이 프로젝트에 속한다.
- 한쪽만 가입해서는 열리지 않는다. 한 저장소가 다른 저장소를 동의 없이 끌어들이지 못하게 하기 위해서다.
- 가입과 탈퇴는 `config.json`의 `scopes`에 `{"root": …}` 구성원으로 기록된다. 손으로 쓴 범위와 이름이 같으면 가입을 거부한다. 조용히 범위가 넓어지는 것을 막기 위해서다.
- **가입과 탈퇴는 사람의 결정이고, xsm이 강제한다**(ADR-0009). 터미널의 사람은 `xsm join`을 바로 쓴다. 세션에서는 MCP 도구 `xsm_join`이 사용자에게 양식으로 묻는다. `/xsm-join`, `/xsm-leave`도 이 도구를 부른다. 다른 폴더 이름으로 말하는 `--dir`는 사람 터미널에서만 받는다.
- **세션 하나만 끊기.** `xsm block <ref>`는 그 세션이 이 기계의 누구와도 주고받지 못하게 한다. 누구나 걸 수 있다(좁히기만 하므로). 해제(`xsm unblock`)는 사람만 한다.
- **멈춘 세션 명의의 메시지는 막는다.** 수신 검문은 발신자가 살아 있을 때만 통과시킨다.

런타임이나 홈까지 좁히려면 `~/.xsm/config.json`에 직접 적는다.

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

## 채널

사람과 세션이 함께 남기는 기록이다(ADR-0005). 즉시 전달(`xsm send`)과는 따로다. 게시는 기록만 하고 누구도 깨우지 않는다.

```
xsm post "Which queue should the importer use?" --tag question
xsm post "retries: 3" --tag decision            # 사람만. 터미널에서
xsm channel show [--tag decision] [--channel demo]
xsm channel export --out docs/DECISIONS.md      # 사람이 검토하고 직접 커밋한다
xsm channel list
```

- **채널은 범위다.** 이 폴더의 기본 프로젝트, 또는 가입한 이름 붙인 프로젝트의 채널에만 쓰고 읽는다. 원본은 `~/.xsm/channels/`에 있고 자동 정리하지 않는다. 저장소에는 `export`로 만든 요약만 들어간다.
- **태그.** `note`, `question`, `proposal`, `result`, `hypothesis`, `decision`. 스레드는 `--reply-to <id>`로 잇는다.
- **작성자는 xsm이 정한다.** 에이전트 표식이 없는 터미널이면 사람이고, 등록된 세션이면 그 세션이다.
- **`decision`은 사람의 것이다.** 에이전트는 CLI나 `xsm_post`로 결정을 올릴 수 없다. 대신 MCP 도구 `xsm_decide`로 사용자에게 묻는다. 사용자 화면에 질문과 선택지 양식이 뜨고, 사용자가 고른 답은 모델을 거치지 않고 xsm에 도착한다. xsm은 그 답을 질문 원문과 함께 결정으로 기록한다. Claude Code와 Codex 모두 된다(실측). 작성자는 `사람 via mcp-elicitation`으로 남는다.
- **MCP 서버.** 설치할 때 각 홈에 `xsm`이라는 이름으로 등록된다(`claude mcp add --scope user`, `codex mcp add`). 도구는 `xsm_post`, `xsm_channel`, `xsm_decide` 셋이다. 세션이 띄우고 세션과 함께 끝난다. Codex가 띄운 MCP 서버는 샌드박스 밖에서 돌기 때문에, 샌드박스 Codex도 채널에 쓸 수 있다(실측). Codex는 MCP 도구마다 호출 허용을 묻는다. 등록을 원하지 않으면 `xsm install --no-mcp`.
- **한계.** 같은 사용자 권한의 에이전트는 파일을 직접 고쳐 작성자나 결정을 위조할 수 있다.

## 공동 문서

여러 세션이 함께 쓰는 조사 문서는 파일을 직접 고치지 않고 **노드**를 쌓는다(ADR-0006, agora 방식).

```
xsm doc add docs/research/cache.md --tag result --text "redis is 2x faster"
xsm doc add docs/research/cache.md --tag report --file draft.md --parent <id>
xsm doc render docs/research/cache.md        # 노드에서 문서를 만든다
xsm doc log|leaves docs/research/cache.md
xsm doc show docs/research/cache.md <id>
```

- **노드.** 기여 하나가 `<문서>.nodes/<id>.md` 한 파일이다. 한 번 쓰면 바꾸지 않는다. 고칠 때는 `--parent`로 이전 노드를 가리키는 새 노드를 쓴다. 노드마다 파일 이름이 다르므로, 여러 세션이 동시에 써도 git 병합에서 충돌하지 않는다(실측).
- **태그.** `setup`, `result`, `insight`, `hypothesis`, `verification`, `report`, `wip`, `endorsed`. `endorsed`는 사람만 단다. 세션은 MCP 도구 `xsm_doc_endorse`로 사용자에게 양식으로 묻는다.
- **문서는 만들어진다.** `render`는 가장 최근의 `endorsed` 노드, 없으면 가장 최근의 `report` 노드 본문을 문서로 쓴다. 그 아래에 아직 이어지지 않은 노드와 검증되지 않은 가설을 붙인다. 문서 머리에 "생성된 파일"이라고 적히므로 손으로 고치지 않는다. 병합에서 문서가 충돌하면 다시 `render`한다.
- 작성자는 채널과 같은 규칙으로 xsm이 정한다. 사람이 아니고 등록된 세션도 아닌 셸에서는 거부한다.

## 다른 기계의 세션

두 기계가 서로 SSH로 들어갈 수 있으면, 한 프로젝트끼리 짝지어 주고받는다(ADR-0007).

```
xsm remote add jaesol-macmini --project demo [--remote-project demo] [--reach-me-as jaesol-macbookpro] [--remote-xsm /path/to/bin/xsm]
xsm remote sessions jaesol-macmini            # 그쪽 짝 프로젝트의 살아 있는 세션
xsm send agent@claude@jaesol-macmini --text "…"
xsm remote list | remove jaesol-macmini
```

- **짝짓기.** `add`는 사용자의 SSH로 한 번 들어가서 xsm 전용 키(`~/.xsm/remote/id_ed25519`)를 서로 교환한다. 각 기계의 `authorized_keys`에는 상대 키가 xsm 수신기만 실행하도록 제한된 채로 들어간다(`command="… xsm-remote.py <상대>"`, `no-pty` 등). 그 뒤 반대 방향으로도 들어가는지 확인하고, 안 되면 짝짓기를 되돌린다. 사람이 해야 한다. 터미널이거나, 에이전트라면 `xsm_grant`의 `remote` 허가가 있어야 한다.
- **신원.** 받는 쪽은 어느 키로 들어왔는지로 상대를 안다. 메시지에 적힌 주장은 쓰지 않는다. 신뢰 단위는 세션이 아니라 기계와 키다.
- **범위.** 짝지은 프로젝트에 속한 세션끼리만 주고받는다. 이름이 같다고 열리지 않는다.
- **검문.** 받는 쪽 게이트는 자기 수신기가 기록한 메시지 id만 통과시킨다. 봉투에는 `uds:` 회신 주소가 없고 `origin`이 붙는다. 답장 명령은 `ref:xxxx@<상대>` 형식이라 그대로 되돌아간다.
- **실제 환경에서 알아 둘 것**(2026-09-22 실측).
  - xsm 호출은 SSH 설정의 키와 키 에이전트를 무시하고 xsm 키만 쓴다. 주소는 `ssh -G`로 읽으므로 `~/.ssh/config`의 호스트 별칭은 그대로 된다.
  - macOS에서는 sshd가 띄운 프로세스가 `~/Documents` 같은 보호 폴더를 읽지 못한다. 그래서 수신기는 `~/.xsm/remote/pkg`의 사본으로 돈다.
  - 원격 짝은 이름 붙인 프로젝트로만 맺는다.
  - 샌드박스 Codex는 MCP 도구 `xsm_send`로 답한다. Codex 백그라운드 서비스가 오래 떠 있었다면, 새 MCP 등록은 그 서비스가 다시 시작해야 반영된다(`codex app-server daemon restart`).
- **넣지 않은 것.** `xsm list`에 원격 세션을 합치는 것, 채널·문서 공유, 원격 워커. 원격 대상은 이미 떠 있는 세션뿐이다. macOS에서는 비대화식 SSH로 새 Claude 세션을 띄울 수 없다.

## 워커

세션이 작업을 맡길 새 세션을 직접 띄우고, 끝나면 종료한다.

```
xsm spawn claude --model haiku --once --task "…"     # 워커를 띄우고 과제를 보낸다
xsm spawn codex --effort high --task "…"            # Codex 기본 모델: gpt-5.6-luna
xsm workers | attach <워커> | stop <워커>
xsm approvals | approve <id> | deny <id>
```

| 부른 곳 | 워커 | 승인 창 |
|---|---|---|
| tmux 안 | 부른 패널 옆에 분할한 패널에서 실제 TUI. Claude는 `--permission-mode default`, Codex는 `-s workspace-write -a on-request`로 띄워 사용자 기본 설정(auto, YOLO)을 따르지 않는다 | 사람이 그 패널에서 답한다 |
| 일반 셸, 또는 `--background` | 분리된 tmux 세션 `xsm-workers`의 창에서 실제 TUI. 헤드리스(`claude -p`, `codex exec`)로는 띄우지 않는다 | 작업 폴더 샌드박스 안은 묻지 않는다(Codex `-s workspace-write -a never`, Claude `acceptEdits` + OS 샌드박스). 그 밖은 Claude만 xsm이 사람에게 넘긴다(`xsm approve`/`deny`, `xsm_approve`) |
| Orca·herdr 안 | 띄우지 않는다. 워커 관리는 그 도구의 몫이고, xsm은 세션 간 메시지만 맡는다. 사람이 `xsm frameworks ignore orca`로 끄면 밖과 같다 | |

- **워커는 권한 때문에 놀지 않는다.** 백그라운드 워커가 승인을 기다리기 시작하면, 부모 세션에 `task`가 간다. 내용은 "지금 `xsm_approve`를 불러라"다. 부모가 MCP 도구 `xsm_approve`를 부르면 사용자 화면에 워커의 요청이 양식으로 뜬다. 사용자는 허용이나 거부만 고르면 된다. 터미널도 `!`도 필요 없다. 대기 시간이 지나거나 거부되면 부모에게 결과가 간다. 워커의 과제에는 규칙이 붙는다. 할 수 있는 것은 다 하고, 막힌 단계는 "못 했다"로 끝내지 말고 필요한 권한과 이유를 적어 보고하고, 한 것과 확인한 것만 말하라는 것이다. 작업 폴더도 명시된다.
- **auto 모드에서 도구 호출이 막히면 묻는다.** Claude Code auto 모드의 분류기가 `xsm_approve`나 `xsm_grant` 호출을 막을 수 있다. 그때 에이전트는 멈추지 않고 질문 도구로 사용자에게 물은 뒤, 동의를 받으면 다시 부른다. 실측에서는 동의 뒤의 재호출이 통과해 양식이 떴다.
- **폴더 신뢰도 사람에게 묻는다.** 백그라운드 워커가 처음 여는 폴더에서 신뢰 확인 화면에 멈추면, spawn은 그 화면을 승인 요청으로 바꾸고 요청 id와 함께 바로 돌아온다. 에이전트가 `xsm_approve`를 부르면 사용자 화면에 양식이 뜨고, 사람이 터미널에서 직접 spawn했으면 그 자리에서 묻는다. 답이 오면 xsm이 워커 화면에 그 답을 입력하고 등록을 마친 뒤 과제를 보낸다. 사람이 tmux 화면으로 갈 필요가 없다.
- **승인은 사람만 한다.** 백그라운드 Claude 워커가 승인이 필요해지면 그 워커 전용 `PermissionRequest` 훅이 요청을 기록하고, 부모 세션에 무엇을 기다리는지 알리고, 답을 기다린다(기본 600초, 넘기면 거부). `xsm approve`는 터미널에서만 동작한다. 에이전트의 셸 도구에는 터미널이 없다(실측). 거부는 어디서나 된다. 범위를 좁히기만 하기 때문이다.
- 이 검사는 보안 경계가 아니다. 같은 사용자로 도는 에이전트는 가짜 터미널을 만들거나(`script`) 승인 파일을 직접 써서 넘을 수 있다. 막는 것은 에이전트가 습관처럼 또는 다른 세션의 지시를 따라 승인하는 경우까지다. xsm 검문의 다른 부분과 같은 한계다.
- 워커가 미리 허용받는 것은 `xsm send`(보고)뿐이다. 과제가 `xsm stop`이나 `xsm install`을 시켜도 승인을 거친다.
- 보고는 승인 없이 된다. Claude 워커는 xsm 실행 권한을 받고 뜬다. Codex 워커는 셸에서 `xsm send`로, 샌드박스에 막히면 MCP `xsm_send`로 보고한다.
- **깊이 제한(`max_depth`, 기본 1).** 최상위 세션 아래로 워커가 몇 단계까지 이어질 수 있는지다. 1이면 세션은 워커를 띄우고, 워커는 더 띄우지 못한다. 전역값은 `~/.xsm/config.json`의 `max_depth`나 환경변수 `XSM_MAX_DEPTH`로 정하고, 워커를 띄울 때 `--max-depth N`으로 그 워커 아래의 한도를 정한다. 워커는 물려받은 한도를 낮출 수만 있다. 한도는 워커 기록에 있으므로 워커가 환경변수를 바꿔서 늘릴 수 없다.
- **동시 워커 수(`max_workers`, 기본 4).** 한 세션이 동시에 돌릴 수 있는 워커 수다. config `max_workers`나 `XSM_MAX_WORKERS`로 정한다. 넘기면 돌고 있는 워커 이름과 함께 거부한다.
- **남은 워커 정리.** 워커를 띄운 세션이 끝나면(정상 종료, 흔적 없는 정지, 기록 삭제) 그 워커를 종료하고 기록을 지운다. Claude 부모가 정상 종료하면 SessionEnd 훅이 부모 프로세스가 사라지기를 기다렸다가 바로 정리한다(실측 2초 안). 그 밖의 경우는 `xsm workers`, `xsm spawn`, 매시간 정리 때 잡힌다. 부모가 정지한 워커면 그 아래 워커도 이어서 정리된다. 상태를 확인할 수 없는(unknown) 부모의 워커는 건드리지 않는다.
- **범위 밖 폴더의 워커.** 부모 세션과 범위가 다른 폴더에 워커를 띄우면, 그 워커는 그쪽 프로젝트의 세션들과 통하게 된다. 그래서 `xsm_grant`의 `outside_scope` 허가가 있어야 한다. 사람이 터미널에서 친 `spawn`은 예외다.
- **전체 권한과 훅 신뢰 우회.** `--full-access`는 샌드박스와 승인 없이 띄운다(Codex `--dangerously-bypass-approvals-and-sandbox`, Claude `bypassPermissions`). `--trust-hooks`는 Codex 워커를 `--dangerously-bypass-hook-trust`로 띄운다. 둘 다 **사용자의 명시적 허가**가 있어야 한다. 에이전트는 MCP 도구 `xsm_grant`로 이유와 함께 요청한다. 사용자가 양식에서 "allow once"를 고르면 1회용 허가 id가 나오고, `spawn … --grant <id>`에 쓴다. 허가는 요청한 세션, 런타임, 폴더, 옵션에 묶이고 10분 뒤 만료된다. 허가와 거절은 채널에 결정으로 남는다. 사람이 터미널에서 직접 치는 `spawn`은 그 자체가 허가라서 id가 필요 없다. `xsm workers`에는 `FULL-ACCESS`, `hooks-untrusted`로 표시된다.
- auto 모드 세션에서 `xsm_grant`가 분류기에 막히면, 위의 규칙대로 에이전트가 먼저 사용자에게 묻는다. xsm은 권한 설정을 바꾸지 않는다.
- `--once`면 과제의 답이 부모에게 도착하는 순간 부모 쪽 훅이 워커를 종료하고 기록을 지운다.
- **Codex 워커는 사용자 설정과 관계없이 샌드박스를 명시한다.** 패널 Codex TUI는 `-s workspace-write -a on-request`로 띄워 사용자의 YOLO 설정을 따르지 않고 패널에서 묻는다. 백그라운드 Codex TUI는 물을 사람이 없고 Codex에는 질문을 중계할 훅 이벤트가 없으므로 `-s workspace-write -a never`로 띄운다. 작업 폴더 안에서만 일한다.

## 종료된 세션

- 정상 종료한 세션은 `ended`, 흔적 없이 사라진 세션은 `stale`로 표시된다. 기본 목록에는 보이지 않고 `xsm list -a`에 나온다.
- **바로 지우기.** `xsm list clear`는 이 프로젝트의 멈춘 세션(`ended`, `stale`) 기록을 보존 기간을 기다리지 않고 지운다. `xsm list clear -a`는 모든 프로젝트의 기록을 지운다. 살아 있는 세션과 상태를 확인할 수 없는 세션은 남긴다. 지운 세션도 재개하면 같은 ref로 다시 등록된다.
- 한 Claude 프로세스에서 `/clear`나 재개로 세션 id가 바뀌면, 옛 id는 그 프로세스가 살아 있어도 `ended`(대체됨)로 본다. Claude의 네이티브 기록에 적힌 현재 세션 id와 비교해 판정한다.
- 멈춘 세션으로 보내면 거부되고, 재개 명령이 함께 나온다. `claude --resume <id>`로 재개하면 **같은 주소와 ref**로 돌아온다.
- 원장에 `queued`로 남았는데 대상이 멈췄으면 `undelivered`로 보인다.
- 정리는 자동이다. 멈춘 세션 포인터는 7일, 원장과 보류 본문은 30일 뒤 지워진다. 기간은 `config.json`의 `retention_days`, `ledger_retention_days`로 바꾼다. `xsm prune --dry-run`으로 미리 볼 수 있다.

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
