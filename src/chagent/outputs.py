"""Persistent output intents. Only the host consumes confirmation tokens.

A staged intent is durable before disk mutation. A process crash between disk
and DB commit is reported as staged/unknown and never automatically replayed.
"""
import asyncio
import logging
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import uuid4

from chagent.files import FileService, FileConflict
from chagent.tools.workspace import coordinated

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 20 * 1024 * 1024


async def disk_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class OutputFS(FileService):
    @coordinated
    def snapshot(self, path):
        with self.parent(path) as (parent, name):
            data, _ = self._read_at(parent, name, MAX_OUTPUT_BYTES)
            info = self.object_info(parent, name)
        return sha256(data).hexdigest(), self.token(info)

    @coordinated
    def stage(self, path, data):
        try:
            version, identity = self.snapshot(path)
        except FileNotFoundError:
            # Missing parent must fail; write never creates unrequested directories.
            self.write(path, data, limit=MAX_OUTPUT_BYTES)
            return dict(status='committed', output_path=path)
        return dict(status='pending', expected_hash=version, expected_identity=identity)

    @coordinated
    def decide(self, row, choice):
        path, data = row['path'], bytes(row['payload'])
        try:
            version, identity = self.snapshot(path)
        except FileNotFoundError:
            raise FileConflict('同名文件已删除或移动，请取消后重新生成') from None
        if (version, identity) != (row['expected_hash'], row['expected_identity']):
            return dict(status='pending', expected_hash=version, expected_identity=identity, token=uuid4())
        if choice == 'overwrite':
            self.write(path, data, overwrite=True, expected_hash=version, limit=MAX_OUTPUT_BYTES)
        else:
            original = PurePosixPath(path)
            for number in range(1, 1001):
                candidate = str(original.with_name(f'{original.stem}-{number}{original.suffix}'))
                try:
                    self.write(candidate, data, limit=MAX_OUTPUT_BYTES)
                except FileExistsError:
                    continue
                path = candidate
                break
            else:
                raise ValueError('同名文件过多，请更换目标目录')
        return dict(status='committed', output_path=path)


def public(row):
    return {key: row[key] for key in ('id', 'workspace_id', 'session_id', 'source', 'path', 'status', 'token', 'output_path', 'size', 'expires_at')}


