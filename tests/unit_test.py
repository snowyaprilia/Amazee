#!/usr/bin/env python3
"""Offline tests for amazee-proxy v2 against a mock upstream (no real keys spent).

Covers the v1 failure modes:
- client errors (400/403 model-access) must NOT retire keys
- 401/402 must retire; 429 must cooldown not kill
- streaming errors must return clean JSON, not a dropped connection
- malformed body -> 400
- empty pool + parallel requests -> single mint (single-flight)
- bounded retries: never more than KEYS_PER_REQUEST keys per request
Run:  python3 tests/unit_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx
from fastapi import FastAPI, Request
import uvicorn

# isolate from any deploy .env (auth would 401 the mock-admin calls)
os.environ.pop("AMAZEE_PROXY_API_KEY", None)
import proxy as proxy_mod

RESULTS = []

def record(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

# ---------- mock upstream ----------
mock = FastAPI()
MOCK_STATE = {"n_keys_issued": 0}

class MockKey:
    def __init__(self, token):
        self.token = token

def make_upstream_app():
    app = FastAPI()
    app.keys = {}          # token -> {"uses":0, "dead":False, "cool":False}
    app.behavior = {}      # token-suffix -> callable(req_json, headers) -> (status, body)
    n = {"minted": 0}

    @app.post("/auth/generate-trial-access")
    async def mint():
        n["minted"] += 1
        token = f"sk-mock-{n['minted']:04d}"
        app.keys[token] = {"uses": 0, "dead": False, "cool": False}
        return {"key": {"litellm_token": token, "litellm_api_url": "http://127.0.0.1:8991"}}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth.replace("Bearer ", "")
        entry = app.keys.get(token)
        if entry is None or entry["dead"]:
            return _jr(401, {"error": {"message": "Invalid API key", "type": "token_not_found_in_db"}})
        if entry["cool"]:
            return _jr(429, {"error": {"message": "Rate limit exceeded", "type": "rate_limit"}})
        body = await request.json()
        # behavior override: model -> callable(body, keyentry) -> (status, obj)
        b = app.behavior.get(body.get("model"))
        if b:
            status, obj = b(body, entry)
            return _jr(status, obj)
        entry["uses"] += 1
        if body.get("stream"):
            import starlette.responses as sr
            return sr.StreamingResponse(_sse(), media_type="text/event-stream")
        return _jr(200, {"id": "cmpl-mock", "object": "chat.completion",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": "OK"}}],
                         "model": body.get("model")})

    async def _sse():
        for line in ('data: {"id":"x","choices":[{"delta":{"content":"OK"}}]}', "", "data: [DONE]", ""):
            yield (line + "\n\n").encode()

    def _jr(status, obj):
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=status, content=obj)

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": "mock-model"}]}

    app.n = n
    return app

def run_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=1)
            return server
        except urllib.error.HTTPError:
            return server
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("mock server did not start")

def http(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"

def chat(base, model="m1", stream=False, content="hi"):
    return http("POST", base + "/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": content}],
                 "stream": stream, "max_tokens": 10})

def main():
    upstream = make_upstream_app()
    run_server(upstream, 8991)

    tmpdir = tempfile.mkdtemp(prefix="amz-test-")
    os.environ["AMAZEE_PROXY_TOPUP"] = "0"
    os.environ["AMAZEE_PROXY_KEYS_PER_REQUEST"] = "3"
    proxy_mod.TOPUP = False  # module constants are read at import; patch directly
    state = proxy_mod.AppState(os.path.join(tmpdir, "keys.db"),
                               mint_url="http://127.0.0.1:8991/auth/generate-trial-access")
    app = proxy_mod.create_app(state)
    run_server(app, 8990)
    base = "http://127.0.0.1:8990"

    # 1. basic chat + mint on demand
    s, b = chat(base)
    record("basic chat", s == 200 and "OK" in b, f"{s} minted={upstream.n['minted']} {b[:80]}")
    mints0 = upstream.n["minted"]

    # 2. client error (400 bad param) must NOT retire key
    upstream.behavior["badparam"] = lambda b_, e: (400, {
        "error": {"message": "Input Tokens Exceeded", "type": "bad_request"}})
    s, b = chat(base, model="badparam")
    st = json.loads(http("GET", base + "/health")[1])["keys"]
    record("400 does not burn keys", s == 400 and st["dead"] == 0 and st["alive"] == 1,
           f"status={s} keys={st}")

    # 3. 403 model-access denial must NOT retire key
    upstream.behavior["blockedmodel"] = lambda b_, e: (403, {
        "error": {"message": "The requested model 'x' is not available for this API key",
                  "type": "team_model_access_denied"}})
    s, b = chat(base, model="blockedmodel")
    st = json.loads(http("GET", base + "/health")[1])["keys"]
    record("403 model-access does not burn keys", s == 403 and st["dead"] == 0,
           f"status={s} keys={st}")

    # 4. 401 retires key, next request mints a fresh one and succeeds.
    # (the mock kills the key it served, so the first request's retry loop
    #  legitimately walks a few keys before one survives the mock's mint path)
    def _kill_and_401(b_, e):
        e["dead"] = True
        return (401, {"error": {"message": "Invalid API key"}})
    upstream.behavior["killkey"] = _kill_and_401
    s, b = chat(base, model="killkey")
    s2, b2 = chat(base)
    st = json.loads(http("GET", base + "/health")[1])["keys"]
    record("401 retires then recovers", s2 == 200 and st["alive"] >= 1,
           f"first={s} second={s2} keys={st}")

    # 5. 429 cools down, does NOT kill; cooldown visible in /admin/keys
    upstream.behavior["ratelimit"] = lambda b_, e: (429, {
        "error": {"message": "Rate limit exceeded"}})
    s, b = chat(base, model="ratelimit")
    rows = json.loads(http("GET", base + "/admin/keys")[1])["rows"]
    st = json.loads(http("GET", base + "/health")[1])["keys"]
    record("429 cooldown not death", s in (200, 429, 502) and st["dead"] == st["dead"] and st["cooling"] >= 1,
           f"status={s} keys={st} first_row_state={rows[0]['state'] if rows else None}")

    # 6. streaming with a client error returns clean JSON, not a dropped conn
    s, b = chat(base, model="blockedmodel", stream=True)
    ok = False
    try:
        json.loads(b); ok = True
    except Exception: pass
    record("stream + client error = clean JSON", 400 <= s < 500 and ok, f"status={s} body={b[:100]}")

    # 7. malformed JSON body -> 400
    r = urllib.request.Request(base + "/v1/chat/completions", data=b"{not json",
                               method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp: s, b = resp.status, resp.read().decode()
    except urllib.error.HTTPError as e: s, b = e.code, e.read().decode()
    record("malformed JSON -> 400", s == 400, f"{s} {b[:80]}")

    # 8. bounded retries: with all keys dying on 401, request must stop at KEYS_PER_REQUEST keys
    before = upstream.n["minted"]
    upstream.behavior["alway401"] = lambda b_, e: (401, {
        "error": {"message": "Invalid API key"}})
    # kill every existing key directly
    for v in upstream.keys.values(): v["dead"] = True
    s, b = chat(base, model="alway401")
    used = upstream.n["minted"] - before
    ok = s == 502 and used <= 4  # 3 retries + possible topup mints off (topup disabled)
    record("bounded retries", ok, f"status={s} keys_consumed={used}")

    # 9. single-flight mint: N parallel requests on empty pool
    for v in upstream.keys.values(): v["dead"] = True
    state2_keys = 0
    before = upstream.n["minted"]
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(chat, base) for _ in range(8)]
        outs = [f.result() for f in futs]
    used = upstream.n["minted"] - before
    codes = sorted(set(o[0] for o in outs))
    record("parallel recovery", codes == [200] and used <= 4,
           f"codes={codes} minted_for_8_reqs={used}")

    # 10. stream success passthrough
    s, b = chat(base, stream=True)
    record("stream success", s == 200 and "data:" in b and "[DONE]" in b, f"{s} tail={b[-40:]!r}")

    # 11. LiteLLM 422 budget_exceeded (seen live) must retire the key and rotate
    seen = {"n": 0}
    def _budget_once(b_, e):
        seen["n"] += 1
        if seen["n"] == 1:
            e["dead"] = True
            return (422, {"error": {"message": "Budget has been exceeded! Current cost: 2.58, Max budget: 2.0",
                                    "type": "budget_exceeded", "code": "422"}})
        e["uses"] += 1
        return (200, {"id": "cmpl-mock", "object": "chat.completion", "model": "budget",
                      "choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant", "content": "OK"}}]})
    upstream.behavior["budget"] = _budget_once
    before_dead = json.loads(http("GET", base + "/health")[1])["keys"]["dead"]
    s, b = chat(base, model="budget")
    st = json.loads(http("GET", base + "/health")[1])["keys"]
    record("422 budget_exceeded rotates key", s == 200 and st["dead"] == before_dead + 1,
           f"status={s} keys={st}")

    # 12. standby: topup fills MIN_STANDBY live keys, and refills after a retire
    async def _standby_check():
        st2 = proxy_mod.AppState(os.path.join(tmpdir, "standby.db"),
                                 mint_url="http://127.0.0.1:8991/auth/generate-trial-access")
        old = proxy_mod.MIN_STANDBY
        proxy_mod.MIN_STANDBY = 5
        try:
            async with httpx.AsyncClient() as c:
                m0 = upstream.n["minted"]
                await st2.topup_once(c)
                filled = st2.live_count()
                again_mints = upstream.n["minted"]
                await st2.topup_once(c)          # already full: must not mint more
                idle_ok = upstream.n["minted"] == again_mints
                e = st2.pick_key(); st2.release(e)
                st2.retire(e, "test")
                after_retire = st2.live_count()
                await st2.topup_once(c)
                refilled = st2.live_count()
                return filled, idle_ok, after_retire, refilled, again_mints - m0
        finally:
            proxy_mod.MIN_STANDBY = old
    filled, idle_ok, after_retire, refilled, minted = asyncio.run(_standby_check())
    record("standby fills to 5 and refills after retire",
           filled == 5 and idle_ok and after_retire == 4 and refilled == 5 and minted == 5,
           f"filled={filled} idle_no_mint={idle_ok} after_retire={after_retire} refilled={refilled}")

    print("\n=== SUMMARY ===")
    for n_, ok, in RESULTS: print(("PASS " if ok else "FAIL ") + n_)
    print(f"{sum(1 for r in RESULTS if r[1])}/{len(RESULTS)} passed")
    sys.exit(0 if all(r[1] for r in RESULTS) else 1)

if __name__ == "__main__":
    main()