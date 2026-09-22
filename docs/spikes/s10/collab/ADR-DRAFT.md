# ADR-0012: 여러 세션이 한 문제를 나눠 풀 때, 다음에 무엇을 할지는 누가 정하는가

- 상태: Proposed
- 관련 목표: G3, G6, G7, C1
- 작성일: 2026-09-22

## 질문

여러 세션이 한 문서 그래프를 함께 키울 때, 다음에 확장할 노드를 **누가, 무엇을 보고** 정하는가 —
그리고 그 결정은 메시징 채널을 지나는가.

## 맥락

INTENT "추가2"의 요구는 한 문장이다: "Swarm Agent, Graph Engineering에 **이런 메시징 채널을**
어떻게 쓸 수 있을지 고민을 확장." 주어가 메시징 채널이다. 이 ADR은 그 점을 끝까지 붙든다.

전제가 한 번 무너진 주제다. `T7-fix`(`docs/references/supplement/T7-g8-swarm-graph.md:322`)에 따르면
INTENT가 근거로 건 X 게시물은 "135개 블록, 'swarm' 0회, 'message(ing)' 0회"로 멀티에이전트를 다루지
않고, 8계층 GraphRAG는 "단일 Kimi K3 모델이 각 프롬프트를 순차 실행"(`:272`)하는 파이프라인이다.
**"Agora/swarm graph"라는 이름으로 확정된 모델은 없다.** 그래서 이 ADR은 조사 노트가 아니라 논문
원문(arXiv:2609.18094v2)을 근거로 쓴다. 노트에서 "확인불가"였던 것 대부분이 원문에 있었고, 노트의
해석 하나("메시지 채널 없음")는 원문에 없는 것으로 확정돼 정정했다(`docs/references/agora.md`
v2 정정 표시).

### 이미 있는 것

- `xsm/doc.py`의 `add()`는 다중 부모를 받는다 — **이미 DAG**이며, 부모가 기존 노드여야 하고 id가
  부모를 포함한 내용 해시라 사이클이 구조적으로 불가능하다(`doc.py:80-96`). `leaves()`는 frontier,
  `unverified()`는 미검증 **가설** 큐(`:122-125`). `TAGS`에 agora 예약 태그 8종이 그대로 있다.
- 그중 `wip`은 태그로 허용될 뿐 **어느 뷰도 다루지 않는다.** agora에서 `wip`의 역할은 "Visible"
  (보이게 해서 중복을 줄임)인데, xsm에는 그 보이게 하는 뷰가 없다.
- `xsm/workers.py`는 `parent_ref` 단일 필드(`:392`)로 **트리**만 안다. 깊이·동시성 게이트, `once`,
  `_auto_reply`는 있으나 그래프를 모른다.
- 빠진 것은 그 사이다: 다음에 무엇을 할지 고르는 근거(뷰), 누가 무엇을 하는 중인지 보이게 하기,
  그것을 채널에 싣는 규약.

### C1의 실제 경계

ADR-0003이 금지하는 것은 **터미널을 쥐는 상주 진입점**이다(orca 위반 사유 `0003:14`). 명시적으로
허용한 것: 일회성 CLI, "일이 끝나면 사라지는 보조 프로세스"(`0003:63-64`). `xsm spawn`은 워커를
띄우고 Accepted됐다(ADR-0010). 결정 기준 넷(`0003:73-76`): 핵심 경로가 데몬·코디네이터·외부 서비스
없이 동작 / 런타임 자체 수단만 / 사라지는 보조 프로세스 허용 / 기본값 로컬.

**따라서 "DAG를 읽어 후보를 출력만 하는 일회성 명령"은 C1을 위반하지 않는다.** C1이 가르는 것은
"상주하는가"이지 "배정하는가"가 아니다. 배정의 문제는 다른 축 — T7 R-G8-04(선택 자율성) — 에 있다.
T7 §6이 "동적 토폴로지 생성이 숨겨진 코디네이터를 필요로 할 가능성"을 스스로 적었고, 이 ADR이 그
토론을 여는 자리다.

### agora — 시스템과 조정 방식을 나눈다

