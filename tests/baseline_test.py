#!/usr/bin/env python3
"""Black-box baseline test of the running amazee-proxy (no code changes).
Usage: python3 baseline_test.py [base_url]"""
import json, sys, time, threading, urllib.request, urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8127"
MODEL = "gpt-4.1-mini"
results = []

def req(method, path, body=None, timeout=120, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
            return resp.status, txt, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t

def stats():
    s, b, _ = req("GET", "/health")
    try: return json.loads(b)["keys"]
    except Exception: return {"err": b[:100]}

def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

def chat(model=MODEL, content="Reply with exactly: OK", **kw):
    body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 20}
    body.update(kw)
    return req("POST", "/v1/chat/completions", body)

# 1 health
s, b, _ = req("GET", "/health"); record("health", s == 200, f"{s} {b[:120]}")
# 2 models
s, b, _ = req("GET", "/v1/models")
try: ids = [m["id"] for m in json.loads(b)["data"]]
except Exception: ids = []
record("models", s == 200 and ids, f"{s} {len(ids)} models")
print("   models:", ", ".join(sorted(ids)))
# 3 chat non-stream
s, b, dt = chat()
try: txt = json.loads(b)["choices"][0]["message"]["content"]
except Exception: txt = b[:200]
record("chat non-stream", s == 200, f"{s} {dt:.1f}s -> {txt!r}")
# 4 chat stream
s, b, dt = chat(stream=True)
record("chat stream", s == 200 and "data:" in b and "[DONE]" in b,
       f"{s} {dt:.1f}s chunks={b.count('data:')} done={'[DONE]' in b} head={b[:80]!r}")
# 5 legacy completions
s, b, _ = req("POST", "/v1/completions", {"model": MODEL, "prompt": "Say OK", "max_tokens": 5})
record("completions", s == 200, f"{s} {b[:160]}")
# 6 embeddings (pick an embedding-looking model if listed)
emb = next((m for m in ids if "embed" in m.lower()), None)
s, b, _ = req("POST", "/v1/embeddings", {"model": emb or "text-embedding-3-small", "input": "hi"})
record(f"embeddings ({emb})", s == 200, f"{s} {b[:160]}")
# 7 tool calling passthrough
tools = [{"type": "function", "function": {"name": "get_weather", "description": "weather",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
s, b, _ = chat(content="What's the weather in Paris? Use the tool.", tools=tools, max_tokens=100)
try: tc = json.loads(b)["choices"][0]["message"].get("tool_calls")
except Exception: tc = None
record("tool calling", s == 200 and bool(tc), f"{s} tool_calls={bool(tc)} {b[:120] if not tc else ''}")

# --- negative / key-burn tests: a client error must NOT kill keys ---
def burn_test(name, **kw):
    before = stats(); s, b, dt = chat(**kw); after = stats()
    burned = after.get("total", 0) - before.get("total", 0)
    ok = 400 <= s < 500 and burned == 0
    record(name, ok, f"status={s} {dt:.1f}s keys_minted={burned} before={before} after={after} body={b[:200]!r}")

burn_test("bad model (non-stream)", model="no-such-model-xyz")
burn_test("team-blocked model gpt-5-mini", model="gpt-5-mini")
# context too long: amazee truncates rather than erroring for gpt-4.1-mini
# (finish_reason "length"), so accept both a clean 4xx and a truncated 200 —
# what must NOT happen is key churn.
before = stats(); s, b, dt = chat(content="word " * 400000); after = stats()
burned = after.get("total", 0) - before.get("total", 0)
truncated = False
try: truncated = json.loads(b)["choices"][0]["finish_reason"] == "length"
except Exception: pass
record("context too long", (400 <= s < 500 or (s == 200 and truncated)) and burned == 0,
       f"status={s} {dt:.1f}s keys_minted={burned} truncated={truncated}")
burn_test("invalid param (max_tokens=-5)", max_tokens=-5)

# stream + error: should return a clean JSON/HTTP error, not crash
before = stats(); s, b, dt = chat(model="no-such-model-xyz", stream=True); after = stats()
record("bad model (stream)", 400 <= s < 500,
       f"status={s} minted={after.get('total',0)-before.get('total',0)} body={b[:200]!r}")

# malformed JSON body
r = urllib.request.Request(BASE + "/v1/chat/completions", data=b"{not json", method="POST",
                           headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=30) as resp: s, b = resp.status, resp.read().decode()
except urllib.error.HTTPError as e: s, b = e.code, e.read().decode()
record("malformed JSON", 400 <= s < 500, f"{s} {b[:120]!r}")

# concurrency: 8 parallel chats
out = []
def worker(i): out.append(chat(content=f"Reply with exactly: C{i}"))
before = stats(); th = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
t0 = time.time(); [t.start() for t in th]; [t.join() for t in th]; after = stats()
codes = [o[0] for o in out]
record("8 concurrent chats", all(c == 200 for c in codes),
       f"codes={codes} {time.time()-t0:.1f}s minted={after.get('total',0)-before.get('total',0)}")

print("\n=== SUMMARY ===")
for n, ok, _ in results: print(("PASS " if ok else "FAIL ") + n)
print(f"{sum(1 for r in results if r[1])}/{len(results)} passed; final pool {stats()}")
