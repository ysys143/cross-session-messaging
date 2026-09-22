# ADR-0011: SDK 없이 관측 가능하게 만들기

- 상태: Proposed
- 관련 목표: G2, G3, C1
- 작성일: 2026-09-22

## 질문

xsm의 송수신을 관측 가능하게 만들되, 의존성 제로 원칙을 지킬 것인가 깰 것인가.

## 맥락

ADR-0001~0010이 모두 Accepted되고 두 머신 SSH까지 검증된 시점에서, 남은 문제는
"동작하지만 관찰할 수 없다"는 것이다.

이미 있는 것: `paths.append_jsonl`이 쓰는 `decisions.jsonl`. 무엇을 결정했는지는 남지만
**얼마나 걸렸는지**와 **무엇이 무엇으로 이어졌는지**는 남지 않는다. "메시지가 안 온다"는
신고에 답할 수 없고, 워커가 승인 대기로 몇 분을 앉아 있었는지 알 수 없다.

알려진 사실:

- 이 저장소에는 `pyproject.toml`도 `setup.py`도 없다. `bin/xsm`은 `PYTHONPATH`만 설정하고
  `python3 -m xsm`을 실행하며, 코드는 stdlib만 쓴다. (`README.md:43`의 `pip install -e .`는
  뒷받침하는 파일이 없는 죽은 안내문이다.)
- 훅은 죽으면 게이트가 열린다(ADR-0002, `receive.py` 모듈 docstring). 수신 경로에 새로
  실패할 수 있는 것을 넣는 일은 비용이 크다.
- xsm은 상주 프로세스를 두지 않는다(ADR-0003). 메트릭을 메모리에 누적할 주체가 없다.
- 표준 관측 도구(Grafana, Jaeger, Prometheus)와 붙으려면 OTLP를 말해야 한다.

## 선택지

### A. OTel SDK를 의존성으로 추가

- 방식: `pyproject.toml`을 만들고 `opentelemetry-sdk`, exporter를 install_requires에 넣는다.
- 장점: 표준 구현. 배치, 재시도, 샘플링, 컨텍스트 전파가 다 들어 있다.
- 단점: 의존성 제로가 깨진다. `bin/xsm`이 더 이상 PYTHONPATH만으로 돌지 않고, 훅이 도는
  모든 환경(샌드박스된 Codex, 원격 머신의 forced command 포함)에 패키지가 설치돼 있어야
  한다. 설치가 안 된 곳에서 훅이 import 에러로 죽으면 게이트가 열린다.

### B. stdlib로 OTLP wire format을 직접 만든다

- 방식: trace_id/span_id를 W3C 규격(16/8바이트)대로 `os.urandom`으로 만들고, span과 metric을
  로컬 JSONL에 append한다(기존 감사 로그와 같은 방식). 별도 명령 `xsm otlp-export`가 그
  JSONL을 OTLP/HTTP+JSON 페이로드로 변환해 `OTEL_EXPORTER_OTLP_ENDPOINT`에 POST한다.
- 장점: 의존성 제로 유지. 송수신 경로는 파일 append만 하므로 새로 실패할 것이 거의 없다.
  collector가 없어도 `xsm metrics`로 로컬에서 볼 수 있다. 표준 백엔드와 그대로 호환된다.
- 단점: 배치/재시도/샘플링을 직접 짜야 한다. OTLP JSON 인코딩의 세부(특히 id 표현)를
  스펙이 아니라 구현체 동작에 맞춰야 할 수 있다.

### C. 로컬 JSONL만, 표준 없이

- 방식: 자체 포맷으로만 기록하고 내보내기는 하지 않는다.
- 장점: 가장 단순하다.
- 단점: Grafana/Jaeger에 붙이려면 나중에 변환기를 어차피 써야 한다. 그때 trace_id 규격을
  안 지켜놨으면 소급이 불가능하다.

### D. 상주 collector 데몬을 xsm이 띄운다

- 방식: xsm이 백그라운드 프로세스를 하나 두고 거기서 배치 전송한다.
- 장점: 실시간에 가깝다.
- 단점: ADR-0003 위반. "우리 것을 계속 띄워두지 않는다"가 이 프로젝트의 전제다.

## 근거

- 측정: `docs/references/telemetry-overhead.md` — B안 구현의 실측 오버헤드는 send 1회당
  0.175ms(로컬 send의 1.75%). 비용은 사실상 `append_jsonl` 하나(전체의 95%)다.
- 기존 관례: `paths.append_jsonl`의 "Never raises: audit must not break a hook" 계약이
  이미 있고, B안은 그것을 그대로 물려받는다.
- id 인코딩(확정됨): OTLP 스펙이 명시적으로 예외를 둔다. `opentelemetry-proto`의
  `docs/specification.md`:

  > The `traceId` and `spanId` byte arrays are represented as case-insensitive
  > hex-encoded strings; they are **not** base64-encoded as is defined in the
  > standard Protobuf JSON Mapping.

  즉 구현체의 관례가 아니라 스펙 자체가 hex를 요구한다. B안의 유일한 미검증 지점이었고,
  1차 출처로 해소됐다.

- 실물 검증(2026-09-22): `otel/opentelemetry-collector:latest`(v0.161.0)를 4318에 띄우고
  `xsm otlp-export --once`를 실행. traces/metrics 모두 2xx, collector의 debug exporter가
  다음을 그대로 디코딩했다.

  - trace/span id를 hex 그대로 인식, `xsm.send`를 루트로 `xsm.remote.ssh`와 `xsm.deliver`가
    자식으로 붙은 트리
  - span kind 숫자 → `Producer` / `Client` / `Consumer` / `Internal`
  - `Status code: Error` + `Status message: out of scope: different project`
  - `DataType: Sum`, `IsMonotonic: true`, `AggregationTemporality: Delta`
  - `DataType: Histogram`, `Unit: ms`, Count/Sum/Min

  즉 B안이 만든 페이로드는 SDK가 만든 것과 구별되지 않는다.

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 0 | Claude | 사실 수집 | 위 "맥락"의 네 가지 사실과 오버헤드 실측 | — |
| 1 | 사용자 | "stdlib으로 otel 표준에 맞출 수 있는 거 아님?" | OTLP는 wire format 표준이지 SDK 표준이 아니다 | 이것이 B안의 출발점. A안의 "표준을 쓰려면 SDK가 필요하다"는 전제를 깬다 |

(라운드 2 이후는 사람과 다른 세션이 이어서 채운다.)

## 결정

(Accepted 이후 작성)

미해결로 남은 것:

1. `otel-spans.jsonl`의 보존 기간. `decisions.jsonl`처럼 무한 append인지, 로테이션을 둘지.
   `housekeeping.maybe_prune()`이 이미 있으므로 거기 얹는 것이 자연스럽지만 별도 결정 사항이다.
2. span attributes에 메시지 본문 일부를 넣을지. 디버깅 편의 대 로그 부피·민감정보.
   현재는 넣지 않는다(id, kind, ref, 결과 상태만).
