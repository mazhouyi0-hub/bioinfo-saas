# Google indexing investigation — 2026-10-26

Status: application fix tested, not deployed. Search Console affected URL list
and effective production server configuration are not yet accessible.

## Observed production responses

Anonymous HTTPS GET requests using curl, without credentials or redirects:

| Path | HTTP | Actual response |
| --- | --- | --- |
| `/` | 200 | BioInfo Portal home HTML |
| `/pricing` | 200 | Pricing HTML |
| `/docs` | 200 | Documentation HTML |
| `/skills` | 200 | Public JSON skill catalog (intentional per README) |
| `/changelog` | 200 | Changelog HTML |
| `/pricing/`, `/docs/`, `/skills/`, `/changelog/` | 200 | Home HTML, not the requested content |
| `/robots.txt`, `/sitemap.xml` | 200 | Home HTML, not robots text or sitemap XML |
| `/api/v1/me`, `/api/v1/keys` | 401 | `{"error": "unauthorized"}` |
| `/admin/metrics`, `/setup/read` | 401 | `{"error": "invalid setup token"}` |

Home and all slash aliases, robots.txt, and sitemap.xml had identical response
bytes (22,764 bytes; SHA-256
`1689a16c20f624f61ce33ba32cf170fdb2d50e8ce78366e4bec34a2042678620`).
The digest should be checked against freshly captured evidence if content changes.

A separate urllib client received HTTP 403 with `error code: 1010` at these
paths. This is a client-dependent observation, not evidence that Googlebot is
blocked, and is distinct from the app's JSON 401. No Googlebot identity was
spoofed, and no bot-specific authentication exception is proposed.

## Confirmed application defect

`setup_protocol_routes` registers both slashless and trailing-slash versions of
four public content routes. `auth_mw` originally allowlisted only the slashless
versions. An anonymous request reaching aiohttp at `/docs/`, for example,
returned 401 before the public handler could run.

The fix adds only the four exact registered slash aliases. It does not normalize
all paths, add prefix exemptions, or modify `setup_auth_mw`. Neither APIs nor
admin/setup routes are opened. Unknown subpaths remain protected.

Tests use the actual middleware definitions and production content routes through
an aiohttp HTTP test server. Only middleware definitions are compiled from
`main.py` to avoid its import-time writes to `/var/lib` and admin-token logging.
Before the fix: 12 failing subcases. After: all five test methods pass, including
GET/HEAD, body parity, API rejection, admin token enforcement, and no subpath
exemption. Run `python -m unittest discover -s tests -v` with aiohttp installed.

## Production routing remains unresolved

The production slash aliases return home HTML instead of the application's 401.
This is consistent with a frontend/proxy fallback (for example nginx `try_files`),
but cannot identify the responsible rule without the effective server config.
The repository does not contain that config. The tested application fix alone
will not correct a proxy that never forwards these requests to aiohttp.

With server access, inspect `nginx -T`, the systemd service, active source revision,
and any Cloudflare rules. Treat config output and logs as private and redact
secrets. Compare loopback responses on port 9101 with public HTTPS responses.
Apply narrowly scoped changes after backing up the active config:

- Route the four exact slash aliases to their existing handlers, or permanently
  redirect those aliases to their slashless equivalents. Preserve query strings.
- Serve a real UTF-8 robots.txt and valid XML sitemap; do not return home HTML
  for them. Include only canonical public content URLs in the sitemap.
- Keep protected API/admin/setup locations and all authentication gates intact.
- Check `nginx -t` before reload; roll back on failed checks. Verify actual page
  bodies as well as status codes after deployment.

## Search Console follow-up

The warning email reports 401 but does not enumerate URLs. The current browser
has no authenticated Search Console session; the connected local browser/server
bridge is offline. Therefore the exact affected URLs and last crawl dates have
not been obtained. This defect is not yet proven to be the warning's cause.

Obtain the 401 report's example URLs (including full path, query, hostname,
scheme, and last crawl date), then classify individually:

- Protected API, account, sandbox, or admin URL: keep authentication. Exclusion
  from the index is expected; remove any accidental sitemap/internal SEO links.
- Intended public content URL: compare current response, historical access logs,
  edge rules, and upstream response; fix the specific interception or routing.
- Missing/nonexistent URL: use an appropriate 404/410 rather than manufacturing
  a public success page or weakening authentication.

Only after deployment and a successful live inspection of the affected public
URLs should Search Console validation or reindexing be requested. A 200 response
does not guarantee Google indexing.

Reference: https://developers.google.com/crawling/docs/troubleshooting/http-status-codes
