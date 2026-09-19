// S9 E2/E3 driver: uses the published cc-peer 1.5.0 exactly as agent-comms' default front does
// (CcPeer.create({name}) with no homeDir, peer.send(target, body) with no fromMode).
// Run with HOME pointed at a scratch home so registry/key writes stay out of the real ~/.claude.
import { CcPeer } from "/tmp/xsm-spike/s9/pkg/node_modules/cc-peer/dist/cc-peer.mjs";

const [cmd, ...args] = process.argv.slice(2);
const peer = await CcPeer.create({ name: "s9-ccpeer-front" });
try {
  const roster = await peer.roster();
  console.log(JSON.stringify({ home: process.env.HOME, roster: roster.map((e) => ({ pid: e.pid, name: e.name, cwd: e.cwd })) }));
  if (cmd === "send") {
    const [pid, probe] = args;
    const res = await peer.send({ pid: Number(pid) }, `[S9-E2 probe=${probe}] cc-peer default send (no fromMode). Reply with just: ack ${probe}`);
    console.log(JSON.stringify({ sent: probe, to: Number(pid), ...res }));
  }
} catch (e) {
  console.log(JSON.stringify({ error: e.name, message: e.message }));
} finally {
  await peer.stop();
}
