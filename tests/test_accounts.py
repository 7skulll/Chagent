"""Registration, persisted login, roles, tenant boundaries and admin-only tests."""
import asyncio
import json
from contextlib import asynccontextmanager
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from chagent.users.store import AccountStore, AuthenticationError, COOKIE
from chagent.web.tenants import TenantRuntimes
from eval.platform import Platform
from eval.web import create_app

PASSWORD = 'correct-local-password-123'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / 'accounts')

    def admin(self, name='owner'):
        return self.store.register(name, PASSWORD, setup_code=self.store.setup_path.read_text())

    def test_registration_is_always_regular_even_before_first_admin(self):
        user = self.store.register('visitor', PASSWORD)
        self.assertEqual(user['role'], 'user')
        self.assertTrue(self.store.setup_required())
        self.assertEqual(self.admin()['role'], 'admin')
        self.assertFalse(self.store.setup_required())

    def test_admin_setup_requires_local_code_and_is_one_time(self):
        with self.assertRaises(AuthenticationError):
            self.store.register('badowner', PASSWORD, setup_code='wrong' * 10)
        self.admin()
        with self.assertRaises(ValueError):
            self.admin('another-owner')
        self.assertEqual(len(self.store.users()), 1)

    def test_only_one_first_admin_wins_concurrent_setup(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        code = self.store.setup_path.read_text()
        def attempt(index):
            barrier.wait()
            try:
                self.store.register('owner' + str(index), PASSWORD, setup_code=code)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, (1, 2))), 1)
        self.assertEqual(len(self.store.users()), 1)

    def test_password_and_session_not_stored_in_plaintext_and_survive_restart(self):
        user = self.admin()
        token, _ = self.store.login('owner', PASSWORD)
        with self.store.connection() as db:
            row = db.execute('SELECT password_hash,salt FROM users').fetchone()
            session = db.execute('SELECT token_hash FROM sessions').fetchone()
        self.assertNotEqual(row['password_hash'], PASSWORD)
        self.assertEqual(session['token_hash'], sha256(token.encode()).hexdigest())
        cookie = f'{COOKIE}={token}'
        self.assertEqual(AccountStore(self.store.root).session(cookie)['id'], user['id'])
        self.assertIsNone(self.store.session(f'{COOKIE}=forged'))

    def test_logout_and_expiry_revoke_session(self):
        self.admin()
        token, _ = self.store.login('owner', PASSWORD)
        cookie = f'{COOKIE}={token}'
        self.store.logout(cookie)
        self.assertIsNone(self.store.session(cookie))
        token, _ = self.store.login('owner', PASSWORD)
        with self.store.connection(write=True) as db:
            db.execute('UPDATE sessions SET expires=0')
        self.assertIsNone(self.store.session(f'{COOKIE}={token}'))

    def test_malformed_cookies_are_not_authenticated(self):
        for text in ('', 'bad;', 'a=\\', f'{COOKIE}='):
            self.assertIsNone(self.store.session(text))

    def test_regular_user_cannot_promote_self_and_last_admin_is_preserved(self):
        admin = self.admin()
        visitor = self.store.register('visitor', PASSWORD)
        with self.assertRaises(PermissionError):
            self.store.update(visitor['id'], visitor['id'], role='admin')
        for change in ({'role': 'user'}, {'active': False}):
            with self.assertRaises(ValueError):
                self.store.update(admin['id'], admin['id'], **change)

    def test_deactivation_revokes_existing_cookie_immediately(self):
        admin = self.admin()
        user = self.store.register('visitor', PASSWORD)
        token, _ = self.store.login('visitor', PASSWORD)
        self.store.update(admin['id'], user['id'], active=False)
        self.assertIsNone(self.store.session(f'{COOKIE}={token}'))
        with self.assertRaises(AuthenticationError):
            self.store.login('visitor', PASSWORD)

    def test_user_directories_reject_other_user_and_symlink_escape(self):
        a, b = self.admin(), self.store.register('visitor', PASSWORD)
        wa, wb = self.store.workspace(a['id']), self.store.workspace(b['id'])
        self.assertNotEqual(wa, wb)
        with self.assertRaises(PermissionError):
            self.store.guard(a['id'], wb)
        (wa / 'escape').symlink_to(wb, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.store.guard(a['id'], wa / 'escape')
        with self.assertRaises(PermissionError):
            self.store.guard(a['id'], self.store.root)

    def test_failed_logins_are_throttled(self):
        self.admin()
        for _ in range(5):
            with self.assertRaises(AuthenticationError):
                self.store.login('owner', 'incorrect-password-123')
        with self.assertRaisesRegex(AuthenticationError, '稍后'):
            self.store.login('owner', PASSWORD)

    def test_short_password_and_duplicate_normalized_username_rejected(self):
        with self.assertRaises(ValueError):
            self.store.register('visitor', 'short')
        self.store.register('Visitor', PASSWORD)
        with self.assertRaises(ValueError):
            self.store.register('visitor', PASSWORD)


class AccountHTTPTests(unittest.TestCase):
    admin = StoreTests.admin
    # Reuse setup only; do not re-run all store cases through inheritance.
    def setUp(self):
        StoreTests.setUp(self)
        self.owner = self.admin()
        self.visitor = self.store.register('visitor', PASSWORD)
        self.template = Platform(Path(self.temp.name) / 'results')
        self.client = TestClient(create_app(self.template, accounts=self.store))
        self.addCleanup(self.client.close)

    def login_as(self, username):
        r = self.client.post('/api/accounts/login', json={'username': username, 'password': PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_anonymous_test_api_and_openapi_are_denied(self):
        for path in ('/api/jobs', '/api/catalog', '/openapi.json'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertIn('登录', self.client.get('/').text)
        self.assertEqual(self.client.get('/health').status_code, 200)

    def test_regular_user_cannot_read_or_start_or_stop_any_test(self):
        self.login_as('visitor')
        for path in ('/api/jobs', '/api/catalog', '/api/jobs/web-20261114-111111111111/report'):
            self.assertEqual(self.client.get(path).status_code, 403)
        for path in ('/api/plans', '/api/jobs/web-20261114-111111111111/start',
                     '/api/jobs/web-20261114-111111111111/stop'):
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/accounts/users').status_code, 403)
        self.assertIn('普通用户', self.client.get('/').text)

    def test_signup_cannot_supply_role_and_cookies_are_httponly(self):
        r = self.client.post('/api/accounts/register', json={'username': 'attacker',
            'password': PASSWORD, 'role': 'admin'})
        self.assertEqual(r.status_code, 400)
        r = self.login_as('owner')
        self.assertIn('httponly', r.headers['set-cookie'].lower())
        self.assertIn('samesite=strict', r.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/api/catalog').status_code, 200)

    def test_admin_test_resources_are_separate(self):
        second = self.store.register('second', PASSWORD)
        self.store.update(self.owner['id'], second['id'], role='admin')
        from eval.web import PlanRequest
        from chagent.config import Settings
        from chagent.models.settings import ModelSettings
        import test_eval_report
        pinned = test_eval_report.trial('pass', arm='baseline').manifest['execution']
        pinned = {k: v for k, v in pinned.items() if k != 'end_identity_sha256'}
        self.login_as('owner')
        with patch('eval.platform.resolved', side_effect=lambda overrides, arm: (
            {}, Settings(), ModelSettings('deepseek', arm.model, 'https://api.deepseek.com', 'synthetic'), pinned)):
            body = PlanRequest(cases=['project-work/refuse-outside-workspace-host@1'],
                               pricing_confirmed=True).model_dump()
            r = self.client.post('/api/plans', json=body)
        self.assertEqual(r.status_code, 201, r.text)
        job = r.json()['id']
        self.login_as('second')
        self.assertEqual(self.client.get('/api/jobs').json()['jobs'], [])
        self.assertEqual(self.client.get('/api/jobs/' + job).status_code, 404)
        self.assertEqual(self.client.post('/api/jobs/' + job + '/start', json={}).status_code, 404)

    def test_admin_revocation_takes_effect_on_an_existing_cookie(self):
        self.login_as('owner')
        self.store.update(self.owner['id'], self.visitor['id'], role='admin')
        self.login_as('visitor')
        self.store.update(self.owner['id'], self.visitor['id'], role='user')
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)
        self.login_as('visitor')
        self.assertEqual(self.client.get('/api/jobs').status_code, 403)

    def test_injected_runtimes_cannot_implicitly_bypass_login(self):
        from types import SimpleNamespace
        from chagent.config import Settings
        from chagent.web.app import create_app as product_app
        runtime = SimpleNamespace(settings=Settings())
        workspace = Path(self.temp.name) / 'unowned'
        workspace.mkdir()
        with self.assertRaisesRegex(ValueError, 'separate account runtimes'):
            product_app(workspace=workspace, injected_runtime=runtime, accounts=self.store)
        with self.assertRaisesRegex(ValueError, 'requires an injected runtime'):
            product_app(workspace=workspace, _test_without_accounts=True)

    def test_eval_honors_the_same_account_directory_from_local_configuration(self):
        directory = Path(self.temp.name) / 'configured-accounts'
        with patch('eval.cli._evaluation_env', return_value={'CHAGENT_ACCOUNT_DIR': str(directory)}):
            app = create_app()
        self.assertEqual(app.state.accounts.root, directory.resolve())

    def test_cross_origin_login_cannot_set_a_cookie(self):
        r = self.client.post('/api/accounts/login', json={'username': 'owner', 'password': PASSWORD},
                             headers={'Origin': 'https://outside.example'})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('set-cookie', r.headers)


class TenantPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_users_get_distinct_cached_runtimes_and_revocation_closes_one(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp) / 'accounts')
            a, b = store.register('alice', PASSWORD), store.register('bobby', PASSWORD)
            closed = []
            @asynccontextmanager
            async def factory(user):
                try:
                    yield object()
                finally:
                    closed.append(user['id'])
            pool = TenantRuntimes(store, factory)
            ra, rb = await asyncio.gather(pool.get(a), pool.get(b))
            self.assertIsNot(ra, rb)
            self.assertIs(await pool.get(a), ra)
            await pool.invalidate(a)
            self.assertEqual(closed, [a['id']])
            self.assertIs(await pool.get(b), rb)
            await pool.close()


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'), 'Set CHAGENT_TEST_DATABASE_URL')
class AccountDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_cannot_share_sessions_memory_files_or_workspace_identifiers(self):
        import httpx
        from psycopg import AsyncConnection, sql
        from chagent.config import Settings
        from chagent.web.app import create_app as product_app
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp) / 'accounts')
            alice = store.register('alice', PASSWORD)
            bob = store.register('bobby', PASSWORD)
            legacy = Path(temp) / 'legacy'
            legacy.mkdir()
            app = product_app(workspace=legacy, accounts=store, demo=True,
                settings=Settings(database_url=os.environ['CHAGENT_TEST_DATABASE_URL']))
            try:
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as a, \
                               httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as b:
                        self.assertEqual((await a.get('/api/sessions')).status_code, 401)
                        for client, name in ((a, 'alice'), (b, 'bobby')):
                            r = await client.post('/api/accounts/login', json={'username': name, 'password': PASSWORD})
                            self.assertEqual(r.status_code, 200, r.text)
                        sa = await a.post('/api/sessions', json={'title': 'alice-private'})
                        self.assertEqual(sa.status_code, 201, sa.text)
                        sid = sa.json()['id']
                        self.assertEqual((await b.get('/api/sessions/' + sid)).status_code, 404)
                        self.assertNotIn('alice-private', (await b.get('/api/sessions')).text)
                        wa, wb = store.workspace(alice['id']), store.workspace(bob['id'])
                        sandbox = app.state.tenants.items[alice['id']][0].runner.sandbox
                        self.assertIsNotNone(sandbox.owner_guard)
                        with self.assertRaises(PermissionError):
                            sandbox.owner_guard(wb)
                        (wa / 'alice.txt').write_text('private-marker')
                        denied = await b.post('/api/workspaces', json={'path': str(wa)})
                        self.assertIn(denied.status_code, (400, 403))
                        wid = (await a.get('/api/workspaces')).json()['items'][0]['id']
                        self.assertEqual((await b.get(f'/api/workspaces/{wid}/files')).status_code, 404)
                        r = await a.post('/api/memories', json={'key': 'private-key', 'content': 'alice-memory',
                                                              'scope': 'workspace', 'pinned': False})
                        self.assertIn(r.status_code, (200, 201), r.text)
                        self.assertNotIn('alice-memory', (await b.get('/api/memories')).text)
                        (wb / 'link').symlink_to(wa, target_is_directory=True)
                        self.assertIn((await b.post('/api/workspaces', json={'path': str(wb / 'link')})).status_code, (400, 403))
                        self.assertIn(alice['id'], (await a.get('/')).text)
                        self.assertNotIn(alice['id'], (await b.get('/')).text)
            finally:
                async with await AsyncConnection.connect(os.environ['CHAGENT_TEST_DATABASE_URL'], autocommit=True) as db:
                    for user in (alice, bob):
                        await db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                            sql.Identifier('chagent_user_' + user['id'])))


