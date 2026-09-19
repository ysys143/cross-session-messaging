# Agora: Git as Shared Memory for Collective AutoResearch 논문 조사

## 요약

Agora는 자율 연구 에이전트 간 협업을 위한 Git 기반 공유 메모리 시스템이다. 연구 과정을 append-only DAG(directed acyclic graph)로 기록하여, 모든 주장·결과·검증·실패가 불변 커밋으로 저장되고 그 계보가 명시적이 된다. 13개의 언어 모델 에이전트가 12일 간 1,703개 커밋을 통해 weight transfer 문제를 협업하여, 무작위 초기화 3.39 bpb에서 1.899 bpb로 개선했다 (훈련된 GPT-2 124M의 62% gap closure). 시스템은 단일 중앙 플래너 없이 에이전트의 자발적 선택과 발견을 지원하며, 동시성 제어와 충돌 회피를 위해 Git의 immutable history와 SQLite 인덱스를 활용한다.

---

## 1. 문제 정의 및 제안 시스템

### 문제 정의

단일 에이전트의 자율 연구 루프(예: AutoResearch)는 무인으로 학습 설정을 개선할 수 있다. 그러나 여러 에이전트를 함께 실행하면 각 세션이 처음부터 시작되므로 중복 탐색이 증가하고 발견이 감소한다(§1). 에이전트들이 서로의 학습 결과(성공/실패, 가설, 검증 상태)를 알지 못하기 때문에:
- 세션-로컬 발견과 부정적 결과가 사라짐
- 워커들이 같은 가지를 중복 탐색
- 리더보드가 모든 워커를 한 곳으로 수렴시킴
- 검증이 없어 결과의 신뢰성 불명확

### 제안: Agora (한 줄 정의)

Git으로 저장된 append-only 기여 DAG로, 모든 주장·결과·가설·검증·보고가 불변 커밋이고 의존성이 명시적이며, 자동 인덱싱과 다양성 인식 주의 할당을 통해 중앙 플래너 없는 분산 에이전트 커뮤니티가 협업하도록 지원(§3).

### 저자 및 날짜

- **저자**: Yifan Zhang, Yunheng Zou, Shaokun Zhang, Jian Hu, Hao Zhang, Binfeng Xu, Jan Kautz, Yi Dong (NVIDIA)
- **이메일**: {yifazhang,yidong}@nvidia.com
- **발행**: arXiv:2609.18094 [cs.LG], 2026-09-16 제출

---

## 2. 에이전트 간 통신 모델

### 통신 방식: Git 기반 비동기 메시지 + 인덱싱 뷰

Agora는 "메시지 채널"이 아니라 **공유 Git 저장소**를 통신 매체로 사용한다.

#### 구조:
- **Primary state**: Git 저장소에 append-only commit으로 저장되는 기여(contribution)
- **Derived state**: SQLite 인덱스 (queries 고속화)
- **Views**: HTTP API와 CLI로 제공되는 분석 뷰

> "Git is the only state the system depends on: the SQLite index that answers queries, the analyze views below, and every figure in this report are derived from the Git history and can be rebuilt from it." (§3.2)

#### 메시지 형식 (기여 타입):

각 노드는 다음을 포함(§3.2):
v = (h, a, T, d, x, m, P, τ)
- h: 정규 커밋 해시 (content-addressed)
- a: 게시자 계정
- T: 태그 집합 (예: result, insight, hypothesis, verification, report)
- d: 설명 (자유 텍스트)
- x: 구조화된 메타데이터
- m: 선택적 프로젝트 메트릭 값
- P: 부모 커밋 집합 (의존성)
- τ: 서버 타임스탬프

**예약된 태그** (Table 2, §3.2):
| 태그 | 가중치 | 역할 | 규칙 |
|------|-------|------|------|
| setup | +5 | 초기 설정 | 프로젝트 zero 커밋 |
| result | +5 | 실험 결과 (성공/실패) | 메트릭 값 포함 |
| insight | +5 | 해석·패턴 | 증거 노드에 parent 링크 |
| hypothesis | +5 | 미테스트 제안 | 결과를 테스트된 것으로 표현 불가 |
| report | +5 | 인간 가독 종합 | 결론 노드를 인용 |
| verification | [+-]20/+10 | 재현·검증 | 다른 계정의 작업만, 실패/부분/확인 |
| endorsed | 0 | 승인 | 영향도에서 제외 |
| wip | 0 | 진행 중 | 중복 줄임, 점수 전파 안 함 |

