# 스파이크 S8 설계: 범위 안 핸드셰이크와 신뢰 상태

- 상태: **실행 완료(2026-09-20)**. 결과는 `S8-results.md`
- 작성일: 2026-09-19
- 관련 ADR: 0009(세션 간 신뢰 수립), 0004(통신 범위), 0007(원격 신뢰), 0008(이름·주소)
- 목적: 사용자가 미리 정한 범위 안에서만 두 세션이 핸드셰이크로 신뢰 관계(pair)를 맺고, 그 뒤에만 메시지를 허용하는 방식이 **런타임 없이** 동작하는지 검증한다.

## 1. 왜 필요한가 (지금까지의 실측)

| 문제 | 근거 |
|---|---|
| 같은 uid면 누구든 inbox 소켓에 쓰고 `from-mode`·헤더를 위조할 수 있다 | S1, S6, T8 2.3절 |
| Claude의 보류 승인은 메시지마다 사람이 해야 하고, 에이전트는 대신 승인할 수 없다 | S1(분류기 거부) |
| Codex 훅 차단은 보류가 아니라 소멸이다 | S6 |
| 원격 `from` 경로가 받는 머신의 같은 경로로 오류 없이 오배송된다 | S5-2 |
| SSH 터널을 쓰면 `isolatePeerMachines`가 적용되지 않고, 수신 측이 보는 peer는 `sshd-session`이다 | S5-1, S5-3 |
| 강제 종료된 세션의 레코드가 남고, 재시작하면 PID와 세션이 바뀐다 | S3 |

메시지마다 판정하는 방식(Claude 모드 동등성, 헤더 검사)은 "누가 누구와 대화해도 되는가"를 기억하지 못한다. 핸드셰이크는 이것을 **한 번 확인하고 기록된 관계(pair)**로 만든다.

## 2. 위협 모델: 무엇을 막고 무엇을 막지 못하는가

| 막는 것 | 막지 못하는 것 |
|---|---|
| 범위 밖 세션과의 통신(다른 프로젝트, 다른 홈) | 같은 uid의 악의적 프로세스. 신뢰 저장소를 읽고 토큰을 쓸 수 있다 |
| 오배송: 답장 주소가 가리키는 엉뚱한 세션(S5-2의 미끼)은 pair가 없어 거부한다 | 이미 신뢰된 세션이 프롬프트 주입으로 악용되는 경우. pair 안에서는 허용된다 |
| 오래된 관계: 세션 재시작, PID 재사용, 강제 종료 후 남은 레코드 | 원격 머신 자체의 탈취(SSH 키 탈취) |
| 에이전트끼리 사람 모르게 관계를 넓히는 것. pair 수립은 정책이나 사람만 할 수 있다 | |
| 원격 메시지가 로컬로 위장되는 것. pair에 머신 정보와 SSH 호스트 키 지문을 고정한다 | |

같은 uid 안에서는 암호학적 비밀이 서로 다른 프로세스를 구별해 주지 못한다. 그래서 로컬 핸드셰이크의 목적은 **보안이 아니라 동의와 범위의 기록**이고, 암호학은 원격(머신 경계)에서만 의미가 있다. 이 구분을 실험에서 명시적으로 확인한다(S8-f).

## 3. 제안 구조 (실험 대상)

### 3.1 사용자 정책 (사전 설정)

위치는 CONFIG_DIR 밖의 `~/.agent-mesh/policy.toml`(가칭)이다. 사용자만 편집하고, 에이전트의 요청으로 수정하지 않는다.

```toml
[[scope]]
id = "xsm"                      # 범위 이름
members = [                     # 이 조건에 맞는 세션만 범위에 속한다
  { runtime = "claude", home = "claude-4", cwd = "~/Documents/GitHub/cross-session-messaging/**" },
  { runtime = "codex",  home = "codex",    cwd = "~/Documents/GitHub/cross-session-messaging/**" },
]
trust = "auto"                  # auto: 범위 안이면 자동 수립 / approve: 사람이 1회 승인
ttl = "8h"                      # 신뢰 관계 유효 시간
remote = "deny"                 # deny | approve (원격 머신 세션 허용 여부)
```

### 3.2 핸드셰이크 절차

1. **해석**: 발신 도구(가칭 `mesh send`)가 ADR-0008 규칙으로 대상 B를 (runtime, home, session id)로 확정한다.
2. **범위 검사**: 발신 A와 수신 B가 **같은 scope의 members 조건**에 맞는지 레지스트리 원본(Claude 레코드의 `cwd`, Codex 훅 기록의 `cwd`)으로 확인한다. 맞지 않으면 즉시 거부한다(메시지를 보내지 않는다).
3. **신뢰 수립**:
   - `trust = "auto"`: pair 레코드를 만든다.
   - `trust = "approve"`: 사람이 `mesh trust approve <pair>`로 1회 승인한 뒤에만 만든다.
   - pair 레코드(`~/.agent-mesh/trust/<pair-id>.json`, 0600): `{pair_id, scope, a:{runtime, home, session_id, pid, proc_start}, b:{…}, token, created, expires, approved_by, policy_hash, host_fingerprints?}`
4. **메시지**: 발신 도구가 본문 헤더에 `pair=<pair_id>`, `id`, `mac=HMAC(token, id‖body)`를 넣는다. Claude 수신에는 네이티브 봉투(`from-mode` 정직 기재)도 함께 쓴다.
5. **수신 검문**: 수신 세션의 `UserPromptSubmit` 훅(Claude, Codex 공통. 둘 다 `decision: "block"` 지원)이 다음을 모두 확인하고 통과하면 `additionalContext`를 붙인다.
   - pair가 존재하고 만료되지 않았다.
   - 수신자 자신이 pair의 한쪽과 일치한다(session id, pid, proc_start).
   - 상대 세션이 살아 있다(생존 판정).
   - 현재 정책 해시가 수립 당시와 같다. 다르면 재평가한다.
   - HMAC이 맞다.
