# xsm (Cross-Session Messaging)

[English](README.md) | 한국어

xsm은 실행 중인 Claude Code와 Codex 세션이 서로를 찾고 메시지를 주고받게 합니다.

![XSM이 런타임의 네이티브 전달 경로와 명시적 통신 범위로 에이전트 세션을 연결하는 구조](docs/assets/xsm-overview.svg)

## TL;DR

코딩 에이전트(Claude Code나 Codex)에게 아래 프롬프트를 주면, 에이전트가 두 런타임 모두에 xsm을 설치하고
결과를 검증합니다.

```
https://github.com/ysys143/xsm 의 xsm을 이 머신의 Claude Code와 Codex 양쪽에 설치해줘.
그다음 `xsm doctor`와 `xsm selftest`로 검증하고, Codex에서 훅 신뢰하기나 새 세션 시작하기처럼
내가 직접 해야 할 일이 남았으면 알려줘.
```

에이전트는 이 README를 따라 두 런타임에 설치하고, 무엇을 검증했는지 보고합니다. 코딩 에이전트는 `/plugin`
같은 슬래시 명령을 직접 입력하지 못하는 경우가 많습니다. 그때는 빠른 시작의 직접 설치(`xsm install`)를 씁니다.

설치가 끝나면 어느 쪽이든 새 세션을 열어 이렇게 물어보세요.

```
이거 어떻게 쓰는 거야? 실제 데모로 보여줘.
```

에이전트가 스킬의 참고 문서를 읽고 직접 `xsm`을 실행하므로, 명령을 미리 익힐 필요가 없습니다.
직접 설치하려면 아래를 이어서 읽으세요.

## 빠른 시작

### 1. 직접 설치

macOS와 Linux에서 동작합니다. Unix 소켓, `ps`, `/dev/tty`, tmux가 필요하므로 네이티브 Windows는 지원하지
않습니다. 의존성은 Python 표준 라이브러리뿐입니다. 설치란 각 세션이 xsm의 훅을 실행하게 만드는 일입니다.

**Claude Code 플러그인** (권장). 저장소 자체가 마켓플레이스입니다.

```
/plugin marketplace add ysys143/xsm
/plugin install xsm@xsm
```

플러그인에는 훅, 스킬, MCP 서버, `bin/`이 함께 들어 있습니다. `plugin.json`의 `version`이 오르면 갱신되고,
플러그인을 끄면 훅도 함께 꺼집니다.

**Codex 플러그인** (권장). 같은 저장소가 Codex 마켓플레이스이기도 합니다.

```bash
codex plugin marketplace add ysys143/xsm
codex plugin add xsm@xsm
```

플러그인에는 훅 두 개, 스킬, MCP 서버가 들어 있습니다. Codex는 플러그인의 훅을 신뢰한 뒤에만 실행하므로,
첫 세션에서 `/hooks`의 "Trust all and continue"를 고르세요.

<details>
<summary>Codex 플러그인 더 보기: PATH, 갱신, 이전 설치의 잔여물</summary>

- Codex는 플러그인의 `bin/`을 PATH에 넣지 않으므로, 세션이 시작될 때마다 `~/.local/bin/xsm`을 플러그인에
  링크합니다. 그 자리에 xsm 플러그인 폴더를 가리키지 않는 링크나 파일이 있으면 건드리지 않습니다.
- 갱신하려면 `codex plugin marketplace upgrade xsm`을 실행한 뒤 `codex plugin add xsm@xsm`을 다시 실행합니다.
- 이 홈에 예전에 `xsm install --codex-home`으로 설치한 적이 있다면, 플러그인을 추가한 뒤 `xsm install --refresh`를
  한 번 실행하세요. 그 설치가 남긴 훅 그룹, MCP 항목, 스킬 링크를 지웁니다. 남아 있으면 플러그인의 것과
  나란히 실행됩니다.

</details>

<details>
<summary>플러그인 없이: <code>xsm install</code></summary>

