# humanize 조사

humanize는 사용자가 이미 로그인해 둔 코딩 에이전트 CLI(Claude Code, Codex 등 12종과 ACP CLI)를 런타임으로 쓰고, Python으로 쓴 **flow**가 그 에이전트들에게 차례로 턴을 주며 모든 대화를 기록하는 오케스트레이터다. 런타임을 새로 만들지 않고 감싼다는 점에서 xsm과 철학이 가장 가깝다. 다만 세션을 humanize가 띄우고 소유하므로, 사람이 띄운 세션에 붙는 xsm과는 소유 모델이 반대다. 경쟁이 아니라 보완 관계다.

- 작성일: 2026-10-03
- 출처(모두 2026-10-03 조회):
  - 문서: `https://docs.humanfia.ai/humanize`의 개요, `features/backends`, `user/remote-execution`, `user/security`, `reference/agents` (Jina Reader)
  - 저장소 메타데이터: GitHub API `repos/humanfia/humanize`
- 방법: 문서 열람만 했다. 소스를 읽거나 실행하지 않았다.

## 1. 구조

| 층 | 하는 일 |
|---|---|
| flow | 일이 무엇인지 정한다. 어떤 역할이 차례로 턴을 받는지, 각자 무엇을 요청받는지, 언제 멈추는지. `@flow`를 붙인 async Python 함수다 |
| humanize | flow를 실행한다. 모든 턴을 넘기고, 모든 대화를 보관하고, 예산을 지키고, 실행을 기록한다 |
| 코딩 에이전트 | 모델에 닿는 통로. 대부분 사용자가 이미 로그인한 CLI다 |
| 환경 | 작업이 일어나는 곳. 현재 디렉터리, git worktree, 컨테이너, SSH 원격 |

flow의 예(개요 문서): 역할 `builder` 하나가 세션을 열고(`builder.spawn`), 과제를 수행한 뒤, **같은 세션**에서 자기 작업을 검토한다. 공식 flow `ralph_loop`은 매 라운드 **새 세션**에서 같은 과제를 반복하고, 예산이나 사람이 멈출 때까지 돈다.

## 2. 백엔드

claude, codex, cursor-agent, dsh(DeepSeek Harness), grok, kimi, pi, qwen, agy(Antigravity), opencode, mcode, mimo, 그리고 ACP를 말하는 CLI. 대부분 API 키 없이 CLI 자체 로그인을 쓰고, 한 CLI에 계정을 둘 이상 둘 수 있다. 사람도 `HumanAgent` 백엔드로 역할을 하나 맡을 수 있다.

구동 방식(`reference/agents`):

| 백엔드 | 구동 |
|---|---|
| Claude Code | 세션마다 `claude --print`를 stream-json으로 열어 둔다 |
| Codex | 에이전트마다 `codex app-server --stdio` 하나를 띄우고 그 세션들이 공유한다 |
| Antigravity, qwen | 프로세스를 stream-json으로 열어 두고, 형식 있는 턴은 명령을 따로 실행한다 |
| pi | 세션마다 `pi --mode rpc` |
| Kimi Code | 에이전트마다 `kimi web` 데몬 |
| cursor-agent | 턴마다 `cursor-agent --print` |
| ACP CLI | 프로세스 하나를 열어 두고 stdio JSON-RPC |

세션은 백엔드 자신의 id로 식별된다(첫 턴이 끝난 뒤).

## 3. 실행 환경과 원격

- `-e 역할=백엔드@공급자/작업디렉터리` 형식으로 환경을 지정한다. 예: `ssh@build-box/home/me/proj`, `docker@local/home/me/proj`, `local@/srv/project`.
- SSH 원격에서는 사용자의 ssh 설정·에이전트·키를 그대로 쓴다. 원격에는 Linux 또는 macOS와 Python 3.12 이상만 있으면 되고, humanize가 필요한 것을 가져간다.
- 에이전트 CLI(하니스)를 어디서 돌릴지는 따로 정한다. 기본은 원격에 CLI가 있으면 원격, 없으면 이쪽 기계에서 돌리고 원격의 파일과 명령만 다룬다. 하니스 위치가 어느 로그인을 쓸지, 세션이 어디 저장될지, 모델에 어느 네트워크로 닿을지를 정한다.
- 원격 실행을 시키는 쪽 기계는 Linux(x86-64, aarch64)여야 한다.