class OutputService:
    def __init__(self, database):
        self.database = database

    async def record(self, workspace_id, source, operation, path):
        async with self.database.connection() as connection:
            await connection.execute('INSERT INTO file_operations (workspace_id, source, operation, path) VALUES (%s,%s,%s,%s)',
                                     (workspace_id, source, operation, path))

    async def submit(self, workspace_id, root, path, data, *, source, session_id=None, run_id=None, tool_call_id=None, request_id=None, workspace_identity=None):
        fs = OutputFS(root, expected_identity=workspace_identity)
        if not fs.parts(path):
            raise ValueError('文件路径不能为空')
        if len(data) > MAX_OUTPUT_BYTES:
            raise ValueError('输出超过 20 MiB 上限')
        digest = sha256(data).hexdigest()
        identity = request_id or uuid4()
        async with self.database.transaction() as connection:
            if session_id is not None:
                cursor = await connection.execute('SELECT workspace_id FROM sessions WHERE id=%s', (session_id,))
                session = await cursor.fetchone()
                if session is None or session['workspace_id'] != workspace_id:
                    raise ValueError('输出会话与工作区不一致')
            cursor = await connection.execute(
                "INSERT INTO file_outputs (id,workspace_id,session_id,run_id,tool_call_id,source,path,payload,content_hash,size,status,token) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'staged',%s) ON CONFLICT DO NOTHING RETURNING *",
                (identity,workspace_id,session_id,run_id,tool_call_id,source,path,data,digest,len(data),uuid4()))
            row = await cursor.fetchone()
            fresh = row is not None
            if not fresh:
                cursor = await connection.execute('SELECT * FROM file_outputs WHERE id=%s OR (run_id=%s AND tool_call_id=%s)', (identity,run_id,tool_call_id))
                row = await cursor.fetchone()
                if row is None or (row['workspace_id'],row['path'],row['content_hash']) != (workspace_id,path,digest):
                    raise FileConflict('输出请求标识已用于其他内容')
        if not fresh:
            return public(row)
        try:
            result = await disk_call(fs.stage, path, data)
        except Exception:
            async with self.database.connection() as connection:
                await connection.execute("UPDATE file_outputs SET status='failed',payload=NULL WHERE id=%s", (identity,))
            raise
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                'UPDATE file_outputs SET status=%s,expected_hash=%s,expected_identity=%s,output_path=%s, '
                "payload=CASE WHEN %s='committed' THEN NULL ELSE payload END WHERE id=%s RETURNING *",
                (result['status'], result.get('expected_hash'), result.get('expected_identity'), result.get('output_path'), result['status'], identity))
            row = await cursor.fetchone()
            if row['status'] == 'committed':
                await connection.execute('INSERT INTO file_operations(workspace_id,source,operation,path) VALUES(%s,%s,%s,%s)', (workspace_id,source,'output',row['output_path']))
        return public(row)

    async def pending(self, workspace_id):
        async with self.database.connection() as connection:
            await connection.execute("UPDATE file_outputs SET status='expired',payload=NULL WHERE workspace_id=%s AND status='pending' AND expires_at<=now()", (workspace_id,))
            cursor = await connection.execute("SELECT * FROM file_outputs WHERE workspace_id=%s AND status IN ('pending','staged') ORDER BY created_at", (workspace_id,))
            return [public(row) for row in await cursor.fetchall()]

    async def decide(self, workspace_id, root, identity, token, choice):
        if choice not in {'overwrite', 'keep-both', 'cancel'}:
            raise ValueError('Unknown output decision')
        stale = False
        async with self.database.transaction() as connection:
            cursor = await connection.execute('SELECT *, expires_at<=now() AS expired FROM file_outputs WHERE id=%s AND workspace_id=%s FOR UPDATE', (identity,workspace_id))
            row = await cursor.fetchone()
            if row is None:
                raise FileNotFoundError('确认记录不存在')
            if row['status'] != 'pending' or row['token'] != token:
                raise FileConflict('确认已失效或已处理，请刷新')
            if row['expired'] or choice == 'cancel':
                status = 'expired' if row['expired'] else 'cancelled'
                cursor = await connection.execute("UPDATE file_outputs SET status=%s,payload=NULL,decided_at=now() WHERE id=%s RETURNING *", (status,identity))
                updated = await cursor.fetchone()
            else:
                # Consume before touching disk. A crash/cancel leaves staged (unknown),
                # which cannot be replayed with an old token.
                await connection.execute("UPDATE file_outputs SET status='staged',token=%s WHERE id=%s", (uuid4(),identity))
                updated = None
        if updated is not None:
            if updated['status'] == 'expired':
                raise FileConflict('确认已超时，未写入；请重新提交输出')
            return public(updated)
        try:
            result = await disk_call(OutputFS(root).decide, row, choice)
        except Exception:
            async with self.database.connection() as connection:
                await connection.execute("UPDATE file_outputs SET status='failed',payload=NULL WHERE id=%s", (identity,))
            raise
        stale = result['status'] == 'pending'
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                'UPDATE file_outputs SET status=%s,token=%s,expected_hash=%s,expected_identity=%s,output_path=%s, '
                "payload=CASE WHEN %s='pending' THEN payload ELSE NULL END,decided_at=now() WHERE id=%s RETURNING *",
                (result['status'],result.get('token',row['token']),result.get('expected_hash',row['expected_hash']),result.get('expected_identity',row['expected_identity']),result.get('output_path'),result['status'],identity))
            updated = await cursor.fetchone()
            if updated['status']=='committed':
                await connection.execute('INSERT INTO file_operations(workspace_id,source,operation,path) VALUES(%s,%s,%s,%s)', (workspace_id,row['source'],'confirmed-output',updated['output_path']))
        if stale:
            raise FileConflict('文件再次变化，旧确认失效；请查看新版本并重新确认')
        if updated['status']=='expired':
            raise FileConflict('确认已超时，未写入；请重新提交输出')
        if updated['status']=='committed' and row['run_id'] is not None and row['source'].startswith('container:'):
            from chagent.storage.repositories import RunRepository
            try:
                await RunRepository(self.database).audit_event(row['run_id'], 'file_collected', {
                    'status': 'committed', 'output_id': str(identity),
                    'container_path': row['source'].removeprefix('container:'),
                    'workspace_path': updated['output_path'], 'size': row['size'],
                    'sha256': row['content_hash'],
                })
            except Exception as error:
                logger.warning('Could not audit confirmed container output (%s)', type(error).__name__)
        return public(updated)
