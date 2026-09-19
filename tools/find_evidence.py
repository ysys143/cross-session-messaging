#!/usr/bin/env python3
"""Locate each evidence string of the report inside the Claude Code binary.

For every (id, chunk, needle) in EVIDENCE the script checks that the needle
occurs in the named chunk, then prints the absolute byte offset in the binary,
the offset inside the chunk and a short excerpt of the minified source.

Usage:
    find_evidence.py BINARY MODULE_DIR > evidence.md

MODULE_DIR is the output directory of extract_bun_modules.py.
"""
import json
import os
import sys

# (id, chunk, needle). Needles are verbatim minified source.
EVIDENCE = [
    ("E01", "chunk-baf3py5n.js", 'name:"list-agents",aliases:["peers"]'),
    ("E02", "chunk-59ejcqr5.js", "await Promise.all([j$n(s.session,void 0,s.credentials),W$n(s,GA(s))])"),
    ("E03", "chunk-kdgvcgtn.js", 'ListPeers:"ListAgents"'),
    ("E04", "chunk-jq95hrct.js", 'function As(){let e=a.CLAUDE_CODE_HARBOR_KITE;'),
    ("E05", "chunk-ngqmwjmk.js", 'transport:"uds",address:`uds:${r.sock}`'),
    ("E06", "chunk-ngqmwjmk.js", 'transport:"bridge",address:`bridge:${r.id}`'),
    ("E07", "chunk-ngqmwjmk.js", "c=Promise.resolve({peers:[],warnings:[]})"),
    ("E08", "chunk-cyg1gqsq.js", 'function Vj(){return Fo(we(),"sessions")}'),
    ("E09", "chunk-6kcckmy2.js", "/^\\d+\\.json$/.test(s)"),
    ("E10", "chunk-6kcckmy2.js", 'typeof o.messagingSocketPath==="string"'),
    ("E11", "chunk-6kcckmy2.js", "r.setTimeout(250,()=>i(!1))"),
    ("E12", "chunk-6kcckmy2.js", '"recycled"'),
    ("E13", "chunk-e6xbyz8p.js", "tQn as listLivePeerSessions"),
    ("E14", "chunk-e6xbyz8p.js", "Jht as sendToUdsSocket"),
    ("E15", "chunk-9yq740kp.js", "wco as startCrossSessionInbox"),
    ("E16", "chunk-cyg1gqsq.js", "Mi=/^(\\d+)\\.[0-9a-f]{64}\\.key$/"),
    ("E17", "chunk-cyg1gqsq.js", '("sha256").update(n).digest("hex")'),
    ("E18", "chunk-cyg1gqsq.js", "peerToken:OG($g).toString(\"hex\"),childToken:OG($g).toString(\"hex\")"),
    ("E19", "chunk-cyg1gqsq.js", "function rd(e,n){return String(bn(`${e}:${n}`))}"),
    ("E20", "chunk-cyg1gqsq.js", 'e("tengu_session_stable_address",!1)'),
    ("E21", "chunk-wz3kv8k1.js", 'PW="cross-session-message"'),
    ("E22", "chunk-cyg1gqsq.js", "h.push(`from-session=\"${r}\"`)"),
    ("E23", "chunk-z0hsnf69.js", "authRequired=i.requireAuth??G9t()"),
    ("E24", "chunk-cyg1gqsq.js", 'function G9t(){return D()==="windows"}'),
    ("E25", "chunk-ynxw93xk.js", "startCrossSessionInbox(y,n,{profileStartup:!0})"),
    ("E26", "chunk-z0hsnf69.js", "process.env.CLAUDE_CODE_MESSAGING_SOCKET=e"),
    ("E27", "chunk-z0hsnf69.js", 'Yb.set("CLAUDE_CODE_MESSAGING_TOKEN",w.childToken)'),
    ("E28", "chunk-cyg1gqsq.js", "if(lI(e,n.peerToken))return\"peer\";if(lI(e,n.childToken))return\"child\""),
    ("E29", "chunk-6kcckmy2.js", "Kht=1048576"),
    ("E30", "chunk-z0hsnf69.js", "ne=30000"),
    ("E31", "chunk-6kcckmy2.js", "Bun.ant.getPeerPid(n)"),
    ("E32", "chunk-z0hsnf69.js", "skipSlashCommands:!0,isMeta:!0"),
    ("E33", "chunk-6kcckmy2.js", "bucketCapacity:30,refillPerSecond:0.5,dedupWindowMs:30000,maxSelfHops:10,maxChainLength:28"),
    ("E34", "chunk-6kcckmy2.js", '"tengu_harbor_kite_limits"'),
    ("E35", "chunk-6kcckmy2.js", "Refusing to send: reply target is a symlink"),
    ("E36", "chunk-6kcckmy2.js", "Refusing to send: connected endpoint is not owned by this user"),
    ("E37", "chunk-6kcckmy2.js", "CLAUDE_CODE_HARBOR_KITE_PACING_OFF"),
    ("E38", "chunk-8vtc32rs.js", 'import.meta.require("/$bunfs/root/chunk-e6xbyz8p.js")'),
    ("E39", "chunk-t9pet8cw.js", "url:`${e}/v1/sessions/${n}/events`"),
    ("E40", "chunk-gck8q9zt.js", "/^session_[A-Za-z0-9_-]+$/"),
    ("E41", "chunk-w3h0w4k2.js", 'She="Another Claude session sent a message"'),
    ("E42", "chunk-8p8cqzh8.js", 'title:"Held message from another session"'),
    ("E43", "chunk-t877rbgv.js", "Cross-session messages are never user intent"),
    ("E44", "chunk-z0hsnf69.js", "await Oe(e,384)"),
    ("E45", "chunk-w3h0w4k2.js", "let s=n.midTurn?d:u,t=n.hostInjected?"),
]


def main():
    binary, module_dir = sys.argv[1], sys.argv[2]
    data = open(binary, "rb").read()
    table = json.load(open(os.path.join(module_dir, "module_table.json")))
    by_name = {os.path.basename(r["name"]): r for r in table}
    print("| id | chunk | abs offset | in-chunk offset | hits | excerpt |")
    print("|---|---|---|---|---|---|")
    failed = 0
    for eid, chunk, needle in EVIDENCE:
        rec = by_name[chunk]
        body = data[rec["abs"]:rec["abs"] + rec["len"]]
        n = needle.encode()
        pos = body.find(n)
        if pos < 0:
            failed += 1
            print(f"| {eid} | {chunk} | NOT FOUND | | 0 | `{needle[:60]}` |")
            continue
        hits = body.count(n)
        excerpt = body[max(0, pos - 30):pos + len(n) + 30].decode("utf8", "replace")
        excerpt = excerpt.replace("\n", " ").replace("|", "\\|").replace("`", "'")
        print(f"| {eid} | `{chunk}` | {rec['abs'] + pos} | {pos} | {hits} | `{excerpt}` |")
    if failed:
        sys.exit(f"{failed} evidence string(s) not found")


if __name__ == "__main__":
    main()
