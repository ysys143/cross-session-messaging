// S9 mesh driver: joins the agent-comms mesh as its own node via the published core API.
//   node mesh.mjs list [waitMs]
//   node mesh.mjs dm <nameOrId> <text> [streamingBehavior] [waitMs]
// Run with HOME pointed at the scratch home (identity/lock files land in $HOME/.agent-comms).
import { createBridgeMesh, ensureRegistered, buildAction } from "/tmp/xsm-spike/s9/pkg/node_modules/agent-comms/dist/core/index.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const [cmd, ...args] = process.argv.slice(2);
const slot = { harness: "s9drv", cwd: process.cwd() };
const { store, tool } = await createBridgeMesh(slot);
await store.init();
const reg = await ensureRegistered({ store, harness: "s9drv", cwd: process.cwd(), defaultName: "s9-driver" });
const ctx = { agentId: reg.agentId, harness: "s9drv", cwd: process.cwd(), pid: process.pid };
const call = (p) => tool.handle(ctx, buildAction(p));
const t0 = Date.now();
await sleep(Number(cmd === "list" ? args[0] ?? 3000 : 3000));

const agents = await store.listAgents(reg.agentId);
const brief = agents.map((a) => ({ id: a.id.slice(0, 12), name: a.name, harness: a.harness, status: a.status, cwd: a.cwd }));
console.log(JSON.stringify({ me: reg.agentId.slice(0, 12), coordinator: store.isCoordinator?.() ?? store.role, agents: brief }));

if (cmd === "dm") {
  const [who, text, sb, waitMs] = args;
  const target = agents.find((a) => a.name === who || a.id === who || a.id.startsWith(who));
  if (!target) {
    console.log(JSON.stringify({ error: "no such agent", who }));
  } else {
    const p = { action: "dm", target: target.id, content: text };
    if (sb) p.streamingBehavior = sb;
    const res = await call(p);
    console.log(JSON.stringify({ t: Date.now() - t0, dm: target.name, res }));
    await sleep(Number(waitMs ?? 4000));
  }
}
await store.shutdown?.();
process.exit(0);
