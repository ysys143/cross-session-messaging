"""Spike (2026-09-22, ADR-0005/0006): concurrent O_APPEND records and
first-come claims on one local file.

  python3 tools/spike_append_claims.py append   # 16 writers x 300 records, sizes 100 B / 4 KB / 60 KB
  python3 tools/spike_append_claims.py claims   # 50 rounds x 8 concurrent claimants

Measured on local APFS: no torn or lost record at any size; exactly one
winner in every round. Not measured on NFS or synced folders.
"""
import json, os, subprocess, sys, tempfile, time

def writer(path, who, n, size):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    for i in range(n):
        os.write(fd, (json.dumps({"who": who, "i": i, "body": "x" * size}) + "\n").encode())
    os.close(fd)

def claimant(path, who, target):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, (json.dumps({"who": who, "target": target, "t": time.time()}) + "\n").encode())
    os.close(fd)
    # Resolved by position in the file, not by timestamp: everything before our
    # own line was appended before it, and we read after our write.
    first = next(json.loads(l) for l in open(path) if json.loads(l)["target"] == target)
    print("WIN" if first["who"] == who else "LOSE")

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "_writer":
        writer(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])); sys.exit()
    if mode == "_claim":
        claimant(sys.argv[2], sys.argv[3], sys.argv[4]); sys.exit()
    d = tempfile.mkdtemp()
    if mode == "append":
        for size in (100, 4000, 60000):
            p = os.path.join(d, "log-%d.jsonl" % size)
            procs = [subprocess.Popen([sys.executable, __file__, "_writer", p, "w%d" % w, "300", str(size)])
                     for w in range(16)]
            [q.wait() for q in procs]
            ok = bad = 0
            seen = set()
            for line in open(p, "rb"):
                try:
                    e = json.loads(line); seen.add((e["who"], e["i"])); ok += 1
                except ValueError:
                    bad += 1
            print("size %d: ok %d corrupt %d unique %d expected %d" % (size, ok, bad, len(seen), 16 * 300))
    elif mode == "claims":
        one = 0
        for r in range(50):
            p = os.path.join(d, "claims-%d.jsonl" % r)
            procs = [subprocess.Popen([sys.executable, __file__, "_claim", p, "c%d" % c, "sec"],
                                      stdout=subprocess.PIPE, text=True) for c in range(8)]
            wins = sum(q.communicate()[0].strip() == "WIN" for q in procs)
            one += wins == 1
        print("rounds with exactly one winner: %d/50" % one)
