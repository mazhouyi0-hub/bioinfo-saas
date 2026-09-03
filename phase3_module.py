#!/usr/bin/env python3
"""
Phase 3 module: sandbox execution + admin metrics + billing dashboard.

Endpoints registered (all require auth except /admin/metrics & /admin/billing
which require X-Setup-Token):
  - POST /api/v1/sandbox/run           Run Python code in subprocess sandbox
  - GET  /api/v1/sandbox/jobs          List recent sandbox jobs
  - GET  /api/v1/admin/resources       VPS resource snapshot (JSON)
  - GET  /api/v1/admin/billing         Billing aggregation (JSON)
  - GET  /admin/metrics                VPS metrics HTML page
  - GET  /admin/billing                Billing dashboard HTML page

Sandbox design:
  - subprocess.run() with timeout (configurable, default 30s, max 120s)
  - runs as `ubuntu` user (not root) via sudo -u ubuntu
  - writes to /tmp/pikachu-sandbox/<user_id>/<uuid>.py
  - RLIMIT_AS (memory) and RLIMIT_CPU via preexec_fn in subprocess
  - tracks usage in usage_events (1 quota per execution)
"""
import os
import sys
import uuid
import json
import time
import shutil
import resource
import tempfile
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from aiohttp import web

LOG = None  # will be set by setup_phase3_routes

# ---------------------------------------------------------------------------
# Sandboxed Python execution
# ---------------------------------------------------------------------------

SANDBOX_ROOT = Path('/tmp/pikachu-sandbox')
SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
SANDBOX_ROOT.chmod(0o755)

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT_BYTES = 256 * 1024  # 256KB per stream
MAX_MEMORY_MB = 512


def _preexec_sandbox(user_uid=1000, user_gid=1001, mem_mb=MAX_MEMORY_MB, cpu_sec=120):
    """Drop privileges + set rlimits before exec. Runs in child process."""
    try:
        # Set rlimits
        mem_bytes = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
        # Drop privileges to ubuntu
        os.setgroups([user_gid])
        os.setgid(user_gid)
        os.setuid(user_uid)
    except Exception as e:
        # If privilege drop fails, log and continue (still has rlimits)
        sys.stderr.write(f'sandbox preexec err: {e}\n')


def _run_python_sync(code: str, user_id: int, timeout: int, mem_mb: int) -> dict:
    """Run a snippet of Python code in a sandboxed subprocess. Returns a result dict."""
    job_id = uuid.uuid4().hex[:16]
    user_dir = SANDBOX_ROOT / f'user_{user_id}'
    user_dir.mkdir(parents=True, exist_ok=True)
    user_dir.chmod(0o755)
    code_path = user_dir / f'job_{job_id}.py'
    code_path.write_text(code, encoding='utf-8')
    code_path.chmod(0o644)

    # Estimate memory: clamp to [64, MAX_MEMORY_MB]
    mem_mb = max(64, min(MAX_MEMORY_MB, int(mem_mb) if mem_mb else MAX_MEMORY_MB))
    # Estimate CPU: timeout + 5s buffer
    cpu_sec = max(5, min(MAX_TIMEOUT + 5, timeout + 5))

    start = time.time()
    timed_out = False
    oom = False
    stdout = b''
    stderr = b''
    exit_code = -1
    try:
        proc = subprocess.run(
            ['sudo', '-u', 'ubuntu', '-H', '/usr/bin/python3', str(code_path)],
            capture_output=True,
            timeout=timeout,
            preexec_fn=lambda: _preexec_sandbox(mem_mb=mem_mb, cpu_sec=cpu_sec),
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/home/ubuntu'},
        )
        exit_code = proc.returncode
        stdout = proc.stdout[:MAX_OUTPUT_BYTES]
        stderr = proc.stderr[:MAX_OUTPUT_BYTES]
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b'')[:MAX_OUTPUT_BYTES]
        stderr = (e.stderr or b'')[:MAX_OUTPUT_BYTES]
    except Exception as e:
        stderr = f'sandbox internal error: {e}'.encode()
    finally:
        try:
            code_path.unlink()
        except Exception:
            pass
    duration_ms = int((time.time() - start) * 1000)
    return {
        'job_id': job_id,
        'exit_code': exit_code,
        'timed_out': timed_out,
        'oom': oom,
        'stdout': stdout.decode('utf-8', errors='replace'),
        'stderr': stderr.decode('utf-8', errors='replace'),
        'duration_ms': duration_ms,
        'memory_mb_quota': mem_mb,
        'cpu_sec_quota': cpu_sec,
        'timeout_s': timeout,
    }


