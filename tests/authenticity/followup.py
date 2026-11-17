#!/usr/bin/env python3
"""Follow-up: true ~50k needle test, 131k empty-answer diagnosis, harder thinking, raw stream deltas."""
import json, random, string, sys, time, urllib.request, urllib.error
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from opus_battery import call, msg, BASE

MODELS = ["claude-opus-5-5", "claude-4-7-opus"]
WORDS = ["river", "stone", "ledger", "copper", "window", "orchard", "signal", "harbor", "lantern", "meadow",
         "engine", "canvas", "thunder", "pillow", "garden", "compass", "violet", "summit", "anchor", "fabric"]

def needle(m, n_sent, seed):
    rnd = random.Random(seed)
    n1 = "".join(rnd.choices(string.ascii_uppercase + string.digits, k=8))
    n2 = "".join(rnd.choices(string.ascii_uppercase + string.digits, k=8))
    s = [" ".join(rnd.choices(WORDS, k=12)) + "." for _ in range(n_sent)]
    s.insert(int(len(s) * .3), f"The first secret vault code is {n1}.")
    s.insert(int(len(s) * .9), f"The second secret vault code is {n2}.")
    q = " ".join(s) + "\n\nWhat are the first and second secret vault codes? Reply exactly as: FIRST=<code> SECOND=<code>"
    st, r, t = call({"model": m, "max_tokens": 200, "temperature": 0, "messages": [{"role": "user", "content": q}]}, timeout=600)
    if st != 200: return {"status": st, "err": str(r)[:300]}
    a = msg(r).get("content") or ""
    return {"prompt_tokens": r["usage"]["prompt_tokens"], "completion_tokens": r["usage"].get("completion_tokens"),
            "finish": r["choices"][0].get("finish_reason"), "secs": round(t, 1), "answer": a.strip()[:80],
            "ok": n1 in a and n2 in a}

HARD = ("How many positive integers n <= 2000 satisfy: n is divisible by 3 or 7, but not by 21, "
        "and the digit sum of n is even? Answer with just the number.")
TRUTH = sum(1 for n in range(1, 2001) if (n % 3 == 0 or n % 7 == 0) and n % 21 and sum(map(int, str(n))) % 2 == 0)

def thinking(m):
    out = {}
    for name, extra in {"none": {}, "adaptive": {"thinking": {"type": "adaptive"}},
                        "effort_high": {"reasoning_effort": "high"}}.items():
        st, r, t = call({"model": m, "max_tokens": 8000, "messages": [{"role": "user", "content": HARD}], **extra}, timeout=400)
        if st != 200: out[name] = {"status": st, "err": str(r)[:200]}; continue
        mm = msg(r); u = r.get("usage", {})
        out[name] = {"answer": (mm.get("content") or "").strip()[-20:], "correct": str(TRUTH) in (mm.get("content") or ""),
                     "reasoning_chars": len(mm.get("reasoning_content") or ""),
                     "thinking_blocks": len(mm.get("thinking_blocks") or []),
                     "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                     "completion_tokens": u.get("completion_tokens"), "secs": round(t, 1)}
    return out

def raw_stream(m):
    body = {"model": m, "stream": True, "max_tokens": 4000, "reasoning_effort": "high",
            "messages": [{"role": "user", "content": HARD}]}
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    keys, think, text = {}, 0, ""
    try:
        for line in urllib.request.urlopen(req, timeout=400):
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]": continue
            d = (json.loads(line[5:]).get("choices") or [{}])[0].get("delta", {})
            for k, v in d.items():
                if v: keys[k] = keys.get(k, 0) + 1
            think += len(d.get("reasoning_content") or "")
            for b in d.get("thinking_blocks") or []: think += len(b.get("thinking") or "")
            text += d.get("content") or ""
    except urllib.error.HTTPError as e:
        return {"status": e.code, "err": e.read().decode()[:200]}
    return {"delta_keys": keys, "think_chars": think, "answer": text.strip()[-20:], "correct": str(TRUTH) in text}

if __name__ == "__main__":
    print("TRUTH", TRUTH, flush=True)
    res = {}
    for m in MODELS:
        res[m] = {"ctx_50k": needle(m, 1700, m + "50"), "ctx_131k_retry": needle(m, 4400, m)}
        print(m, "ctx", json.dumps(res[m]), flush=True)
        res[m]["thinking"] = thinking(m); print(m, "think", json.dumps(res[m]["thinking"]), flush=True)
        res[m]["stream_thinking"] = raw_stream(m); print(m, "stream", json.dumps(res[m]["stream_thinking"]), flush=True)
    json.dump(res, open(__file__.rsplit("/", 1)[0] + "/followup.json", "w"), indent=1)
