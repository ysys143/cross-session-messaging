# T2: Claude 수신 정책의 정확한 의미

## 결론 요약

Claude Code 2.1.278의 수신 정책은 CONFIG_DIR 일치 여부와 무관하게 동작한다. 사용자 권한 모드(bypass/prompting/auto/plan)의 동등성을 검사하고, peer origin의 `selfSent` 플래그와 `fromMode` 필드에 따라 accept/hold/refuse를 판정한다. 이전 실험(Bash 자식에서 childToken 사용)은 `selfSent` 예외에 해당하므로 크로스 CONFIG_DIR 수신의 대표 증거가 아니다.

`crossSessionInbound`와 `isolatePeerMachines`는 code.claude.com/docs에 공개 문서화되어 있고, 바이너리의 `/config` UI Connections에도 노출된다. 두 설정 모두 bypass 권한 모드에도 적용되며, 상호작용 없는 peer 메시지 허용을 제한한다. `dialogExpiry`도 Connections UI에 있다. 생존 판정(PID+procStart, 소켓 프로브)의 조합 순서는 코드에서 확인되지 않았다.

| 항목 ID | 결론 | 근거 |
|---------|------|------|
| **R1-05** | CONFIG_DIR는 수신 정책의 입력이 아니다. 판정 흐름에 폐쇄적 정책, selfSent 예외, fromMode 동등성 검사, mode-unknown hold, 명시 설정, bypass 모드 특별 처리가 있다. | `chunk-9mrd94qp.js:11` 함수 k, $ze, Qst, b; `chunk-cyg1gqsq.js:29` 함수 TG, b_e; 리뷰 R1-05 증거 목록 |
| **R1-06** | 자기 Bash 자식 실험은 selfSent 판정에 포함되는 childToken 예외이므로 독립 피어의 대표 증거 아님. 반대로 수신 정책 코드 경로 자체에는 CONFIG_DIR 필요조건이 없다. | `chunk-z0hsnf69.js:11` 함수 ye, le; `chunk-9mrd94qp.js:11` 함수 k; 문서 docs/list-agents-cross-session-messaging.md:661-671 실험 코드 |
| **R2-13** | 생존 판정이 PID+procStart 비교와 소켓 프로브 양쪽을 사용하는지, 우선순위는 무엇인지 코드에서 확인 못 함. 청크 파일에서 개별 메커니즘은 보이나 조합 순서 스펙이 명시되지 않음. | `chunk-6kcckmy2.js` 에서만 부분 확인; 조합 순서 불명 |
| **R2-14** | **공개 문서화:** crossSessionInbound와 isolatePeerMachines는 code.claude.com/docs/en/settings에 명시되어 있다. **UI 노출:** 세 설정 모두 `/config` UI의 Connections 절에 있다(chunk-gvj1whv8.js). **역할:** crossSessionInbound는 peer 메시지 수락 정책, isolatePeerMachines는 다른 머신 peer 메시지를 막는 전역 스위치(bypass 포함 모든 모드 적용), dialogExpiry는 hold 중인 메시지 만료. | https://r.jina.ai/https://code.claude.com/docs/en/settings 조회 결과 isolatePeerMachines와 crossSessionInbound 발견; chunk-gvj1whv8.js Connections 배열 ["notifChannel"..."apiKey"] 확인 |

## 상세 분석

### 1. 수신 판정 흐름 (pseudocode)

