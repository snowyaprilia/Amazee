# amazee-llm-proxy

OpenAI-compatible gateway in front of [amazee.ai](https://amazee.ai) anonymous free-trial
LiteLLM keys. It mints trial keys automatically, keeps a standby pool, and rotates keys when
one runs out of budget, so clients (Hermes, 9router, any OpenAI SDK) see one stable endpoint.

Done = `scripts/doctor.sh` ends with `0 failure(s)`. Agents: the whole setup contract is in
this file; nothing else needs reading.

## Requirements

- Docker + compose plugin v2.24+ (`docker compose version`)
- Outbound HTTPS to `api.amazee.ai`. No accounts, keys or credentials are needed.

## Quick start (clone and run)

```bash
git clone https://github.com/<owner>/amazee-llm-proxy.git
cd amazee-llm-proxy
cp .env.example .env          # optional edits: port, bind IP, API key
docker compose up -d --build
scripts/doctor.sh             # expects "0 failure(s)"
```

Point any OpenAI client at `http://127.0.0.1:8127/v1` (api key: anything, or your
`AMAZEE_PROXY_API_KEY` if set).

```bash
curl http://127.0.0.1:8127/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"claude-opus-5-5","stream":false,"messages":[{"role":"user","content":"hi"}]}'
```

## Setup steps in detail

1. `cp .env.example .env`
2. Check the port: `ss -tln | grep ':8127 '`. If busy, set `HOST_PORT` to a free port.
3. If other containers must call it, set `BIND_IP` to the docker bridge gateway
   (`docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'`).
   If anything beyond this host can reach it, set `AMAZEE_PROXY_API_KEY` (`openssl rand -hex 24`).
4. `docker compose up -d --build`
5. `scripts/doctor.sh`. `curl -s localhost:<HOST_PORT>/health` should then show `live`
   rising toward `min_standby` within a minute.

## What you may need to change (all in `.env`)

| Variable | Default | Change when |
|---|---|---|
| `HOST_PORT` | `8127` | the port is already in use on your host |
| `BIND_IP` | `127.0.0.1` | other containers must reach it (docker bridge gateway) or LAN use |
| `AMAZEE_PROXY_API_KEY` | empty (no auth) | anything other than you can reach the port. Set it before using `0.0.0.0` |
| `AMAZEE_PROXY_MIN_STANDBY` | `5` | raise for heavy long-context/agent use, lower for light use |

Other tuning vars are documented in `.env.example`.

## Rules

- Never commit `.env` or `data/` (both gitignored; `data/keys.db` holds live keys).
- Do not publish on `0.0.0.0` without `AMAZEE_PROXY_API_KEY`.

## Recommended models (tested 2026-11)

| Model id | Notes |
|---|---|
| `claude-opus-5-5` | best; thinking, streaming, tool calls, 130k+ context verified |
| `claude-sonnet-5-5` | faster, cheaper |
| `claude-5-opus`, `claude-4-8-opus`, `claude-4-7-opus` | older Opus; 4.7 has a stricter content filter |

The upstream catalog changes; `GET /v1/models` is the live list. Some listed models are
blocked for trial keys (403, passed straight to the client). With thinking enabled use
`max_tokens` of 16000 or more, or the model can spend the whole limit thinking.


## All models in the catalog (48, snapshot 2026-11)

This is what `GET /v1/models` returned. Being listed does not mean a trial key can call it: amazee's team policy blocks some models, and the list changes over time. Run `curl http://127.0.0.1:8127/v1/models` for the current list.

<details>
<summary>Anthropic Claude (12)</summary>

- `claude-opus-5-5`
- `claude-sonnet-5-5`
- `claude-haiku-5-5`
- `claude-5-opus`
- `claude-5-sonnet`
- `claude-4-8-opus`
- `claude-4-7-opus`
- `claude-4-6-opus`
- `claude-4-6-sonnet`
- `claude-4-5-opus`
- `claude-4-5-sonnet`
- `claude-4-5-haiku`

</details>

<details>
<summary>Google Gemini / Gemma (7)</summary>

- `gemini-3.5-flash`
- `gemini-3.5-flash-lite`
- `gemini-3.1-flash-lite`
- `gemini-2.5-flash-image`
- `gemma-4-31b`
- `gemma-4-26b-a4b`
- `gemma-4-e2b`

</details>

<details>
<summary>OpenAI (6)</summary>

- `gpt-4.1`
- `gpt-4.1-mini`
- `gpt-o4-mini`
- `gpt-oss-120b`
- `gpt-oss-20b`
- `gpt-4o-transcribe`

</details>

<details>
<summary>Amazon Nova (4)</summary>

- `nova-pro`
- `nova-2-lite`
- `nova-lite`
- `nova-micro`

</details>

<details>
<summary>Other chat models (10)</summary>

- `glm-5-2`
- `glm-4-7-flash`
- `minimax-m2-5`
- `minimax-m2-1`
- `qwen3-235b`
- `qwen3-32b`
- `qwen3-coder-30b-a3b`
- `devstral-2`
- `pixtral-large`
- `nvidia-nemotron-3-super-120b-a12b`

</details>

<details>
<summary>Embeddings (5)</summary>

- `embed-v4`
- `embed-english`
- `embed-multilingual`
- `titan-embed-text-v2:0`
- `embeddings`

</details>

<details>
<summary>Task aliases (amazee/Drupal presets) (4)</summary>

- `chat`
- `chat_with_complex_json`
- `chat_with_image_vision`
- `text_to_image`

</details>

## How keys are obtained

`POST https://api.amazee.ai/auth/generate-trial-access` with header `Referer: drupal-install`,
the same call the amazee.ai Drupal module makes for its free trial. No signup or credentials.
The reply carries a LiteLLM key (about $2 budget) and its endpoint URL. Keys are stored in
`data/keys.db`.

Key lifecycle:
- retired on 401, or on 402/403/400/422 whose error is about the key or budget (`budget_exceeded`)
- paused 120 s on 429
- every other 4xx (bad model, bad params, oversized prompt) goes back to the client, key untouched
- standby pool refilled immediately when a key retires, plus a periodic top-up

## Endpoints and status codes

`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`, `/health`,
`/admin/keys` (masked), `POST /admin/mint`, `POST /admin/cooldowns/clear`.
When `AMAZEE_PROXY_API_KEY` is set, `/v1/*` and `/admin/*` require it.

- 502: every key tried failed (`KEYS_PER_REQUEST`), or minting failed
- 400: malformed JSON body. 401: missing or wrong local API key

## Troubleshooting

| Symptom | Fix |
|---|---|
| doctor: health unreachable | `docker compose ps` / `docker compose logs`; port clash -> change `HOST_PORT` |
| compose rejects `env_file` `required` | compose plugin older than v2.24; upgrade |
| 403 model not allowed | model blocked for trial keys; use one from the table above |
| empty `content`, `finish_reason: length` | thinking used all tokens; raise `max_tokens` to 16000+ |
| repeated 502 mint failed | api.amazee.ai trial endpoint down or changed; nothing to fix locally |
| `claude-opus-4-7` not available | the upstream id is `claude-4-7-opus` |

## Tests

```bash
# offline, mock upstream, spends nothing
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests" --entrypoint python amazee-proxy tests/unit_test.py
# live, needs a running proxy, spends a little trial budget
python3 tests/baseline_test.py http://127.0.0.1:8127
PROXY_BASE=http://127.0.0.1:8127 python3 tests/authenticity/opus_battery.py claude-opus-5-5
```

## Wiring into a router (example: 9router)

Add an OpenAI-compatible provider node with base URL `http://<BIND_IP>:<HOST_PORT>/v1`, an
API key (placeholder if auth is off), and register the model ids above. Routers that stream
by default need `"stream": false` from clients that expect one JSON reply.

## Caveats

This relies on an undocumented free-trial endpoint. amazee.ai can rate-limit or close it at
any time, and heavy minting may break their trial terms.

## License

MIT, see [LICENSE](LICENSE). The license covers this proxy's code only. It grants no rights to amazee.ai's service; using its trial endpoint is subject to amazee's own terms.
