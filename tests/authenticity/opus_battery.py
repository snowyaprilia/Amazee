#!/usr/bin/env python3
"""Opus 5.5 / 4.7 battery via local amazee proxy: authenticity fingerprint,
parallel, thinking, streaming, tool calls, 50k-context needles.
Writes results JSON next to this file. Stdlib only."""
import json, time, random, string, sys, threading, urllib.request, urllib.error, os

import os
BASE = os.environ.get("PROXY_BASE", "http://127.0.0.1:8127") + "/v1"
MODELS = sys.argv[1:] or ["claude-opus-5-5", "claude-4-7-opus"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
results = {}

def call(body, timeout=300, stream=False):
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        if not stream:
            return r.status, json.loads(r.read()), time.time() - t0
        chunks, first, text, think = 0, None, "", ""
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            d = json.loads(line[5:])
            ch = (d.get("choices") or [{}])[0].get("delta", {})
            if ch.get("content") or ch.get("reasoning_content"):
                chunks += 1
                if first is None:
                    first = time.time() - t0
            text += ch.get("content") or ""
            think += ch.get("reasoning_content") or ""
        return r.status, {"chunks": chunks, "ttft": first, "text": text, "think_len": len(think)}, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400], time.time() - t0
    except Exception as e:
        return -1, repr(e)[:400], time.time() - t0

def msg(r):
    try: return r["choices"][0]["message"]
    except Exception: return {}

PROBES = [
    "Hello.",
    "The quick brown fox jumps over the lazy dog. 1234567890 !@#$%^&*()",
    "日本語のテキスト、中文文本、한국어 텍스트, emoji 🦊🔥🧪, Ελληνικά, עברית, ภาษาไทย",
    "def f(x):\n    return [i**2 for i in range(x) if i % 3 == 0]  # comment\n" * 3,
]

def fingerprint(m):
    out = {"prompt_tokens": []}
    for p in PROBES:
        s, r, _ = call({"model": m, "messages": [{"role": "user", "content": p}], "max_tokens": 1, "temperature": 0})
        out["prompt_tokens"].append(r["usage"]["prompt_tokens"] if s == 200 else f"ERR {s}")
        if s == 200 and "id" not in out:
            out["id_prefix"] = r.get("id", "")[:12]
            out["model_field"] = r.get("model")
    return out

def test_parallel(m):
    res = [None, None]
    def w(i):
        res[i] = call({"model": m, "max_tokens": 200, "messages": [{"role": "user",
                       "content": f"Count from 1 to 40 separated by spaces. Run {i}."}]})
    t0 = time.time()
    ts = [threading.Thread(target=w, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wall = time.time() - t0
    each = [round(r[2], 1) for r in res]
    return {"status": [r[0] for r in res], "each_s": each, "wall_s": round(wall, 1),
            "truly_parallel": all(r[0] == 200 for r in res) and wall < sum(each) * 0.8}

def test_thinking(m):
    out = {}
    variants = {
        "adaptive": {"thinking": {"type": "adaptive"}},
        "enabled_budget": {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        "reasoning_effort": {"reasoning_effort": "medium"},
    }
    q = "A bat and ball cost $1.10 total. The bat costs $1.00 more than the ball. Ball price in cents? Answer with just the number."
    for name, extra in variants.items():
        body = {"model": m, "max_tokens": 4096, "messages": [{"role": "user", "content": q}], **extra}
        s, r, t = call(body)
        if s != 200:
            out[name] = {"status": s, "err": r[:200]}; continue
        mm = msg(r)
        tb = mm.get("thinking_blocks") or []
        out[name] = {"status": s, "answer": (mm.get("content") or "").strip()[:40],
                     "reasoning_chars": len(mm.get("reasoning_content") or ""),
                     "thinking_blocks": len(tb),
                     "has_signature": any(b.get("signature") for b in tb if isinstance(b, dict)),
                     "reasoning_tokens": (r.get("usage", {}).get("completion_tokens_details") or {}).get("reasoning_tokens"),
                     "secs": round(t, 1)}
    return out

def test_stream(m):
    s, r, t = call({"model": m, "stream": True, "max_tokens": 300,
                    "messages": [{"role": "user", "content": "Write 5 short sentences about the sea."}]}, stream=True)
    if s != 200: return {"status": s, "err": str(r)[:200]}
    return {"status": s, "chunks": r["chunks"], "ttft_s": round(r["ttft"] or -1, 2), "total_s": round(t, 1),
            "chars": len(r["text"])}

def test_stream_thinking(m):
    s, r, t = call({"model": m, "stream": True, "max_tokens": 4096, "thinking": {"type": "adaptive"},
                    "messages": [{"role": "user", "content": "Is 391 prime? Think, then answer yes/no."}]}, stream=True)
    if s != 200: return {"status": s, "err": str(r)[:200]}
    return {"status": s, "chunks": r["chunks"], "think_chars": r["think_len"], "answer": r["text"].strip()[-60:]}

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Get current weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
                         "required": ["city"]}}}]

