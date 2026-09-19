# 스파이크 S8 실험 결과: 범위 안 핸드셰이크와 신뢰 상태

- 실행일: 2026-09-20 01:37~01:55 (KST)
- 설계: `S8-handshake-plan.md`
- 관련 ADR: 0009
- 환경: macOS, Claude Code 2.1.278, Codex CLI 0.155.1, 원격 호스트 jaesol-macmini(SSH)
- 시제품(실험용, 제품 아님): `tools/mesh/`

| 파일 | 역할 |
|---|---|
| `common.py` | 정책, 범위 검사, 세션 신원(레지스트리·PID·`ps lstart`), 신뢰 관계 저장소, HMAC, SSH 호스트 키 지문 |
| `mesh_trust.py` | `establish` / `approve`(macOS 대화상자) / `revoke` / `list` |
| `mesh_send.py` | 관계 확인, 헤더와 HMAC, Claude 소켓 또는 Codex 큐 전송, 원격 pull(`--pull-from`) |
| `mesh_ups.py` | 수신 `SessionStart`·`UserPromptSubmit` 훅. Claude·Codex 공용. Codex 세션 등록, 검문, 보류 기록 |

- 실험 상태: `/tmp/xsm-spike/mesh/`(`policy.toml`, `trust/`, `held/`, `decisions.jsonl`, `seen/`)
- 원칙:
  - 전용 세션만 사용했다. Claude는 `--setting-sources project,local`과 `--settings`로 훅을 걸었고, Codex는 프로젝트 로컬 훅과 `--dangerously-bypass-hook-trust`를 썼다.
  - 사람의 승인은 사용자가 macOS 대화상자에서 직접 눌렀다(2회).
- 설정 파일 변경 확인: 실험 도중 `~/.claude*/settings.json`의 수정 시각이 01:52로 바뀌었다. 백업 `settings.json.pre-korean-hook-20260920-015233`과 비교한 결과, 차이는 `UserPromptSubmit`에 `inject-korean-reminder.sh` 훅이 추가된 것뿐이다. 사용자의 별도 작업이며 실험과 무관하다. `~/.codex/config.toml`은 바뀌지 않았다.

## 실험 구성

정책(`policy.toml`):
- `strict_peers = true`
- scope `xsm`: `trust = "auto"`. Claude `claude-4`의 `s8/in/**`와 Codex `codex`의 `.local/s8/in/**`
- scope `xsm-approve`: `trust = "approve"`, `remote = "approve"`. Claude `s8/appr/**`와 원격 `jaesol-macmini`

| 세션 | 런타임·위치 | 역할 |
|---|---|---|
| A, A2 | Claude, `s8/in/a`, `s8/in/a2` (xsm) | 발신자 |
| B | Claude, `s8/in/b` (xsm), 수신 훅 | 수신자 |
| C | Claude, `s8/out/c` (범위 밖), 수신 훅 | 범위 밖 수신자 |
| P | Claude, `s8/appr/p` (xsm-approve), 수신 훅 | 승인 모드·원격 수신자 |
| Q | Claude, `s8/appr/q` (xsm-approve) | 승인 모드 발신자 |
| X | Codex, `.local/s8/in/x` (xsm), 수신 훅 | 교차 런타임 |
| remote | jaesol-macmini의 파일 `/tmp/xsm-s8/outbox.txt` | 원격 발신자(SSH pull) |

발신은 이 조사 세션의 셸이 각 세션을 대신해 `mesh_send.py --as <세션>`으로 실행했다. 대상 세션은 모두 실제로 살아 있는 세션이다.

## 결과

