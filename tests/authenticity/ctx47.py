#!/usr/bin/env python3
"""Opus 4.7 content_filter diagnosis: same ~50k haystack, neutral needle wording,
plus a natural-prose haystack and a 5k control. Also Opus 5.5 stream thinking with room to answer."""
import json, random, sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from opus_battery import call, msg

W = ["river", "stone", "ledger", "copper", "window", "orchard", "signal", "harbor", "lantern", "meadow",
     "engine", "canvas", "thunder", "pillow", "garden", "compass", "violet", "summit", "anchor", "fabric"]
PROSE = ("The village market opened early each morning. Farmers arranged baskets of apples, pears and squash "
         "while bakers carried warm bread from the ovens across the square. Children walked to school along the "
         "canal, and the old clock tower rang every hour as boats drifted slowly toward the harbor. ")

def run(m, mode, n):
    rnd = random.Random(mode + str(n))
    a, b = rnd.randint(10000, 99999), rnd.randint(10000, 99999)
    if mode == "salad":
        s = [" ".join(rnd.choices(W, k=12)) + "." for _ in range(n)]
    else:
        s = [PROSE] * n
    s.insert(int(len(s) * .3), f"Note: the library opens at gate number {a}.")
    s.insert(int(len(s) * .9), f"Note: the museum opens at gate number {b}.")
    q = " ".join(s) + "\n\nWhich gate numbers are mentioned for the library and the museum? Reply as: LIBRARY=<n> MUSEUM=<n>"
    st, r, t = call({"model": m, "max_tokens": 100, "temperature": 0, "messages": [{"role": "user", "content": q}]}, timeout=600)
    if st != 200:
        return {"status": st, "err": str(r)[:200]}
    ans = msg(r).get("content") or ""
    return {"prompt_tokens": r["usage"]["prompt_tokens"], "finish": r["choices"][0].get("finish_reason"),
            "secs": round(t, 1), "answer": ans.strip()[:60], "ok": str(a) in ans and str(b) in ans}

if __name__ == "__main__":
    m = "claude-4-7-opus"
    for mode, n in [("salad", 170), ("salad", 1700), ("prose", 900), ("prose", 2400)]:
        print(m, mode, n, json.dumps(run(m, mode, n)), flush=True)
    # Opus 5.5 streaming + thinking with enough room to finish the answer
    from followup import HARD, TRUTH
    import urllib.request
    from opus_battery import BASE
    body = {"model": "claude-opus-5-5", "stream": True, "max_tokens": 16000, "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": HARD}]}
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    think, text = 0, ""
    for line in urllib.request.urlopen(req, timeout=500):
        line = line.decode().strip()
        if not line.startswith("data:") or line == "data: [DONE]": continue
        d = (json.loads(line[5:]).get("choices") or [{}])[0].get("delta", {})
        think += len(d.get("reasoning_content") or ""); text += d.get("content") or ""
    print("opus55 stream+think", json.dumps({"think_chars": think, "answer": text.strip()[-30:],
                                            "correct": str(TRUTH) in text}), flush=True)