def test_tools(m):
    msgs = [{"role": "user", "content": "What's the weather in Singapore and in Oslo, in celsius? Use the tool."}]
    s, r, _ = call({"model": m, "max_tokens": 500, "messages": msgs, "tools": TOOLS})
    if s != 200: return {"status": s, "err": str(r)[:200]}
    mm = msg(r); tcs = mm.get("tool_calls") or []
    out = {"status": s, "finish": r["choices"][0].get("finish_reason"),
           "calls": [(c["function"]["name"], c["function"]["arguments"]) for c in tcs]}
    if tcs:  # round trip: feed results back
        msgs.append(mm)
        for c in tcs:
            city = json.loads(c["function"]["arguments"]).get("city", "?")
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps({"city": city, "temp_c": 31 if "Sing" in city else -4})})
        s2, r2, _ = call({"model": m, "max_tokens": 300, "messages": msgs, "tools": TOOLS})
        out["followup_status"] = s2
        out["followup"] = (msg(r2).get("content") or "")[:200] if s2 == 200 else str(r2)[:200]
    return out

def test_50k(m):
    rnd = random.Random(m)
    words = ["river", "stone", "ledger", "copper", "window", "orchard", "signal", "harbor", "lantern", "meadow",
             "engine", "canvas", "thunder", "pillow", "garden", "compass", "violet", "summit", "anchor", "fabric"]
    n1 = "".join(rnd.choices(string.ascii_uppercase + string.digits, k=8))
    n2 = "".join(rnd.choices(string.ascii_uppercase + string.digits, k=8))
    sents = [" ".join(rnd.choices(words, k=12)) + "." for _ in range(4400)]
    sents.insert(int(len(sents) * 0.30), f"The first secret vault code is {n1}.")
    sents.insert(int(len(sents) * 0.90), f"The second secret vault code is {n2}.")
    doc = " ".join(sents)
    q = doc + "\n\nWhat are the first and second secret vault codes? Reply exactly as: FIRST=<code> SECOND=<code>"
    s, r, t = call({"model": m, "max_tokens": 60, "temperature": 0, "messages": [{"role": "user", "content": q}]}, timeout=600)
    if s != 200: return {"status": s, "err": str(r)[:300]}
    a = (msg(r).get("content") or "")
    return {"status": s, "prompt_tokens": r["usage"]["prompt_tokens"], "secs": round(t, 1), "answer": a.strip()[:80],
            "needle1_ok": n1 in a, "needle2_ok": n2 in a}

if __name__ == "__main__":
    ref = [x for x in ["claude-4-5-haiku", "gpt-4.1-mini"] if x not in MODELS]
    results["fingerprint"] = {m: fingerprint(m) for m in MODELS + ref}
    for m in MODELS:
        r = {}
        for name, fn in [("parallel", test_parallel), ("thinking", test_thinking), ("stream", test_stream),
                         ("stream_thinking", test_stream_thinking), ("tools", test_tools), ("ctx_50k", test_50k)]:
            try: r[name] = fn(m)
            except Exception as e: r[name] = {"exception": repr(e)[:300]}
            print(m, name, json.dumps(r[name])[:400], flush=True)
        results[m] = r
    json.dump(results, open(OUT, "w"), indent=1)
    print("FINGERPRINT", json.dumps(results["fingerprint"], indent=1))