async def handle_sandbox_run(request):
    user = request.get('user') or {}
    if not user or 'id' not in user:
        return web.json_response({'error': 'auth required'}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid JSON'}, status=400)

    code = (body.get('code') or '').strip()
    if not code:
        return web.json_response({'error': 'code is required'}, status=400)
    if len(code) > 50_000:
        return web.json_response({'error': 'code too long (max 50KB)'}, status=400)

    timeout = int(body.get('timeout', DEFAULT_TIMEOUT))
    if timeout < 1 or timeout > MAX_TIMEOUT:
        return web.json_response({'error': f'timeout must be 1..{MAX_TIMEOUT}s'}, status=400)
    mem_mb = int(body.get('memory_mb', MAX_MEMORY_MB))

    if LOG:
        LOG.info('sandbox.run user=%s len=%d timeout=%d', user.get('id'), len(code), timeout)

    # Run in thread pool to not block event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _run_python_sync, code, int(user['id']), timeout, mem_mb
    )

    # Track usage
    api_key = request.get('api_key') or {}
    try:
        from main import track_usage
        await track_usage(int(user['id']), api_key.get('id') if api_key else None,
                          'sandbox', 1, 200)
    except Exception as e:
        if LOG:
            LOG.warning('sandbox track_usage err: %s', e)

    return web.json_response(result)


