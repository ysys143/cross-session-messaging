# T6: 공동 문서 편집 선택지 확장과 agora 전문 대조

**작성일**: 2026-09-19  
**대상 리뷰 항목**: R3-F7, R3-F8, R3-F9

---

## 결론 (첫 문단)

G7(공동 문서 편집)의 선택지가 불완전했다. 기존 ADR-0006은 소유권 분할(A), 기한 있는 잠금(B), append-only 로그(C) 세 가지만 제시했으나, Git 브랜치+PR(d), CRDT/OT(e), 절 단위 소유권+명시적 병합(f) 선택지가 추가로 검토되어야 한다. agora 논문의 저자 목록(arXiv 메타데이터와 docs/references/agora.md 일치 확인)과 코드 공개 상태도 재검증했다. 보강 조사 후에도 각 선택지의 trade-off를 정량화하기 위한 실험(예: 사용자 규모, 메시지 량)이 남아 있다.

---

## 리뷰 항목별 해소 결과

| 항목 ID | 결론 | 근거 |
|---|---|---|
| **R3-F7** (agora 저자 목록 미확인) | **일치(확인)** | arXiv 메타데이터(curl citation_author): ['Zhang, Yifan', 'Zou, Yunheng', 'Zhang, Shaokun', 'Hu, Jian', 'Zhang, Hao', 'Xu, Binfeng', 'Kautz, Jan', 'Dong, Yi']. agora.md(줄 25): 동일 순서. Jina Reader 결과가 첫 저자(Yifan Zhang)를 누락했음. arXiv 페이지 메타데이터와 agora.md의 저자 순서가 일치 |
| **R3-F8** (G7 선택지 불완전) | **확인, 추가 선택지 4개 도출** | 기존 ADR-0006(줄 21~39): A, B, C만 제시. 추가 선택지: (d) Git 브랜치+PR+3-way merge, (e) CRDT/OT, (f) 절 단위 소유권+명시적 병합. 각 선택지의 비교표 아래 상세 분석 |
| **R3-F9** (비교표 불완전, Git 미포함) | **확인, 비교표 확장** | docs/references/README.md(줄 106~115): 동시성 제어 방식에 "소유권 분할" 행의 무한 대기 항목이 비어있고, Git 기반 접근이 미포함. 2.6절 표를 재작성하고 모든 선택지(a~f)를 포함한 확대 비교표 제시 |

---

## 상세 분석

### 1. Agora 논문 저자 목록 및 코드 공개 상태

#### 확인 결과

**저자 목록 (arXiv 메타데이터 직접 확인):**
- `curl -s -A 'Mozilla/5.0' https://arxiv.org/abs/2609.18094 | grep citation_author`:
  ```
  citation_author" content="Zhang, Yifan
  citation_author" content="Zou, Yunheng
  citation_author" content="Zhang, Shaokun
  citation_author" content="Hu, Jian
  citation_author" content="Zhang, Hao
  citation_author" content="Xu, Binfeng
  citation_author" content="Kautz, Jan
  citation_author" content="Dong, Yi
  ```

**agora.md와의 일치:**
- docs/references/agora.md(줄 25): `Yifan Zhang, Yunheng Zou, Shaokun Zhang, Jian Hu, Hao Zhang, Binfeng Xu, Jan Kautz, Yi Dong`
- 저자 순서가 arXiv 메타데이터와 동일하게 일치. Jina Reader 결과가 첫 저자 Yifan Zhang을 누락했음
- arXiv 메타데이터가 공식 저자 순서이며, agora.md의 저자 목록이 정확함

**코드 공개 상태:**
- arXiv abs 페이지 메타데이터: 저장소 링크 없음
- arXiv HTML 본문(§3.4 Prototype implementation): "The codebase is small enough to audit end to end"이라 하지만 저장소 공개 링크 미제시
- References 섹션(Karpathy, OpenAI 등): GitHub 링크 있지만 agora 항목은 저장소 링크 없음
- **결론**: arXiv 페이지와 본문에서 코드가 공개되었다는 명시 없음. 미공개로 판단 유지