```bash
bin/xsm install --claude-home <my-claude-config-dir>          # 플러그인 대신 직접 설치할 때
bin/xsm install --claude-home <dir-A> --claude-home <dir-B>   # 여러 홈을 한 명령으로
bin/xsm install --codex-home <my-codex-home>                  # Codex 훅, 스킬, MCP
bin/xsm install --refresh                                     # 이미 설치한 모든 홈을 최신으로
bin/xsm doctor                                                # 설치 상태, 낡은 사본, 지금 막힌 것
```

`--claude-home`에는 Claude Code의 설정 디렉터리(`CLAUDE_CONFIG_DIR`)를, `--codex-home`에는 Codex의
설정 디렉터리(`CODEX_HOME`)를 넣습니다. 바꾸지 않았다면 각각 `~/.claude`, `~/.codex`입니다. 기본 홈은 없으므로
`--refresh`를 쓰는 경우를 빼면 홈을 하나 이상 적어야 합니다. 홈이 여러 개면 `--claude-home`을 반복하세요.

직접 설치한 사본은 저장소가 바뀌어도 따라가지 않습니다. `xsm doctor`가 낡았다고 알려 주면
`xsm install --refresh`로 갱신하세요. 플러그인을 갱신한 뒤에는 새 버전의 `bin/xsm`으로 `install --refresh`를
실행합니다.

</details>

<details>
<summary>주의: 훅 중복, PATH, Linux의 이름 충돌</summary>

- 한 홈(Claude든 Codex든)에 플러그인과 직접 설치가 함께 있으면 훅이 두 번 실행되어 위험합니다.
  `xsm install`은 그런 홈을 거부하며, `--force`를 주면 넘어갑니다.
- `install --codex-home`은 `bin/xsm`을 `~/.local/bin/xsm`에 자동으로 링크하므로, `~/.local/bin`이 PATH에
  있는지 확인하세요. 플러그인 없이 `--claude-home`만 지정했다면 `bin/xsm`을 PATH에 직접 링크해야 합니다.
  Claude 플러그인은 세션 안에서 PATH를 설정하고, Codex 플러그인은 위의 링크를 유지합니다.
- Linux에서는 X.Org의 세션 관리자도 이름이 `xsm`입니다(x11-session-utils 패키지). 설치되어 있다면
  `command -v xsm`으로 이쪽이 먼저 잡히는지 확인하세요.

</details>

### 2. 써 보기

따로 등록하지 않습니다. 훅이 설치된 세션은 시작할 때와 프롬프트를 낼 때마다 스스로 등록합니다.
등록이 곧 동의이므로, 훅을 실행한 적 없는 세션은 목록에 `unregistered`로만 보이고 주소로 쓸 수 없습니다
(ADR-0001, 세션 레지스트리).

세션 안에서는 명령을 스킬 `xsm`의 인자로 넘깁니다. Claude Code는 `/xsm`, Codex는 `$xsm`입니다.

```
/xsm list                            # Claude Code: 이 프로젝트에서 말을 걸 수 있는 세션들
$xsm list                            # Codex
/xsm who                             # 이 세션이 다른 세션에 어떻게 보이는지
/xsm send ref:a1b2c3 이것 좀 봐줘      # 대상 다음은 모두 메시지
```

터미널에서는 같은 명령을 `xsm list`, `xsm who`, `xsm send ...`로 씁니다. 인자 없이 부르면 스킬이 사용법 한 줄을
출력합니다. 말로 부탁해도("저쪽 세션에 물어봐") 에이전트가 스킬의 참고 문서를 읽고 직접 `xsm`을 실행합니다.
같은 이름의 개인 스킬이 있다면 플러그인 쪽은 `/xsm:xsm list`로 부릅니다.

전체 명령: `list`, `who`, `log`, `projects`, `doctor`, `send`, `link`, `join`, `leave`, `reach`.

## 사용 사례

### 1. 세션 이름 짓고 찾기

이름은 런타임이 가진 것이라 `xsm rename` 같은 명령은 없습니다. xsm은 조회할 때마다 런타임에서 이름을 읽으므로,
세션 자체의 이름을 바꾸면 xsm도 따라갑니다.