| ID | 시나리오 | 관찰 | 판정 |
|---|---|---|---|
| S8-a | 범위 안 자동 수립: Claude A → Claude B | 핸드셰이크로 pair `975004df0bb4`가 자동 수립됐다(scope xsm, TTL 8h). B의 훅이 allow했다. B: "another Claude session named xsm-s8-a sent this, not my user" | 통과 |
| S8-a | 교차 런타임: Claude A2 → Codex X | pair `7511a6e5b52e` 자동 수립, `codex queue`로 전달, X의 훅이 allow. X: "xsm-s8-a2(kind=claude, mode=bypass)가 보냈습니다" | 통과 |
| S8-a | 교차 런타임: Codex X → Claude B | pair `d28081ea32d6` 수립, B의 훅이 allow. X의 표시 이름은 Codex 자동 제목 "Reply READY"였다 | 통과 |
| S8-b | 범위 밖: A → C (`mesh_send`) | 핸드셰이크 단계에서 "not in a common scope"로 거부. 전송 자체가 없었다 | 통과 |
| S8-b | 헤더 없는 피어 메시지를 C에 강제 주입 | C의 훅이 "unpaired peer message (strict_peers)"로 차단 | 통과 |
| S8-d | 오배송: A↔B pair의 **올바른 HMAC** 프레임을 C에 주입 | C의 훅이 "this session is not a party to the pair"로 차단. **S5-2의 조용한 오배송이 보이는 실패로 바뀌었다** | 통과 |
| S8-e | 수신자 재시작: B `/exit` 후 같은 이름으로 재시작 | 이전 pair 프레임 → "not a party (misrouted or restarted)"로 차단. `mesh_send`는 새 pair `f6c073def2ea`를 자동 수립해 allow | 통과 |
| S8-e | 발신자 강제 종료: Q kill -9 후 Q↔P pair로 전송 | P의 훅이 "sender session is gone or restarted"로 차단. **단, `mesh_send`는 발신자 생존을 확인하지 않고 보냈다**(아래 개선점) | 수신 측 통과 |
| S8-f | 위조 1: MAC 없이 헤더만 | "bad mac"으로 차단 | 통과 |
| S8-f | 위조 2: 신뢰 저장소를 읽은 프로세스가 올바른 MAC으로 A를 사칭 | **allow**. 같은 uid 프로세스는 신뢰 관계를 사칭할 수 있다 | 예측대로(경계 확인) |
| S8-f | 재전송: 같은 msg id | "replayed message id"로 차단 | 통과 |
| S8-g | Claude 네이티브 정책과 훅의 순서: 유효한 pair지만 봉투 `from-mode`만 불일치 | **네이티브 보류 창이 먼저 떴고 훅은 실행되지 않았다**(판정 기록 0건). 창에는 "from an unidentified session (peer claims name: xsm-s8-a)". Deny로 정리했다 | 확인 |
| S8-g | Claude 훅 `block`이 피어 메시지에 동작하는가 | 동작한다. `suppressOriginalPrompt`로 화면에는 "UserPromptSubmit operation blocked by hook: agent-mesh: …"만 표시되고 본문은 숨겨졌다 | 확인 |
| S8-h | 철회 | "pair is revoked"로 차단 | 통과 |
| S8-h | 철회 후 `auto` 재수립 | **결함 발견**: 철회한 pair가 다음 `establish`에서 조용히 다시 active가 됐다. 철회를 고정(sticky)으로 고친 뒤 재시험: `establish`와 `mesh_send` 모두 "pair was revoked; needs human approval"로 거부 | 수정 후 통과 |
| S8-h | 만료 | "pair expired"로 차단 | 통과 |
| S8-j | 정책 무해 수정(해시만 변경) | 범위를 재평가해 여전히 범위 안 → allow, 해시 갱신 | 통과 |
| S8-j | B를 범위에서 제외 | "out of scope after policy change"로 차단하고 pair를 자동 revoked 처리. **`mesh_send`는 재평가 없이 보냈다**(아래 개선점) | 수신 측 통과 |
| S8-c | 승인 모드: Q → P | 대기 상태에서 `mesh_send`는 "not-sent (needs human approval)", 강제 프레임은 "pair is pending"으로 차단. 사용자가 macOS 대화상자에서 Approve → 전송 → allow(`ACK S8-C`) | 통과 |
| S8-i | 원격 pair: jaesol-macmini → P | 수립 시 호스트 키 지문 `SHA256:MLhVDDFG…`를 고정했다(`known_hosts`의 ed25519 키와 일치). 사용자 승인 후 pull로 전달 → allow. P: "xsm-s8-remote@jaesol-macmini, a remote session on the jaesol-macmini host … verified on pair 57195a47b1a6" | 통과 |
| S8-i | 지문 변경(`peer_changed`) | 고정 지문을 바꾸자 `mesh_send`가 "peer_changed"로 거부(pinned와 current 지문을 함께 보고) | 통과 |
| 추가 | Codex에 헤더 없는 큐 메시지 | 훅이 "no mesh header"로 통과시켰고 모델이 처리했다. **Codex는 큐 메시지와 사람 입력을 구분할 수 없어 `strict_peers`를 적용할 수 없다** | 한계 확인 |

