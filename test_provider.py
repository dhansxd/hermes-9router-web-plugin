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

    def test_null_and_insecure_config_rejected(self):
        for url in (None, 42, 'http://router.example', 'https://user:pass@router.example', 'https://router.example?q=secret'):
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
