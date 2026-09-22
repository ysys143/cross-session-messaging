# Cross-Session Messaging

Claude Code와 Codex 에이전트 세션들이 서로를 인식하고 메시지를 주고받을 수 있도록 하는 메시징 시스템입니다.

## 개요

`cross-session-messaging` (XSM)은 다양한 환경에서 실행되는 Claude Code, Codex, 그 외 에이전트 런타임 간의 직접 통신을 가능하게 합니다.

### 주요 특징

- **세션 간 메시징**: 같은 머신이나 원격 서버의 서로 다른 세션에 메시지 송수신
- **설정 독립적**: 각 세션의 `CONFIG_HOME`(`~/.claude`, `~/.codex` 등)에 관계없이 통신 가능
- **오케스트레이션 기반**: 별도의 런타임 없이 기존 Claude Code/Codex에서 자연스럽게 동작
- **신뢰 기반**: 프로젝트 범위의 세션 그룹 정의 및 권한 관리
- **런타임 자체 경로 사용**: 전달은 각 런타임이 이미 가진 수단(Claude의 인박스 소켓,
  `codex queue`)으로만 하므로, 계속 띄워둬야 할 우리 쪽 프로세스가 없음
- **관측 가능**: 스팬·메트릭을 OTLP 형식으로 기록하며, SDK 의존성은 없음

## 구조

```
├── .claude-plugin/               # 플러그인·마켓플레이스 매니페스트, 플러그인용 명령 사본
├── hooks/hooks.json, .mcp.json   # 플러그인이 제공하는 훅과 MCP 서버
├── bin/xsm                       # 런처 (PYTHONPATH 설정 후 python3 -m xsm)
├── xsm/                          # 구현 전체 (Python, stdlib만)
│   ├── cli.py                   # 서브커맨드 전부
│   ├── registry.py              # 세션 레지스트리 (훅이 기록, 조회 시 런타임에서 보강)
│   ├── send.py / receive.py     # 송신, 그리고 훅이 부르는 수신 게이트
│   ├── envelope.py              # 메시지 봉투와 헤더
│   ├── adapters.py              # 두 가지 네이티브 전달 경로 (UDS 소켓, codex queue)
│   ├── config.py                # 프로젝트·범위·차단 설정
│   ├── remote.py                # 다른 머신 (양방향 SSH, ADR-0007)
│   ├── workers.py               # 워커 생성·승인·종료 (ADR-0010)
│   ├── channel.py / doc.py      # 채널 기록 (0005), 공동 문서 (0006)
│   ├── telemetry.py             # 스팬·메트릭 기록 (ADR-0011)
│   ├── otlp_export.py           # OTLP/HTTP+JSON 전송
│   ├── install.py               # 훅 설치·진단
│   └── mcp.py                   # MCP 서버
├── hooks/                        # 런타임이 부르는 훅 진입점
├── commands/                     # 슬래시 명령
├── docs/
│   ├── adr/                     # 아키텍처 결정 기록 (0001–0011)
│   ├── xsm/                     # 프로토콜·테스트 계획
│   ├── references/              # 조사 자료, 오버헤드 실측
│   ├── plan/ · spikes/ · reviews/
│   └── list-agents-cross-session-messaging.md
├── tests/                        # unittest, 벡터 포함
└── tools/                        # 스파이크·벤치마크 스크립트
```

## 빠른 시작

### 설치

의존성은 stdlib뿐입니다. 설치란 각 세션이 훅을 돌리게 만드는 일이고, 두 가지 길이 있습니다.

**Claude Code: 플러그인** (권장). 저장소 자체가 마켓플레이스입니다.

```
/plugin marketplace add jaesolshin/cross-session-messaging
/plugin install xsm@xsm
```

훅·명령·스킬·MCP 서버·`bin/`이 함께 들어오고, `plugin.json`의 `version`이 오를 때 갱신됩니다.
명령은 `/xsm:list`, `/xsm:who`처럼 플러그인 이름이 앞에 붙습니다. 플러그인을 끄면 훅도 함께 꺼집니다.

**Codex, 그리고 플러그인을 쓰지 않는 Claude 홈: `xsm install`.**

```bash
bin/xsm install --codex-home ~/.codex          # Codex 훅·스킬·MCP
bin/xsm install --claude-home ~/.claude-2      # 플러그인 대신 직접 설치할 때
bin/xsm install --refresh                      # 이미 설치한 모든 홈을 최신으로
bin/xsm doctor                                 # 설치 상태, 낡은 사본, 지금 막힌 것
```

한 Claude 홈에 플러그인과 직접 설치가 같이 있으면 훅이 두 번 돌아 위험합니다. `xsm install`은 그런 홈을
거부합니다(`--force`로 넘길 수 있음). 직접 설치한 사본은 저장소가 바뀌어도 자동으로 따라가지 않으므로,
`xsm doctor`가 낡았다고 알려 주면 `xsm install --refresh`로 갱신합니다.

`~/.local/bin` 등 PATH에 `bin/xsm`을 링크해두면 이후 `xsm`으로 부를 수 있습니다(플러그인으로 설치하면
세션 안에서는 자동으로 PATH에 들어갑니다).

### 세션 등록

따로 등록하지 않습니다. 훅이 설치된 세션은 시작할 때와 프롬프트를 낼 때 스스로 등록합니다.
등록이 곧 동의이므로, 훅을 돌린 적 없는 세션은 목록에 `unregistered`로만 보이고
주소로 쓸 수 없습니다 (ADR-0001).