수신 훅 판정 합계(`decisions.jsonl`):

| 판정 | 건수 |
|---|---|
| allow | 8 |
| block | 10 — not a party 2, unpaired 1, bad mac 1, replay 1, revoked 1, expired 1, out of scope 1, pending 1, sender gone 1 |
| pass(헤더 없음) | 2 |

보류 기록(`held/`)은 10건으로 차단 건수와 같다. S8-g의 네이티브 보류는 훅까지 오지 않았으므로 이 합계에 없다.

## 발견한 설계 문제와 개선

| 문제 | 조치 |
|---|---|
| `auto` 모드가 철회를 조용히 되돌린다 | 시제품에서 수정: 철회는 고정이며 사람 승인(`approve` 대화상자)으로만 다시 활성화된다 |
| 발신 측이 정책 변경과 발신자 생존을 재확인하지 않는다 | 수신 측이 막았지만 이중 방어를 위해 `mesh_send`에도 재평가·생존 검사를 넣어야 한다(미수정) |
| 세션이 재시작되면 이전 pair가 active로 남는다 | 수신 측에서는 무효로 처리되지만 저장소 정리(당사자가 죽은 pair 폐기)가 필요하다(미수정) |
| Codex는 헤더 없는 큐 메시지를 구분하지 못한다 | Codex 쪽 강제력은 헤더가 붙은 메시지에만 미친다. 헤더 없는 전달을 막으려면 발신 경로 자체를 mesh 도구로 제한해야 한다(정책·교육 수준) |
| Claude 네이티브 보류가 훅보다 먼저다 | 발신 측이 `from-mode`를 정직하게 적어야 한다. 모드가 다르면 사람 보류가 먼저 걸린다(이중 구조) |
| Codex 표시 이름이 자동 제목("Reply READY")이다 | 이름 규칙(ADR-0008)에서 Codex 자동 제목을 이름 후보로 쓰지 않는 쪽이 낫다는 근거 |

## 해석

- **범위 안 핸드셰이크는 런타임 없이 동작한다.** 필요한 것은 정책 파일, 신뢰 저장소, 세션 훅(Claude `--settings`/설정, Codex 프로젝트 훅), 발신 도구뿐이다.
- **로컬 핸드셰이크는 보안이 아니라 동의·범위의 기록이다**(S8-f 위조 2). 그래도 다음을 보이는 실패로 바꾼다.
  - 오배송(S8-d)
  - 재시작한 세션(S8-e)
  - 범위 밖 통신(S8-b)
  - 정책 변경(S8-j)
  - 철회·만료(S8-h)
- **승인은 사람만**: macOS 대화상자 승인은 사람이 버튼을 눌러야 한다. 단, 에이전트가 화면 자동화(computer use 등)로 버튼을 누를 수 있다면 이 보장은 깨진다. 이번 실험에서는 시도하지 않았다. 실제 배포에서는 이런 도구를 막는 정책이 함께 필요하다.
- **원격**: 호스트 키 지문 고정과 pull(ADR-0007 D의 변형)로 원격 표시와 `peer_changed` 거부가 동작했다. S5의 소켓 포워딩 문제(오배송, 격리 무력화)를 피한다.

## 남긴 흔적

- 테스트 세션과 프로세스는 모두 종료했다. Claude 레지스트리에 테스트 레코드가 없다(kill -9로 남은 1건은 PID가 죽은 것을 확인하고 삭제했다).
- 삭제: 테스트 폴더 `.local/s8`, macmini의 `/tmp/xsm-s8`
- 남은 것:
  - `/tmp/xsm-spike/mesh/`(정책, 신뢰 저장소, 판정 로그, 보류 기록. 증거로 보존)
  - `~/.claude-4/projects/`의 테스트 대화 기록(`-private-tmp-xsm-spike-s8-*`)
  - `~/.codex`의 테스트 thread 1개(`01a0ba8d…`, 큐 대기 0건)
- 모델 사용량:
  - Claude: 짧은 턴 7회(S8-a, S8-e, S8-f2, S8-j1, X→B, S8-c, S8-i)
  - Codex: 3회(READY, S8-AX, unpaired)
  - 차단된 메시지는 모델 턴을 쓰지 않았다.