#### 정정 사항
- 이전 보고(T6): Jina Reader 결과가 첫 저자를 누락하여 "저자 순서 불일치"로 잘못 판정
- 정정(T6-fix): arXiv 메타데이터 직접 확인 결과 agora.md의 저자 목록이 정확함을 확인

---

### 2. 공동 문서 편집 선택지 비교표

다음 6개 선택지를 기준 5가지(충돌, 사본 증식, 무한 대기, 사람 가독성, 에이전트 구현 난이도)로 평가:

#### 기준 정의

| 기준 | 측정 방식 |
|------|---------|
| **충돌** | 동시 편집 시 내용 손실 가능성 (높음/중간/낮음) |
| **사본 증식** | 완료된 작업이 파일·커밋·버전으로 증식하는 정도 |
| **무한 대기** | 잠금·lease·claim이 해제되지 않는 시나리오 가능성 |
| **사람 가독성** | 편집 기록, 충돌 해결 과정, 최종 상태를 인간이 읽기 쉬운 정도 |
| **에이전트 구현 난이도** | 에이전트 코드에 필요한 병합·충돌 해결 로직 복잡도 |

#### 선택지별 분석

**선택지 (a): 절 단위 소유권 + 단일 통합자**

| 항목 | 평가 |
|------|------|
| 충돌 | 낮음 - 각 절마다 담당자 고정, 동시 편집 불가 |
| 사본 증식 | 낮음 - 정본만 유지 |
| 무한 대기 | 없음 - 잠금이 명시적 & 짧음 |
| 사람 가독성 | **높음** - 누가 어느 절을 편집했는지 명확 |
| 에이전트 난이도 | **낮음** - 잠금만 확인하면 됨 |
| **근거** | ADR-0006 § A |
| **제약** | 통합자가 병목; 통합자 자신도 절을 쓸 수 없음; 병렬 문서 작성 속도 제한 |

**선택지 (b): 기한 있는 임대(lease) 잠금**

| 항목 | 평가 |
|------|------|
| 충돌 | 낮음 - 절·파일 단위 lease 동안만 배타적 쓰기 |
| 사본 증식 | 낮음 - lease 만료 시 명시적 병합 또는 재전달 |
| 무한 대기 | 없음 - lease 만료 후 재전달 (moai 사례: 10분 만료) |
| 사람 가독성 | 중간 - lease 상태와 만료 시각을 추적해야 함 |
| 에이전트 난이도 | 중간 - lease 획득 재시도, 만료 처리 로직 필요 |
| **근거** | ADR-0006 § B; moai-adk/internal/sessionmsg/lock.go:12~113 (flock + atomic replace) |
| **제약** | 시계 skew 시 claim 중복 발행 가능; 네트워크 지연 시 재전달 대기 시간 누적; 파일 시스템 시계 신뢰도 의존 |

**선택지 (c): Append-only 로그 + 주기적 도출된 정본**

| 항목 | 평가 |
|------|------|
| 충돌 | 없음 - 모든 기여가 DAG에 추가되어 손실 없음 |
| 사본 증식 | 높음 - 로그와 도출 정본 모두 유지; 병렬 기여는 모두 DAG에 남음 |
| 무한 대기 | 없음 - 어떤 에이전트도 쓰기 차단 안 함 |
| 사람 가독성 | **높음** - 정본은 결과, 로그는 계보 & 분기 시각화 (agora 논문 Fig. 3~4) |
| 에이전트 난이도 | 중간 - append 권한만 필요, 도출 규칙 정의 필요 |
| **근거** | agora 논문 § 3.2~3.4 (arXiv:2609.18094v1); docs/references/README.md 줄 106~111 |
| **제약** | 정본 도출 규칙이 복잡하면 기여자 기대와 어긋날 수 있음; SQLite 인덱스 재구축 비용 |

**선택지 (d): Git 브랜치 + PR/merge + 3-way merge**