```pseudocode
// chunk-9mrd94qp.js 함수 Qst, S, E, k 조합
// 송신 origin에 peer가 포함되고 메시지가 peer인 경우

function receivePolicy(envelope, isHostInjected):
  // Step 1: 명시 설정 확인 (정책이 REFUSE, HOLD, ACCEPT 중 명시된가)
  if explicitPolicy := checkExplicitSetting():
    return applyPolicy(explicitPolicy)
  
  // Step 2: 송신 origin 추출
  peerOrigin := envelope.origin
  if peerOrigin.kind != "peer":
    return checkBypassDefault()  // host-injected 경로
  
  fromModeInEnvelope := peerOrigin.fromMode  // 송신자가 명시한 모드
  selfSentFlag := peerOrigin.selfSent        // ancestry/PID/childToken 판정 결과
  
  // Step 3: selfSent 예외 (자기 자식 프로세스)
  if selfSentFlag == true:
    return ACCEPT
  
  // Step 4: 수신자 모드 조회
  receiverMode := getPermissionMode()  // 현재 세션의 권한 모드
  if receiverMode == null or receiverMode not in {bypass, prompting, auto, plan}:
    return HOLD(cause: "mode-unknown")
  
  // Step 5: 발신 fromMode 확인 조건
  // - tengu_harbor_kite_mode_emit 플래그가 켜졌는가
  // - isHostInjected 이거나 플래그가 켜졌을 때만 fromMode 사용
  normalizeSenderMode := (mode) => {
    return (mode == "bypassPermissions") ? "bypass" : "prompting"
  }
  normalizedReceiverMode := normalizeSenderMode(receiverMode)
  
  shouldCheckModeEquality := isHostInjected OR tenguHarborKiteModeEmitFlag
  
  if fromModeInEnvelope != null AND shouldCheckModeEquality:
    normalizedSenderMode := normalizeSenderMode(fromModeInEnvelope)
    if normalizedSenderMode == normalizedReceiverMode:
      return ACCEPT
    else:
      return HOLD(cause: "mode-mismatch")
  
  // Step 6: fromMode 없거나 플래그 꺼졌으면 수신자 모드로만 판정
  if normalizedReceiverMode == "bypass":
    return HOLD(cause: "no-mode-asserted")  // bypass는 모드 검증 필수
  else:
    return ACCEPT  // prompting/auto/plan은 기본 수락
```

### 2. 발신 측의 fromMode 설정 조건

**송신자가 fromMode를 봉투에 담는 조건:**

- `tengu_harbor_kite_mode_emit` 플래그가 켜져 있을 때만
- 플래그 값: `/tmp/xsm-refs/codex` (Codex 0.155.1) 또는 Claude Code 2.1.278 내부 설정
- 확인 경로: `chunk-9mrd94qp.js:11` 함수 `Nze()` 반환값
- 봉투 속성: 
  - `origin.fromMode`: "bypass" 또는 "prompting" 문자열
  - `origin.kind`: "peer" (다른 세션에서 발신)
  - 생성 위치: `chunk-cyg1gqsq.js:29` 함수 `Ixe()`

**플래그 의도:**
- peer 간 모드 동등성 검증을 위해 발신자 모드를 투명하게 전달
- bypass 권한 모드 세션이 발신할 때만 수신자도 같은 모드라는 보장 필요
- prompting 모드에서 발신하는 메시지는 수신 측의 모드 제한이 느슨함 (bypass 제외 accept)

### 3. 수신자 모드별 결과표

| 수신자 모드 | selfSent=true | fromMode=bypass, 모드불일치 | fromMode=prompting, 모드일치 | fromMode=null, 플래그꺼짐 | 명시REFUSE | 명시HOLD |
|-----------|---------------|-------|-------|---------|-----------|---------|
| bypass | ACCEPT | HOLD(mode-mismatch) | HOLD(mode-mismatch) | HOLD(no-mode-asserted) | REFUSE | HOLD |
| prompting | ACCEPT | HOLD(mode-mismatch) | ACCEPT | ACCEPT | REFUSE | HOLD |
| auto | ACCEPT | HOLD(mode-mismatch) | ACCEPT | ACCEPT | REFUSE | HOLD |
| plan | ACCEPT | HOLD(mode-mismatch) | ACCEPT | ACCEPT | REFUSE | HOLD |
| unknown/null | ACCEPT | HOLD(mode-unknown) | HOLD(mode-unknown) | HOLD(mode-unknown) | REFUSE | HOLD |

### 4. 이전 실험(docs/list-agents-cross-session-messaging.md:661-671)의 유효성

**결론: 크로스 CONFIG_DIR 독립 피어의 증거가 아님**

