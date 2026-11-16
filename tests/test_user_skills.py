"""Imported bundles, tenant isolation, revocation and ordinary-user HTTP flows."""
import asyncio
import base64
from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import httpx
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.config import Settings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.context import ToolContext
from chagent.tools.user_skills import UserSkillTool
from chagent.user_skills import parse_import, UserSkillStore
from chagent.users.store import AccountStore, COOKIE
from chagent.web.app import create_app
from chagent.web.accounts import mount_accounts
from chagent.web.deps import runtime

CONTENT = b'---\nname: project-explorer\ndescription: "Personal review workflow"\n---\nCheck evidence before conclusions.\n'

def archive(entries):
    out = BytesIO()
    with ZipFile(out, 'w') as z:
        for path, data in entries:
            z.writestr(path, data)
    return out.getvalue()


class ImportTests(unittest.TestCase):
    def test_md_and_zip_keep_identity_and_references_without_extracting(self):
        plain = parse_import('SKILL.md', CONTENT)
        bundle = parse_import('download.zip', archive([('sample/SKILL.md', CONTENT), ('sample/references/a.md', b'Reference')]))
        self.assertEqual(plain['name'], 'project-explorer')
        self.assertEqual(base64.b64decode(bundle['files']['references/a.md']), b'Reference')
        self.assertEqual(bundle['content'], plain['content'])

    def test_rejects_traversal_symlink_duplicate_and_multiple_skills(self):
        for path in ('../bad', '/absolute', 'sample/../../bad', 'sample\\bad'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_import('x.zip', archive([('SKILL.md', CONTENT), (path, b'bad')]))
        link = ZipInfo('link'); link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in ([('SKILL.md', CONTENT), (link, b'/etc/passwd')],
                        [('a/SKILL.md', CONTENT), ('b/SKILL.md', CONTENT)],
                        [('a/SKILL.md', CONTENT), ('outside.md', b'x')]):
            with self.assertRaises(ValueError):
                parse_import('x.zip', archive(entries))

    def test_rejects_invalid_metadata_encoding_size_and_zip(self):
        for filename, content in [('SKILL.md', b'plain'), ('SKILL.md', CONTENT.replace(b'project-explorer', b'../evil')),
                                  ('SKILL.md', CONTENT+b'x'*65536), ('x.zip', b'broken'), ('x.exe', CONTENT),
                                  ('SKILL.md', b'\xff'), ('SKILL.md', b'---\nname: ok\ndescription: [bad]\n---\nbody')]:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                parse_import(filename, content)


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'), 'Set CHAGENT_TEST_DATABASE_URL')
class UserSkillIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin = await AsyncConnection.connect(os.environ['CHAGENT_TEST_DATABASE_URL'], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        self.databases = []
        for _ in range(2):
            schema = 'chagent_skill_test_' + uuid4().hex
            await self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            self.addAsyncCleanup(self.drop, schema)
            db = Database(Settings(database_url=make_conninfo(os.environ['CHAGENT_TEST_DATABASE_URL'], options=f'-c search_path={schema}')))
            await db.connect(); self.addAsyncCleanup(db.close)
            await migrate(db); self.databases.append(db)
        self.store = UserSkillStore(self.databases[0])

    async def drop(self, schema):
        await self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    async def test_real_runner_reads_and_audits_enabled_bundle(self):
        from chagent.bootstrap import create_demo_runner
        from chagent.domain.messages import ToolResultMessage
        row = await self.store.import_skill(parse_import('SKILL.md', CONTENT))
        await self.store.change(row['id'], 1, enabled=True)
        runner = create_demo_runner(self.databases[0], tool_name='read_user_skill',
                                    tool_arguments={'skill_id': str(row['id'])})
        runner.tools.registry.register(UserSkillTool(self.databases[0], read=True))
        with tempfile.TemporaryDirectory() as folder:
            session = await runner.conversations.create_session(Path(folder))
            result = await runner.run(session, 'Use my personal workflow')
            self.assertIn('Check evidence before conclusions', result.message.content[0].text)
            messages = await runner.conversations.messages(session)
            loaded = [m.message for m in messages if isinstance(m.message, ToolResultMessage)
                      and m.message.tool_name == 'read_user_skill']
            self.assertEqual(loaded[0].details['sha256'], row['sha256'])
            await self.store.change(row['id'], 2, enabled=False)
            result = await runner.run(session, 'Try loading it again')
            self.assertIn('技能未开启', result.message.content[0].text)

    async def test_disable_cancels_only_runs_that_loaded_this_skill(self):
        from unittest.mock import Mock
        from chagent.web.skills_api import disable, SkillAction
        row = await self.store.import_skill(parse_import('SKILL.md', CONTENT))
        await self.store.change(row['id'], 1, enabled=True)
        used, unused = uuid4(), uuid4()
        runner = SimpleNamespace(runs=SimpleNamespace(database=self.databases[0]),
            _states={used: SimpleNamespace(loaded_user_skills={str(row['id'])}),
                     unused: SimpleNamespace(loaded_user_skills=set())}, cancel=Mock(return_value=True))
        result = await disable(row['id'], SkillAction(expected_revision=2), SimpleNamespace(runner=runner))
        runner.cancel.assert_called_once_with(used)
        self.assertEqual(result['cancellation_requested'], [str(used)])

    async def test_default_off_snapshot_isolation_reference_and_revocation(self):
        skill = parse_import('s.zip', archive([('s/SKILL.md', CONTENT), ('s/references/a.md', b'Evidence')]))
        row = await self.store.import_skill(skill)
        self.assertFalse(row['enabled']); self.assertEqual(await self.store.list(enabled=True), [])
        self.assertEqual(await UserSkillStore(self.databases[1]).list(), [])
        with self.assertRaises(LookupError):
            await UserSkillStore(self.databases[1]).get(row['id'])
        await self.store.change(row['id'], 1, enabled=True)
        active = await self.store.get(row['id']); key = str(row['id'])
        context = ToolContext(Path('/tmp'), user_skills={key: active})
        read = UserSkillTool(self.databases[0], read=True)
        found = await UserSkillTool(self.databases[0]).execute('s', {'query': 'personal'}, context=context)
        self.assertIn(key, found.content[0].text)
        result = await read.execute('r', {'skill_id': key, 'path': 'references/a.md'}, context=context)
        self.assertIn('Evidence', result.content[0].text); self.assertIn(key, context.loaded_user_skills)
        with self.assertRaises(ValueError):
            await read.execute('r', {'skill_id': key, 'path': '../SKILL.md'}, context=context)
        with self.assertRaises(ValueError):
            await read.execute('r', {'skill_id': key}, context=ToolContext(Path('/tmp'), user_skills={}))
        await self.store.change(row['id'], 2, enabled=False)
        with self.assertRaises(ValueError):
            await read.execute('r', {'skill_id': key}, context=context)
        await self.store.change(row['id'], 3, enabled=True)
        with self.assertRaises(ValueError):
            await read.execute('r', {'skill_id': key}, context=context)
        with self.assertRaises(ValueError):
            await UserSkillTool(self.databases[1], read=True).execute('r', {'skill_id': key}, context=context)

    async def test_regular_users_can_import_but_cannot_manage_each_others_skills(self):
        from chagent.bootstrap import create_demo_runner
        from chagent.web.runtime import WebRuntime
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            accounts = AccountStore(root / 'accounts')
            users = [accounts.register(name, 'long-enough-password-123') for name in ('alice', 'bob')]
            runtimes = [WebRuntime(create_demo_runner(db), root) for db in self.databases]
            app = create_app(workspace=root, injected_runtime=runtimes[0], _test_without_accounts=True)
            mount_accounts(app, accounts)
            async def selected(request: __import__('fastapi').Request):
                return runtimes[0 if request.scope['account']['id'] == users[0]['id'] else 1]
            app.dependency_overrides[runtime] = selected
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as client:
                self.assertEqual((await client.get('/api/skills')).status_code, 401)
                for index, name in enumerate(('alice', 'bob')):
                    token, _ = accounts.login(name, 'long-enough-password-123'); client.cookies.set(COOKIE, token)
                    self.assertEqual(users[index]['role'], 'user')
                    if index == 0:
                        result = await client.post('/api/user-skills/import', json={'filename':'SKILL.md','data':base64.b64encode(CONTENT).decode()})
                        self.assertEqual(result.status_code, 201, result.text); row = result.json()
                        self.assertFalse(row['enabled'])
                        self.assertEqual((await client.post('/api/user-skills/'+row['id']+'/activate', json={'expected_revision':1})).status_code, 200)
                        stale = await client.post('/api/user-skills/'+row['id']+'/disable', json={'expected_revision':1})
                        self.assertEqual(stale.status_code, 409)
                    else:
                        self.assertEqual((await client.get('/api/skills')).json()['user'], [])
                        self.assertEqual((await client.post('/api/user-skills/'+row['id']+'/activate', json={'expected_revision':2})).status_code, 409)
                        self.assertEqual((await client.request('DELETE', '/api/user-skills/'+row['id'], json={'expected_revision':2})).status_code, 409)