| 항목 | 평가 |
|------|------|
| 충돌 | 중간 - 3-way merge로 충돌 감지하나, 충돌 해결에 사람 개입 필요할 수 있음 |
| 사본 증식 | 낮음 - 브랜치마다 하나, merge 후 정본 유일 |
| 무한 대기 | 없음 - merge conflict 타임아웃 정책 설정 가능 |
| 사람 가독성 | **높음** - 기존 Git/GitHub 워크플로우; PR 코멘트, diff, 리뷰 익숙함 |
| 에이전트 난이도 | 중간~높음 - conflict marker 파싱 & 자동 해결 필요; 마크다운 특화 3-way merge 도구 부재 |
| **근거** | Git 3-way merge 알고리즘 표준; GitHub PR 워크플로우; 단, 마크다운 문서 협업 사례는 제한적 |
| **제약** | 문서 기반 협업에서 PR 리뷰 프로세스는 엄격할 수 있음; conflict 시 자동 해결 한계; 바이너리/구조화 데이터에는 부적합 |

**선택지 (e): CRDT/OT (Automerge, Yjs 등)**

| 항목 | 평가 |
|------|------|
| 충돌 | 없음 - 모든 동시 편집이 결정적으로 병합됨 |
| 사본 증식 | 낮음 - CRDT state가 유일한 진실의 원천 |
| 무한 대기 | 없음 - 어떤 에이전트도 쓰기 차단 안 함 |
| 사람 가독성 | **낮음** - CRDT 상태는 바이너리·JSON이 많아 직접 열람 어려움; 정본 렌더링 필수 |
| 에이전트 난이도 | **높음** - CRDT 라이브러리 통합, 메모리 관리, 구조화된 데이터 정의 필요 |
| **근거** | Automerge(CRDT, JSON 기반), Yjs(마크다운 연동 플러그인 제한적); 마크다운 특화 예시는 드물지만 가능(Yjs+Monaco 조합) |
| **제약** | 마크다운 문서에 CRDT 특화 라이브러리 부재(OT/CRDT는 주로 JSON/XML 기반); 에이전트-리얼타임 UI 혼합 모델에서 state sync 복잡; 학습 곡선 가파름 |

**선택지 (f): 워커별 파일 + 코디네이터 종합 (이번 조사 방식)**

| 항목 | 평가 |
|------|------|
| 충돌 | 없음 - 각 워커가 독립 파일 쓰기 |
| 사본 증식 | **매우 높음** - 워커 수만큼 파일 증식 |
| 무한 대기 | 없음 - 어떤 에이전트도 차단 안 함 |
| 사람 가독성 | 중간 - 최종 종합 문서는 읽기 쉬우나 중간 기여 산재 |
| 에이전트 난이도 | **매우 낮음** - append 권한만 필요 |
| **근거** | 이번 run(docs/references/README.md 2.6절, R1~R5 각각 독립 파일 작성) |
| **제약** | 사본 정리 부담; 워커 수 증가 시 종합 난이도 증가; 중복 조사 탐지 어려움 |

#### 확대된 비교표

| 기법 | 사본 증식 | 충돌 | 무한 대기 | 사람 가독성 | 에이전트 난이도 | 근거 |
|------|---------|------|---------|----------|------------|------|
| **(a) 절 단위 소유권** | 낮음 | 낮음 | 없음 | 높음 | 낮음 | ADR-0006 §A |
| **(b) Lease 잠금** | 낮음 | 낮음 | 없음 | 중간 | 중간 | moai-adk §8, ADR-0006 §B |
| **(c) Append-only + 도출** | 높음* | 없음 | 없음 | 높음 | 중간 | agora §3.2~3.4 |
| **(d) Git 브랜치+PR** | 낮음 | 중간 | 없음 | 높음 | 중간~높음 | GitHub PR 프로토콜 |
| **(e) CRDT/OT** | 낮음 | 없음 | 없음 | 낮음 | 높음 | Automerge, Yjs |
| **(f) 워커별 파일** | 매우 높음 | 없음 | 없음 | 중간 | 매우 낮음 | 이번 조사 실행 |

