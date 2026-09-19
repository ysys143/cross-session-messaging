claude code, codex 같은 코딩 에이전트 세션들이 서로를 인식하고 메시지를 보낼 수 있는 기능을 구현하고자 함.

#### claude code의 /list-agents 와 cross-session messaging을 확장.

- 현재 cross-sessions messaging은 단순히 messaging channel이나 communicating board를 가지는 것을 넘어, 다른 세션에게 직접 메시지를 invoke할 수 있다는 특징을 지님. 그렇게 때문에 A 세션에서 B, C, D 세션에게 업무를 지시하고, 결과를 보고받는 등의 동작이 가능하며, 중간에 개입하거나 상호간 조율도 가능함.
- 그러나 현재 cross-sessions messaging의 문제점은 같은 CLAUDE_CONFIG_HOME을 공유하는 세션 간에만 인식 및 소통이 가능하다는 것.
- ~/.claude, ~/.claude-2, ~/.claude-3 처럼 서로 다른 클로드 코드 세션 간에도 메시징이 가능했으면 함.

#### codex에서 messaging 구현 및 agent runtime 확장

- 나아가 codex &lt;-&gt; codex, claude &lt;-&gt; codex 간 메시징도 가능하도록 확장.
- 이때 ~/.claude, ~/.claude-3, ~/.codex, ~/.codex-2 등의 CONFIG_DIR의 영향은 받지 않고 메시징이 활성화된 세션을 인식하고 메시지를 보낼 수 있는 기능.
- ssh로 연결된 다른 서버나 머신의 세션과도 소통이 가능하면 이상적일 것.
- 무분별하게 확장되면 곤란하므로, 프로젝트 별로 소통이 가능한 세션의 범위를 정할 수 있어도 좋을 것 같음(확정은 아니고 ADR은 몇차례 토론 라운드를 거쳐 결정)
- codex, claude 위에 오케스트레이션을 위한 별도의 진입점이 되는 런타임을 만들지 말 것. 작업은 codex, claude로 하되, 자연스럽게 다른 종류의 세션에게 메시지를 주고 받을 수 있기를 바람.

**추가1) 인간과 에이전트가 함께 볼 수 있는 messaging channel** 

- cross-sessions messaging과 같이 mailbox와 invoke/wakeup 기능은 즉시적 협업에 필수적이지만, 의사결정의 흐름을 기록하고 지속하기에는 부족함. 1:1 소통이 아니라 1:N, N:N 소통에 매우 적합한 채널-스레드가 필요.
- 이런 것들은 buzz, slack, discord 같은 서비스를 이용할 수도 있을 것 같음(특히 오픈소스 buzz를 이렇게 이용가능한지 검토). 아니면 엄청 가볍게 .md, .sqlite를 이용해 기록하는 규약으로 해결해볼 수도 있을 것 같음. 현재는 github issue tracker를 이렇게 이용 중이지만, github issue가 사람이 보기에 깔끔하게 관리되지 못하는 문제가 있음. github issue tracker는 깔끔한 정본만 담고 그보다 일상적인 에이전트 간 N:N 비동기 소통과 작업 기록, 중재는 다른 채널이 필요.
- 같이 조사한 것들을 문서에 기록하는데 충돌이 나지 않으면서도 사본이 마구 증식하거나 무한정 잠금/대기가 발생하지 않도록 할 수 있는 방법도 필요.
- agora 케이스도 함께 검토 arxiv.org/abs/2609.18094



&nbsp;

**추가2)  Graph Engineering과 Swarm Agents**

Swarm Agent, Graph Engineering에 이런 메시징 채널을 어떻게 쓸 수 있을지 고민을 확장.

[https://x.com/kirillk_web3/status/2087619214915826155](https://x.com/kirillk_web3/status/2087619214915826155)

[https://youtu.be/6AgOfiZOWiY?si=eflgAp3AjRmeVg1q](https://youtu.be/6AgOfiZOWiY?si=eflgAp3AjRmeVg1q)

Summary saved: /Users/jaesolshin/.local/share/open-scribe/transcript/OpenAI_researcher_on_agent_swarms_&amp;_recursive_self-improvement_summary.txt  
Summary copied to: /Users/jaesolshin/Downloads/OpenAI_researcher_on_agent_swarms_&amp;_recursive_self-improvement_summary.txt

[https://youtu.be/jLKQp4SgGr0?si=r9f0SBCDfadrGsM3](https://youtu.be/jLKQp4SgGr0?si=r9f0SBCDfadrGsM3)

Summary saved: /Users/jaesolshin/.local/share/open-scribe/transcript/한영자막_Cursor_핵심_개발자_Lauren_Tan_AI_에이전트를_실전에서_제대로_신뢰하는_법_xAI_GrokBot_워크숍_summary.txt  
Summary copied to: /Users/jaesolshin/Downloads/한영자막_Cursor_핵심_개발자_Lauren_Tan_AI_에이전트를_실전에서_제대로_신뢰하는_법_xAI_GrokBot_워크숍_summary.txt



**레퍼런스**

orca([https://github.com/stablyai/orca](https://github.com/stablyai/orca/blob/main/skills/orchestration/SKILL.md))

herdr([https://github.com/herdrdev/herdr](https://github.com/herdrdev/herdr))

moai-sdk([https://github.com/modu-ai/moai-adk](https://github.com/modu-ai/moai-adk))

buzz([https://github.com/block/buzz](https://github.com/block/buzz))



&nbsp;