"""Lazy per-account runtimes, private local workspaces and PostgreSQL namespaces."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo


class TenantRuntimes:
    def __init__(self, store, factory):
        self.store, self.factory = store, factory
        self.items = {}
        self.lock = asyncio.Lock()
        self.watcher = None

    async def get(self, user):
        async with self.lock:
            current = self.store.user(user['id'])
            if current is None:
                raise PermissionError('账户已停用')
            if user['id'] not in self.items:
                stack = AsyncExitStack()
                try:
                    runtime = await stack.enter_async_context(self.factory(current))
                except BaseException:
                    await stack.aclose()
                    raise
                self.items[user['id']] = (runtime, stack)
            return self.items[user['id']][0]

    async def invalidate(self, user):
        async with self.lock:
            item = self.items.pop(user['id'], None)
            if item:
                await item[1].aclose()

    async def watch(self):
        while True:
            await asyncio.sleep(2)
            for uid in list(self.items):
                if self.store.user(uid) is None:
                    await self.invalidate({'id': uid})

    def start(self):
        self.watcher = asyncio.create_task(self.watch(), name='account-runtime-revocation')

    async def close(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        async with self.lock:
            for _, stack in self.items.values():
                await stack.aclose()
            self.items.clear()


def runtime_factory(store, configured, model_settings, demo):
    @asynccontextmanager
    async def factory(user):
        from chagent.web.app import create_app
        uid = user['id']
        # uid is minted by AccountStore, never taken from a request path.
        store.data_root(uid)
        schema = 'chagent_user_' + uid
        async with await AsyncConnection.connect(configured.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
        settings = replace(configured, database_url=make_conninfo(configured.database_url,
            options=f'-c search_path={schema}'))
        inner = create_app(workspace=store.workspace(uid), settings=settings,
            model_settings=model_settings, demo=demo, _tenant=True, _native_allowed=False,
            _workspace_guard=lambda path: store.guard(uid, path))
        async with inner.router.lifespan_context(inner):
            runtime = inner.state.runtime
            runtime.auth_guard = lambda path: store.guard(uid, path)
            runtime.account_id = uid
            yield runtime
    return factory
