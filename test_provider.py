"""Run: PYTHONPATH=/path/to/hermes-agent python -m unittest -v test_provider."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from hermes_9router_web_search import provider as m


class ConfigTests(unittest.TestCase):
    def test_real_profile_switch(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        with tempfile.TemporaryDirectory() as td:
            for name in ('alpha','beta'):
                home=Path(td)/name; home.mkdir()
                (home/'config.yaml').write_text(f'web:\n  9router:\n    search_model: {name}\n')
                token=set_hermes_home_override(home)
                try: self.assertEqual(m._load_config()['search_model'],name)
                finally: reset_hermes_home_override(token)

    def test_active_profile_config(self):
        with patch('hermes_cli.config_effective.load_user_config_effective', return_value={'web': {'9router': {'search_model': 'profile-specific'}}}):
            self.assertEqual(m._load_config()['search_model'], 'profile-specific')

    def test_remote_never_borrows_local_key(self):
        with patch.dict('os.environ', {'NINEROUTER_KEY': ''}), patch.object(m, '_load_config', return_value={'base_url': 'https://router.example'}), patch('agent.secret_scope.get_secret', return_value=''), patch.object(m, '_read_db_key', return_value='SYNTHETIC') as db:
            self.assertEqual(m._api_key(), '')
            db.assert_not_called()

    def test_other_profile_does_not_inherit_db(self):
        with patch.object(m,'_load_config',return_value={}), patch('agent.secret_scope.get_secret',return_value=''), patch('hermes_constants.get_hermes_home',return_value=Path('/synthetic/profile')), patch.object(m,'_read_db_key') as db:
            self.assertEqual(m._api_key(),''); db.assert_not_called()

    def test_multiplex_missing_scope_fails_closed(self):
        from agent.secret_scope import UnscopedSecretError
        with patch.object(m,'_load_config',return_value={}), patch('agent.secret_scope.get_secret',side_effect=UnscopedSecretError('NINEROUTER_KEY')):
            with self.assertRaises(UnscopedSecretError): m._api_key()

    def test_routed_scope_never_falls_back_to_process_key(self):
        with patch('agent.secret_scope.serves_routed_profile',return_value=True), patch('agent.secret_scope.current_secret_scope',return_value={}), patch('agent.secret_scope.get_secret',return_value='OTHER_PROFILE') as process:
            self.assertEqual(m._env('NINEROUTER_KEY'),''); process.assert_not_called()

    def test_invalid_yaml_fails_closed(self):
        with patch('hermes_cli.config_effective.load_user_config_effective',side_effect=ValueError('private config text')):
            with self.assertRaisesRegex(ValueError,'Cannot read active profile'): m._load_config()

    def test_db_active_key_only(self):
        with tempfile.TemporaryDirectory() as td, patch.object(m.Path, 'home', return_value=Path(td)):
            db = Path(td)/'.9router/db/data.sqlite'; db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE apiKeys (id TEXT, key TEXT, isActive INTEGER)')
                conn.executemany('INSERT INTO apiKeys VALUES (?,?,?)', [('1','DISABLED',0),('2','ACTIVE',1)])
            self.assertEqual(m._read_db_key(), 'ACTIVE')

    def test_shared_backend_availability(self):
        for key in ("backend", "search_backend", "extract_backend"):
            with self.subTest(key=key), patch(
                "hermes_cli.config_effective.load_user_config_effective",
                return_value={"web": {key: " 9Router "}},
            ), patch.object(m, "_env", return_value=""):
                self.assertTrue(m.NineRouterWebSearchProvider().is_available())

    def test_request_settings_use_one_snapshot(self):
        remote = {"base_url": "https://router.example", "use_local_db_key": True,
                  "search_model": "remote-search"}
        local = {"base_url": m.LOCAL_URL, "use_local_db_key": True}
        with patch.object(m, "_load_config", side_effect=[remote, local]) as config, patch.object(
            m, "_env", return_value=""
        ), patch.object(m, "_read_db_key", return_value="SYNTHETIC_LOCAL") as db:
            base_url, headers, model = m._request_settings("search")
        self.assertEqual(base_url, "https://router.example")
        self.assertEqual(headers, {})
        self.assertEqual(model, "remote-search")
        config.assert_called_once_with()
        db.assert_not_called()

    def test_local_db_eligibility_is_exact(self):
        for base_url in (m.LOCAL_URL, "http://localhost:20128",
                         "http://127.0.0.1:20129", "http://127.0.0.1:20128/prefix",
                         "https://router.example"):
            with self.subTest(base_url=base_url), patch.object(
                m, "_load_config", return_value={"base_url": base_url, "use_local_db_key": True}
            ), patch.object(m, "_env", return_value=""), patch.object(
                m, "_read_db_key", return_value="SYNTHETIC_LOCAL"
            ) as db:
                resolved, headers, _ = m._request_settings("fetch")
                self.assertEqual(resolved, base_url)
                self.assertEqual(headers, {"Authorization": "Bearer SYNTHETIC_LOCAL"}
                                 if base_url == m.LOCAL_URL else {})
                self.assertEqual(db.call_count, int(base_url == m.LOCAL_URL))

    def test_null_and_insecure_config_rejected(self):
        for url in (None, 42, 'http://router.example', 'https://user:pass@router.example', 'https://router.example?q=secret', 'https://@router.example', 'https://router.example#fragment'):
            with self.subTest(url=url), patch.object(m,'_load_config',return_value={'base_url':url}):
                with self.assertRaises(ValueError): m._base_url()


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.provider = m.NineRouterWebSearchProvider()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(m, '_load_config', return_value={}))
        self.addCleanup(self.stack.close)

    def test_search_limit_and_normalization(self):
        with patch.object(m, '_post', return_value={'results':[{'url':'https://example.com','title':'Example','snippet':'Body'}]}) as post:
            result = self.provider.search('query',100)
            self.assertTrue(result['success'])
            self.assertEqual(post.call_args.args[1]['max_results'],100)

    def test_normalized_search_content(self):
        for content in ({"format": "text", "text": "full body", "length": 9}, "full body", None):
            for snippet in ("summary", ""):
                with self.subTest(content=content, snippet=snippet), patch.object(
                    m, "_post", return_value={"results": [
                        {"url": "https://example.com", "content": content, "snippet": snippet}
                    ]}
                ):
                    result = self.provider.search("query")
                    self.assertTrue(result["success"])
                    self.assertEqual(result["data"]["web"][0]["description"],
                                     snippet or ("full body" if content is not None else ""))

    def test_search_rejects_malformed_content_and_ports(self):
        for row in ({"url": "https://example.com", "content": {"text": []}},
                    {"url": "https://example.com", "content": {}},
                    {"url": "https://example.com:bad"},
                    {"url": "https://example.com:99999"},
                    {"url": "https://@example.com"}):
            with self.subTest(row=row), patch.object(m, "_post", return_value={"results": [row]}):
                self.assertFalse(self.provider.search("query")["success"])

    def test_post_transport_and_snapshot(self):
        import json
        seen = []
        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"results": []})
        real = m.httpx.Client
        with patch.object(m, "_request_settings",
                          return_value=("https://router.example", {"Authorization": "Bearer SYNTHETIC"}, "combo")), patch.object(
            m.httpx, "Client", side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        ) as client:
            self.assertEqual(m._post("/v1/search", {"query": "query", "max_results": 7}), {"results": []})
        self.assertEqual(str(seen[0].url), "https://router.example/v1/search")
        self.assertEqual(json.loads(seen[0].content),
                         {"model": "combo", "query": "query", "max_results": 7})
        self.assertEqual(seen[0].headers["authorization"], "Bearer SYNTHETIC")
        self.assertIs(client.call_args.kwargs["follow_redirects"], False)
        self.assertIs(client.call_args.kwargs["trust_env"], False)

    def test_malformed_search_and_errors(self):
        for data in ({'results':None}, {'results':[None]}, {'results':[{'url':'javascript:bad'}]}, {'results':[],'errors':['quota']}, {'results': [{'url':'https://example.com','content':[]}]}):
            with self.subTest(data=data), patch.object(m,'_post',return_value=data):
                self.assertFalse(self.provider.search('query')['success'])

    def test_decode_and_error_redaction(self):
        for data in (None, [], {'error':'SYNTHETIC_SECRET'}, {'success':False}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    m._decode(httpx.Response(200,json=data,request=httpx.Request('POST','https://example.com')))
        self.assertNotIn('SYNTHETIC_SECRET',m._error(ValueError('SYNTHETIC_SECRET')))

    def test_registration(self):
        import hermes_9router_web_search as package
        self.assertTrue(callable(package.register))
        class Context:
            def register_web_search_provider(self, provider):
                self.provider = provider
        ctx = Context(); package.register(ctx)
        self.assertEqual(ctx.provider.name,'9router')
        with patch.object(ctx,'register_web_search_provider',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): package.register(ctx)


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m,'_load_config',return_value={}))
        self.stack.enter_context(patch.object(m,'_env',return_value=''))
        self.stack.enter_context(patch.object(m,'_headers',return_value={}))
        self.safety=self.stack.enter_context(patch('tools.url_safety.async_is_safe_url',return_value=True))
        self.policy=self.stack.enter_context(patch('tools.website_policy.check_website_access',return_value=None))
        self.provider=m.NineRouterWebSearchProvider()

    def transport(self,handler):
        real=m.httpx.AsyncClient
        return self.stack.enter_context(patch.object(m.httpx,'AsyncClient',side_effect=lambda **kw: real(transport=httpx.MockTransport(handler),**kw)))

    async def test_normal_format(self):
        import json
        seen=[]
        def handler(req):
            seen.append(json.loads(req.content))
            return httpx.Response(200,json={'url':'https://example.com','title':None,'content':{'text':'body'}})
        self.transport(handler)
        result=await self.provider.extract(['https://example.com'],format='html')
        self.assertEqual(result[0]['content'],'body'); self.assertEqual(result[0]['title'],'')
        self.assertEqual(seen[0]['format'],'html')

    async def test_blocked_initial_url_never_sent(self):
        self.policy.return_value={'error':'blocked'}
        self.transport(lambda req: self.fail('blocked request sent'))
        result=await self.provider.extract(['https://example.com'])
        self.assertTrue(result[0]['blocked_by_policy'])

    async def test_private_and_final_policy(self):
        self.safety.return_value=False
        self.transport(lambda req: self.fail('unsafe request sent'))
        result=await self.provider.extract(['http://127.0.0.1'])
        self.assertTrue(result[0]['blocked_by_policy'])

    async def test_final_blocked(self):
        self.safety.side_effect=[True,False]
        self.transport(lambda req:httpx.Response(200,json={'url':'http://127.0.0.1','content':{'text':'secret'}}))
        result=await self.provider.extract(['https://example.com'])
        self.assertTrue(result[0]['blocked_by_policy']); self.assertNotIn('secret',str(result))

    async def test_malformed_content_and_http_errors(self):
        for content in (None, [], {'text':[]}, {'text':None}, ''):
            with self.subTest(content=content):
                with patch.object(m,'_decode',return_value={'content':content}):
                    with patch.object(m.httpx.AsyncClient,'post',return_value=None):
                        result=await self.provider.extract(['https://example.com'])
                self.assertIn('error',result[0]); self.assertEqual(result[0]['content'],'')

    async def test_checker_failures_are_sanitized_policy_refusals(self):
        self.transport(lambda req: self.fail("unverified request sent"))
        for checker in (self.safety, self.policy):
            for exc in (ImportError("SYNTHETIC_SECRET"), RuntimeError("SYNTHETIC_SECRET"),
                        PermissionError("SYNTHETIC_SECRET")):
                with self.subTest(checker=checker, exc=type(exc).__name__):
                    checker.side_effect = exc
                    result = await self.provider.extract(["https://example.com"])
                    self.assertTrue(result[0]["blocked_by_policy"])
                    self.assertNotIn("SYNTHETIC_SECRET", str(result))
                    checker.side_effect = None

    async def test_unrelated_permission_error_is_redacted(self):
        self.transport(lambda req: self.fail("request sent after configuration error"))
        with patch.object(m, "_request_settings", side_effect=PermissionError("SYNTHETIC_SECRET")):
            result = await self.provider.extract(["https://example.com"])
        self.assertIn("error", result[0])
        self.assertNotIn("blocked_by_policy", result[0])
        self.assertNotIn("SYNTHETIC_SECRET", str(result))

    async def test_initial_gate_timeout_never_sends(self):
        finished = asyncio.Event()
        async def hanging_safety(url):
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        self.safety.side_effect = hanging_safety
        self.transport(lambda req: self.fail("request sent after URL-check deadline"))
        with patch.object(m, "FETCH_TIMEOUT", 0.02):
            result = await asyncio.wait_for(self.provider.extract(["https://example.com"]), 1)
        self.assertTrue(result[0]["blocked_by_policy"])
        self.assertTrue(finished.is_set())

    async def test_final_gate_timeout_never_returns_content(self):
        calls = 0
        finished = asyncio.Event()
        async def safety(url):
            nonlocal calls
            calls += 1
            if calls == 1:
                return True
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        self.safety.side_effect = safety
        self.transport(lambda req: httpx.Response(
            200, json={"url": "https://example.org", "content": {"text": "UNVERIFIED_CONTENT"}}
        ))
        with patch.object(m, "FETCH_TIMEOUT", 0.02):
            result = await asyncio.wait_for(self.provider.extract(["https://example.com"]), 1)
        self.assertTrue(result[0]["blocked_by_policy"])
        self.assertNotIn("UNVERIFIED_CONTENT", str(result))
        self.assertTrue(finished.is_set())

    async def test_preparation_consumes_same_deadline(self):
        self.transport(lambda req: self.fail("request sent after preparation deadline"))
        clock = [0.0]
        real_settings = m._request_settings
        def settings(kind):
            clock[0] = m.FETCH_TIMEOUT + 1
            return real_settings(kind)
        with patch.object(m, "monotonic", side_effect=lambda: clock[0]), patch.object(
            m, "_request_settings", side_effect=settings
        ):
            result = await self.provider.extract(["https://example.com"])
        self.assertEqual(result[0]["error"], "9Router request timed out")

    async def test_gates_and_http_share_one_deadline(self):
        clock = [0.0]
        def safety(url):
            clock[0] += 10
            return True
        self.safety.side_effect = safety
        def handler(req):
            clock[0] += 30
            return httpx.Response(200, json={"url": "https://example.com", "content": {"text": "body"}})
        self.transport(handler)
        with patch.object(m, "monotonic", side_effect=lambda: clock[0]):
            result = await self.provider.extract(["https://example.com"])
        # Each phase alone fits 45s; the cumulative 50s must refuse the content.
        self.assertTrue(result[0]["blocked_by_policy"])
        self.assertEqual(result[0]["content"], "")

    async def test_partial_errors_order_and_source_url(self):
        seen = []
        def handler(req):
            seen.append(str(req.url))
            import json
            url = json.loads(req.content)["url"]
            if url.endswith("/b"):
                return httpx.Response(503, text="SYNTHETIC_SECRET")
            return httpx.Response(200, json={
                "url": "https://example.org/final", "content": {"text": "body"},
                "metadata": {"sourceURL": "https://example.com/wrong", "language": "en"},
            })
        self.transport(handler)
        result = await self.provider.extract(["https://example.com/a", "https://example.com/b"])
        self.assertEqual(result[0]["metadata"]["sourceURL"], "https://example.com/a")
        self.assertEqual(result[0]["metadata"]["language"], "en")
        self.assertEqual(result[1]["url"], "https://example.com/b")
        self.assertEqual(result[1]["error"], "9Router HTTP 503")
        self.assertNotIn("SYNTHETIC_SECRET", str(result))
        self.assertTrue(all(url == m.LOCAL_URL + "/v1/web/fetch" for url in seen))

    async def test_cancellation_during_url_check_stops_batch(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def safety(url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.safety.side_effect = safety
        self.transport(lambda req: self.fail("request sent during cancelled check"))
        task = asyncio.create_task(self.provider.extract(["https://example.com/a", "https://example.com/b"]))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.safety.call_count, 1)

    async def test_cancellation_stops_batch(self):
        calls=[]; started=asyncio.Event(); cancelled=asyncio.Event()
        async def handler(req):
            calls.append(req); started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        self.transport(handler)
        task=asyncio.create_task(self.provider.extract(['https://example.com/a','https://example.com/b']))
        await asyncio.wait_for(started.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cancelled.is_set()); self.assertEqual(len(calls),1)


if __name__ == '__main__':
    unittest.main()
