"""Exercise production middleware and content routes without import-time ops.

main.py creates /var/lib files and logs an admin token on import. Compile only
its two middleware definitions so these HTTP tests never touch production state.
"""
import ast
import hmac
from pathlib import Path
import sys
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from openai_compat import setup_protocol_routes


def middleware_namespace():
    tree = ast.parse((ROOT / 'main.py').read_text())
    definitions = [node for node in tree.body
                   if isinstance(node, ast.AsyncFunctionDef)
                   and node.name in {'auth_mw', 'setup_auth_mw'}]
    namespace = {'web': web, 'hmac': hmac, 'SETUP_TOKEN': 'test-admin-token'}
    exec(compile(ast.Module(body=definitions, type_ignores=[]),
                 str(ROOT / 'main.py'), 'exec'), namespace)
    return namespace


class PublicAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ns = middleware_namespace()
        app = web.Application(middlewares=[ns['setup_auth_mw'], ns['auth_mw']])
        setup_protocol_routes(app)
        async def private_handler(request):
            return web.json_response({'ok': True})
        for path in ('/api/v1/me', '/api/v1/keys', '/api/v1/sandbox/jobs',
                     '/api/v1/admin/resources', '/admin/metrics', '/setup/read'):
            app.router.add_get(path, private_handler)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_public_content_get_and_head(self):
        for path in ('/pricing', '/docs', '/skills', '/changelog'):
            for suffix in ('', '/'):
                for method in ('GET', 'HEAD'):
                    with self.subTest(path=path + suffix, method=method):
                        response = await self.client.request(method, path + suffix)
                        self.assertEqual(response.status, 200)

    async def test_slash_alias_returns_same_content(self):
        for path in ('/pricing', '/docs', '/skills', '/changelog'):
            with self.subTest(path=path):
                canonical = await self.client.get(path)
                alias = await self.client.get(path + '/')
                self.assertEqual(canonical.status, 200)
                self.assertEqual(alias.status, 200)
                self.assertEqual(await canonical.read(), await alias.read())

    async def test_private_api_still_requires_auth(self):
        for path in ('/api/v1/me', '/api/v1/keys', '/api/v1/sandbox/jobs',
                     '/api/v1/openai/v1/chat/completions',
                     '/api/v1/anthropic/v1/messages'):
            with self.subTest(path=path):
                method = 'POST' if path.endswith(('completions', 'messages')) else 'GET'
                response = await self.client.request(method, path)
                self.assertEqual(response.status, 401)
                self.assertEqual(await response.json(), {'error': 'unauthorized'})

    async def test_admin_still_requires_setup_token(self):
        for path in ('/admin/metrics', '/api/v1/admin/resources', '/setup/read'):
            for headers in ({}, {'X-Setup-Token': 'wrong-token'},
                            {'Authorization': 'Bearer test-user-token'}):
                with self.subTest(path=path, headers=headers):
                    response = await self.client.get(path, headers=headers)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(await response.json(), {'error': 'invalid setup token'})
        response = await self.client.get('/admin/metrics',
                                        headers={'X-Setup-Token': 'test-admin-token'})
        self.assertEqual(response.status, 200)

    async def test_public_page_names_do_not_exempt_subpaths(self):
        for path in ('/docs/private', '/pricing/admin', '/skills/secrets',
                     '/changelog/private', '/docs//', '/pricing//'):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 401)


if __name__ == '__main__':
    unittest.main()
