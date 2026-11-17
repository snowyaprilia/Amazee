#!/usr/bin/env python3
"""
amazee-proxy v2: OpenAI-compatible gateway in front of amazee.ai trial keys.

Rewrite of the original quick-and-dirty proxy. Key changes:
- In-memory key pool (SQLite only for persistence/restart survival); no per-request DB reads.
- Precise error classification: only real key/budget failures retire a key;
  429 = temporary cooldown; every other 4xx is passed straight back to the client.
- Single-flight minting (one mint at a time) + spare-key top-up loop.
- Clean JSON errors everywhere, including for streaming requests and malformed bodies.
- Bounded retries: a request uses at most KEYS_PER_REQUEST distinct keys.
- App factory so tests can inject a mock upstream.

Env:
    AMAZEE_PROXY_PORT=8127
    AMAZEE_PROXY_HOST=127.0.0.1
    AMAZEE_PROXY_DB=./keys.db
    AMAZEE_PROXY_API_KEY=            # if set, require Authorization: Bearer ...
    AMAZEE_PROXY_KEYS_PER_REQUEST=3  # max distinct keys tried per request
    AMAZEE_PROXY_TOPUP=1             # 1 = keep standby live keys minted
    AMAZEE_PROXY_MIN_STANDBY=5       # minimum live (not dead/cooling) keys kept ready
    AMAZEE_PROXY_TOPUP_INTERVAL=30   # seconds between top-up checks (also refills on retire)
    AMAZEE_PROXY_MODELS_TTL=600      # /v1/models cache seconds
    AMAZEE_PROXY_COOLDOWN_S=120      # cooldown after a 429
"""
import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---- config -------------------------------------------------------------
PORT = int(os.environ.get("AMAZEE_PROXY_PORT", "8127"))
HOST = os.environ.get("AMAZEE_PROXY_HOST", "127.0.0.1")
DB_PATH = os.environ.get(
    "AMAZEE_PROXY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.db"),
)
LOCAL_KEY = os.environ.get("AMAZEE_PROXY_API_KEY", "").strip()
KEYS_PER_REQUEST = int(os.environ.get("AMAZEE_PROXY_KEYS_PER_REQUEST", "3"))
TOPUP = os.environ.get("AMAZEE_PROXY_TOPUP", "1") == "1"
TOPUP_INTERVAL = int(os.environ.get("AMAZEE_PROXY_TOPUP_INTERVAL", "30"))
MIN_STANDBY = max(1, int(os.environ.get("AMAZEE_PROXY_MIN_STANDBY", "5")))
MODELS_TTL = int(os.environ.get("AMAZEE_PROXY_MODELS_TTL", "600"))
COOLDOWN_S = int(os.environ.get("AMAZEE_PROXY_COOLDOWN_S", "120"))
MINT_URL = "https://api.amazee.ai/auth/generate-trial-access"
MINT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "drupal-install",
    "User-Agent": "amazee-proxy/2.0",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amazee-proxy")


def _err(status: int, message: str, etype: str = "amazee_proxy_error", code: Any = None):
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": etype, "code": code if code is not None else status}
    })


# ---- error classification -------------------------------------------------
# RETIRE: the key itself is dead (invalid/exhausted/disabled). Try another key.
# COOLDOWN: key is fine but throttled right now. Skip it for a while.
# CLIENT: the caller's fault. Pass the upstream status/body back untouched.
RETIRE, COOLDOWN, CLIENT = "retire", "cooldown", "client"


def classify(status: int, body: str) -> tuple[str, Optional[str]]:
    """Classify an upstream error response. Returns (action, reason|None)."""
    low = body.lower()
    # LiteLLM budget exhaustion. Observed live as HTTP 422
    # {"type":"budget_exceeded","message":"Budget has been exceeded! ... Max budget: 2.0"}.
    # Status-independent: retire on the explicit marker whatever the code.
    if "budget_exceeded" in low or "budget has been exceeded" in low:
        return RETIRE, f"{status} budget_exceeded: {body[:160]}"
    # Authentication: the key is not in the upstream DB anymore.
    if status == 401:
        return RETIRE, f"401 auth: {body[:160]}"
    # Payment required: trial budget spent.
    if status == 402:
        return RETIRE, f"402: {body[:160]}"
    # 403: only a key/budget problem retires the key. Model-access denials
    # (team policy / bad model name) are universal -> client error.
    if status == 403:
        if any(s in low for s in ("budget", "quota", "credit", "disabled", "expired", "key")):
            # careful: "key" also appears in model-access messages
            if "not available for this api key" in low or "model_access" in low:
                return CLIENT, None
            return RETIRE, f"403: {body[:160]}"
        return CLIENT, None
    # Rate limit: pause, don't kill.
    if status == 429:
        return COOLDOWN, f"429: {body[:160]}"
    # Everything else (400 bad params, 404 model, 413, 5xx...) is not the key's fault.
    return CLIENT, None