*주: (c)에서 "높음"은 DAG 노드 개수 기준이지만, 정본의 최종 크기는 증가하지 않음

---

### 3. 우리 상황에 맞는 권고 조합

#### 배경 조건

- **참가자 규모**: 현재 알려지지 않음(스파이크 S3 필요)
- **문서 특성**: 기술 보고서, 마크다운, 실시간 편집 비필수
- **에이전트 모델**: Claude Code & Codex 모두 지원 필요
- **C1 제약**: 별도 런타임 금지 → CRDT 서버 불가
- **G1~G2와 연결**: 전역 registry, CONFIG_DIR 무관

#### 권고 1: **선택지 (c) Append-only + 도출된 정본** (우선)

**이유:**
- agora의 검증된 구조(논문 12일 run, 13개 에이전트, 1,703 기여, 165 재현)
- 어떤 에이전트도 차단 없음 → 별도 런타임 필수 없음 (C1 준수)
- 정본 도출 규칙이 명확하면 Git history 자체가 감사 로그가 됨
- Git을 state의 유일한 원천으로 삼아 consistency 보장 (agora §3.2: "Git is the only state")

**구현 경로:**
1. `docs/` 아래 append-only 로그 저장소 (`.git` 이용 또는 별도 markdown 로그)
2. 각 기여 노드: 태그(`result`, `insight`, `hypothesis`, `verification`, `report`), 부모 ref, 설명, metric
3. 주기적 도출 규칙: report 태그를 종합 정본으로 렌더링
4. SQLite 인덱스: 검색 가능 views (frontier, unverified, contested)

**제약:**
- 도출 규칙의 투명성이 매우 중요 (정본과 로그의 간극 우려)
- 처음 설계 비용이 높음 (tag schema, metadata contract)

#### 권고 2: **선택지 (a) + (c) 혼합** (대안)

- **우선 phase**: 선택지 (a) 절 단위 소유권으로 시작 (낮은 복잡도, 빠른 운영 시작)
- **장기 phase**: 동시 기여 필요 시 (c)로 전환 (append-only로 기존 기여 보존, 앞으로 브랜칭 허용)

**이유:**
- 초기 비용 최소화: 절 담당자 할당만으로 충돌 방지
- 순차적 확장 가능: 필요할 때만 append-only 모델로 업그레이드
- Agora와 달리 초기엔 병렬도 낮을 수 있어 (a)로 충분할 수 있음

---

### 4. 각 선택지의 현황 & 미해결 질문

| 선택지 | 현황 | 미해결 |
|-------|------|--------|
| (a) | ADR 초안 완성 | 절 경계 정의, 통합자 역할 명확화 |
| (b) | moai 구현 참고 | 시계 skew 환경(SSH 원격)에서 재시도 전략 |
| (c) | agora 논문 검증됨 | 도출 규칙의 merging 로직(overlapping 기여 대응) |
| (d) | 표준 Git 프로토콜 | 마크다운 특화 conflict resolution 도구 |
| (e) | 원형 구현 많음 | 마크다운 연동, 에이전트 메모리 관리 |
| (f) | 이번 조사 사용 | 확장성(100+ 워커) 평가 미실시 |

---

## 확인 못 한 것

- **agora 저자 순서** (부분 확인): arXiv HTML 초록의 저자 순서는 확인됨. PDF 논문 본문의 순서 및 저자 정보 상세는 전문 접근 불가
- **agora 코드 저장소**: arXiv 초록 & 본문에 저장소 링크 없음. 저자 GitHub 프로필 직접 확인 필요 (현재 아래 링크만 참고 가능)
- **docs/plan/README.md 2.3절 표의 buzz 위치**: T5 보강 조사 후 재평가 필요
- **선택지 (d)~(f)의 실제 구현 & 운영 데이터**: 학술 논문 또는 오픈소스 프로젝트 실측 필요
- **agora 마크다운 연동 사례**: 논문은 weight-transfer(Python) 기반. 마크다운 기여의 append-only merge 예시 미제시
- **참가자 규모별 적정 선택지**: 현재 규모 미정. "10명 이상이면 buzz, 5명 이하면 append-only" 같은 기준 수립 필요

