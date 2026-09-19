# ADR-0005: 채널-스레드 기록 저장소

- 상태: Proposed
- 관련 목표: G6
- 작성일: 2026-09-19

## 질문

즉시 전달(mailbox)과 별개로, 사람과 에이전트가 함께 보는 1:N·N:N 채널-스레드 기록을 어디에 둘 것인가?

## 맥락

- INTENT.md: GitHub issue는 정본만 담고, 일상적인 에이전트 간 비동기 소통·작업 기록·중재는 다른 채널에서 한다.
- buzz는 Nostr 이벤트 로그 기반의 자체 호스팅 워크스페이스다. PostgreSQL·Redis를 쓰고 에이전트 연동을 위한 ACP 하니스가 있다(`docs/references/buzz.md` 1절).
- agora는 기록을 append-only Git DAG로 남긴다. 노드마다 result, insight, hypothesis, verification, report 같은 태그를 달고, SQLite 인덱스와 웹 뷰는 Git에서 도출한다(논문 §3.2, §3.4). 코드는 공개되지 않았다(`docs/references/agora.md`).

## 선택지

- 보강 조사 T5(`docs/references/supplement/T5-buzz-channel.md`)와 코디네이터 확인:
  - 기본 compose 구성: `relay`(웹 클라이언트 서빙) + PostgreSQL 17 + Redis 7 + MinIO(S3).
  - 에이전트 연동: `buzz-cli`(REST, NIP-98 서명)와 `buzz-dev-mcp`(MCP).
  - Claude/Codex 연동은 미검증이다.
  - 가벼운 파일 규약과의 비교표는 T5 보고서에 있다.

### A. buzz 자체 호스팅

- 방식: buzz relay를 띄우고 에이전트가 CLI/ACP로 게시한다
- 장점: 사람용 UI, 스레드, 검색 제공
- 단점: PostgreSQL·Redis 등 운영 부담

### B. 외부 메신저

- 방식: Slack/Discord 채널을 쓴다
- 장점: 사람이 이미 익숙한 UI
- 단점: 외부 서비스 의존, 에이전트 기록이 외부로 나감

### C. 파일 규약

- 방식: 저장소 안의 append-only `.md`/`.jsonl`/`.sqlite` 규약을 둔다
- 장점: 가볍고 git으로 추적 가능
- 단점: 사람이 보기 좋은 UI가 없음, 동시 쓰기 규약 필요(ADR-0006)

## 근거

- 레퍼런스: `docs/references/README.md` 2.5절, `docs/references/buzz.md`, `docs/references/agora.md`
- 스파이크: `docs/plan/README.md` 5장

## 토론 기록

| 라운드 | 참가자 | 입장 | 근거 | 반론/응답 |
|---|---|---|---|---|

## 결정

아직 없음. 토론 라운드를 거친 뒤 사용자가 결정한다.