async def handle_sandbox_jobs(request):
    user = request.get('user') or {}
    if not user or 'id' not in user:
        return web.json_response({'error': 'auth required'}, status=401)

    # Read recent sandbox jobs from journal
    # (we don't store sandbox results persistently in this MVP)
    from main import LOG as MAIN_LOG
    try:
        si, so, se = await asyncio.create_subprocess_exec(
            'journalctl', '-u', 'bioinfo-saas', '-n', '200', '--no-pager', '--output=json',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(si.communicate(), timeout=5)
        lines = out.decode('utf-8', errors='replace').splitlines()
        entries = []
        for ln in lines[-100:]:
            try:
                e = json.loads(ln)
                msg = e.get('MESSAGE', '')
                if 'sandbox.run' in msg:
                    entries.append({'ts': e.get('__REALTIME_TIMESTAMP', ''), 'msg': msg})
            except Exception:
                pass
        return web.json_response({'jobs': entries[-20:]})
    except Exception as e:
        return web.json_response({'jobs': [], 'note': f'journal read err: {e}'})


# ---------------------------------------------------------------------------
# VPS resource monitoring
# ---------------------------------------------------------------------------

def _read_proc_stat():
    """Return aggregated CPU stats from /proc/stat (jiffies)."""
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = line.split()
        # cpu user nice system idle iowait irq softirq steal guest guest_nice
        nums = [int(x) for x in parts[1:8]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return {'total_jiffies': total, 'idle_jiffies': idle, 'raw': nums}
    except Exception as e:
        return {'err': str(e)}


_prev_cpu = {'total': 0, 'idle': 0}


def get_cpu_usage_percent():
    """Return CPU usage percent since last call (sample-and-hold)."""
    global _prev_cpu
    s = _read_proc_stat()
    if 'err' in s:
        return 0.0
    total = s['total_jiffies']
    idle = s['idle_jiffies']
    if _prev_cpu['total'] == 0:
        _prev_cpu = {'total': total, 'idle': idle}
        return 0.0
    d_total = total - _prev_cpu['total']
    d_idle = idle - _prev_cpu['idle']
    _prev_cpu = {'total': total, 'idle': idle}
    if d_total <= 0:
        return 0.0
    return round(100.0 * (1.0 - d_idle / d_total), 2)


def get_memory_info():
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                key, val = line.split(':', 1)
                info[key.strip()] = int(val.strip().split()[0])  # kB
        total = info.get('MemTotal', 0)
        avail = info.get('MemAvailable', 0)
        used = total - avail
        return {
            'total_kb': total,
            'available_kb': avail,
            'used_kb': used,
            'used_pct': round(100.0 * used / total, 2) if total else 0,
        }
    except Exception as e:
        return {'err': str(e)}


def get_disk_info(path='/'):
    try:
        u = shutil.disk_usage(path)
        return {
            'path': path,
            'total_bytes': u.total,
            'used_bytes': u.used,
            'free_bytes': u.free,
            'used_pct': round(100.0 * u.used / u.total, 2) if u.total else 0,
        }
    except Exception as e:
        return {'err': str(e)}


def get_network_info():
    """Return aggregate RX/TX bytes for non-loopback interfaces."""
    try:
        rx_total = 0
        tx_total = 0
        interfaces = []
        with open('/proc/net/dev') as f:
            next(f); next(f)  # skip headers
            for line in f:
                if ':' not in line:
                    continue
                iface, data = line.split(':', 1)
                iface = iface.strip()
                if iface == 'lo':
                    continue
                parts = data.split()
                rx = int(parts[0])
                tx = int(parts[8])
                rx_total += rx
                tx_total += tx
                interfaces.append({'name': iface, 'rx_bytes': rx, 'tx_bytes': tx})
        return {
            'rx_total': rx_total,
            'tx_total': tx_total,
            'interfaces': interfaces,
        }
    except Exception as e:
        return {'err': str(e)}


def get_loadavg():
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
        return {'1m': float(parts[0]), '5m': float(parts[1]), '15m': float(parts[2])}
    except Exception as e:
        return {'err': str(e)}


def get_uptime():
    try:
        with open('/proc/uptime') as f:
            s = float(f.read().split()[0])
        return s
    except Exception as e:
        return 0


def get_process_info():
    """List our key processes."""
    procs = []
    for name in ['python3', 'nginx', 'sshd']:
        try:
            si, so, se = subprocess.run(
                ['pgrep', '-af', name],
                capture_output=True, text=True, timeout=5,
            ).communicate() if False else (None, None, None)
        except Exception:
            pass
    # Use pgrep via shell
    try:
        out = subprocess.run(['pgrep', '-af', 'python3|nginx|sshd'],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                procs.append({'pid': int(parts[0]), 'cmd': parts[1][:200]})
    except Exception as e:
        return [{'err': str(e)}]
    return procs


async def handle_admin_resources(request):
    """Return VPS resource snapshot as JSON."""
    # Sample CPU twice (250ms apart) for accurate reading
    cpu1 = get_cpu_usage_percent()
    await asyncio.sleep(0.25)
    cpu2 = get_cpu_usage_percent()
    cpu_pct = max(cpu1, cpu2)

    mem = get_memory_info()
    disk = get_disk_info('/')
    net = get_network_info()
    load = get_loadavg()
    uptime = get_uptime()
    procs = get_process_info()

    # bioinfo-saas process uptime (from /proc/<pid>/stat for systemd PID 1 child)
    saas_proc = None
    try:
        out = subprocess.run(['pgrep', '-f', '/opt/bioinfo-saas/main.py'],
                             capture_output=True, text=True, timeout=5)
        for pid_str in out.stdout.strip().splitlines():
            pid = int(pid_str)
            with open(f'/proc/{pid}/stat') as f:
                parts = f.read().split()
            start_jiffies = int(parts[21])  # starttime in clock ticks
            with open('/proc/uptime') as f:
                total_uptime = float(f.read().split()[0])
            clk_tck = os.sysconf('SC_CLK_TCK')
            proc_uptime = total_uptime - (start_jiffies / clk_tck)
            with open(f'/proc/{pid}/status') as f:
                status_info = {}
                for line in f:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        status_info[k.strip()] = v.strip()
            saas_proc = {
                'pid': pid,
                'uptime_s': round(proc_uptime, 1),
                'rss_kb': int(status_info.get('VmRSS', '0').split()[0]) if 'VmRSS' in status_info else 0,
                'threads': status_info.get('Threads', '?'),
            }
            break
    except Exception as e:
        saas_proc = {'err': str(e)}

    return web.json_response({
        'cpu_pct': cpu_pct,
        'memory': mem,
        'disk': disk,
        'network': net,
        'loadavg': load,
        'uptime_s': uptime,
        'bioinfo_saas': saas_proc,
        'processes': procs,
        'ts': datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Billing dashboard
# ---------------------------------------------------------------------------

PLAN_PRICES = {
    'free': 0,
    'pro': 49,
    'team': 199,
    'enterprise': None,  # custom
}


async def handle_admin_billing(request):
    """Aggregate usage_events for billing dashboard."""
    import sqlite3
    import os
    SAAS_DB = os.environ.get('SAAS_DB', '/var/lib/bioinfo-saas/saas.db')

    def _query():
        conn = sqlite3.connect(SAAS_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Total users by plan
        by_plan = {r['plan']: r['n'] for r in cur.execute(
            'SELECT plan, COUNT(*) AS n FROM users GROUP BY plan').fetchall()}
        # Active API keys
        keys_n = cur.execute('SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL').fetchone()[0]
        # Usage this month + last 30 days
        ym = datetime.now(timezone.utc).strftime('%Y-%m')
        month_total = cur.execute(
            "SELECT COALESCE(SUM(quota_cost), 0) FROM usage_events WHERE substr(ts,1,7)=?",
            (ym,)).fetchone()[0]
        # By endpoint (all-time)
        by_endpoint = {r['endpoint']: r['n'] for r in cur.execute(
            'SELECT endpoint, COUNT(*) AS n FROM usage_events GROUP BY endpoint ORDER BY n DESC').fetchall()}
        # By user (top 10)
        by_user = [dict(r) for r in cur.execute('''
            SELECT u.email, u.plan, COALESCE(SUM(e.quota_cost), 0) AS used
            FROM users u LEFT JOIN usage_events e ON u.id=e.user_id
            GROUP BY u.id ORDER BY used DESC LIMIT 10
        ''').fetchall()]
        # Last 7 days daily counts
        daily = []
        for i in range(7):
            d = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=i)).strftime('%Y-%m-%d')
            n = cur.execute(
                "SELECT COUNT(*) FROM usage_events WHERE substr(ts,1,10)=?", (d,)).fetchone()[0]
            daily.append({'date': d, 'calls': n})
        daily.reverse()
        return {
            'users_by_plan': by_plan,
            'active_api_keys': keys_n,
            'current_month_quota_used': month_total,
            'current_month': ym,
            'calls_by_endpoint': by_endpoint,
            'top_users': by_user,
            'daily_7d': daily,
            'plan_prices': PLAN_PRICES,
        }

    data = await asyncio.get_event_loop().run_in_executor(None, _query)
    data['ts'] = datetime.now(timezone.utc).isoformat()
    return web.json_response(data)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

ADMIN_HTML_HEAD = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", sans-serif;
       max-width: 1080px; margin: 30px auto; padding: 0 20px; line-height: 1.6; color: #1a1a1a; }}
h1, h2 {{ color: #1e3a5f; }}
h1 {{ border-bottom: 2px solid #0891b2; padding-bottom: 8px; }}
h2 {{ margin-top: 1.4em; }}
.gauge {{ display: inline-block; margin: 8px 12px 8px 0; padding: 12px 18px;
         background: #f4f4f8; border-radius: 6px; min-width: 140px; }}
.gauge .num {{ font-size: 1.8em; font-weight: 700; color: #1e3a5f; }}
.gauge .label {{ font-size: 0.85em; color: #666; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 0.92em; }}
th, td {{ border: 1px solid #e0e0e0; padding: 6px 10px; text-align: left; }}
th {{ background: #f4f4f8; color: #1e3a5f; }}
tr:nth-child(even) {{ background: #fafafa; }}
.bar {{ background: #0891b2; height: 8px; border-radius: 4px; }}
.bar-wrap {{ background: #e0e0e0; height: 8px; border-radius: 4px; min-width: 100px; }}
code {{ background: #f4f4f8; padding: 1px 6px; border-radius: 3px; color: #c7254e;
       font-family: "JetBrains Mono", "Fira Code", Consolas, monospace; }}
pre {{ background: #1e1e2e; color: #cdd6f4; padding: 14px 18px; border-radius: 6px;
      overflow-x: auto; font-size: 13px; }}
pre code {{ background: transparent; color: inherit; padding: 0; }}
a {{ color: #0891b2; }}
.muted {{ color: #888; font-size: 0.9em; }}
.ok {{ color: #16a34a; font-weight: 600; }}
.warn {{ color: #f59e0b; font-weight: 600; }}
.crit {{ color: #dc2626; font-weight: 600; }}
</style></head><body>
'''

ADMIN_HTML_FOOT = '''<p class="muted" style="margin-top:40px;">⚡ Pikachu Admin · auto-refresh every 30s ·
  <a href="/admin/metrics">metrics</a> · <a href="/admin/billing">billing</a> ·
  <a href="/changelog">changelog</a></p>
<script>setTimeout(() => location.reload(), 30000);</script>
</body></html>'''


async def page_admin_metrics(request):
    # Sample internal endpoint
    from main import LOG as MAIN_LOG
    # Call our own /api/v1/admin/resources via in-process (avoid HTTP roundtrip)
    # Reuse handler logic
    data = await handle_admin_resources(request)
    payload = json.loads(data.body)

    def fmt_bytes(n):
        if n is None: return '?'
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if n < 1024: return f'{n:.1f} {unit}'
            n /= 1024
        return f'{n:.1f} PB'

    cpu = payload.get('cpu_pct', 0)
    mem = payload.get('memory', {})
    disk = payload.get('disk', {})
    net = payload.get('network', {})
    load = payload.get('loadavg', {})
    up = payload.get('uptime_s', 0)
    saas = payload.get('bioinfo_saas', {})
    procs = payload.get('processes', [])

    def pct_color(pct):
        if pct < 60: return 'ok'
        if pct < 85: return 'warn'
        return 'crit'

    html = ADMIN_HTML_HEAD.format(title='VPS Metrics — Pikachu Admin')
    html += '<h1>⚡ VPS Metrics</h1>'
    html += f'<p class="muted">Snapshot at {payload["ts"]} · auto-refresh 30s</p>'

    html += '<h2>CPU / Memory / Disk</h2>'
    html += f'<div class="gauge"><div class="num"><span class="{pct_color(cpu)}">{cpu}</span>%</div><div class="label">CPU 使用率</div></div>'
    if isinstance(mem, dict) and 'total_kb' in mem:
        html += f'<div class="gauge"><div class="num"><span class="{pct_color(mem.get("used_pct", 0))}">{mem.get("used_pct", 0)}</span>%</div>'
        html += f'<div class="label">内存 {fmt_bytes(mem.get("used_kb", 0)*1024)} / {fmt_bytes(mem.get("total_kb", 0)*1024)}</div></div>'
    if isinstance(disk, dict) and 'total_bytes' in disk:
        html += f'<div class="gauge"><div class="num"><span class="{pct_color(disk.get("used_pct", 0))}">{disk.get("used_pct", 0)}</span>%</div>'
        html += f'<div class="label">磁盘 {fmt_bytes(disk.get("used_bytes", 0))} / {fmt_bytes(disk.get("total_bytes", 0))}</div></div>'

    if isinstance(load, dict):
        html += f'<div class="gauge"><div class="num">{load.get("1m", 0):.2f}</div><div class="label">Load 1m / 5m / 15m = {load.get("1m", 0):.2f} / {load.get("5m", 0):.2f} / {load.get("15m", 0):.2f}</div></div>'

    days = up / 86400
    html += f'<div class="gauge"><div class="num">{days:.1f}d</div><div class="label">系统已运行 {up:.0f} 秒</div></div>'

    if isinstance(saas, dict) and 'uptime_s' in saas:
        html += '<h2>bioinfo-saas 进程</h2>'
        html += f'<table><tr><th>PID</th><td>{saas.get("pid")}</td></tr>'
        html += f'<tr><th>已运行</th><td>{saas.get("uptime_s", 0):.0f} 秒 (≈ {saas.get("uptime_s", 0)/3600:.1f} 小时)</td></tr>'
        html += f'<tr><th>内存 (RSS)</th><td>{saas.get("rss_kb", 0)} KB ({fmt_bytes(saas.get("rss_kb", 0)*1024)})</td></tr>'
        html += f'<tr><th>线程数</th><td>{saas.get("threads", "?")}</td></tr></table>'

    if isinstance(net, dict) and 'interfaces' in net:
        html += '<h2>网络</h2>'
        html += f'<p>总 RX {fmt_bytes(net.get("rx_total", 0))} · 总 TX {fmt_bytes(net.get("tx_total", 0))}</p>'
        html += '<table><tr><th>网卡</th><th>RX</th><th>TX</th></tr>'
        for i in net.get('interfaces', []):
            html += f'<tr><td><code>{i["name"]}</code></td><td>{fmt_bytes(i["rx_bytes"])}</td><td>{fmt_bytes(i["tx_bytes"])}</td></tr>'
        html += '</table>'

    if procs:
        html += '<h2>相关进程</h2>'
        html += '<table><tr><th>PID</th><th>命令</th></tr>'
        for p in procs[:15]:
            html += f'<tr><td>{p.get("pid")}</td><td><code>{p.get("cmd", "")[:160]}</code></td></tr>'
        html += '</table>'

    html += ADMIN_HTML_FOOT
    return web.Response(text=html, content_type="text/html")


async def page_admin_billing(request):
    data = await handle_admin_billing(request)
    payload = json.loads(data.body)

    html = ADMIN_HTML_HEAD.format(title='Billing — Pikachu Admin')
    html += '<h1>💰 Billing & Usage</h1>'
    html += f'<p class="muted">Snapshot at {payload["ts"]} · auto-refresh 30s</p>'

    by_plan = payload.get('users_by_plan', {})
    keys = payload.get('active_api_keys', 0)
    month_q = payload.get('current_month_quota_used', 0)
    ym = payload.get('current_month', '?')
    by_ep = payload.get('calls_by_endpoint', {})
    top = payload.get('top_users', [])
    daily = payload.get('daily_7d', [])
    prices = payload.get('plan_prices', {})

    html += '<h2>概览</h2>'
    html += f'<div class="gauge"><div class="num">{sum(by_plan.values())}</div><div class="label">总用户数</div></div>'
    html += f'<div class="gauge"><div class="num">{keys}</div><div class="label">活跃 API keys</div></div>'
    html += f'<div class="gauge"><div class="num">{int(month_q)}</div><div class="label">{ym} 已用配额</div></div>'

    if by_plan:
        html += '<h2>用户按 plan 分布</h2>'
        html += '<table><tr><th>Plan</th><th>用户数</th><th>单价/月 (USD)</th></tr>'
        for plan in ['free', 'pro', 'team', 'enterprise']:
            if plan in by_plan:
                price = prices.get(plan)
                price_s = '自定义' if price is None else f'${price}'
                html += f'<tr><td>{plan}</td><td>{by_plan[plan]}</td><td>{price_s}</td></tr>'
        html += '</table>'

    if by_ep:
        html += '<h2>累计调用按端点 (all-time)</h2>'
        html += '<table><tr><th>端点</th><th>调用次数</th></tr>'
        for ep, n in by_ep.items():
            html += f'<tr><td><code>{ep}</code></td><td>{n}</td></tr>'
        html += '</table>'

    if top:
        html += '<h2>Top 10 用户 (累计用量)</h2>'
        html += '<table><tr><th>Email</th><th>Plan</th><th>已用配额</th></tr>'
        for u in top:
            used = u.get('used', 0) or 0
            html += f'<tr><td>{u.get("email")}</td><td>{u.get("plan")}</td><td>{int(used)}</td></tr>'
        html += '</table>'

    if daily:
        max_calls = max(d.get('calls', 0) for d in daily) or 1
        html += '<h2>过去 7 天每日调用</h2>'
        html += '<table><tr><th>日期</th><th>调用</th><th>柱</th></tr>'
        for d in daily:
            bar_w = int(100 * d['calls'] / max_calls)
            html += f'<tr><td><code>{d["date"]}</code></td><td>{d["calls"]}</td>'
            html += f'<td><div class="bar-wrap"><div class="bar" style="width:{bar_w}%"></div></div></td></tr>'
        html += '</table>'

    html += '<h2>沙箱执行 (Phase 3 #1)</h2>'
    html += """<pre><code>curl -X POST https://mzydsg.me/api/v1/sandbox/run \
  -H "Authorization: Bearer &lt;API_KEY&gt;" \
  -H "Content-Type: application/json" \
  -d '{"code":"print(sum(range(101)))","timeout":5}'</code></pre>"""


    html += ADMIN_HTML_FOOT
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_phase3_routes(app, log=None):
    """Register all Phase 3 routes on the given aiohttp app."""
    global LOG
    LOG = log
    # Sandbox (require auth — handled by auth_mw in main.py)
    app.router.add_post('/api/v1/sandbox/run', handle_sandbox_run)
    app.router.add_get('/api/v1/sandbox/jobs', handle_sandbox_jobs)
    # Admin (public, gated by setup_auth_mw which we'll extend)
    app.router.add_get('/api/v1/admin/resources', handle_admin_resources)
    app.router.add_get('/api/v1/admin/billing', handle_admin_billing)
    app.router.add_get('/admin/metrics', page_admin_metrics)
    app.router.add_get('/admin/billing', page_admin_billing)
    if LOG:
        LOG.info('Phase 3 routes registered: sandbox + admin/metrics + admin/billing')