---

## 후속 액션

### 우선순위 1: ADR-0006 개정
- 선택지 추가: (d) Git 브랜치+PR, (e) CRDT/OT, (f) 워커별 파일 → 비교표 확장
- 각 선택지마다 "moai:", "agora:", "GitHub 표준" 같이 구현 근거 명시
- 토론 라운드 계획 수립 (예: 2026-09-30 deadline)

### 우선순위 2: Agora.md 확인 완료
- 저자 목록: arXiv 메타데이터와 일치 확인 (조정 불필요)
- "코드 미공개" 표현 유지 (arXiv 페이지 확인 결과)

### 우선순위 3 (선택): 실험 설계 (스파이크 S3)
- **question**: 현재 project의 예상 워커 수, 월별 기여 건 수 범위?
- **근거**: 선택지 (a) vs (c) 선택이 scale에 민감함
- **산출물**: 예상 운영 규모(예: "15~25 에이전트, 월 500~2000 기여")에 따른 권고 선택지 재조정

### 우선순위 4 (선택): agora 프로토타입 검토
- 코드 미공개라면 논문 저자에게 문의(스파이크 S4)
- 공개되어 있다면 go/CLI 코드 검토(agora.md의 "code 미공개"는 수정)

---

## 참고 자료

**agora 논문:**
- URL: https://arxiv.org/abs/2609.18094 (v1, 제출 2026-09-16)
- HTML: https://arxiv.org/html/2609.18094v1
- 메타데이터(citation_author): Yifan Zhang, Yunheng Zou, Shaokun Zhang, Jian Hu, Hao Zhang, Binfeng Xu, Jan Kautz, Yi Dong

**레퍼런스 소스:**
- `/tmp/xsm-refs/agora/` (미보유, 논문 자체만 가능)
- `/tmp/xsm-refs/moai-adk/internal/sessionmsg/lock.go` (T1과 공유)
- `/tmp/xsm-refs/orca/src/main/runtime/orchestration/` (R1, R4 조사)

**기존 조사 보고서:**
- docs/references/README.md § 2.6 (동시성 제어 원본 비교표)
- docs/adr/0006-shared-document-editing.md (초안)
- docs/reviews/R3-channel-concurrency.md (이 task의 배경)

---

## 정정 이력

### 2026-09-19 (초안 T6)
- **오류**: "arXiv HTML 초록(Jina Reader 경유): Yunheng Zou가 첫 저자"로 기술하여 agora.md(Yifan Zhang)와 불일치로 판정
- **근거**: Jina Reader 결과가 첫 저자를 누락함
- **결론**: R3-F7을 "부분 확인, 이상 발견"으로 기술

### 2026-09-19 (정정 T6-fix)
- **재확인 방법**: `curl -s -A 'Mozilla/5.0' https://arxiv.org/abs/2609.18094 | grep citation_author`
- **정정 내용**: arXiv 메타데이터(citation_author)의 공식 저자 순서는 Yifan Zhang부터 시작하며, agora.md의 저자 목록이 정확함
- **수정 사항**:
  1. 결론 첫 문단: "agora.md의 부정확성" 삭제, "저자 목록과 코드 공개 상태 재검증" 명시
  2. R3-F7 결론: "부분 확인, 이상 발견" → "일치(확인)"
  3. 상세 분석 § 1: Jina Reader 누락 명시, agora.md 일치 재확인
  4. 후속 액션 § 2: "저자 목록 Yifan Zhang 제거" 삭제, "저자 목록이 정확함" 반영
- **코드 공개 상태**: arXiv 메타데이터 및 HTML 본문 재확인 결과 미공개 판정 유지