class BrowserStorageTests(unittest.TestCase):
    def test_storage_and_draft_pruning_are_scoped_by_account(self):
        import subprocess
        module = Path(__file__).resolve().parent.parent / 'src/chagent/web/static/core/account-storage.js'
        script = r'''
const {scopedStorage}=await import(process.argv[1]);
const data=new Map();const raw={get length(){return data.size},key:i=>[...data.keys()][i]??null,getItem:k=>data.get(k)??null,setItem:(k,v)=>data.set(k,String(v)),removeItem:k=>data.delete(k)};
const a=scopedStorage(raw,'alice'),b=scopedStorage(raw,'bob');
a.setItem('chagent.session','private-session');a.setItem('chagent.draft.file','private-draft');
if(b.length!==0||b.getItem('chagent.session')!==null)throw Error('cross-user read');
b.setItem('chagent.session','bob-session');b.removeItem('chagent.draft.file');
if(a.getItem('chagent.draft.file')!=='private-draft'||a.length!==2||b.length!==1)throw Error('cross-user mutation');
console.log('account storage isolation OK');
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, module.as_uri()],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class TenantContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_users_containers_are_never_inspected_cleaned_or_listed(self):
        from hashlib import sha256
        from chagent.tools.sandbox import Sandbox, SandboxSettings
        with tempfile.TemporaryDirectory() as temporary:
            own, other = Path(temporary) / 'own', Path(temporary) / 'other'
            own.mkdir()
            other.mkdir()
            def name(root):
                return 'chagent-ws-' + sha256(os.fsencode(str(root))).hexdigest()[:20]
            class Cursor:
                async def fetchall(self):
                    return [{'host_path': str(own), 'state': 'active'}]
                async def fetchone(self):
                    return None
            class Connection:
                async def execute(self, query, values=None):
                    return Cursor()
            class Database:
                @asynccontextmanager
                async def connection(self):
                    yield Connection()
            sandbox = Sandbox(SandboxSettings(host_controller=True))
            def guard(path):
                if not path.resolve().is_relative_to(own.resolve()):
                    raise PermissionError('other account')
            sandbox.owner_guard = guard
            calls = []
            async def docker(*args, **kwargs):
                calls.append(args)
                if args[0] == 'ps':
                    return 0, name(own) + '\n' + name(other) + '\n'
                if args[0] == 'inspect' and args[1] == name(other):
                    # With the old global sweep, this foreign stopped container
                    # would be misclassified as an unbound orphan and deleted.
                    return 0, json.dumps({'Mounts': [
                        {'Type': 'bind', 'Source': str(other), 'Destination': str(other)},
                        {'Type': 'volume', 'Destination': '/cache'}],
                        'State': {'Running': False}, 'Config': {'Labels': {
                        'chagent.workspace': sha256(os.fsencode(str(other))).hexdigest()}}})
                if args[0] == 'inspect':
                    # An incomplete mount record is intentionally retained.
                    return 0, json.dumps({'Mounts': [], 'State': {'Running': False},
                        'Config': {'Labels': {'chagent.workspace': sha256(os.fsencode(str(own))).hexdigest()}}})
                return 0, ''
            sandbox._docker = docker
            self.assertEqual(await sandbox.reap_terminal_scratch(Database()), 0)
            self.assertEqual(await sandbox.reap_orphan_containers(Database()), 0)
            inventory = await sandbox.container_inventory(Database())
            self.assertEqual([row['name'] for row in inventory], [name(own)])
            self.assertTrue(all(name(other) not in call for call in calls if call[0] != 'ps'))
            with self.assertRaises(PermissionError):
                await sandbox.ensure_container(other)