6. **실패 처리**: 실패하면 `block`한다. 막힌 메시지는 훅이 보류 저장소(`~/.agent-mesh/held/`)에 기록하고, 사용자에게 알린다(S6에서 차단 메시지가 소멸함을 확인했으므로).
7. **철회와 만료**: `mesh trust revoke`, TTL 만료, 세션 종료(PID·proc_start 불일치)가 발생하면 이후 메시지를 차단하고 재핸드셰이크를 요구한다.
8. **원격**: pair에 상대 머신의 SSH 호스트 키 지문을 고정한다(Orca의 `peer_fingerprint` → `peer_changed` 방식과 같다). 전송은 ADR-0007 D(SSH 원격 실행)로 한다. 원격 pair는 정책이 `remote = "approve"`일 때만 허용한다.

### 3.3 Claude 네이티브 정책과의 관계

Claude는 네이티브 수신 판정(`crossSessionInbound`, 모드 동등성)을 **훅보다 먼저** 적용한다. 따라서 네이티브에서 보류된 메시지는 훅까지 오지 않는다. 실험에서 두 층의 순서와 조합을 확인한다(S8-g).

## 4. 실험 항목

공통 원칙: `docs/plan/README.md` 5장. 전용 세션을 쓰고, 사용자 설정은 건드리지 않는다. 정책 파일과 신뢰 저장소는 실험용 경로(`/tmp/xsm-spike/mesh/`)에 둔다. 훅은 Claude `--settings`, Codex 프로젝트 로컬 `.codex/hooks.json`으로 건다.

| ID | 시나리오 | 절차 | 판정 기준 |
|---|---|---|---|
| S8-a | 범위 안 자동 수립 | scope에 맞는 Claude A와 Codex B. `trust = "auto"`로 핸드셰이크 후 메시지 교환 | pair 생성, 양방향 메시지가 훅을 통과하고 경고 문맥이 붙음 |
| S8-b | 범위 밖 거부 | cwd가 다른 세션 C에 보내기 | 발신 단계에서 거부. 강제로 프레임을 넣어도 C의 훅이 차단하고 보류 저장소에 기록 |
| S8-c | 1회 승인 모드 | `trust = "approve"`. 승인 전 전송 → 차단. 사용자가 `mesh trust approve` 실행 후 재전송 → 통과 | 승인은 사람만 할 수 있음. 에이전트가 승인 명령을 실행하려 하면 분류기나 권한 규칙이 막는지 |
| S8-d | 오배송 방어 | S5-2의 순진한 주소 상황을 재현. 답장이 미끼(다른 세션)로 가게 함 | 미끼 세션의 훅이 pair 불일치로 차단. 조용한 오배송이 보이는 실패로 바뀜 |
| S8-e | 재시작·PID 재사용 | B를 종료 후 재시작(같은 이름), kill -9 후 남은 레코드 | 이전 pair로 보낸 메시지 차단. 재핸드셰이크 후 통과 |
| S8-f | 같은 uid 위조 경계 | (1) 토큰 없이 헤더만 위조 → 차단. (2) 신뢰 저장소를 읽어 올바른 MAC 생성 → 통과 | (2)가 통과함을 명시적으로 기록한다. 로컬 핸드셰이크가 보안이 아니라 동의 기록이라는 결론의 근거 |
| S8-g | Claude 네이티브 정책과 훅의 순서 | Claude B에서 `crossSessionInbound` 미설정/accept × 봉투 `from-mode` 일치/불일치 × pair 유효/무효 | 네이티브 보류가 먼저 걸리는지. 피어 메시지에 대한 훅 `block`과 `suppressOriginalPrompt`가 동작하는지 |
| S8-h | 철회와 만료 | `mesh trust revoke`, TTL을 1분으로 두고 만료 | 이후 메시지 차단. 이미 처리된 메시지와 대기 중 메시지(Codex 큐)의 처리 구분 |
| S8-i | 원격 pair | macmini의 발신자와 이 머신의 Claude B. pair에 macmini SSH 호스트 키 지문 고정. 호스트 키가 다른 호스트(또는 지문을 바꾼 기록)로 시도 | 지문 불일치 시 `peer_changed` 상당 거부. 원격 메시지에 원격 표시가 보존됨 |
| S8-j | 정책 변경 | pair 수립 후 policy에서 B를 범위 밖으로 변경 | 다음 메시지에서 정책 해시 불일치로 재평가·차단 |

## 5. 구현 준비물 (실험용 최소 시제품)

- `tools/mesh_trust.py`: 정책 로드, 범위 검사, pair 수립·승인·철회·조회
- `tools/mesh_send.py`: `mesh_resolve.py` + pair 헤더 + HMAC + 전송(Claude 소켓, Codex 큐, SSH 원격 실행)
- 수신 훅 `mesh_ups.py`: Claude와 Codex 공용. 검문, 보류 기록, `additionalContext`
- 실험 경로: `/tmp/xsm-spike/mesh/{policy.toml, trust/, held/}`

## 6. 결정이 필요한 것 (ADR-0009)

- 기본 신뢰 방식: 범위 안 자동(`auto`)을 기본으로 할지, 1회 승인(`approve`)을 기본으로 할지
- 신뢰 관계를 무엇에 묶을지: 세션 id만(재시작 시 유지), 세션 id + PID·proc_start(재시작 시 재수립)
- TTL 기본값
- 원격 pair 허용 여부와 기본값