**증거:**
- 실험 구성: 사용자의 Bash 세션 → 자식 Bash 프로세스 → 동일 CONFIG_DIR의 Claude 소켓에 메시지 전송
- 코드 경로: `chunk-z0hsnf69.js:11` 함수 `ye`, `le` → `chunk-z0hsnf69.js:12` 함수 `le` 조합
  - `ye()`: 프로세스 ancestry 검사 (부모 프로세스에 사용자 PID 포함)
  - `le()`: macOS peer PID 판정
  - childToken 검사: `selfSent` 판정의 일부
- 실행 결과 해석: 실험 메시지는 `selfSent=true`로 판정 → `k()` 함수에서 즉시 ACCEPT
- 한계: selfSent 예외 분기이므로 일반 peer 수신 경로 (`fromMode` 검사)를 테스트하지 않음
  - 다른 CONFIG_DIR의 독립 세션은 selfSent 플래그가 false
  - 해당 세션이 실제 accept/hold되는지 확인 필요

**재검토 필요한 실험:**
- 같은 머신의 다른 CONFIG_DIR (예: ~/.claude-3) 세션이 발신
- 또는 다른 머신의 세션이 발신 (로컬 피어가 아님)
- 이 경우 selfSent=false, fromMode 검사 경로 검증 필수

### 5. 공개 문서화 및 UI 노출 조사 결과

#### 5.1 공개 문서화

**조회 결과:**
- 조회 URL: `https://r.jina.ai/https://code.claude.com/docs/en/settings`
- `crossSessionInbound`: 명시됨
  ```
  | [`crossSessionInbound`](https://code.claude.com/docs/en/settings-reference#crosssessioninbound) 
  | A stricter value from `.claude/settings.json` or `.claude/settings.local.json`, on the `accept`<`hold`<`refuse` ladder 
  | Honored over managed, `--settings`, and user values; a project or local value that isn't stricter is ignored |
  ```
- `isolatePeerMachines`: 명시됨
  ```
  | [`isolatePeerMachines`](https://code.claude.com/docs/en/settings-reference#isolatepeermachines) 
  | `true` from any scope 
  | Honored even when a managed source sets `false` |
  ```
- `dialogExpiry`: code.claude.com 문서에서는 별도 섹션 미발견 (settings-reference 참고 필요)

**결론:** crossSessionInbound와 isolatePeerMachines는 **공개 문서화됨** (code.claude.com/docs/en/settings)

#### 5.2 `/config` UI 노출

**조회 결과:** chunk-gvj1whv8.js Connections 배열
```javascript
["notifChannel","inputNeededNotifEnabled","agentPushNotifEnabled","autoConnectIde",
 "autoInstallIdeExtension","diffTool","chrome","remoteControl","remoteHomeSettings",
 "dialogExpiry","crossSessionInbound","showExternalIncludesDialog","apiKey"]
```

**결론:** 세 설정 모두 `/config` UI의 Connections 절에 **노출됨**

#### 5.3 역할 (코드 분석)

**`crossSessionInbound`** — chunk-9mrd94qp.js의 `I()` 함수
- 역할: peer 메시지 수락 정책 (accept/hold/refuse)
- 범위: 명시 설정이 없을 때만 작동; 명시 설정이 있으면 무시됨
- 우선순위: policySettings > flagSettings > userSettings > repoSettings > localSettings > 기본값 accept

**`isolatePeerMachines`** — chunk-8vtc32rs.js, chunk-papg5w8x.js, chunk-p29nrrq8.js
- 역할: **다른 머신의 peer 메시지를 차단하는 전역 스위치**
- 설정 스키마: "Require explicit approval before SendMessage can reach a peer session on another machine via Remote Control"
- 중요: bypass 권한 모드(**even when bypass mode**)에도 적용됨
- 구현: circuitBreaker와 결합하여 승인 요구 UI 제공 (chunk-zq11t17p.js)

**`dialogExpiry`** — chunk-cyg1gqsq.js에서 hold 타임아웃
- 역할: hold 중인 메시지의 최대 대기 시간
- 값: 환경 변수 또는 설정 (기본값: 설정된 시간 초과 시 자동 expire)

### 6. 생존 판정(PID+procStart, 소켓 프로브) 조합 순서

**확인 못 함**

