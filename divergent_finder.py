import glob, json
from collections import defaultdict

seqs = defaultdict(list)
for p in glob.glob("episodes/*.json"):
    r = json.load(open(p))
    seqs[tuple(t["tool"] for t in r["trace"])].append(r["session_id"])

for calls, sessions in sorted(seqs.items(), key=lambda kv: -len(kv[1])):
    print(f"{len(sessions):3d}x  {list(calls)}\n      e.g. {sessions[0]}")