# ---- key pool -------------------------------------------------------------
class KeyEntry:
    __slots__ = ("key", "url", "created_at", "last_used", "dead", "dead_reason",
                 "cooldown_until", "in_flight")

    def __init__(self, key: str, url: str, created_at: int):
        self.key = key
        self.url = url
        self.created_at = created_at
        self.last_used = 0
        self.dead = False
        self.dead_reason: Optional[str] = None
        self.cooldown_until = 0.0
        self.in_flight = 0


class AppState:
    def __init__(self, db_path: str, mint_url: str = MINT_URL):
        self.db_path = db_path
        self.mint_url = mint_url
        self.keys: dict[str, KeyEntry] = {}
        self.lock = threading.Lock()          # guards self.keys
        self.mint_lock = asyncio.Lock()       # single-flight minting
        self.models_cache: tuple[float, Any] = (0.0, None)
        self._topup_task: Optional[asyncio.Task] = None
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        self._load()

    # -- sqlite persistence (schema-compatible with the v1 database) --
    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_db(self):
        c = self._conn()
        c.execute("""CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            exhausted INTEGER NOT NULL DEFAULT 0,
            last_used INTEGER NOT NULL DEFAULT 0,
            exhaust_reason TEXT,
            cooldown_until REAL NOT NULL DEFAULT 0
        )""")
        # migrate a v1 database (no cooldown column)
        cols = [r[1] for r in c.execute("PRAGMA table_info(keys)").fetchall()]
        if "cooldown_until" not in cols:
            c.execute("ALTER TABLE keys ADD COLUMN cooldown_until REAL NOT NULL DEFAULT 0")
        c.close()

    def _load(self):
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT key, url, created_at, last_used, exhausted, exhaust_reason, cooldown_until "
                "FROM keys").fetchall()
        finally:
            c.close()
        with self.lock:
            for key, url, created_at, last_used, exhausted, reason, cd in rows:
                e = KeyEntry(key, url, created_at)
                e.last_used = last_used
                e.dead = bool(exhausted)
                e.dead_reason = reason
                e.cooldown_until = cd or 0.0
                self.keys[key] = e
        log.info("loaded %d keys from db (%d live)", len(self.keys), self.live_count())

    def _persist(self, e: KeyEntry):
        with contextlib.suppress(sqlite3.Error):
            c = self._conn()
            try:
                c.execute(
                    "INSERT OR REPLACE INTO keys(key,url,created_at,exhausted,last_used,"
                    "exhaust_reason,cooldown_until) VALUES(?,?,?,?,?,?,?)",
                    (e.key, e.url, e.created_at, int(e.dead), e.last_used,
                     e.dead_reason, e.cooldown_until))
            finally:
                c.close()

    # -- pool operations --
    def live_count(self) -> int:
        now = time.time()
        with self.lock:
            return sum(1 for e in self.keys.values()
                       if not e.dead and e.cooldown_until < now)

    def pick_key(self) -> Optional[KeyEntry]:
        """Least-recently-used live key."""
        now = time.time()
        with self.lock:
            best: Optional[KeyEntry] = None
            for e in self.keys.values():
                if e.dead or e.cooldown_until >= now:
                    continue
                if best is None or (e.last_used, e.created_at) < (best.last_used, best.created_at):
                    best = e
            if best:
                best.last_used = int(now)
                best.in_flight += 1
            return best

    def release(self, e: KeyEntry):
        with self.lock:
            e.in_flight = max(0, e.in_flight - 1)

    def retire(self, e: KeyEntry, reason: str):
        with self.lock:
            e.dead = True
            e.dead_reason = reason[:300]
        self._persist(e)
        log.warning("retired key %s...%s reason=%s", e.key[:8], e.key[-4:], reason[:120])
        self.kick_topup()

    def kick_topup(self):
        ev = getattr(self, "_topup_event", None)
        if ev is not None:
            ev.set()

    def cooldown(self, e: KeyEntry, reason: str):
        with self.lock:
            e.cooldown_until = time.time() + COOLDOWN_S
        self._persist(e)
        log.info("cooldown key %s...%s for %ds reason=%s",
                 e.key[:8], e.key[-4:], COOLDOWN_S, reason[:120])

    def stats(self) -> dict:
        now = time.time()
        with self.lock:
            total = len(self.keys)
            dead = sum(1 for e in self.keys.values() if e.dead)
            cooling = sum(1 for e in self.keys.values()
                          if not e.dead and e.cooldown_until >= now)
            return {"total": total, "alive": total - dead, "dead": dead,
                    "cooling": cooling, "in_flight": sum(e.in_flight for e in self.keys.values())}

    def masked_rows(self, limit: int = 50) -> list[dict]:
        now = time.time()
        with self.lock:
            rows = sorted(self.keys.values(), key=lambda e: -e.created_at)[:limit]
            return [{
                "key": f"{e.key[:10]}...{e.key[-6:]}",
                "url": e.url,
                "state": "dead" if e.dead else ("cooldown" if e.cooldown_until >= now else "live"),
                "created_at": e.created_at,
                "last_used": e.last_used,
                "in_flight": e.in_flight,
                "reason": e.dead_reason,
                "cooldown_remaining": max(0, int(e.cooldown_until - now)) if not e.dead else 0,
            } for e in rows]

    # -- minting --
    async def mint(self, client: httpx.AsyncClient) -> KeyEntry:
        """Mint exactly one new trial key (callers serialize via mint_lock)."""
        r = await client.post(self.mint_url, headers=MINT_HEADERS, content=b"", timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"mint failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        kblock = data.get("key") or {}
        key = (kblock.get("litellm_token") or data.get("litellm_token")
               or kblock.get("litellm_api_key") or data.get("litellm_api_key"))
        url = (kblock.get("litellm_api_url") or data.get("litellm_api_url") or "").rstrip("/")
        if not key or not url:
            raise RuntimeError(f"mint returned no key/url: {json.dumps(data)[:300]}")
        e = KeyEntry(key, url, int(time.time()))
        with self.lock:
            self.keys[key] = e
        self._persist(e)
        log.info("minted key %s...%s @ %s", key[:8], key[-4:], url)
        return e

    async def get_or_mint(self, client: httpx.AsyncClient) -> KeyEntry:
        e = self.pick_key()
        if e:
            return e
        async with self.mint_lock:
            e = self.pick_key()  # re-check: another coroutine may have minted
            if e:
                return e
            return await self.mint(client)

    async def topup_once(self, client: httpx.AsyncClient, spares: Optional[int] = None):
        """Mint until at least `spares` live keys exist. One mint per lock hold, so
        request-path minting is never blocked behind a whole refill batch."""
        target = MIN_STANDBY if spares is None else spares
        failures = 0
        while self.live_count() < target and failures < 3:
            async with self.mint_lock:
                if self.live_count() >= target:
                    break
                try:
                    await self.mint(client)
                except Exception as ex:
                    failures += 1
                    log.warning("topup mint failed (%d/3): %s", failures, ex)
                    await asyncio.sleep(2 * failures)

    async def topup_loop(self, client: httpx.AsyncClient):
        self._topup_event = asyncio.Event()
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._topup_event.wait(), TOPUP_INTERVAL)
            self._topup_event.clear()
            with contextlib.suppress(Exception):
                await self.topup_once(client)