## 4. 보안 모델

- 모든 flow의 에이전트는 **항상 승인을 건너뛰고** 돈다. 파일 편집, 명령 실행, 커밋을 스스로 한다.
- 보호는 실행 전에 정한다. (1) flow 코드가 역할별로 로컬 읽기·쓰기와 네트워크 접근을 선언한다(예: `Permission(local=PermissionKind.READ, online=PermissionKind.NONE)`). (2) 어떤 flow 모음(flowverse)을 믿을지 사용자가 정한다. flow 목록을 보는 것만으로 그 코드가 실행된다. (3) 각 역할이 어느 계정으로 과금되는지 사용자가 정한다.
- 되돌리는 수단으로 실행 전 git 태그를 권한다.

## 5. 그 밖의 기능

실행 중인 턴에 끼어들어 말하기, flow가 사람에게 묻기, 실행별 예산(비용·시간), 터미널을 닫아도 실행 유지(데몬), 모든 에이전트를 한 타임라인으로 추적, 중단 지점부터 재개, 여러 대화 동시 실행, 타입 있는 응답(스키마 검증), 에이전트가 완료를 판정하는 목표, 턴의 각 시점에 반응하는 훅.

## 6. 프로젝트 상태

| 항목 | 값 |
|---|---|
| 저장소 | `humanfia/humanize` |
| 라이선스 | Apache-2.0 |
| 생성 | 2026-07-24 |
| 커밋 | 1,014 |
| 별 | 184 |
| 마지막 푸시 | 2026-10-03 |

문서의 저작권 표기는 한 사람(Zijian Zhang)이다.

## 7. xsm과 비교

| | xsm | humanize |
|---|---|---|
| 철학 | 기존 런타임을 그대로 쓴다 | 같다 |
| 세션 소유 | 사람이 TUI로 띄운 세션에 붙는다 | humanize가 세션을 띄우고 대화를 보관한다 |
| 다음 턴을 누가 정하나 | 세션끼리 메시지로 정한다(ADR-0012 Proposed) | flow의 Python 코드가 정한다 |
| 에이전트 간 전달 | 평문 메시지와 깨우기 | flow 코드가 한 에이전트의 출력을 다음 프롬프트로 넘긴다 |
| 사람의 자리 | 각 세션의 터미널. 범위 확대와 결정은 사람이 승인 | 실행 전에 flow와 권한을 고른다. 실행 중에는 끼어들기, `HumanAgent` 역할 |
| 승인 | 사람에게 묻는 것이 기본. 워커도 승인 창을 유지한다(ADR-0010) | 항상 건너뛴다 |
| 원격 | SSH 짝, 원격의 살아 있는 세션에 메시지 | SSH와 docker 환경에서 작업 실행 |
| 맞는 일 | 대화형 협업, 즉흥적 조율 | 정해진 절차의 반복 실행 |

## 8. xsm에 대한 시사점

| 판단 | 내용 |
|---|---|
| 보완 | 같은 철학이 두 방향으로 갈라졌다. humanize는 오케스트레이터가 소유하는 쪽, xsm은 사람이 소유한 세션에 붙는 쪽이다. xsm 세션이 반복 작업을 humanize flow로 맡기고 결과를 xsm으로 보고받는 조합이 가능하다 |
| 참고 | ADR-0012의 선택지로 "코드가 다음 노드를 고른다"의 실제 사례다 |
| 참고 | xsm 워커는 실제 TUI와 승인 창을 유지하고, humanize는 headless로 승인 없이 돈다. "워커를 headless로 띄우지 않는다"는 xsm의 선택(ADR-0010)을 다시 볼 때의 비교 대상이다 |
| 확인 필요 | humanize는 Codex를 별도 `codex app-server --stdio`로 구동한다. Codex 스레드에는 잠금이 없어서, 별도 app-server가 사람이 쓰는 TUI의 스레드를 이어받으면 안 된다(`README.md` 0.5절). humanize가 자기 스레드만 만든다면 문제가 없지만, 기존 스레드를 재개하는 기능이 있는지는 확인하지 않았다 |
