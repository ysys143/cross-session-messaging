// S9 E6 long-running mesh node. One process per directory (identity slot = harness+cwd).
//   HOME=<scratch> node node.mjs <name>
// Logs JSON lines to ./node.log. Auto-accepts room join requests (DM consent) so E6 isolates
// coordinator/queue behaviour from the consent flow measured in E1.
// Commands: append one JSON object per line to ./cmd.jsonl, e.g.
//   {"action":"create_room","room":"e6","type":"public"}   {"action":"send","room":"e6","content":"x"}
//   {"action":"dm","agent":"<name>","content":"x"}          {"action":"read_room","room":"e6"}
import fs from "node:fs";
const PKG = process.env.S9_PKG ?? "/tmp/xsm-spike/s9/pkg";
const { createBridgeMesh, ensureRegistered, buildAction, formatDeliveryEvent } = await import(`${PKG}/node_modules/agent-comms/dist/core/index.js`);

const name = process.argv[2];
const log = (o) => fs.appendFileSync("node.log", JSON.stringify({ ts: Date.now(), pid: process.pid, ...o }) + "\n");
// S9_HUB overrides the relay hub (default wss://mesh.exadev.io/); E5 points it at a dead local port to stay tailnet-only.
const { store, tool } = await createBridgeMesh({ harness: "s9node", cwd: process.cwd() }, process.env.S9_HUB ? { hubUrl: process.env.S9_HUB } : undefined);
let ctx;
store.onDelivery = async (_target, event) => {
  log({ ev: event.type, text: formatDeliveryEvent(event), ...(event.type === "connection_request" ? { raw: event } : {}) });
  if (event.type === "room_join_request" && ctx) {
    const r = await tool.handle(ctx, buildAction({ action: "room_accept", room: event.room, requesterId: event.requesterId }));
    log({ auto_accept: r.content, isError: r.isError });
  }
};
await store.init();
const reg = await ensureRegistered({ store, harness: "s9node", cwd: process.cwd(), defaultName: name });
ctx = { agentId: reg.agentId, harness: "s9node", cwd: process.cwd(), pid: process.pid };
log({ up: name, agentId: reg.agentId.slice(0, 12) });
// Graceful path, like the real bridges' SIGTERM handler: store.shutdown() runs the coordinator handover.
process.on("SIGTERM", () => {
  log({ sigterm: true });
  store.shutdown().catch(() => {}).finally(() => process.exit(0));
});

let offset = fs.existsSync("cmd.jsonl") ? fs.statSync("cmd.jsonl").size : 0;
setInterval(async () => {
  if (!fs.existsSync("cmd.jsonl")) return;
  const size = fs.statSync("cmd.jsonl").size;
  if (size <= offset) return;
  const chunk = fs.readFileSync("cmd.jsonl", "utf8").slice(offset);
  offset = size;
  for (const line of chunk.split("\n").filter(Boolean)) {
    const p = JSON.parse(line);
    if (p.action === "dm" && p.agent) {
      const agents = await store.listAgents(ctx.agentId);
      const t = agents.find((a) => a.name === p.agent);
      if (t) p.agent = t.id;
    }
    const t0 = Date.now();
    try {
      const r = await tool.handle(ctx, buildAction(p));
      log({ cmd: p.action, ms: Date.now() - t0, res: String(r.content).slice(0, 400), isError: r.isError });
    } catch (e) {
      log({ cmd: p.action, ms: Date.now() - t0, error: e.message });
    }
  }
}, 300);