| | agora 시스템 (§3.4) | agora 조정 방식 (§3.1, §4.2, §4.3) |
|---|---|---|
| 무엇 | Go 서비스 + bare repo + SQLite + 26 HTTP 라우트 + Next.js + bearer 인증 + Docker | "no assigned tasks or central planner"; "Nothing in the prompt or brief names a method, assigns a role, or ranks the participants"; 루프 `analyze → pick a parent → run locally → publish → analyze again` |
| C1 | **위반**(기준 1·4) | **양립** |
| 공유하지 않는 것 (의도) | — | "share **no conversation**, manager, role graph, runtime, or filesystem"(§2) |

이식 대상은 오른쪽 열이다. 즉시 통신 부재는 결함이 아니라 설계다. Related Work(§2)는 agora를
blackboard 계보(Hayes-Roth 1985)에 두되 "explicit control policy"가 없는 것으로 구별한다.

### 식 2 — 있고, 임베딩 없이 계산된다 (§3.2)

```
S(u) = Σ_{v : (u,v)∈E}  1[a(u) ≠ a(v)] · w(v)
```

u를 부모로 삼은 **다른 계정**의 자식들의 태그 가중치 합(Table 2: setup/result/insight/hypothesis/
report +5, verification +20/+10/−20, endorsed 0, wip 0). `doc.py`의 `children()`, `author` 문자열,
태그로 바로 계산된다.

### 식 3 — 임베딩이 필요한 항은 ρ(v) 하나 (§3.3)

```
U(v) = 100·Q(v) + C·sqrt(log(N+1)/(n(v)+1)) + 100·D/sqrt(1+ρ(v))
```

Q(v) 품질 백분위(식 2 기반), n(v) "follow-on work on v out of N overall", ρ(v) "near-duplicate
descriptions" 수(임베딩 single-link 클러스터, cosine 0.90). C는 "grows when the metric distribution
is tightly bunched near its best". **D의 정의와 C의 수치는 원문에도 없다.** 3슬롯: exploit /
explore known / explore novel. **arm은 에이전트가 아니라 노드다** — 조정이 아니라 선택 문제로
풀려 있다.

### 중복 — agora의 대응은 채널이 아니라 가시성이었다

- Table 1 "Frontier visibility: Workers guess what is open and duplicate the same branch → Leaf,
  hypothesis, verification, cluster, and metric-landscape **views**."
- Table 2 `wip`: "In-flight work, **to reduce duplication**. Visible, but does not propagate score."
- §4.6: "Of 696 pairs of different accounts posting identical scores, 63% are within an hour of each
  other and 80% within six." 결론: "**Shared visibility supported reuse, but did not by itself
  sustain broad exploration.**"
- §4.7: May 2, 활동의 1/3이 한 클러스터에 몰리고 리더보드가 정체하자 클러스터링·다양성 요약·UCB를
  배포. "Workers immediately began using the new views." 다음날 00:13 첫 SSM 편집.

즉 **agora는 `wip`을 갖고도 696쌍이 났다.** "보이게 하기"만으로 충분하다는 근거는 원문에 없다.

### 자발적 조정 관례 (§4.3)

"workers state the parent and its score, the single change made, a predicted outcome band, the
measured result, and named follow-ups for others. ... **The brief required reproducible contributions
but did not prescribe prediction bands or named follow-ups.**" description이 비동기 채널 역할을
했고 에이전트들이 스스로 관례를 만들었다. T7/OpenAI의 "primitive만 주면 토폴로지가 자발 형성"
주장의 부분 실증. 단 그것만으로는 5일간 한 lineage에 정체했다.

### 실증의 무게

13 worker(Claude Code/Opus 4.7 ×5, Codex/GPT-5.5 ×8) + setup + 저자 3건 = 17 계정, 1,703 기여,
11일 19시간, 165 재현·실패 0. **그러나 §4.5: "Those 18 scored contributions account for about 98%
of the total reduction. The remaining 1,106 found the next 0.03."** 초록 마지막 문장: "Measuring
the effect on discovery per unit of compute requires a matched comparison." 저자 스스로 효과를
주장하지 않는다. 1,703은 스웜이 *돌았다*는 증거이지 *효과가 있었다*는 증거가 아니다.

