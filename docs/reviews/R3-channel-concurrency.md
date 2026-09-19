# 적대적 리뷰 R3: 채널-스레드 기록과 공동 문서 편집 (G6, G7)

## 발견 목록

| ID | 유형 | 심각도 | 대상(문서:줄 또는 절) | 문제 | 원본 증거(경로:줄) |
|---|---|---|---|---|---|
| F1 | WRONG | High | docs/references/buzz.md:97 | `crates/buzz-relay/src/architecture.md`는 존재하지 않음. 실제 파일은 루트 `ARCHITECTURE.md`. 줄 단위 범위를 지정하려면 실제 위치를 알아야 함 | /tmp/xsm-refs/buzz/ARCHITECTURE.md (존재), /tmp/xsm-refs/buzz/crates/buzz-relay/src/architecture.md (없음) |
| F2 | UNSUPPORTED | Medium | docs/references/buzz.md:207 | `VISION_MESH.md:1-54`는 범위 초과. 실제 파일은 53줄인데 54줄을 지정 | /tmp/xsm-refs/buzz/VISION_MESH.md (wc 결과: 53줄) |
| F3 | UNSUPPORTED | Medium | docs/references/buzz.md:201 | `VISION_REMOTE_AGENTS.md:1-74`는 범위 초과. 실제 파일은 73줄인데 74줄을 지정 | /tmp/xsm-refs/buzz/VISION_REMOTE_AGENTS.md (wc 결과: 73줄) |
| F4 | GAP | High | docs/references/buzz.md 전체 | buzz self-hosting의 최소 운영 구성이 명시되지 않음. 요약에서 "자체 호스팅 가능"이라고만 하고, 실제로는 PostgreSQL, Redis, relay 서버(Rust Axum), 웹 클라이언트(React)가 모두 필요함. INTENT.md와의 비교: "가볍게 .md, .sqlite를 이용해 기록"과의 선택지 비교가 불충분 | docs/references/buzz.md:13-32 (형태·의존성 나열), INTENT.md:21 ("엄청 가볍게") |
| F5 | UNSUPPORTED | High | docs/references/buzz.md:174-179 | "CONFIG_DIR 무관 발견" 섹션이 buzz의 실제 설계와 맞지 않음. 작성자가 "하지만 이는 Buzz 자체가 아직 Claude Code와 통합되지 않았다"고 명시했으나, 통합 경로와 현재 상태를 구체화하지 않음. 계획만 있고 구현은 미확인 | docs/references/buzz.md:174-179 (섹션 제목과 본문 모순) |
| F6 | GAP | High | docs/references/buzz.md:134-146 (ACP/MCP 섹션) | buzz-acp가 "Claude Code/Codex 연동 후보"라고 언급되지만, 실제 integration point가 명확하지 않음. MCP 서버 구현은 있으나(buzz-dev-mcp), Claude Code의 MCP 클라이언트가 buzz 메시지를 consume하는 방식이 기술되지 않음 | docs/references/buzz.md:132-147 (ACP/MCP 단락) |
| F7 | UNSUPPORTED | Medium | docs/references/agora.md:25 | 저자 목록이 arXiv 원문과 대조되지 않았다고 명시됨. 초록에서 "저자 목록은 초록 페이지에서 대조하지 못했다" (줄 152). 이메일 주소로 추론하면 "Yifan Zhang"이 맞지만, 논문 원문과 직접 비교되지 않음 | docs/references/agora.md:25 (저자 목록), 줄 152 (확인 안 함 명시) |
| F8 | GAP | High | docs/adr/0006-shared-document-editing.md (전체) | 공동 문서 편집(G7)의 선택지가 불완전함. A(소유권 분할), B(lease), C(append-only)만 나열되고, 다음 선택지들이 검토되지 않음: (1) Git 브랜치+PR 모델 (merge conflict resolution), (2) OT/CRDT 기반 동시 편집, (3) 절 단위 ownership + 명시적 merging | docs/adr/0006-shared-document-editing.md:21-39 (선택지) |
| F9 | GAP | High | docs/references/README.md:2.6절 표 (동시성/충돌) | G7과 관련해 동시성 제어 방식 비교표가 있지만, "소유권 분할" 행의 "무한 대기" 항목이 비어있음. moai의 claim+재전달 만료 방식의 세부사항도 없음. 또한 git 기반 접근(merge conflict, 3-way merge)이 전혀 나열되지 않음 | docs/references/README.md:106-115 |
| F10 | UNSUPPORTED | Medium | docs/references/README.md:147-151 (buzz.md 정오표) | buzz.md의 인용 오류를 "일부 오류"로만 정리하고, 구체적으로 어느 줄의 인용이 패턴화된 부정확성인지 분석하지 않음. "`:1`처럼 파일 전체를 가리키는 인용이 많아 정밀도가 낮다"고 하지만, 어느 인용이 대상 파일과 불일치하는지 명시되지 않음 | docs/references/README.md:151 (정오표), docs/references/buzz.md 참고 |
| F11 | GAP | Medium | docs/plan/README.md:75 (P6 단계) | P6 (G6·G7: 채널-스레드와 문서 협업 규약)의 완료 기준이 "ADR-0005·0006 확정 후 최소 구현"이라고만 함. ADR 둘 다 상태가 Proposed이고, 토론 라운드가 비어있음(docs/adr/0005:42-44, docs/adr/0006:46-48). "토론 라운드를 거친 뒤"의 기준이 없음 | docs/plan/README.md:75, docs/adr/0005:42-44, docs/adr/0006:46-48 |

