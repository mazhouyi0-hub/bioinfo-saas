"""BioInfo Portal · SaaS API Gateway (unified build)
- User system (email + password + JWT)
- API key auth (sk_live_xxx)
- Per-key rate limiting (in-process token bucket)
- Usage tracking (SQLite)
- Public REST API: /api/v1/blast /uniprot /gene /pubmed /sequence /pathway
- /api/v1/me, /api/v1/keys CRUD
- /api/v1/billing/plans + stub /api/v1/billing/stripe-webhook
- /setup endpoint (admin token-gated) for ops from HTTPS
"""
from __future__ import annotations
import asyncio, base64, hashlib, hmac, json, logging, os, secrets, sqlite3, subprocess, time, traceback
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web
from openai_compat import setup_protocol_routes, track_openai_usage, track_anthropic_usage  # Phase 2
from phase3_module import setup_phase3_routes  # Phase 3: sandbox + admin/metrics + admin/billing

HOST = '127.0.0.1'
PORT = int(os.environ.get('SAAS_PORT', '9101'))
DB_PATH = Path(os.environ.get('SAAS_DB', '/var/lib/bioinfo-saas/saas.db'))
JWT_SECRET = os.environ.get('SAAS_JWT_SECRET', secrets.token_urlsafe(32))
# Stable token: env > file > generate. File is only written on first run.
SETUP_TOKEN = os.environ.get('SAAS_SETUP_TOKEN')
if not SETUP_TOKEN:
    _token_path = Path('/var/lib/bioinfo-saas/setup.token')
    if _token_path.exists():
        try:
            SETUP_TOKEN = _token_path.read_text().split('\n')[0].strip()
            if not SETUP_TOKEN or len(SETUP_TOKEN) < 8:
                SETUP_TOKEN = None
        except Exception:
            SETUP_TOKEN = None
if not SETUP_TOKEN:
    SETUP_TOKEN = secrets.token_urlsafe(24)
JWT_TTL_HOURS = 24 * 30

RATE_LIMIT_PER_MIN_FREE = 30
RATE_LIMIT_PER_MIN_PRO = 600
RATE_LIMIT_PER_MIN_TEAM = 3000
PLANS = {
    'free':  {'monthly_quota': 100,    'rate_per_min': RATE_LIMIT_PER_MIN_FREE},
    'pro':   {'monthly_quota': 10000,  'rate_per_min': RATE_LIMIT_PER_MIN_PRO},
    'team':  {'monthly_quota': 50000,  'rate_per_min': RATE_LIMIT_PER_MIN_TEAM},
    'enterprise': {'monthly_quota': 10**9, 'rate_per_min': 10**6},
}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# Only write the file if it doesn't already exist OR the env var changed
_token_file = Path('/var/lib/bioinfo-saas/setup.token')
if not _token_file.exists() or os.environ.get('SAAS_SETUP_TOKEN_ROTATED') == '1':
    with open(_token_file, 'w') as f:
        f.write(SETUP_TOKEN + '\n')
        f.write(f'# generated at {datetime.now(timezone.utc).isoformat()}\n')
    try: os.chmod(_token_file, 0o600)
    except Exception: pass

print(f'[setup] SAAS_SETUP_TOKEN={SETUP_TOKEN}', flush=True)

def db():
    """Open a SQLite connection. Caller MUST use `with db() as c:` to close.

    Previous bug: this returned the raw Connection and `with db() as c:` relied
    on sqlite3.Connection's built-in context manager, which only commits and
    does NOT close the file descriptor. Every register/login leaked one fd.
    Fix: provide a proper context manager that closes the conn in finally.
    """
    return _db_ctx()