```bash
# Claude Code 세션에서
/rename my-reviewer                            # 세션 이름 변경

# 그 다음, 다른 세션에서
xsm list                                       # 이 프로젝트에서 말 걸 수 있는 세션들
xsm who                                        # 이 세션이 어떻게 보이는지
```

### 2. 다른 저장소 세션과 연결

```bash
/xsm link ~/src/other-repo                     # 어느 한쪽 세션에서 직접 입력 (Codex: $xsm link ...)하거나 에이전트에게 연결을 부탁
xsm projects                                   # 어떤 폴더들이 연결·참여되어 있는지
```

### 3. 메시지 보내고 답장 받기

```bash
xsm send my-reviewer --text "이것 좀 봐줘"     # 그냥 알림
xsm send my-reviewer --text "이 테스트 고쳐줘" --kind task --wait 15   # 일 시키고 답장 기다림
xsm ledger                                     # 전달 상태 확인
```

### 4. 워커에게 일 시키기

```bash
xsm spawn codex --task "이 테스트 고쳐줘"      # 띄우고, 지시하고, 답장으로 결과를 받음
xsm workers                                    # 워커 상태
```

### 5. 설치가 안 된 홈 진단

```bash
xsm doctor                                     # 어느 홈에 훅이 없는지, 낡은 사본, 지금 막힌 것
xsm selftest                                   # 훅이 실제로 도는지
```

## 참고

### 통신 범위

같은 Git 저장소 안의 세션끼리는 하위 폴더가 달라도 통신할 수 있습니다. Git 저장소 밖에서는 같은 폴더의
세션끼리 통신합니다. 다른 폴더와 연결하려면 어느 한쪽 세션의 에이전트에게 연결해 달라고 부탁하세요. 에이전트가
사용자에게 말로 묻고 `xsm link <폴더>`를 직접 실행하며, 사용자의 답이 동의입니다. `/xsm link <폴더>`를 직접
입력해도 됩니다. 한쪽에서만 하면 되고, 연결은 양방향이며 직접 풀 때까지 유지됩니다.

```bash
xsm link ~/src/other-repo            # 사용자가 허락한 뒤 에이전트가 실행하는 명령
/xsm link ~/src/other-repo           # 또는 세션에서 직접 입력 (Codex: $xsm link ...). 직접 입력했으므로 따로 묻지 않습니다
xsm unlink ~/src/other-repo          # 연결은 누구나 풀 수 있습니다
```

- `join`: 여러 폴더를 한 묶음으로 두려면 각 폴더의 세션에서 에이전트에게 `<이름>` 프로젝트에 가입해 달라고
  부탁합니다(`/xsm join <이름>`을 직접 입력해도 됩니다).
- `reach`: 세션 하나만 그 세션이 끝날 때까지 연결하려면 그 세션의 에이전트에게 그 폴더와 연결해 달라고
  부탁합니다(`/xsm reach <폴더>`를 직접 입력해도 됩니다).

### 메시지 송신

```bash
xsm send agent-name --text "이것 좀 봐줘"
xsm send agent-name --text "이 테스트 고쳐줘" --kind task --wait 15
xsm send ref:a1b2c3 --text "..."     # 이름이 겹칠 때는 ref로
xsm send agent@hostB --text "..."    # 다른 머신 (xsm remote add 후)
```

### 메시지 수신

