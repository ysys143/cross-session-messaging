# ADR-0007: 원격 통신과 신뢰 모델

- 상태: Deferred
- 관련 목표: G4
- 작성일: 2026-09-19

## 질문

SSH로 연결된 다른 머신의 세션과 통신할 때, 어떤 전송 경로와 인증을 쓸 것인가?

## 맥락

- Claude의 로컬 inbox는 같은 uid를 신뢰 경계로 삼는다. 원격에는 그대로 확장할 수 없다.
- Claude의 원격 경로(bridge)는 Anthropic API의 `/v1/sessions/{id}/events`를 경유하고 OAuth 인증을 쓴다(같은 문서 11절).
- Codex는 `--remote ws://|wss://|unix://`와 `--remote-auth-token-env`를 지원한다(`docs/plan/README.md` 2.2절).
- Orca federation은 SSH가 아니다. 페어링된 WebSocket 위에서 공개키 유도 공유키와 기기 토큰을 쓰고, dispatch에 공개키 지문과 pairing revision을 고정해 불일치를 `peer_changed`로 거부한다(`docs/references/supplement/T4-remote-transport.md` 1절).
- Codex `--remote`는 TUI를 원격 app-server에 붙이는 옵션이다. 기존 세션을 원격에서 깨우는 경로는 아니다(T4 3절).
- SSH 포워딩(선택지 A)은 Claude 수신 측의 PID·uid 검증과 `from=uds:` 회신 주소를 그대로 보존하지 못할 수 있다. S5로 확인한다(T4 5절).

- **실측(S5 루프백, `docs/spikes/S4-S5-results.md`)**:
  - SSH 소켓 포워딩(`ssh -L`)을 거치면 수신 측이 보는 peer가 `sshd-session` 프로세스로 바뀐다. 원래 발신자의 PID와 신원은 사라진다.
  - Claude inbox는 포워딩된 메시지를 수락했지만, **로컬 uds 피어로 취급해 `isolatePeerMachines` 원격 승인이 적용되지 않는다.**
  - `ssh 호스트 "codex queue …"`(원격 실행)로 원격 Codex 세션을 깨웠고, 수신 훅의 발신 표시도 동작했다. 소켓 포워딩 없이 SSH 키 인증만으로 된다.
  - **실제 원격(jaesol-macmini)**: 봉투 `from`에 원격 머신의 `uds:` 경로를 그대로 적으면, 답장이 **받는 머신의 같은 경로에 있는 다른 프로세스로 오류 없이 오배송**된다. 수신 머신 기준으로 매핑한 경로를 적으면 정확히 도착한다(반대 방향도 매핑 필요). `isolatePeerMachines: true`인 세션도 `uds:` 경로의 원격 답장을 승인 없이 보냈다.
  - Orca 재페어링과 NAT는 아직 확인하지 않았다.
- **비교 실측(S9 E5, agent-comms)**: tailnet 주소에 연 listener로 `mesh_connect` → `mesh_accept` → DM 왕복이 동작했다. 그러나 연결 요청의 `fingerprint`가 비어 있어 승인자가 대조할 값이 없었고, 승인 action은 에이전트 도구에도 있다. 또 코디네이터는 설정 없이 제3자 허브 `wss://mesh.exadev.io/`에 접속하고, 열어 둔 listener 주소를 허브를 통해 광고한다.

## 선택지

### A. SSH 소켓 포워딩

- 방식: `ssh -L`/`-R`로 원격 유닉스 소켓을 로컬에 연결한다
- 장점: SSH 인증을 그대로 사용, 새 서버 없음
- 단점: 연결 관리를 수동으로 해야 함

### D. SSH 원격 실행 (원격 머신에서 로컬 전달)

- 방식: `ssh 호스트 '<원격의 발신 도구> …'`로 원격 머신에서 로컬 전달(Claude inbox 쓰기, `codex queue`)을 실행한다. 소켓을 포워딩하지 않는다
- 장점: 새 서버가 없다. SSH 인증을 그대로 쓴다. Codex는 S5-6에서 실측으로 동작했다. 원격 머신 안에서는 로컬 규칙(생존 판정, 발신 표시 훅)이 그대로 적용된다
- 단점: 원격 머신에도 발신 도구와 레지스트리가 있어야 한다. 원격임을 알리는 표시는 규약으로 붙여야 한다(`from-name="이름@호스트"` 등). 회신은 반대 방향 SSH가 필요하다
- 실측과의 관계: 소켓 포워딩(A)에서 확인된 오배송과 격리 무력화를 피할 수 있다. 원격 메시지의 `from`을 비우고 회신 방법을 본문 규약으로 적기 때문이다. macOS에서는 원격 머신의 Claude 인증이 로그인 키체인에 묶여 비대화식 SSH에서 새 세션을 띄울 수 없다(수신 대상은 이미 실행 중인 세션이어야 한다)

### B. 인증된 WebSocket 릴레이

- 방식: 토큰 인증 ws 릴레이가 머신 간 메시지를 중계한다
- 장점: 여러 머신으로 확장이 쉬움
- 단점: 상주 서버가 필요(ADR-0003과 연동)

### C. 에이전트 벤더 원격 경로

- 방식: Claude bridge, Codex remote를 에이전트별로 쓴다
- 장점: 공식 경로
- 단점: Claude↔Codex 교차 통신은 불가

## 근거

- 레퍼런스: `docs/references/README.md` 2.4절
- 스파이크: `docs/plan/README.md` 5장

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 3-사용자 | 사용자 | v0.1 범위를 발견·전송·수신 표시로 정했다 | 2026-09-20 | 이 ADR은 그 범위 밖이다 |
| 1-보충(S9) | 코디네이터 | D(SSH 원격 실행) 유지. 원격 전송은 사용자가 명시적으로 켠 경로만 쓰고, 기본값으로 외부 서비스에 접속하지 않는다. 원격 승인에는 대조 가능한 지문(SSH 호스트 키)을 보인다 | `docs/spikes/S9-results.md` E5 | 허브 중계는 NAT 너머 연결에 유용하지만, 사용자 범위 밖 제3자에 장치 ID·IP·광고 주소가 노출된다 |

## 결정

**보류한다**(사용자 결정 2026-09-20: v0.1은 발견·전송·수신 표시만 한다).

다시 열 조건: 다른 기계의 세션과 협업할 일이 생길 때. 그때는 ADR-0003의 기준(의존 없음, 기본값은 로컬)을 먼저 적용한다.