```bash
xsm who                              # 이 세션이 누구로 보이는지
xsm list                             # 이 프로젝트에서 말을 걸 수 있는 세션들
```

### 통신 범위

같은 폴더의 세션끼리는 그냥 통합니다. 다른 폴더까지 묶으려면 양쪽이 같은 이름으로 참여해야 합니다.

```bash
xsm join my-project                  # 양쪽에서 각각 실행
xsm projects                         # 어떤 폴더들이 묶여 있는지
```

### 메시지 송신

```bash
xsm send agent-name --text "이것 좀 봐줘"
xsm send agent-name --text "이 테스트 고쳐줘" --kind task --wait 15
xsm send ref:a1b2c3 --text "..."     # 이름이 겹칠 때는 ref로
xsm send agent@hostB --text "..."    # 다른 머신 (xsm remote add 후)
```

### 메시지 수신

받는 쪽은 아무것도 실행하지 않습니다. 훅이 게이트 역할을 해서 범위와 발신자를 확인한 뒤
메시지를 세션의 프롬프트로 직접 넣습니다. 거절된 메시지는 버려지지 않고 보관됩니다.

```bash
xsm ledger                           # 최근 메시지와 전달 상태
xsm status <message-id>              # 한 건의 상태
xsm held                             # 이 머신이 거절하고 보관한 것
```

## 관측 (텔레메트리)

xsm은 자신의 송수신을 스팬과 메트릭으로 기록합니다. OpenTelemetry SDK를 설치하지 않고
OTLP의 형식만 직접 만들기 때문에, 의존성은 여전히 stdlib뿐이면서 표준 백엔드와 붙습니다
(ADR-0011).

```bash
xsm metrics                 # 이 머신에 쌓인 호출 수, 에러, p95
xsm metrics --json
```

collector로 보내려면:

```bash
# OTEL_EXPORTER_OTLP_ENDPOINT 가 없으면 http://localhost:4318
xsm otlp-export --once
xsm otlp-export --follow --interval 5
```

기록은 `$XSM_HOME/otel-spans.jsonl`과 `otel-metrics.jsonl`에 append되고, 전송은 별도
명령이 할 때만 일어납니다. 송수신 경로에서 네트워크를 타는 일은 없습니다.

실제 OpenTelemetry Collector(v0.161.0)로 검증했습니다:

```bash
docker run --rm -p 4318:4318 otel/opentelemetry-collector:latest
xsm otlp-export --once
```

한 메시지의 전 구간(발신 → SSH → 수신 머신 → 대상 세션의 훅)이 하나의 trace로 이어지므로,
Jaeger나 Grafana Tempo에서 "이 메시지가 어디서 멈췄는지"를 그대로 볼 수 있습니다.

끄려면 `XSM_NO_TELEMETRY=1`. 계측 비용은 send 1회당 약 0.175ms로 측정됐습니다
([telemetry-overhead.md](docs/references/telemetry-overhead.md)).

## 설계 원칙

- **No Wrapper Runtime**: Claude Code/Codex 위에 별도의 오케스트레이션 런타임을 두지 않음
- **Project-Scoped Trust**: 프로젝트별로 통신 권한 범위를 정의 가능
- **Session-Independent**: `CONFIG_HOME` 설정에 영향을 받지 않는 세션 발견
- **Async-First**: 비동기 메시징으로 블로킹 없는 협업 지원

## 주요 문서

- **[INTENT.md](INTENT.md)**: 프로젝트 목표 및 비전
- **[ADR (Architecture Decision Records)](docs/adr/)**: 설계 결정 기록
  - [Session Registry](docs/adr/0001-session-registry.md)
  - [Communication Scope](docs/adr/0004-communication-scope.md)
  - [Remote Transport & Trust](docs/adr/0007-remote-transport-and-trust.md)
- **[Protocol Spec](docs/xsm/)**: 메시지 프로토콜 명세
- **[Agent Messaging Guide](docs/list-agents-cross-session-messaging.md)**: 에이전트 메시징 구현

## 사용 사례

### 1. 로컬 세션 간 협업
```
Claude Code Session A → XSM → Claude Code Session B
```

### 2. 크로스 런타임 메시징
```
Claude Code ↔ Codex        (서로 다른 CONFIG_DIR 사이에서도)
```

### 3. 원격 서버 메싱
```
Local Machine → SSH → Remote Server (양쪽 다 xsm 설치, xsm remote add 로 페어링)
```

### 4. 워커에게 일 시키기
```
xsm spawn codex --task "이 테스트 고쳐줘"    # 띄우고, 지시하고, 결과를 답장으로 받음
```

## 테스트

```bash
python3 -m unittest discover -s tests -v      # 전체 (임시 XSM_HOME 위에서 실행됨)
python3 -m unittest tests.test_remote         # 두 머신 (가짜 ssh로 한 박스에서)

XSM_NO_TELEMETRY=1 python3 -m unittest discover -s tests   # 계측을 꺼도 결과는 같아야 함
```

설치된 실제 환경을 점검하려면:

```bash
xsm selftest                                  # 훅이 실제로 도는지
xsm doctor                                    # 설치 상태, 보류 건수, 한도
```

## 참고 자료

- [Buzz](https://github.com/block/buzz): 오픈소스 메시징 플랫폼
- [Orca](https://github.com/stablyai/orca): 에이전트 오케스트레이션 프레임워크
- [Agora](https://arxiv.org/abs/2609.18094): 멀티에이전트 시스템 연구

## 라이선스

MIT

## 피드백

이슈, 제안, PR을 환영합니다!