## 보강 조사 질문

### 우선순위 1: Buzz self-hosting 최소 구성

1. **buzz.md 요약에서 말하는 "자체 호스팅 가능"의 구체적 의미가 무엇인가?**  
   단일 머신에서 buzz relay를 운영할 때:
   - 최소 의존성: PostgreSQL, Redis, Rust relay 바이너리만인가? 아니면 웹 UI도 필수인가?
   - 데이터 영속성: PostgreSQL 백업은 어떻게 관리되는가? (buzz.md 거론 없음)
   - 검증 대상: /tmp/xsm-refs/buzz/crates/buzz-relay/src/main.rs와 docs 참고

2. **INTENT.md의 "가볍게 .md, .sqlite를 이용해 기록하는 규약"과 buzz의 스택을 비교할 때, 버금 기준은 무엇인가?**  
   - 협력자 수: 10명 이상에서는 buzz 규모가 정당화되는가?
   - 메시지 량: 월별 메시지 수가 구체화되지 않음
   - 인간 개입: 사람도 메시지를 쓰면 UI가 필수인가, 아니면 CLI/API만으로 충분한가?

### 우선순위 2: Buzz-Claude Code 통합 경로

3. **buzz-acp가 Claude Code와 연동되는 구체적 경로는?**  
   - buzz-acp는 ACP (Agent Client Protocol) 구현이고, Claude Code가 ACP 클라이언트인가?
   - 아니면 buzz-dev-mcp (MCP 서버)를 Claude Code의 MCP 레지스트리에 등록하는 방식인가?
   - 검증 대상: /tmp/xsm-refs/buzz/crates/buzz-acp/src/lib.rs (§6 "이종 에이전트 지원" 참고), Claude Code의 MCP 문서

4. **현재(2026-09-19) buzz가 Claude Code·Codex와 통합되지 않았다는 것의 의미는?**
   - INTENT.md 제약 C1 ("진입점 래퍼 런타임 금지")을 고려할 때, buzz를 거치려면 어떤 어댑터가 필요한가?
   - MCP를 거쳐 송수신하는 경우 지연시간은?

### 우선순위 3: G7 공동 문서 편집 선택지 누락

5. **왜 Git 브랜치+PR 모델이 ADR-0006의 선택지로 고려되지 않았는가?**  
   - 장점: 이미 존재하는 merge conflict resolution, code review 메커니즘 재사용
   - 단점: 문서 기반 협업이라면 PR 리뷰는 과부하인가?
   - agora.md는 append-only Git DAG를 쓰므로, 단순 Git 브랜치와의 차이점을 명시해야 함

6. **OT (Operational Transform) 또는 CRDT (Conflict-free Replicated Data Type)를 검토하지 않은 이유는?**  
   - 예: Yjs, Automerge, 또는 간단한 last-write-wins + versioning
   - 제약: 에이전트들이 클라이언트(동시 편집)가 되어야 하는가, 아니면 저장소 push만으로 충분한가?

7. **moai의 claim 만료 재전달 방식에서 "만료 처리와 시계 문제"라는 우려(ADR-0006:32)가 구체적으로 무엇인가?**  
   - moai 소스: /tmp/xsm-refs/moai-adk/internal/sessionmsg/lock.go의 재시도 로직
   - 시계 skew 시 claim 재발행과 중복이 어떻게 방지되는가?

### 우선순위 4: Agora 논문 원본과 agora.md 비교

8. **agora.md의 저자 목록이 arXiv 원문 페이지와 정확히 일치하는가?**  
   - 초록 논문에서 보이는: "Yifan Zhang, Yunheng Zou, Shaokun Zhang, Jian Hu, Hao Zhang, Binfeng Xu, Jan Kautz, Yi Dong"
   - 순서와 철자 확인 필요 (agora.md:25와 대조)

9. **agora.md에서 언급한 "코드는 미공개"(줄 374)라는 주장이 최신인가?**  
   - arXiv 페이지나 저자 GitHub에 공개 저장소 링크가 있는가?
   - 검증 대상: arxiv.org/abs/2609.18094 또는 저자 프로필

### 우선순위 5: 계획과의 연결

10. **docs/plan/README.md 2.3절 표의 "Codex 연동"에서 "moai: 세션마다 MCP 서버를 붙이는 방식이라 C1에 가장 가깝다"고 하는데, buzz는 어디에 위치하는가?**  
    - buzz-acp가 런타임을 차용하는가, 아니면 MCP 방식인가?
    - docs/plan/README.md:121 (C1 적합성 표) 에서 buzz를 다시 평가해야 함

## 이 리뷰에서 확인 못 한 것

- Buzz relay의 실제 배포 사례 또는 성능 특성 (논문 없음)
- Claude Code가 MCP를 통해 buzz-dev-mcp에 연결되는 구현 세부사항 (구현이 아직 없을 수 있음)
- agora 논문의 full PDF (HTML 초록만 접근 가능)
- moai-adk의 claim 실패 후 복구 시나리오 (코드상 "재시도 루프에만 시간 제한"이라는 설명의 정확성)
- G5(범위 정책)와 G6·G7의 상호작용: 범위별로 다른 채널-스레드 저장소를 둘 수 있는가?