### 관련 ADR

0003·0006·0010 Accepted. **0011 Proposed** — 구현(`telemetry.py`)은 있으나 미확정이며, 아래 S10이
그 계측에 의존한다. 0006 미해결 행(`0006:88`) "통합자가 없는 경우. 워커가 아닌 피어끼리는 부모가
없다". 0006 Round 4-1 제안 G는 claim을 **채널 레코드**로 두었다 — 이 ADR의 B1이 그것을 되살린다.

## 선택지

각 안에 두 표시를 붙인다. **[채널]** INTENT가 물은 메시징 채널을 실제로 쓰는가. **[상주]** 상주
프로세스가 필요한가(C1).

### A. 선택 뷰 (agora §3.3 이식) — [채널 아니오] [상주 아니오]

- 방식: `doc.py`에 식 2 점수와 뷰(most built-on, underexplored)를 추가하고 `xsm doc next`가 U(v)
  상위 후보를 3슬롯으로 **제안**한다. 고르는 것은 세션. 배정 없음.
- 장점: 식 2·n·N은 그래프에서 바로 나온다. agora §4.7이 뷰 도입 → 하루 내 새 방향 탐색을 기록.
- 단점: ρ(v) 하나만 근사가 필요하다(태그·부모 집합·본문 토큰 겹침). C·D 상수는 원문에도 없어
  튜닝 대상. **채널을 쓰지 않는다** — INTENT의 질문에 직접 답하지 않는다.

### B. `wip`을 보이게 만든다 — 세 크기

agora의 중복 대응 자체가 `wip`이고 `doc.py`에 태그는 이미 있다. 없는 것은 **뷰**다.

- **B0 뷰만** — [채널 아니오] [상주 아니오]: `leaves()`/`log`에서 `wip` 자식이 달린 노드를 표시. 코드 몇 줄.
- **B1 채널 레코드** — [채널 **예**] [상주 아니오]: `wip` 노드를 추가할 때 `channel.post(tag="claim")`도
  남긴다(0006 제안 G). 사람이 읽고, 다른 세션이 채널에서 본다.
- **B2 만료 있는 claim** — [채널 아니오] [상주 아니오]: 별도 파일 + TTL(moai-adk 10분). agora는
  만료를 두지 않았다.
- 장점: 작다. A·C′·D 어느 것과도 결합.
- 단점: agora가 `wip`을 갖고도 696쌍이 났다. B만으로 충분하다는 근거는 없다.

### C. 배정 DAG (orca / Appendix C "Central planner") — [채널 아니오] [상주 **예**]

- 방식: 노드에 `deps`·상태를 주고 디스패처가 위상 정렬해 워커에 민다.
- 장점: 예측 가능, fan-in/join 공짜, orca에서 검증.
- 단점: 디스패처가 워커를 계속 감시하므로 **상주** → C1 위반. 배정이므로 R-G8-04 위반. 저자들도
  이를 비교군으로만 뒀다.

### C′. 일회성 ready 뷰 (배정 없음) — [채널 아니오] [상주 아니오]

- 방식: 노드에 `deps`만 주고 `xsm doc ready`가 선행 조건이 끝난 노드를 **출력만** 한다.
- 장점: C1 네 기준 통과. 의존성이 명시적이라 fan-in이 표현된다(A는 못 한다).
- 단점: `deps`의 저자가 사람이면 T7의 "스캐폴딩", 에이전트면 검증 필요. A와 같은 축에서 더
  구조적이고 덜 탐색적.

### D. 아무것도 만들지 않는다 (Appendix C "Flat log") — [채널 **예** — 이미 쓰고 있음] [상주 아니오]

- 방식: `send`/`channel`로 세션들이 알아서 조율한다.
- 장점: 코드 0. INTENT에 그대로 부합. §4.3이 부분 실증.
- 단점: 같은 §4.6이 그것만으로는 5일 정체를 못 막았다고 기록. T7 "숨겨진 코디네이터" 우려.

### E. 관찰만 — [채널 아니오] [상주 아니오]

