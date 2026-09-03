# ⚡ BioInfo SaaS (Pikachu) ·  OpenAI/Anthropic-Compatible BioInformatics API Gateway

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![aiohttp](https://img.shields.io/badge/aiohttp-3.14-green.svg)](https://docs.aiohttp.org/)
[![Protocols: OpenAI + Anthropic](https://img.shields.io/badge/protocols-OpenAI%20%2B%20Anthropic-orange.svg)](#)

> **Pikachu** is the production AI backend behind [mzydsg.me](https://mzydsg.me) — a
> single aiohttp process that exposes both the **OpenAI Chat Completions** protocol
> and the **Anthropic Messages** protocol on top of a bioinformatician-tuned AI
> agent, plus 67 bundled scientific skills, four pricing plans, a self-hosted
> sandbox for running generated code, and a built-in admin dashboard.

The architecture is heavily inspired by
[synthetic-sciences/openscience](https://github.com/synthetic-sciences/openscience)
(Apache-2.0, 3.4k stars) — we borrow the **protocol-agnostic core** + **skill
catalog** + **bring-your-own-key** philosophy, and skip the Bun / TypeScript /
Seatbelt desktop stack in favor of a tiny Python web app you can read end-to-end
in one sitting.

---

## What you get

- **OpenAI-compatible** `/v1/models` and `/v1/chat/completions` endpoints
- **Anthropic-compatible** `/v1/models` and `/v1/messages` endpoints
- 67-skill catalog at `GET /skills` (BLAST, AlphaFold, Scanpy, GSEA, nf-core, …)
- Pricing page at `GET /pricing` (Free / Pro / Team / Enterprise)
- Documentation at `GET /docs`
- Public changelog at `GET /changelog`
- User registration + JWT login + `sk_live_xxx` API keys
- Per-key rate limiting and per-user monthly quota
- **Sandboxed code execution** at `POST /api/v1/sandbox/run` — Python in a
  uid-1000 subprocess with `RLIMIT_AS` + `RLIMIT_CPU` + timeout
- **VPS resource metrics** at `GET /api/v1/admin/resources` and HTML at
  `GET /admin/metrics`
- **Billing dashboard** at `GET /api/v1/admin/billing` and HTML at
  `GET /admin/billing`
- Built-in setup / provisioning endpoint at `POST /setup` (admin token gated)
  for managing nginx, systemd, and the SQLite database from a single HTTP
  surface

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/mzydsg/bioinfo-saas.git
cd bioinfo-saas
pip install aiohttp  # that's the only runtime dep
```

### 2. Run

```bash
python3 main.py
# → Running on http://127.0.0.1:9101
# → SAAS_SETUP_TOKEN=<random 32-char secret, also written to /var/lib/bioinfo-saas/setup.token>
```

You only need three environment variables to override the defaults:

| Env var | Default | Meaning |
|---|---|---|
| `SAAS_PORT` | `9101` | Listen port |
| `SAAS_DB` | `/var/lib/bioinfo-saas/saas.db` | SQLite path |
| `SAAS_JWT_SECRET` | random 32-byte secret | JWT signing key |
| `SAAS_SETUP_TOKEN` | random 24-byte secret | Admin token for `/setup` and `/admin/*` |

### 3. Use it

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:9101/api/v1/openai/v1",
    api_key="sk_live_<issued from /api/v1/keys>",
)
resp = client.chat.completions.create(
    model="pikachu",
    messages=[{"role": "user", "content": "BLAST this sequence: ATG..."}],
)
print(resp.choices[0].message.content)
```

```python
from anthropic import Anthropic
client = Anthropic(
    base_url="http://localhost:9101/api/v1/anthropic/v1",
    api_key="sk_live_<issued from /api/v1/keys>",
)
msg = client.messages.create(
    model="pikachu", max_tokens=1024,
    messages=[{"role": "user", "content": "Recommend an RNA-seq DEG pipeline"}],
)
print(msg.content[0].text)
```

---

## File layout

```
bioinfo-saas/
├── main.py              # 27 KB  ·  aiohttp app, auth/setup middleware, /setup, /api/v1/{auth,keys,blast,...}
├── openai_compat.py     # 40 KB  ·  OpenAI + Anthropic compat + /pricing + /skills + /docs + /changelog
├── phase3_module.py     # 26 KB  ·  Sandbox execution + /api/v1/admin/* + /admin/{metrics,billing}
├── README.md            # you are here
├── LICENSE              # MIT
└── .gitignore
```

Everything is one process. No build step. No frontend pipeline. No ORM. Just
`aiohttp` + `sqlite3` + the standard library. The whole codebase is about
**2,800 lines of Python** and you can read it in one sitting.

---

## Endpoints

### Public

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/api/v1/health` | Same |
| `POST` | `/api/v1/auth/register` | `{email, password, plan}` → `{user_id, token, ...}` |
| `POST` | `/api/v1/auth/login` | `{email, password}` → `{user_id, token, ...}` |
| `GET` | `/api/v1/billing/plans` | 4 plans + prices + features |
| `GET` | `/api/v1/openai/v1/models` | OpenAI-compatible model list |
| `GET` | `/api/v1/anthropic/v1/models` | Anthropic-compatible model list |
| `GET` | `/pricing` | HTML pricing page |
| `GET` | `/skills` | JSON catalog of 67 skills |
| `GET` | `/docs` | HTML API documentation |
| `GET` | `/changelog` | HTML release log |

### Authenticated (Bearer JWT or `sk_live_xxx`)

| Method | Path | Cost |
|---|---|---|
| `GET` | `/api/v1/me` | 0 |
| `GET/POST/DELETE` | `/api/v1/keys` | 0 |
| `POST` | `/api/v1/openai/v1/chat/completions` | 1 / call |
| `POST` | `/api/v1/anthropic/v1/messages` | 1 / call |
| `POST` | `/api/v1/blast` | 5 |
| `GET` | `/api/v1/uniprot/{acc}` | 1 |
| `GET` | `/api/v1/gene/{symbol}` | 1 |
| `POST` | `/api/v1/pubmed` | 1 |
| `POST` | `/api/v1/sequence/analyze` | 1 |
| `GET` | `/api/v1/pathway/{kegg_id}` | 1 |
| `POST` | `/api/v1/sandbox/run` | 1 |
| `GET` | `/api/v1/sandbox/jobs` | 0 |

### Admin (X-Setup-Token header)

| Method | Path | Format |
|---|---|---|
| `GET/POST` | `/setup` | JSON, action-based |
| `GET` | `/setup/read` | JSON nginx config dump |
| `GET` | `/api/v1/admin/resources` | JSON VPS metrics |
| `GET` | `/api/v1/admin/billing` | JSON billing aggregation |
| `GET` | `/admin/metrics` | HTML metrics dashboard |
| `GET` | `/admin/billing` | HTML billing dashboard |

---

## Setup endpoint actions

The `/setup` endpoint lets you manage the entire production system over HTTPS
once you have the `X-Setup-Token`:

```bash
TOK=$(sudo head -1 /var/lib/bioinfo-saas/setup.token)

# Read current nginx site config
curl -H "X-Setup-Token: $TOK" \
     -H "Content-Type: application/json" \
     -X POST -d '{"action":"read-nginx-config"}' \
     https://mzydsg.me/setup

# Replace it (atomic: writes, syntax-checks, enables, reloads)
curl -H "X-Setup-Token: $TOK" \
     -H "Content-Type: application/json" \
     -X POST -d @payload.json \
     https://mzydsg.me/setup

# Reload nginx
curl -H "X-Setup-Token: $TOK" \
     -H "Content-Type: application/json" \
     -X POST -d '{"action":"reload-nginx"}' \
     https://mzydsg.me/setup

# Restart a service
curl -H "X-Setup-Token: $TOK" \
     -H "Content-Type: application/json" \
     -X POST -d '{"action":"restart-service","name":"bioinfo-saas"}' \
     https://mzydsg.me/setup
```

This is what `replace-nginx-config` writes to disk:

```nginx
# /etc/nginx/sites-available/bioinfo-portal  →  /etc/nginx/sites-enabled/default
server {
    listen 80; server_name mzydsg.me www.mzydsg.me;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2; server_name mzydsg.me www.mzydsg.me;
    # ... SSL, gzip, security headers ...

    # Phase 2: bioinfo-saas API (9101) — priority over bioinfo-chat (9000)
    location /api/v1/         { proxy_pass http://127.0.0.1:9101; ... }
    location = /pricing        { proxy_pass http://127.0.0.1:9101; }
    location = /docs           { proxy_pass http://127.0.0.1:9101; }
    location = /skills         { proxy_pass http://127.0.0.1:9101; }
    location = /changelog      { proxy_pass http://127.0.0.1:9101; }
    location = /setup          { proxy_pass http://127.0.0.1:9101; }
    location = /setup/read     { proxy_pass http://127.0.0.1:9101; }
    location = /admin/metrics  { proxy_pass http://127.0.0.1:9101; }
    location = /admin/billing  { proxy_pass http://127.0.0.1:9101; }

    # bioinfo-chat:9000 — keep for legacy AI chat
    location /api/ { proxy_pass http://127.0.0.1:9000; ... }

    location ~* \.(css|js|svg|png|jpg|jpeg|gif|ico|woff2?)$ { ... }
    location / { try_files $uri $uri/ $uri.html /index.html; }
    location ~ /\. { deny all; }
}
```

---

## Sandbox design

The `/api/v1/sandbox/run` endpoint runs LLM-generated Python in a hardened
subprocess:

```python
subprocess.run(
    ['sudo', '-u', 'ubuntu', '-H', '/usr/bin/python3', code_path],
    capture_output=True,
    timeout=user_timeout,           # 1..120 seconds
    preexec_fn=lambda: (
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem)),   # default 512MB
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu)),
        os.setgroups([1001]), os.setgid(1001), os.setuid(1000),
    ),
    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/home/ubuntu'},
)
```

Each run writes the user code to `/tmp/pikachu-sandbox/user_<id>/job_<uuid>.py`,
executes it, captures stdout / stderr, deletes the file, and records the call
in `usage_events`. Limits are enforced both at the OS level (rlimits) and at
the process level (timeout, output truncation).

The sandbox is intentionally simple. For hostile untrusted code, run it inside
a container or VM — the README explicitly says so. For trusted workflow
execution (LLM writing a script for a registered user), this is enough.

---

## What we borrowed from openscience

[openscience](https://github.com/synthetic-sciences/openscience) is a Bun /
TypeScript / Seatbelt / bubblewrap desktop AI workbench for science. We
borrowed four things, deliberately skipped the rest.

| | openscience | bioinfo-saas (this repo) |
|---|---|---|
| Protocol-agnostic core | OpenAI + Anthropic + Gemini + DeepSeek + local | OpenAI + Anthropic |
| Skill catalog | 312 skills, user-extensible | 67 skills, static JSON |
| Bring-your-own-key | ✅ | ✅ (users issue `sk_live_xxx`) |
| Local-first deployment | Desktop app, sandbox | Single Python process |
| Sandbox | macOS Seatbelt / Linux bubblewrap | `RLIMIT_AS` + `RLIMIT_CPU` + uid drop |
| Workflow runtimes | 5 Python caps + 10 BioNeMo NIM | direct NCBI/Ensembl/UniProt calls |
| Desktop UI | browser workspace with file tree / editor / terminal | none (web only) |

---

## Production deployment

The repo doesn't ship a Dockerfile or systemd unit — it ships the source
that's known to run. On the reference VPS (Ubuntu 22.04 + Python 3.12), the
deployment is two files:

```
/etc/systemd/system/bioinfo-saas.service
/opt/bioinfo-saas/{main.py, openai_compat.py, phase3_module.py}
/var/lib/bioinfo-saas/{saas.db, setup.token}
/etc/nginx/sites-available/bioinfo-portal  ←  /etc/nginx/sites-enabled/default
```

Systemd unit:

```ini
[Service]
Type=simple
User=root
Environment=SAAS_PORT=9101
Environment=SAAS_DB=/var/lib/bioinfo-saas/saas.db
WorkingDirectory=/opt/bioinfo-saas
ExecStart=/usr/bin/python3 /opt/bioinfo-saas/main.py
Restart=always
RestartSec=5
```

Setup is one-time per host:

```bash
sudo mkdir -p /opt/bioinfo-saas /var/lib/bioinfo-saas
sudo chown -R root:root /opt/bioinfo-saas /var/lib/bioinfo-saas
# Copy the three .py files into /opt/bioinfo-saas
sudo systemctl enable --now bioinfo-saas
# Tail journal; copy the SAAS_SETUP_TOKEN line for use with /setup
sudo journalctl -u bioinfo-saas -n 20 --no-pager
```

---

## Security notes

- `setup_auth_mw` requires `X-Setup-Token` for `/setup/*`, `/admin/*`, and
  `/api/v1/admin/*`. The token is generated on first start and stored at
  `/var/lib/bioinfo-saas/setup.token` with mode 0600. Read it once, store it
  in your password manager, and never commit it.
- `auth_mw` accepts both `Authorization: Bearer <jwt>` and `Authorization:
  Bearer <sk_live_xxx>` (or `x-api-key: <sk_live_xxx>` for Anthropic). The
  latter is what API clients use.
- The sandbox is documented as a **process-level** isolation, not a full
  jail. Do not run untrusted code in production without wrapping it in a
  container or VM.
- SQLite is fine for single-process workloads up to a few hundred writes per
  second. If you scale, swap it for Postgres — the only places that touch
  the DB are the `_db_ctx` context manager and the `usage_events` writes.

---

## License

MIT. See [LICENSE](LICENSE).

Inspired by [synthetic-sciences/openscience](https://github.com/synthetic-sciences/openscience)
(Apache-2.0). Pikachu (皮卡丘) is a trademark of The Pokémon Company; we use
the name affectionately to refer to our agent and the project is not
affiliated with or endorsed by them.