### 통신 패턴: 1:1, 1:N, N:N 지원

**1:1**: 개별 에이전트가 단일 부모에서 분기하여 커밋
**N:N**: 
- 다중 부모 기여: 149개 노드가 다중 부모를 가짐 (1,703 중) (§4.6)
- 체이닝: 우승 기여의 145개 커밋 계보가 15개 계정에서 작성, 115개 간선이 계정 경계 횡단 (§4.5)

> "115 of the 144 parent edges cross account boundaries, so no single worker assembled the recipe." (§4.5)

**N:N 채널 기능**: 
- analyze API: 모든 에이전트가 현재 프론티어, 미탐색 가지, 검증 상태를 볼 수 있음
- 자동 응답 루프 없음; 에이전트는 능동적으로 분석 후 부모 선택

---

## 3. 메시지·기록의 데이터 모델과 저장 방식

### 데이터 모델: DAG 구조

G = (V, E)

- V: 기여 노드 집합 (각각 불변 커밋)
- E: 의존성 간선 ((u, v) in E 의미: v는 u 위에 구축)

각 노드는 Git 커밋 메타데이터(해시, 타임스탬프, author, message)와 Agora 특화 필드(태그, 메트릭, 메타데이터)를 결합.

### 저장 방식 (§3.4)

1. **Git 저장소**: 각 프로젝트는 bare repository (서버 데이터 루트 아래)
   - 기여는 Git commit으로 저장, canonical contribution refs로 모든 노드 reach 가능
   - 모든 커밋 체이닝 정보 보존

2. **SQLite 인덱스**: 8개 테이블
   - agents, projects, contributions, parents, tags, cross-project references, embeddings, rate limits
   - 인덱스는 Git 히스토리로부터 재구축 가능

3. **도출된 뷰**:
   - 프로젝트 메타데이터, 계보, DAG 구조, 검색, 분석, 파일 브라우징, diff
   - HTTP API (26개 라우트), CLI (15개 커맨드 그룹)

### 인간이 보는 인터페이스

**웹 인터페이스**: Next.js 기반 (§3.4)
- 리더보드 (metric leaders)
- 계보 시각화
- 클러스터 맵 (의미론적 유사성 기반, cosine 임계값 0.90)
- 프론티어 후보 (미탐색 가지)
- 실시간 활동 피드

**CLI**: agora analyze 등으로 접근
- 각 세션은 분석 -> 부모 선택 -> 로컬 실행 -> 푸시 루프 반복 (§4.3)

---

## 4. 동시성 및 충돌 처리

### 잠금과 충돌 회피: Git의 불변성 활용

Agora는 **낙관적 동시성 제어**를 구현한다.

#### 원리:
1. **Content-addressed identity**: 각 커밋은 SHA-1 해시로 식별 -> 동일한 콘텐츠는 동일한 해시 (Git 특성)
2. **Append-only history**: 새로운 기여는 기존 부모를 참조하여 DAG 연장 -> 히스토리 편집 불가
3. **Atomic commit**: Git 푸시는 성공/실패이지, 부분 성공 불가

> "every claim is a commit anyone can check out and rerun" (Abstract)

#### 충돌 처리 전략:

- **병렬 기여 (parallel contributions)**: 같은 부모를 선택한 여러 에이전트의 기여는 모두 DAG에 포함
  - 결과 696쌍의 동일 점수 (다른 계정) 중 63%가 1시간 내, 80%가 6시간 내 발생 (§4.6, Figure 5)
  - 모순 검증은 자동 아님; verification 태그로 명시적 재현

- **검증과 버전**: 
  - 검증자가 선택사항 변경 시, 최신 검증이 점수에 반영; 구 검증도 히스토리에 남음 (§3.2)
  - 부정 결과(negative results)는 53개 명시적으로 태그되어 나중 워커가 참고 (§4.5)

#### 사본 증식 회피:

Agora는 **단일 Git 저장소**를 중앙 진실(single source of truth)로 유지:
- 에이전트는 fetch/checkout 후 로컬에서만 편집, 완료 후 푸시
- 동일 부모로부터의 기여는 **병렬 branch**로 DAG에 추가되지, 복제 분기가 아님

#### 무한 대기 회피:

- **비동기 설계**: 에이전트는 다른 에이전트 응답을 기다리지 않음
- **Lease 기반 rate limiting** (§3.4): 기여 생성, 검색, 번들 크기 등에 rate 한계
- **Freshness 신호**: 각 노드에 서버 타임스탬프 -> 오래된 분기 자동 감지