받는 쪽은 아무것도 실행하지 않습니다. 훅이 게이트 역할을 해서 범위와 발신자를 확인한 뒤 메시지를
세션의 프롬프트로 직접 넣습니다. 거절된 메시지는 버려지지 않고 보관됩니다. Claude 자체의 세션 간 메시지
(`SendMessage`, xsm 헤더 없음)는 이 기계의 세션이 보냈다면 범위와 상관없이 그대로 들어갑니다. Claude 자체 게이트가
이미 판정했고, 보낸 쪽도 같은 기계의 사용자 자신이기 때문입니다. 이 기계 밖(Remote Control, 클라우드)에서 온 것은
보관됩니다. 범위는 xsm 자체 메시지에 적용되고, `xsm send`는 범위 밖 대상을 보내기 전에 거부합니다. linked worktree(Orca,
`claude --worktree`)와 메인 체크아웃은 같은 저장소로 봅니다. 두 폴더를 연결할 때는 에이전트가 사용자에게 말로 묻고
`xsm link <폴더>`를 직접 실행합니다. 사용자의 답은 판정으로 남습니다(`xsm_link` 도구는 승인 양식으로 묻습니다).
xsm 헤더가 없는 메시지를 모두 보관하려면 `~/.xsm/config.json`에
`"strict_peers": true`를 넣습니다(ADR-0013). Claude 자체 게이트가 먼저 판정하므로, 권한 모드가 달라 Claude가
보류한 메시지는 xsm까지 오지 않습니다.

```bash
xsm ledger                           # 최근 메시지와 전달 상태
xsm status <message-id>              # 한 건의 상태
xsm held                             # 이 머신이 거절하고 보관한 것
```

### 관측 (텔레메트리)

xsm은 자신의 송수신을 스팬과 메트릭으로 기록합니다. OpenTelemetry SDK를 설치하지 않고 OTLP 형식을 직접
만들기 때문에, 의존성은 여전히 표준 라이브러리뿐이면서 표준 백엔드와 연동됩니다(ADR-0011, 텔레메트리).

```bash
xsm metrics                 # 이 머신에 쌓인 호출 수, 에러, p95
xsm metrics --json
```

메시지 본문은 스팬에 넣지 않습니다. 끄려면 `XSM_NO_TELEMETRY=1`을 설정하세요.

<details>
<summary>보관 기간, collector로 내보내기, 계측 비용</summary>

스팬과 메트릭은 기본 7일간 보관합니다(`telemetry_retention_days`). 정리는 이미 내보낸 줄만, 파일 머리에서만
지우므로 아직 전송하지 않은 기록은 남습니다.

기록은 `$XSM_HOME/otel-spans.jsonl`과 `otel-metrics.jsonl`에 append됩니다. 전송은 별도 명령을 실행할 때만
일어나므로, 송수신 경로에서 네트워크를 쓰는 일은 없습니다.

```bash
# OTEL_EXPORTER_OTLP_ENDPOINT가 없으면 http://localhost:4318
xsm otlp-export --once
xsm otlp-export --follow --interval 5
```

실제 OpenTelemetry Collector(v0.161.0)로 검증했습니다.

```bash
docker run --rm -p 4318:4318 otel/opentelemetry-collector:latest
xsm otlp-export --once
```

한 메시지의 전 구간(발신, SSH, 수신 머신, 대상 세션의 훅)이 하나의 trace로 이어지므로, Jaeger나
Grafana Tempo에서 메시지가 어디서 멈췄는지 바로 볼 수 있습니다.

계측 비용은 send 1회당 약 0.175ms입니다
([telemetry-overhead.md](docs/references/telemetry-overhead.md)).

</details>

## 동작 방식

xsm은 메시지를 한 세션의 입력으로 직접 넣습니다. 서버도, 상주 프로세스도 없습니다.

```
Claude Code Session A -> xsm -> Claude Code Session B     같은 머신
Claude Code <-> Codex                                     서로 다른 CONFIG_HOME 사이에서도
Local Machine -> SSH -> Remote Server                     양쪽 다 xsm 설치 + `xsm remote add`
```

각 세션의 훅이 시작할 때와 프롬프트를 낼 때마다 자신을 등록합니다(등록이 곧 동의). 발신은 각 런타임이
이미 가진 수단, 곧 Claude 인박스 소켓과 `codex queue`로만 합니다. 수신 측 훅이 게이트가 되어 범위와 발신자를
확인한 뒤 세션의 프롬프트로 넣고, 거절한 메시지는 보관합니다.

원칙:

