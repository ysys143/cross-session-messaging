# ADR-0011: SDK 없이 관측 가능하게 만들기

- 상태: Accepted (2026-09-23)
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
| 2 | Claude | B안 채택을 제안 | 실물 collector 검증과 오버헤드 0.175ms. 남은 두 미해결은 아래에서 하루치 실측으로 답한다 | 없음 |
| 3 | 사용자 | 확정 요청("1") | — | 상태를 Accepted로 올린다 |

## 결정

**B를 채택한다. stdlib만으로 OTLP wire format을 직접 만든다.** 의존성 제로는 이 프로젝트의 전제이고
(ADR-0003), OTLP는 SDK 표준이 아니라 전송 형식 표준이므로 둘은 충돌하지 않는다. 표준을 말하는 대가는
send 1회당 0.175ms(로컬 send의 1.75%)이고, 그 비용의 95%는 이미 쓰고 있던 `append_jsonl`이다. 실물
collector(v0.161.0)가 우리 페이로드를 SDK의 것과 구별하지 못했다.

구현은 `xsm/telemetry.py`(기록)와 `xsm/otlp_export.py`(전송)이고, 쓰는 쪽은 훅·CLI·send·receive다.
기록은 절대 예외를 올리지 않는다. 감사 로그가 훅을 깨뜨리지 않는다는 `append_jsonl`의 계약을 그대로
물려받는다. `XSM_NO_TELEMETRY`로 끌 수 있다.

**이것이 없었다면 오늘 못 찾았을 것들**(2026-09-23 실측): 샌드박스가 `kill(pid, 0)`을 EPERM으로
막아 모든 피어가 죽은 것으로 보이던 문제는 `xsm.state.kill_eperm` 카운터로 드러났고, Codex 샌드박스가
인박스 소켓 연결을 거부해 살아 있는 동료가 `stale`로 보이던 문제는 `xsm.state.socket_dead`로 드러났다.
두 경우 모두 "메시지가 안 온다"는 증상만으로는 원인이 보이지 않았다.

### 미해결이었던 것 둘

1. **보존 기간: 기본 7일.** `config.json`의 `telemetry_retention_days`이고 `0`이면 무한 보관이다.
   정리는 기존 `housekeeping.maybe_prune()`(한 시간에 한 번)에 얹는다. 근거는 하루치 실측이다.
   워커와 파일럿을 종일 돌린 2026-09-22~23의 12시간 반 동안 span 836줄 340kB, metric 177줄 27kB가
   쌓였다. 무거운 날 기준으로도 일주일이 수 MB다. 세션 포인터(7일)와 같은 창이고 원장(30일)보다 짧은데,
   span은 "그때 무슨 일이 있었나"를 사고 직후에 보는 것이고 "그 메시지가 도착했나"는 원장이 답하기
   때문이다.

   **정리는 머리에서만, 이미 내보낸 줄만 지운다.** `xsm otlp-export`는 어디까지 읽었는지를 줄 번호로
   기억한다(`otlp-cursor.json`). 머리에서 지운 만큼 커서를 내리고, 커서가 아직 닿지 않은 줄은 아무리
   오래됐어도 남긴다. 그러지 않으면 아직 보내지 않은 span이 조용히 건너뛰어진다.

2. **메시지 본문은 넣지 않는다.** span attributes는 id, kind, ref, 결과 상태, 소요 시간까지다. 이유는
   부피가 아니라 경계다. span은 남의 collector로 나가지만 원장은 이 기계에 남는다. 본문이 필요하면
   `xsm ledger`의 200자 미리보기와 `xsm held show`가 각자의 보존 규칙 아래 답한다. 메시지 id가 둘을
   잇는다.