---

## 5. 발견, 주소 지정, 권한 및 범위 제어

### 발견 (Discovery): 분석 뷰와 다양성 인식 선택

에이전트는 리더보드만으로는 프론티어를 알 수 없다. Agora는 **여러 뷰 동시 제공**으로 단일 리더보드 수렴을 방지 (§3.3).

#### analyze 뷰 (§3.3):
1. **Metric leaders**: 최고 점수 노드
2. **Most built-on nodes**: 다른 에이전트가 가장 많이 확장한 노드
3. **Leaves**: 아직 확장되지 않은 노드 (기회)
4. **Promising underexplored**: 양호한 점수지만 활동 적음
5. **Unverified results**: 검증 태그 없음
6. **Contested verifications**: 검증 모순
7. **Open hypotheses**: 테스트 대기 중
8. **Recent activity**: 시간순 피드
9. **Semantic clusters**: 임베딩 기반 그룹화

#### 다양성 인식 UCB (Upper Confidence Bound, 식 3):

U(v) = 100*Q(v) + C*sqrt(log(N+1)/(n(v)+1)) + 100*D/sqrt(1+rho(v))

- Q(v): 품질 백분위 (점수 기반)
- C: 탐색 상수 (메트릭 분포가 tight할 때 증가)
- n(v): v에 대한 후속 작업 수
- rho(v): 유사 설명 수 (중복성 페널티)

**세 가지 슬롯**으로 제시 (§3.3):
- **exploit**: 리더 재현/개선
- **explore_known**: thin cluster의 유망 작업 확장
- **explore_novel**: singleton 또는 초소 cluster의 미터치 노드 검사

임계값(50% 임베딩 커버리지, cosine 0.90)까지는 단순 뷰만 제공, 이후 클러스터 분석 추가 (§3.3).

### 주소 지정: 계정 기반 권한

**계정 시스템** (§4.2):
- 13개 워커 계정: worker1-worker5 (A100, Apr 27), slurm_worker_1-8 (H100, Apr 28)
- 기여는 계정 author로 태그; cross-account 간선에만 점수 가중 (자-인용 제외, 식 2)

> "The self-citation exclusion stops a worker from manufacturing impact by extending its own branch." (§3.2)

### 범위 제어: 프로젝트별 격리

- **프로젝트 경계**: 각 프로젝트는 별도 bare repo, 권한은 프로젝트별
- **Cross-project references**: SQLite 테이블로 지원하나, 기본은 단일 프로젝트 범위 (§3.4)
- **인증**: Write 작업(clone, fetch)에 bearer authentication 필수

---

## 6. 평가 방법과 주요 결과

### 실험 설정

**작업**: Weight transfer problem
- 141개 pretrained donor 모델 (534 GB, 32개 아키텍처 족)
- 고정된 target: 14-layer hybrid (attention + Mamba SSM), 119.6M 파라미터, hidden size 672
- 제약: 학습 데이터 없음, gradient 업데이트 없음, 평가자 편집 금지
- 기준: FineWeb-Edu 200 texts, bits per byte (bpb) 메트릭

**에이전트**:
- Claude Code (Opus 4.7) 5개 + Codex (GPT-5.5) 8개
  [NOTE: 논문의 원문 모델명. GPT-5.5는 future model reference]
- 컨테이너, GPU (A100/H100), 80GB 메모리, Agora CLI + Git + PyTorch
- 프롬프트: 2페이지 brief (program.md), 작업 할당 없음, 역할 순위 없음

**실행 기간**: 12일 (Apr 26 - May 8, 2026)

### 주요 결과

#### 1. 성과 (§4.5)
| 단계 | 점수 (bpb) | 변화 | 설명 |
|------|-----------|------|------|
| Random baseline | 3.3923 | — | — |
| 최종 (cutoff) | 1.8990 | -1.4933 | 62% gap 폐쇄 (trained GPT-2 1.0 기준) |

**총합**:
- 1,703 커밋: 1,124 scored results, 284 insights, 203 hypotheses, 165 verifications, 1 report
- 233개가 신기록 (new best)
- 98%의 개선은 **처음 18개 scored contributions** (Apr 27 00:24 — 13:30)에서 달성:
  - unigram prior (2.52) -> bigram + 28 contexts (1.93)
  - 이후 1,106개 contributions이 추가 0.03 개선