- **No wrapper runtime**: 별도 런타임 없이 기존 Claude Code와 Codex 세션 안에서 동작하며, 계속 띄워 둘 우리 쪽
  프로세스가 없습니다.
- **Project-scoped trust**: 누가 누구와 통신할 수 있는지를 프로젝트별로 정의합니다.
- **Session-independent**: 각 세션의 `CONFIG_HOME`(`~/.claude`, `~/.codex` 등)과 무관하게 동작합니다.
- **Async-first**: 메시지는 비동기라서 협업이 서로를 막지 않습니다.
- **Observable**: 스팬과 메트릭을 OTLP 형식으로 기록하며, SDK 의존성은 없습니다.

<details>
<summary>저장소 구조</summary>

```
├── .claude-plugin/               # 플러그인·마켓플레이스 매니페스트
├── hooks/hooks.json, .mcp.json   # 플러그인이 제공하는 훅과 MCP 서버
├── .codex-plugin/, .agents/plugins/   # Codex용 같은 매니페스트
├── hooks/codex-hooks.json, codex-mcp.json   # Codex 플러그인의 훅과 MCP 서버
├── bin/xsm                       # 런처 (PYTHONPATH 설정 후 python3 -m xsm)
├── xsm/                          # 구현 전체 (Python, stdlib만)
│   ├── cli.py                   # 서브커맨드 전부
│   ├── registry.py              # 세션 레지스트리 (훅이 기록, 조회 시 런타임에서 보강)
│   ├── send.py / receive.py     # 송신, 그리고 훅이 부르는 수신 게이트
│   ├── envelope.py              # 메시지 봉투와 헤더
│   ├── adapters.py              # 두 가지 네이티브 전달 경로 (UDS 소켓, codex queue)
│   ├── codex_daemon.py          # Esc로 멈춘 Codex에 넣은 대기열 항목을 데몬에 시작 요청
│   ├── config.py                # 프로젝트·범위·차단 설정
│   ├── remote.py                # 다른 머신 (양방향 SSH, ADR-0007)
│   ├── workers.py               # 워커 생성·승인·종료 (ADR-0010)
│   ├── channel.py / doc.py      # 채널 기록 (0005), 공동 문서 (0006)
│   ├── telemetry.py             # 스팬·메트릭 기록 (ADR-0011)
│   ├── otlp_export.py           # OTLP/HTTP+JSON 전송
│   ├── install.py               # 훅 설치·진단
│   └── mcp.py                   # MCP 서버
├── hooks/                        # 런타임이 부르는 훅 진입점
├── skills/xsm/                   # 스킬 하나: `/xsm <명령>`(Codex `$xsm <명령>`)과 참고 문서
├── docs/
│   ├── adr/                     # 아키텍처 결정 기록 (0001-0012)
│   ├── xsm/                     # 프로토콜·테스트 계획
│   ├── references/              # 조사 자료, 오버헤드 실측
│   ├── plan/ · spikes/ · reviews/
│   └── list-agents-cross-session-messaging.md
├── tests/                        # unittest, 벡터 포함
└── tools/                        # 스파이크·벤치마크 스크립트
```

</details>

## 주요 문서

- **[INTENT.md](INTENT.md)**: 프로젝트 목표 및 비전
- **[ADR (Architecture Decision Records)](docs/adr/)**: 설계 결정 기록
  - [Session Registry](docs/adr/0001-session-registry.md)
  - [Communication Scope](docs/adr/0004-communication-scope.md)
  - [Remote Transport & Trust](docs/adr/0007-remote-transport-and-trust.md)
- **[Protocol Spec](docs/xsm/)**: 메시지 프로토콜 명세
- **[Test Protocol](docs/xsm/TESTPLAN.md)**: 실사용 점검 절차 (개발자용 테스트는 여기)
- **[Agent Messaging Guide](docs/list-agents-cross-session-messaging.md)**: 에이전트 메시징 구현

## 라이선스

MIT

## 피드백

이슈, 제안, PR을 환영합니다.