# ---- app -----------------------------------------------------------------
def create_app(state: Optional[AppState] = None) -> FastAPI:
    app = FastAPI(title="amazee-proxy", version="2.0.0")
    state = state or AppState(DB_PATH)
    app.state.amz = state

    def _auth(authorization: Optional[str]):
        if not LOCAL_KEY:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer")
        if authorization.split(" ", 1)[1].strip() != LOCAL_KEY:
            raise HTTPException(401, "bad bearer")

    @app.on_event("startup")
    async def _start():
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
        await state.topup_once(app.state.http, spares=1)   # one key before serving; loop fills the rest
        if TOPUP:
            state._topup_task = asyncio.get_event_loop().create_task(
                state.topup_loop(app.state.http))
            await asyncio.sleep(0)
            state.kick_topup()  # fill standby immediately, not after the first interval

    @app.on_event("shutdown")
    async def _stop():
        if state._topup_task:
            state._topup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state._topup_task
        if getattr(app.state, "http", None):
            await app.state.http.aclose()

    def _client() -> httpx.AsyncClient:
        return app.state.http

    # ---- core forwarding ----
    async def _forward(path: str, payload: dict, want_stream: bool):
        """Returns (kind, value): kind 'json' -> (status, parsed_json_or_text),
        kind 'stream' -> AsyncGenerator of bytes."""
        tried = 0
        last_status, last_body = 0, ""
        while tried < KEYS_PER_REQUEST:
            entry = await state.get_or_mint(_client())
            tried += 1
            target = f"{entry.url}{path}"
            headers = {
                "Authorization": f"Bearer {entry.key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if want_stream else "application/json",
            }
            try:
                if want_stream:
                    req = _client().build_request("POST", target, headers=headers, json=payload)
                    resp = await _client().send(req, stream=True)
                else:
                    resp = await _client().post(target, headers=headers, json=payload)
            except httpx.HTTPError as e:
                state.release(entry)
                last_status, last_body = 502, f"upstream network error: {e}"
                log.warning("upstream network err: %s", e)
                continue  # network error: retry (same or another key)

            if resp.status_code < 400:
                state.release(entry)
                if want_stream:
                    return "stream", _stream_iter(resp)
                try:
                    return "json", (resp.status_code, resp.json())
                except Exception:
                    return "json", (resp.status_code, resp.text)

            # error path: read body (works for both streamed and normal)
            body_text = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            state.release(entry)
            action, reason = classify(resp.status_code, body_text)
            if action == RETIRE:
                state.retire(entry, reason or body_text[:160])
                last_status, last_body = resp.status_code, body_text
                continue
            if action == COOLDOWN:
                state.cooldown(entry, reason or body_text[:160])
                last_status, last_body = resp.status_code, body_text
                continue
            # CLIENT: caller's fault — pass through unchanged
            try:
                return "json", (resp.status_code, json.loads(body_text))
            except Exception:
                return "json", (resp.status_code,
                                {"error": {"message": body_text, "type": "upstream_error"}})

        # All retries exhausted due to key-level failures — that's a proxy-side
        # problem, not the client's: never echo the upstream 401/429 back.
        return "json", (502,
                        {"error": {"message": f"amazee-proxy: tried {tried} keys without success. "
                                               f"Last error: {str(last_body)[:400]}",
                                   "type": "amazee_proxy_error"}})

    async def _stream_iter(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    async def _handle(path: str, req: Request):
        _auth(req.headers.get("authorization"))
        try:
            payload = await req.json()
        except Exception:
            return _err(400, "request body is not valid JSON")
        if not isinstance(payload, dict):
            return _err(400, "request body must be a JSON object")
        want_stream = bool(payload.get("stream"))
        kind, value = await _forward(path, payload, want_stream)
        if kind == "stream":
            return StreamingResponse(value, media_type="text/event-stream")
        status, body = value
        return JSONResponse(status_code=status, content=body)

    # ---- routes ----
    @app.get("/health")
    def health():
        return {"ok": True, "version": "2.1.0", "min_standby": MIN_STANDBY, "keys": state.stats()}

    @app.get("/v1/models")
    async def list_models(request: Request, authorization: Optional[str] = Header(None)):
        _auth(authorization)
        ts, cached = state.models_cache
        if cached is not None and time.time() - ts < MODELS_TTL:
            return JSONResponse(content=cached)
        for _ in range(KEYS_PER_REQUEST):
            entry = await state.get_or_mint(_client())
            try:
                r = await _client().get(f"{entry.url}/v1/models",
                                        headers={"Authorization": f"Bearer {entry.key}"},
                                        timeout=30)
            except httpx.HTTPError as e:
                state.release(entry)
                if cached is not None:
                    return JSONResponse(content=cached)  # stale cache beats an error
                return _err(502, f"upstream network error: {e}")
            state.release(entry)
            if r.status_code == 200:
                try:
                    data = r.json()
                    state.models_cache = (time.time(), data)
                    return JSONResponse(content=data)
                except Exception:
                    pass
            action, reason = classify(r.status_code, r.text)
            if action == RETIRE:
                state.retire(entry, reason or r.text[:160])
                continue
            if action == COOLDOWN:
                state.cooldown(entry, reason or r.text[:160])
                continue
            return JSONResponse(status_code=r.status_code, content=r.text)
        return _err(502, "could not list models: no usable key")

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        return await _handle("/v1/chat/completions", req)

    @app.post("/v1/completions")
    async def completions(req: Request):
        return await _handle("/v1/completions", req)

    @app.post("/v1/embeddings")
    async def embeddings(req: Request):
        return await _handle("/v1/embeddings", req)

    @app.post("/admin/mint")
    async def admin_mint(request: Request):
        _auth(request.headers.get("authorization"))
        try:
            e = await state.mint(_client())
        except Exception as e:
            return _err(502, f"mint failed: {e}")
        return {"ok": True, "key_tail": e.key[-6:], "url": e.url, "stats": state.stats()}

    @app.get("/admin/keys")
    async def admin_keys(request: Request):
        _auth(request.headers.get("authorization"))
        return {"stats": state.stats(), "rows": state.masked_rows()}

    @app.post("/admin/cooldowns/clear")
    async def admin_clear_cooldowns(request: Request):
        _auth(request.headers.get("authorization"))
        now = time.time()
        with state.lock:
            n = 0
            for e in state.keys.values():
                if not e.dead and e.cooldown_until > now:
                    e.cooldown_until = 0.0
                    state._persist(e)
                    n += 1
        return {"ok": True, "cleared": n, "stats": state.stats()}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