#### 2. 재현성 (§4.5)
- 우승 기여의 144개 scored 선조 중 40개를 독립적으로 재현
- 165개 검증 기여, 95개 distinct targets, **재현 실패 0개**
- 동일 하드웨어: bit-identical; 다른 GPU (A100 vs H100): ±1.3e-3 bpb 차이 (tolerance 내)

#### 3. 계보 분석 (§4.5)
- 최종 우승 레시피: 145개 커밋, 15개 계정 저자
- 115/144 간선이 계정 경계 횡단 -> **단일 워커가 조립하지 않음**
- Stage A (bigram transition prior): 98% 개선 담당
- Stage B (sparse attention/SSM edits): 나머지 0.03 개선

#### 4. 조율 역학 (§4.6, Figure 5)
- **빠른 착취 (fast exploitation)**: 첫 8개 개선이 ~70% 달성, 처음 18개가 ~98% 달성
- **좁은 척추 (narrow spine)**: 대부분 follow-on이 한 계보에 집중, 측면 분기는 짧음
- **병렬 재발견 (parallel rediscovery)**: 동일 점수 696쌍 중 63%가 1시간 내, 80%가 6시간 내
  - 개선 여지 : 메시지 채널 없음, 리더보드만으로는 중복 감지 어려움

#### 5. 인간 개입 (§4.7)
- **Pre-run**: 작업 정의, donor zoo, 평가자, brief 작성, 프로젝트 생성, 워커 시작
- **Mid-run** (May 2, 유일한 개입): 클러스터링 + 다양성 UCB 배포
  - 1/3 활동이 단일 클러스터에 쌓여 있던 상황 -> diversity views 배포 후 다음날 sub-1.90 달성
  - "a worker that chose to follow the thin state-space cluster rather than extend the dominant one" (§4.7)

### 한계 및 미해결 과제 (§4.8 참고, Appendix C)

1. **단일 태스크**: weight transfer만 평가
2. **통제된 비교 필요**: 공유 상태 vs. 독립 에이전트의 발견 효율 비교를 위해서는 matched, preregistered evaluation 필요 (Appendix C 제안)
3. **메시지 오버헤드**: DAG와 리더보드 기반 협업이 시간대 측정(예: 지연, 병렬화 이득)을 명시하지 않음
4. **거버넌스**: 권한·범위 모델은 기본; cross-project, 다중 팀 시나리오는 미구현

---

## 7. INTENT.md 목표에 대한 시사점

### 핵심: Agora는 무엇을 해결하고 무엇을 하지 않는가?

#### A. INTENT.md의 요구사항 vs. Agora 매칭

| 요구 | Agora 지원 | 평가 |
|------|-----------|------|
| CONFIG_DIR 무관 발견·메시징 | [O] Git 저장소 URL 기반 | 직접 적용 가능 |
| invoke/wakeup | [X] Async-only, pull-based | 설계 차이 |
| 1:N/N:N 채널-스레드 | [O] DAG 및 뷰로 지원 | 그룹 협업에 강함 |
| 인간+에이전트 함께 보기 | [O] 웹 UI + CLI 병행 | 실제 구현 (§4.7 인간 개입) |
| 충돌·사본 증식·무한 잠금 회피 | [O] Git immutability + async | 검증됨 |

#### B. 차용할 설계 (적용 가능)

1. **Append-only DAG in Git**
   - 장점: 모든 클레임이 검증 가능, 히스토리 재구축 가능, 오프라인 작동 지원
   - Agora 구현: "Git is the only state" (§3.2)
   - 추천: cross-session-messaging에서도 commit 기반 lineage 도입

2. **Derived indexing (SQLite)**
   - 장점: 빠른 쿼리, Git으로부터 재생성 가능 (backup 최소화)
   - 추천: 메시지 메타데이터 (타임스탬프, 태그, 참여자)를 SQLite로 인덱싱

3. **Quality scoring from downstream evidence** (식 2)
   - 장점: 투표가 아닌 **행동** (재현, 확장)으로 신뢰도 측정
   - 추천: 크로스-세션 메시지에서 "이 정보를 사용한 에이전트 수"로 유용도 추정

4. **Diversity-aware selection** (식 3, §3.3)
   - 장점: 리더보드 수렴 방지, 미탐색 영역 명시 표시
   - 추천: 메시징 채널에서 "이 아이디어는 아직 N개 에이전트만 시도"로 선택지 다양화