@contextmanager
def _db_ctx():
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=15.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        try: conn.close()
        except Exception: pass

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key_prefix TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_key_id INTEGER,
            endpoint TEXT NOT NULL,
            quota_cost INTEGER NOT NULL DEFAULT 1,
            status_code INTEGER,
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_events(user_id, ts);
        CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys(key_prefix);
        ''')

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000)
    return f'{salt}${h.hex()}'

def verify_password(password, stored):
    try:
        salt, h = stored.split('$', 1)
        expected = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000)
        return hmac.compare_digest(h, expected.hex())
    except Exception:
        return False

def make_jwt(user_id, email):
    # Use stdlib urlsafe_b64encode; the old bytes.maketrans was 62 vs 64 bytes
    # which raised ValueError. Fix: rely on base64.urlsafe_b64encode (RFC 4648 §5).
    def b64u(x):
        return base64.urlsafe_b64encode(json.dumps(x, separators=(',', ':')).encode()).rstrip(b'=')
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {'sub': str(user_id), 'email': email,
               'iat': int(time.time()), 'exp': int(time.time()) + JWT_TTL_HOURS * 3600}
    h = b64u(header) + b'.' + b64u(payload)
    sig = hmac.new(JWT_SECRET.encode(), h, hashlib.sha256).digest()
    return (h + b'.' + base64.urlsafe_b64encode(sig).rstrip(b'=')).decode()

def verify_jwt(token):
    try:
        import base64
        h, sig = token.rsplit('.', 1)
        expected = hmac.new(JWT_SECRET.encode(), h.encode(), hashlib.sha256).digest()
        sig_decoded = base64.urlsafe_b64decode(sig + '==')
        if not hmac.compare_digest(expected, sig_decoded):
            return None
        payload_b64 = h.split('.')[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get('exp', 0) < time.time():
            return None
        return payload
    except Exception:
        return None

def new_api_key():
    raw = secrets.token_urlsafe(32)
    full = f'sk_live_{raw}'
    h = hashlib.sha256(full.encode()).hexdigest()
    return full, h

def hash_api_key(full):
    return hashlib.sha256(full.encode()).hexdigest()

class TokenBucket:
    def __init__(self):
        self.buckets = {}
    def take(self, key, rate_per_min):
        now = time.time()
        cap = max(1, rate_per_min)
        refill_per_sec = cap / 60.0
        tokens, last = self.buckets.get(key, (cap, now))
        tokens = min(cap, tokens + (now - last) * refill_per_sec)
        self.buckets[key] = (tokens, now)
        if tokens >= 1:
            self.buckets[key] = (tokens - 1, now)
            return True
        return False

buckets = TokenBucket()

# ---- Global error middleware (catches unhandled exceptions in any handler) ----
@web.middleware
async def error_mw(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # let aiohttp handle proper HTTP responses (401, 404, etc.)
    except Exception as e:
        LOG.exception('unhandled error in %s %s: %s', request.method, request.path, e)
        return web.json_response(
            {'error': 'internal_server_error', 'detail': str(e)[:300],
             'type': type(e).__name__, 'path': request.path},
            status=500,
        )

# ---- Auth middleware (Bearer JWT or sk_live_xxx API key) ----
@web.middleware
async def auth_mw(request, handler):
    pub = (
        '/api/v1/auth/register', '/api/v1/auth/login',
        '/api/v1/billing/plans', '/api/v1/health',
        '/api/v1/openai/v1/models', '/api/v1/anthropic/v1/models',
        '/setup', '/setup/read',
        '/pricing', '/docs', '/skills', '/changelog',
        # Exact aliases registered by setup_protocol_routes; no prefix bypass.
        '/pricing/', '/docs/', '/skills/', '/changelog/',
        '/admin/metrics', '/admin/billing',
        '/api/v1/admin/resources', '/api/v1/admin/billing',
    )
    if request.path in pub or request.path.startswith('/api/v1/auth/') or request.path == '/health':
        return await handler(request)
    # Accept both OpenAI-style (Authorization: Bearer) and Anthropic-style (x-api-key)
    auth = request.headers.get('Authorization', '')
    x_api_key = request.headers.get('x-api-key', '').strip()
    user = None
    api_key_row = None
    # Extract bearer token (could be JWT or sk_live_)
    bearer = auth[7:].strip() if auth.startswith('Bearer ') else ''
    # The API key candidates: bearer (if sk_live_) OR x-api-key (Anthropic style)
    api_key_candidate = ''
    if bearer.startswith('sk_live_'):
        api_key_candidate = bearer
    elif x_api_key.startswith('sk_live_'):
        api_key_candidate = x_api_key

    if api_key_candidate:
        with db() as c:
            api_key_row = c.execute(
                'SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL',
                (hash_api_key(api_key_candidate),)).fetchone()
            if api_key_row:
                user = c.execute('SELECT * FROM users WHERE id=?', (api_key_row['user_id'],)).fetchone()
                c.execute('UPDATE api_keys SET last_used_at=? WHERE id=?',
                          (datetime.now(timezone.utc).isoformat(), api_key_row['id']))
    elif bearer:
        payload = verify_jwt(bearer)
        if payload:
            with db() as c:
                user = c.execute('SELECT * FROM users WHERE id=?', (int(payload['sub']),)).fetchone()
    if not user:
        return web.json_response({'error': 'unauthorized'}, status=401)
    plan = PLANS.get(user['plan'], PLANS['free'])
    bucket_key = f"key:{api_key_row['id']}" if api_key_row else f"user:{user['id']}"
    if not buckets.take(bucket_key, plan['rate_per_min']):
        return web.json_response({'error': 'rate_limited'}, status=429)
    request['user'] = dict(user)
    request['api_key'] = dict(api_key_row) if api_key_row else None
    return await handler(request)

async def track_usage(user_id, api_key_id, endpoint, cost, status_code):
    try:
        await asyncio.to_thread(_track_usage_sync, user_id, api_key_id, endpoint, cost, status_code)
    except Exception as e:
        LOG.exception('usage track failed: %s', e)

def _track_usage_sync(user_id, api_key_id, endpoint, cost, status_code):
    with db() as c:
        c.execute('INSERT INTO usage_events(user_id, api_key_id, endpoint, quota_cost, status_code, ts) '
                  'VALUES (?,?,?,?,?,?)',
                  (user_id, api_key_id, endpoint, cost, status_code,
                   datetime.now(timezone.utc).isoformat()))

def get_period_usage(user_id):
    ym = datetime.now(timezone.utc).strftime('%Y-%m')
    with db() as c:
        row = c.execute("SELECT COALESCE(SUM(quota_cost),0) AS used FROM usage_events WHERE user_id=? AND substr(ts,1,7)=?", (user_id, ym)).fetchone()
        return row['used']

def err(msg, code=400, **kw):
    return web.json_response({'error': msg, **kw}, status=code)

# ---- Endpoints ----
async def health(request):
    return web.json_response({'ok': True, 'service': 'bioinfo-saas', 'ts': datetime.now(timezone.utc).isoformat()})

async def register(request):
    try:
        try: body = await request.json()
        except Exception: return err('invalid JSON')
        email = (body.get('email') or '').strip().lower()
        password = body.get('password') or ''
        if not email or '@' not in email or len(email) > 254:
            return err('invalid email')
        if len(password) < 8:
            return err('password must be >= 8 chars')
        ph = hash_password(password)
        with db() as c:
            try:
                c.execute('INSERT INTO users(email, password_hash, plan, created_at, updated_at) VALUES (?,?,?,?,?)',
                          (email, ph, 'free', datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError:
                return err('email already registered', 409)
            user = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        return web.json_response({'user_id': user['id'], 'email': user['email'], 'plan': user['plan'],
                                  'token': make_jwt(user['id'], user['email'])}, status=201)
    except Exception as e:
        LOG.exception('register failed: %s', e)
        return web.json_response({'error': 'register failed', 'detail': str(e)[:200],
                                  'type': type(e).__name__}, status=500)

async def login(request):
    try:
        try: body = await request.json()
        except Exception: return err('invalid JSON')
        email = (body.get('email') or '').strip().lower()
        password = body.get('password') or ''
        with db() as c:
            user = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not user or not verify_password(password, user['password_hash']):
            return err('invalid email or password', 401)
        return web.json_response({'user_id': user['id'], 'email': user['email'], 'plan': user['plan'],
                                  'token': make_jwt(user['id'], user['email'])})
    except Exception as e:
        LOG.exception('login failed: %s', e)
        return web.json_response({'error': 'login failed', 'detail': str(e)[:200],
                                  'type': type(e).__name__}, status=500)

async def keys_list(request):
    user = request['user']
    with db() as c:
        rows = c.execute('SELECT id, key_prefix, label, created_at, last_used_at, revoked_at FROM api_keys WHERE user_id=? ORDER BY id DESC', (user['id'],)).fetchall()
    return web.json_response({'keys': [dict(r) for r in rows]})

async def keys_create(request):
    user = request['user']
    try: body = await request.json()
    except Exception: body = {}
    label = (body.get('label') or '').strip()[:64] or None
    full, h = new_api_key()
    prefix = full[:12]
    with db() as c:
        c.execute('INSERT INTO api_keys(user_id, key_prefix, key_hash, label, created_at) VALUES (?,?,?,?,?)',
                  (user['id'], prefix, h, label, datetime.now(timezone.utc).isoformat()))
    return web.json_response({'key': full, 'key_prefix': prefix, 'label': label,
                              'note': 'store this key securely; you will not see it again'}, status=201)

async def keys_revoke(request):
    user = request['user']
    key_id = int(request.match_info['id'])
    with db() as c:
        c.execute('UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=?',
                  (datetime.now(timezone.utc).isoformat(), key_id, user['id']))
    return web.json_response({'revoked': True, 'id': key_id})

async def me(request):
    user = request['user']
    plan = PLANS.get(user['plan'], PLANS['free'])
    used = get_period_usage(user['id'])
    return web.json_response({
        'user_id': user['id'], 'email': user['email'], 'plan': user['plan'],
        'quota': {
            'monthly_limit': plan['monthly_quota'],
            'used_this_month': used,
            'remaining': max(0, plan['monthly_quota'] - used),
            'rate_per_min': plan['rate_per_min'],
            'reset_at': f"{datetime.now(timezone.utc).strftime('%Y-%m')}-01T00:00:00Z",
        },
        'created_at': user['created_at'],
    })

async def api_blast(request):
    user = request['user']
    try: body = await request.json()
    except Exception: return err('invalid JSON')
    query = (body.get('query') or '').strip()
    db_ = body.get('db', 'nt')
    if not query: return err('query is required')
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import bio_local, bio_api
    use_remote = body.get('remote', True)
    if use_remote:
        result = await asyncio.to_thread(bio_api.ncbi_blast_submit, query, db_, body.get('program', 'blastn'))
    else:
        result = await asyncio.to_thread(bio_local.run_blast, query, db_,
                                          program=body.get('program', 'blastn'),
                                          evalue=str(body.get('evalue', '1e-5')))
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'blast', 5, 200)
    return web.json_response(result)

async def api_uniprot(request):
    user = request['user']
    acc = request.match_info['acc']
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import bio_api
    result = await asyncio.to_thread(bio_api.uniprot_get, acc)
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'uniprot', 1, 200)
    return web.json_response(result)

async def api_gene(request):
    user = request['user']
    symbol = request.match_info['symbol']
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import bio_api
    result = await asyncio.to_thread(bio_api.ensembl_gene, symbol, species='homo_sapiens')
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'gene', 1, 200)
    return web.json_response(result)

async def api_pubmed(request):
    user = request['user']
    try: body = await request.json()
    except Exception: return err('invalid JSON')
    q = (body.get('query') or '').strip()
    if not q: return err('query is required')
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import bio_api
    retmax = min(50, int(body.get('retmax', 10)))
    result = await asyncio.to_thread(bio_api.pubmed_search, q, retmax=retmax)
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'pubmed', 1, 200)
    return web.json_response(result)

async def api_sequence_analyze(request):
    user = request['user']
    try: body = await request.json()
    except Exception: return err('invalid JSON')
    fasta = (body.get('fasta') or '').strip()
    if not fasta: return err('fasta is required (sequence or path)')
    if fasta.startswith('/') and Path(fasta).exists():
        path = fasta
    else:
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.fa', delete=False) as f:
            if not fasta.startswith('>'):
                f.write(f'>query\n{fasta}\n')
            else:
                f.write(fasta)
            path = f.name
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import sequence
    result = await asyncio.to_thread(sequence.fasta_stats, path)
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'sequence', 1, 200)
    return web.json_response(result)

async def api_pathway(request):
    user = request['user']
    kegg_id = request.match_info['kegg_id']
    import sys
    SKILL_DIR = '/home/ubuntu/.hermes/skills/bioinformatics'
    if SKILL_DIR not in sys.path: sys.path.insert(0, SKILL_DIR)
    from tools import bio_api
    result = await asyncio.to_thread(bio_api.kegg_get, kegg_id)
    await track_usage(user['id'], request['api_key']['id'] if request.get('api_key') else None, 'pathway', 1, 200)
    return web.json_response(result)

async def billing_plans(request):
    return web.json_response({
        'plans': [
            {'id': 'free', 'name': 'Free', 'price_usd': 0, 'monthly_quota': PLANS['free']['monthly_quota'],
             'rate_per_min': PLANS['free']['rate_per_min'], 'features': ['100 API calls / month', '10 AI chat / month', 'Community support']},
            {'id': 'pro', 'name': 'Pro', 'price_usd': 29, 'monthly_quota': PLANS['pro']['monthly_quota'],
             'rate_per_min': PLANS['pro']['rate_per_min'], 'features': ['10,000 API calls / month', '1,000 AI chat / month', 'Email support', 'Higher rate limit']},
            {'id': 'team', 'name': 'Team', 'price_usd': 199, 'monthly_quota': PLANS['team']['monthly_quota'],
             'rate_per_min': PLANS['team']['rate_per_min'], 'features': ['5 seats', '50,000 API calls / month', '5,000 AI chat / seat', 'Priority support']},
            {'id': 'enterprise', 'name': 'Enterprise', 'price_usd': None, 'monthly_quota': 'Custom',
             'rate_per_min': 'Custom', 'features': ['Self-hosted', 'SLA', 'Compliance support']},
        ],
        'currency': 'USD',
        'note': 'Payment integration coming soon. Contact us to upgrade manually for now.',
    })

async def billing_stripe_webhook(request):
    try: body = await request.json()
    except Exception: return err('invalid JSON')
    event = body.get('type', '')
    LOG.info('stripe webhook received: %s', event)
    return web.json_response({'received': True, 'event': event, 'note': 'stub — wire up Stripe later'})

# ---- /setup endpoint (admin token-gated) ----
@web.middleware
async def setup_auth_mw(request, handler):
    # Gate /setup/* and admin endpoints behind X-Setup-Token
    gated = (
        request.path == '/setup' or request.path.startswith('/setup/')
        or request.path.startswith('/admin')
        or request.path.startswith('/api/v1/admin')
    )
    if gated:
        provided = request.headers.get('X-Setup-Token', '')
        provided = request.headers.get('X-Setup-Token', '')
        expected = SETUP_TOKEN
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            return web.json_response({'error': 'invalid setup token'}, status=401)
    return await handler(request)

async def handle_setup(request):
    body = {}
    if request.method == 'POST':
        try: body = await request.json()
        except Exception: return err('invalid JSON', 400)
    action = body.get('action', '') or request.query.get('action', '')
    # Default: GET /setup/read returns current nginx config (handy one-liner for shell)
    if not action and request.method == 'GET' and request.path == '/setup/read':
        action = 'read-nginx-config'
    log = []
    if action == 'reload-nginx':
        out = subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, text=True, timeout=10)
        log.append('nginx -t: ' + out.stdout + out.stderr)
        if out.returncode == 0:
            subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], timeout=10)
            log.append('nginx reloaded')
        return web.json_response({'ok': out.returncode == 0, 'log': log})
    if action == 'replace-nginx-config':
        new_config = body.get('config', '')
        if not new_config or 'server {' not in new_config:
            return err('config missing or invalid (no "server {" found)', 400)
        try:
            bak = subprocess.run(['sudo', 'cp',
                                   '/etc/nginx/sites-available/bioinfo-portal',
                                   '/var/lib/bioinfo-saas/nginx.bak'],
                                  capture_output=True, text=True, timeout=10)
            log.append('backup: ' + bak.stdout + bak.stderr)
        except Exception as e:
            log.append(f'backup err: {e}')
        b64 = base64.b64encode(new_config.encode()).decode()
        cmd = f'echo {b64} | base64 -d | sudo tee /etc/nginx/sites-available/bioinfo-portal > /dev/null'
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        log.append('write available: ' + out.stdout + out.stderr)
        # Also enable it: copy to sites-enabled/default (the active site)
        en = subprocess.run(['sudo', 'cp', '/etc/nginx/sites-available/bioinfo-portal',
                             '/etc/nginx/sites-enabled/default'],
                            capture_output=True, text=True, timeout=10)
        log.append('enable: ' + en.stdout + en.stderr)
        out = subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, text=True, timeout=10)
        log.append('nginx -t: ' + out.stdout + out.stderr)
        if out.returncode == 0:
            subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], timeout=10)
            log.append('nginx reloaded')
        return web.json_response({'ok': out.returncode == 0, 'log': log})
    if action == 'read-nginx-config':
        try:
            cfg = subprocess.run(['sudo', 'cat', '/etc/nginx/sites-available/bioinfo-portal'],
                                  capture_output=True, text=True, timeout=10)
            return web.json_response({'ok': True, 'config': cfg.stdout, 'stderr': cfg.stderr})
        except Exception as e:
            return err(str(e), 500)
    if action == 'restart-service':
        name = body.get('name', '')
        if name not in ('cloudflared', 'webchat', 'bioinfo-chat', 'bioinfo-saas', 'nginx'):
            return err(f'unknown service {name}', 400)
        out = subprocess.run(['sudo', 'systemctl', 'restart', name], capture_output=True, text=True, timeout=30)
        log.append(f'restart {name}: exit={out.returncode} stderr={out.stderr}')
        return web.json_response({'ok': out.returncode == 0, 'log': log})
    if action == 'exec':
        cmd = body.get('cmd', '')
        if not cmd or not cmd.startswith('sudo '):
            return err('cmd must start with "sudo "', 400)
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return web.json_response({'ok': out.returncode == 0,
                                  'stdout': out.stdout[:5000], 'stderr': out.stderr[:2000]})
    return err(f'unknown action: {action}', 400)

@asynccontextmanager
async def _lifespan(app):
    init_db()
    yield

def build_app():
    app = web.Application(middlewares=[error_mw, setup_auth_mw, auth_mw],
                          client_max_size=10*1024*1024)
    # Public
    app.router.add_get('/health', health)
    app.router.add_get('/api/v1/health', health)
    # Auth
    app.router.add_post('/api/v1/auth/register', register)
    app.router.add_post('/api/v1/auth/login', login)
    # API keys
    app.router.add_get('/api/v1/keys', keys_list)
    app.router.add_post('/api/v1/keys', keys_create)
    app.router.add_delete('/api/v1/keys/{id}', keys_revoke)
    # Me / usage
    app.router.add_get('/api/v1/me', me)
    # Public bioinfo API
    app.router.add_post('/api/v1/blast', api_blast)
    app.router.add_get('/api/v1/uniprot/{acc}', api_uniprot)
    app.router.add_get('/api/v1/gene/{symbol}', api_gene)
    app.router.add_post('/api/v1/pubmed', api_pubmed)
    app.router.add_post('/api/v1/sequence/analyze', api_sequence_analyze)
    app.router.add_get('/api/v1/pathway/{kegg_id}', api_pathway)
    # Billing
    app.router.add_get('/api/v1/billing/plans', billing_plans)
    app.router.add_post('/api/v1/billing/stripe-webhook', billing_stripe_webhook)
    # Setup (admin)
    app.router.add_post('/setup', handle_setup)
    app.router.add_get('/setup/read', handle_setup)
    # Phase 2: multi-protocol API (OpenAI + Anthropic compat) + content pages
    setup_protocol_routes(app)
    setup_phase3_routes(app, log=LOG)
    return app

LOG = logging.getLogger('bioinfo-saas')

def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    LOG.info('SAAS_SETUP_TOKEN=%s', SETUP_TOKEN)
    LOG.info('SAAS_SETUP_TOKEN saved to /var/lib/bioinfo-saas/setup.token')
    init_db()  # ensure schema exists before any handler touches DB
    LOG.info('DB initialized at %s', DB_PATH)
    app = build_app()
    web.run_app(app, host=HOST, port=PORT, access_log=None)

if __name__ == '__main__':
    main()