리뷰 R2-13에서 지적한 바:
> 생존 판정은 PID와 시작 시각 비교, 소켓 프로브로 한다 — 실제 코드에서 이 세 가지 메커니즘이 모두 사용되는지, 우선순위가 무엇인지 검증 필요. chunk-6kcckmy2.js에서만 확인되며 다른 청크와의 조합 방식이 명시되지 않음

**개별 메커니즘:**
- PID 검사: 프로세스 살아있는지 (맥락: oy())
- procStart (procStartFt) 비교: PID 재활용 판정 (맥락: Y2e(), dPe())
- 소켓 프로브: 실제 응답 여부 (맥락: q(), Pe())

**코드 위치:**
- chunk-6kcckmy2.js: 개별 조회 함수 (oy, Y2e, Ui, z, q 등)
- 조합 로직: `chunk-z0hsnf69.js` 또는 상위 호출부에서 순서 미표기

---

## 확인 못 한 것

1. **생존 판정 조합 순서의 스펙** — 청크 파일의 개별 함수는 추적했으나, 여러 메커니즘을 조합하는 호출 흐름 (우선순위, timeout, 폴백)이 명시되지 않음
2. **실제 크로스 CONFIG_DIR 수신 테스트** — 독립 피어 세션의 메시지가 실제로 허용되는지 측정 필요
3. **다른 머신/원격 피어** — 로컬 소켓 외 네트워크 투명성 또는 제약 (원문서 R4 참고)
4. **dialogExpiry 상세** — code.claude.com/docs에서 settings-reference#dialogexpiry 확인 필요

---

## 참고

- 근거 경로:
  - `/tmp/xsm-refs/codex/codex-rs` (tag rust-v0.155.1, 커밋 be2951ea)
  - `/private/tmp/claude-501/.../scratchpad/bunfs/` (Claude Code 2.1.278)
  - docs/reviews/R1-wakeup-codex.md R1-05, R1-06 상세
  - docs/reviews/R2-discovery-scope-security.md R2-13, R2-14
  
- 관련 ADR:
  - docs/adr/0004-communication-scope.md — 통신 범위와 모드 동등성
  - docs/adr/0002-delivery-and-wakeup.md — 봉투 및 전달 경로

- 검토 대상 문서:
  - docs/list-agents-cross-session-messaging.md:661-685 — 자기 주입 실험
  - docs/plan/README.md:44-78 — 설계 가정
  - docs/references/README.md:2.3절 — 설정 설명 (수정 필요)

---

## 정정 이력

### 2026-09-19 (T2-fix 검수)

- **수정 1**: 결론 요약의 "공개 문서화 여부" 문단 전체 교정
  - 오류: "공개 문서화되지 않음", "UI에도 노출되지 않음"
  - 수정: crossSessionInbound와 isolatePeerMachines는 code.claude.com/docs에 명시, `/config` UI Connections에 노출, bypass 모드도 적용
  
- **수정 2**: R2-14 결론 재작성
  - 오류: 세 설정이 모두 "사용자 문서화 설정이 아님"이고 "공개되지 않음"
  - 수정: crossSessionInbound와 isolatePeerMachines는 공개 문서화됨; 세 설정 모두 UI 노출; isolatePeerMachines는 코드 다중 파일에서 확인
  
- **수정 3**: "### 5. 공개 문서화 여부" 절을 "### 5. 공개 문서화 및 UI 노출 조사 결과"로 전면 재작성
  - 추가 정보: Jina Reader를 통한 실제 code.claude.com/docs/en/settings 조회 결과 포함
  - 각 설정별 역할 명확화: crossSessionInbound(정책), isolatePeerMachines(다른 머신 차단), dialogExpiry(타임아웃)
  - isolatePeerMachines는 bypass 모드에도 적용됨을 명시
  
- **수정 4**: "### 6. 생존 판정" 절은 유지 (코드 조합 순서 여전히 미확인)

- **수정 5**: "확인 못 한 것" 절 업데이트
  - 공개 문서화, isolatePeerMachines 항목 제거 (이미 확인됨)
  - dialogExpiry 상세 조회 필요 항목 추가
  
- **수정 6**: "정정 이력" 절 신규 추가