5. **Light/heavy publication paths** (§3.2)
   - 장점: 메타데이터-only 기여는 빠름, 코드/artifact는 번들 검증
   - 추천: cross-session message의 "plan" (light) vs. "result" (heavy) 분리

#### C. 피해야 할 설계 (한계)

1. **Synchronous request-response 없음**
   - Agora: 모두 async pull-based (Git fetch)
   - INTENT 요구: "invoke/wakeup" (즉시 깨우기) <- **비동기만으로는 부족**
   - -> 필요: 메시지 채널 + event notification (Git hook? webhook?)

2. **단일 Git 저장소 모놀리식**
   - Agora: 프로젝트당 하나의 bare repo
   - INTENT 시나리오: ~/.claude, ~/.codex 등 분산된 CONFIG_DIR
   - -> 개선: Git 저장소 federation/discovery 메커니즘 필요

3. **실시간 협업 미지원**
   - Agora: DAG commit은 고정, 동시 편집 없음
   - INTENT 추가1: "공동 문서 편집" <- **Agora는 이를 하지 않음**
   - -> 필요: OT/CRDT 기반 동시 편집 + Git 스냅샷 병합

4. **권한 세분화 부재**
   - Agora: bearer token 인증만, role/scope 미분화
   - -> 필요: per-project, per-session 권한 정책

#### D. "추가1) 인간과 에이전트 함께 볼 수 있는 messaging channel" 관점

Agora의 웹 UI (Next.js)는 이를 **부분 해결**:
- 리더보드, 클러스터 맵, 프론티어, 활동 피드는 인간도 에이전트도 읽을 수 있음
- 그러나 **쓰기는** 에이전트 CLI/API로만 (인간은 메타데이터 편집 불가)

**개선 아이디어** (INTENT 관점):
- Agora의 "tag" + "description" 모델을 확장하여 인간이 메모/질문/지시를 annotation 커밋으로 추가
- Git blame으로 누가 언제 코멘트했는지 추적
- SQLite annotations 테이블로 스레드 관계 인덱싱

#### E. "추가2) Graph Engineering과 Swarm Agents" 관점

Agora의 DAG와 다양성 UCB는 **swarm 탐색 구조를 자동화**:
- 각 에이전트는 독립적 (role/hierarchy 없음)
- 각자 analyze -> 부모 선택 -> 실행 -> 푸시 루프
- 다양성 신호가 자발적으로 분기 확산 방향 제시

**시사점**:
- Graph Engineering: DAG의 **semantic clustering** (§3.3, cosine 0.90)을 활용하여 개념적으로 먼 방향 탐색 유도
- Swarm: No central planner, only Git + heuristic selection -> 스케일링 가능 (본 논문 13개, 장래 더 큼)

**한계**:
- Agora는 단일 메트릭(bpb)에 최적화
- Graph Engineering의 복잡한 constraint propagation (예: 구조 타입 체크, 커스텀 가산성)은 미지원

---

## 출처 목록

1. **arXiv 논문**  
   - https://arxiv.org/abs/2609.18094  
   - https://arxiv.org/html/2609.18094v1  
   - https://arxiv.org/pdf/2609.18094  

2. **alphaXiv 요약**  
   - https://www.alphaxiv.org/abs/2609.18094  

3. **논문 인용 및 참고**  
   - Yifan Zhang, Yunheng Zou, Shaokun Zhang, Jian Hu, Hao Zhang, Binfeng Xu, Jan Kautz, Yi Dong. "Agora: Git as Shared Memory for Collective AutoResearch." arXiv preprint arXiv:2609.18094 (2026).

4. **관련 저장소**  
   - 코드 미공개 (arxiv 페이지에 공개 저장소 링크 없음, 문의 필요)

---

## 추가 노트

### 구현 크기 및 감사 가능성

> "The codebase is small enough to audit end to end." (§3.4)

- Go 서비스, Next.js 웹, 26개 HTTP 라우트, 15개 CLI 커맨드
- Docker 번들
- -> cross-session-messaging 설계 시 유사한 컴팩트성 목표 가능

### 검증 견고성 (§4.8)

- 165개 검증 기여, **실패 0개** (동일 하드웨어)
- 방법론: 각 검증자는 다른 계정 (자-검증 금지)
- -> verify-by-others 정책이 trust 증진

---

*작성일: 2026-09-19*  
*조사 대상: arXiv 2609.18094 (2026-09-16 제출)*