- 방식: `telemetry.span`(0011)으로 통신·기여 그래프를 보여준다. 선택은 사람·세션에 맡긴다.
- 장점: 코드는 뷰 하나. **S10의 측정 도구가 곧 E** — 어느 결정이 나든 남는다.
- 단점: 관찰이 행동을 바꾸지는 않는다.

### F. 고전 패턴 — 한 줄 기각 사유

`docs/references/**`에 언급이 없다는 것은 기각 사유가 아니므로 각각 따진다.

- blackboard: 원문 §2가 agora를 이 계보에 둔다. 제어 정책 없는 블랙보드 = A. 별도 선택지 아님.
- stigmergy: A의 n(v)·ρ(v)가 이미 그것(태그·후속 수가 페로몬).
- contract net/경매: 입찰을 받을 조정자가 필요 → 상주 또는 라운드 동기화. C1·R-G8-04 둘 다 어긋남.
- work stealing: 큐 소유자가 있어야 훔친다. 피어에는 없다(`0006:88`).

### G. 부모 세션이 frontier를 나눔 — 한 줄 기각 사유

워커 트리 안에서는 이미 가능하고(`spawn --task`), 피어에는 부모가 없어(`0006:88`) 스웜의 정의를
벗어난다. 워커 트리에서의 분배는 별도 논의.

## 근거

- 논문 원문: arXiv:2609.18094v2 §2, §3.1–3.3, §4.2–4.3, §4.5–4.7, Appendix C. 조사 노트
  `docs/references/agora.md`는 v2 대조 정정본을 기준으로 한다.
- 다른 후보: `docs/references/orca.md`(코디네이터 push, `deps`, 순환 검사, 상주), `moai-adk.md`
  (claim 10분 재전달, TTL 24h), `buzz.md`, `herdr.md` — 조사 노트 기준, 원문 미확인.
- 스파이크: **S10**(`docs/spikes/S10-swarm-duplication.md`, 미실행). Appendix C를 xsm 규모로 옮긴
  사전 등록 실험. 결과가 이 ADR의 Round 2 근거가 된다.
- 코드: `xsm/doc.py`(DAG·태그·뷰), `xsm/workers.py`(트리·게이트), `xsm/channel.py`(스레드형 로그),
  `xsm/telemetry.py`(0011, S10 측정 도구).

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|
| 0 | Claude | 사실 수집 | 위 "맥락" 전부. 초안 1차는 조사 노트 기준이었고 critic 검증에서 "agora가 C1을 만족한다"(시스템/조정 방식 혼동), "C는 C1 정면 충돌"(상주/배정 혼동), "A+B 권고"(결론 선행), "메시지 채널 없음"(노트 해석을 사실로) 네 결함이 나와 원문을 다시 읽고 재작성 | — |
| 1 | Claude | **결정 불가 선언.** A/B/C′/D를 가르는 것은 (1) xsm에서 중복이 얼마나 나는가, (2) `wip`을 보이게 하면 줄어드는가, (3) 채널에 실으면 더 줄어드는가 — 셋 다 측정된 적 없다. S10을 Round 0 근거로 요구하고 결과별 대응을 **결과를 보기 전에** 사전 등록한다(S10 §5). C·F·G 기각 제안. E는 어느 경우든 채택 | 권고를 미리 적으면 측정이 장식이 된다(critic). agora 저자 스스로 matched comparison 없이는 효과를 주장하지 않는다(초록) | (Round 2는 S10 결과를 받은 뒤 사람과 다른 세션이 이어서 채운다) |

## 결정

(Accepted 이후 작성)

미해결로 남은 것:

1. ρ(v)의 임베딩 없는 근사 — A가 살아남을 경우에만.
2. C·D 상수 — 원문에도 없다. A 채택 시 S11에서 튜닝.
3. `wip`에 만료를 둘 것인가 — agora는 두지 않았다(B0/B1 vs B2).
4. 워커 트리(`parent_ref`)와 문서 DAG(`parents`)를 합칠지.
5. 피어 스웜의 depth/concurrency 예산을 무엇에 매달 것인가(`0006:88`).
6. `deps`의 저자(C′) — 사람인가 에이전트인가.
