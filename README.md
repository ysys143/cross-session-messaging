# Cross-Session Messaging

Claude Code와 Codex 에이전트 세션들이 서로를 인식하고 메시지를 주고받을 수 있도록 하는 메시징 시스템입니다.

## 개요

`cross-session-messaging` (XSM)은 다양한 환경에서 실행되는 Claude Code, Codex, 그 외 에이전트 런타임 간의 직접 통신을 가능하게 합니다.

### 주요 특징

- **세션 간 메시징**: 같은 머신이나 원격 서버의 서로 다른 세션에 메시지 송수신
- **설정 독립적**: 각 세션의 `CONFIG_HOME`(`~/.claude`, `~/.codex` 등)에 관계없이 통신 가능
- **오케스트레이션 기반**: 별도의 런타임 없이 기존 Claude Code/Codex에서 자연스럽게 동작
- **신뢰 기반**: 프로젝트 범위의 세션 그룹 정의 및 권한 관리
- **프로토콜 표준화**: JSON 기반 메시지 봉투와 인증 메커니즘

## 구조

```
├── xsm/                          # 메인 구현 (Python)
│   ├── cli.py                   # CLI 인터페이스
│   ├── registry.py              # 세션 레지스트리 관리
│   ├── send.py                  # 메시지 송신
│   ├── receive.py               # 메시지 수신
│   ├── envelope.py              # 메시지 형식
│   └── config.py                # 설정 관리
├── docs/
│   ├── list-agents-cross-session-messaging.md  # 에이전트 목록 및 메시징
│   ├── adr/                     # 아키텍처 결정 기록
│   ├── plan/                    # 구현 계획
│   └── references/              # 참조 자료
├── tools/mesh/                  # 원격 메싱 도구
├── tests/                       # 테스트 및 벡터
├── hooks/                       # Git/세션 훅
└── skills/                      # 에이전트 스킬
```

## 빠른 시작

### 설치

```bash
pip install -e .
```

### 세션 등록

```bash
xsm register --session-id my-session --config-home ~/.claude
```

### 메시지 송신

```bash
xsm send --to recipient-session --body "작업 요청"
```

### 메시지 수신

```bash
xsm receive --from sender-session
```

## 관측 (텔레메트리)

xsm은 자신의 송수신을 스팬과 메트릭으로 기록한다. OpenTelemetry SDK를 설치하지 않고
OTLP의 형식만 직접 만들기 때문에, 의존성은 여전히 stdlib뿐이면서 표준 백엔드와 붙는다
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
명령이 할 때만 일어난다. 송수신 경로에서 네트워크를 타는 일은 없다.

한 메시지의 전 구간(발신 → SSH → 수신 머신 → 대상 세션의 훅)이 하나의 trace로 이어지므로,
Jaeger나 Grafana Tempo에서 "이 메시지가 어디서 멈췄는지"를 그대로 볼 수 있다.

끄려면 `XSM_NO_TELEMETRY=1`. 계측 비용은 send 1회당 약 0.175ms로 측정됐다
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
Claude Code ↔ Codex ↔ Custom Runtime
```

### 3. 원격 서버 메싱
```
Local Machine → SSH → Remote Server (XSM enabled)
```

## 테스트

```bash
# 프로토콜 벡터 테스트
python -m pytest tests/

# 메시징 통합 테스트
xsm test --vectors tests/vectors.json
```

## 참고 자료

- [Buzz](https://github.com/block/buzz): 오픈소스 메시징 플랫폼
- [Orca](https://github.com/stablyai/orca): 에이전트 오케스트레이션 프레임워크
- [Agora](https://arxiv.org/abs/2609.18094): 멀티에이전트 시스템 연구

## 라이선스

MIT

## 피드백

이슈, 제안, PR을 환영합니다!
