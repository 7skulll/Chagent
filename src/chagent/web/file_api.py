"""Workspace-ID-based file endpoints, independent of Agent execution mode."""
import asyncio
import subprocess
import sys
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Query
from starlette.exceptions import HTTPException
from pydantic import Field, StrictStr

from chagent.files import FileService, FileConflict
from chagent.web.schemas import InputModel
from chagent.web.deps import Current

router = APIRouter(prefix='/api', tags=['工作区文件'])


class FileInput(InputModel):
    path: StrictStr = Field(min_length=1, max_length=4096)


class SaveInput(FileInput):
    content: StrictStr = Field(max_length=1_048_576)
    version: StrictStr = Field(pattern='^[0-9a-f]{64}$')
    identity: StrictStr = Field(max_length=200)


class CreateInput(FileInput):
    type: Literal['file', 'directory'] = 'file'
    content: StrictStr = Field(default='', max_length=1_048_576)


class RenameInput(FileInput):
    target: StrictStr = Field(min_length=1, max_length=4096)
    identity: StrictStr = Field(max_length=200)


class DeleteInput(FileInput):
    identity: StrictStr = Field(max_length=200)


class RestoreInput(InputModel):
    target: StrictStr | None = Field(default=None, min_length=1, max_length=4096)


async def service(current, workspace_id):
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute('SELECT root_path FROM workspaces WHERE id = %s', (workspace_id,))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, '工作区不存在')
    try:
        fs = FileService(current.root(row['root_path']))
        fs.audit_database = current.runner.conversations.database
        fs.audit_workspace_id = workspace_id
        return fs
    except (ValueError, OSError):
        raise HTTPException(403, '工作区未打开或已不可用') from None


async def perform(fs, method, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(getattr(fs, method), *args, **kwargs))
    try:
        result = await asyncio.shield(task)
        if method in {'save_text','create','rename','recycle','restore'}:
            from chagent.outputs import OutputService
            await OutputService(fs.audit_database).record(fs.audit_workspace_id,'manual',method,result.get('path',args[0]))
        return result
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
    except (FileConflict, FileExistsError):
        raise HTTPException(409, '文件已变化或目标已存在，请刷新；未覆盖目标') from None
    except FileNotFoundError:
        raise HTTPException(404, '文件或父目录不存在') from None
    except PermissionError:
        raise HTTPException(403, '无权操作该文件或文件身份不受支持') from None
    except OSError:
        raise HTTPException(400, '文件操作失败，请检查目录权限、磁盘空间和文件类型') from None


@router.get('/workspaces/{workspace_id}/entries')
async def entries(workspace_id: UUID, current: Current, path: str = '', offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), hidden: bool = False):
    return await perform(await service(current, workspace_id), 'listing', path, offset, limit, hidden)


@router.get('/workspaces/{workspace_id}/text')
async def text(workspace_id: UUID, current: Current, path: str):
    return await perform(await service(current, workspace_id), 'text', path)


@router.put('/workspaces/{workspace_id}/text')
async def save(workspace_id: UUID, value: SaveInput, current: Current):
    return await perform(await service(current, workspace_id), 'save_text', value.path, value.content, value.version, value.identity)


@router.post('/workspaces/{workspace_id}/entries', status_code=201)
async def create(workspace_id: UUID, value: CreateInput, current: Current):
    return await perform(await service(current, workspace_id), 'create', value.path, value.type, value.content)


@router.post('/workspaces/{workspace_id}/rename')
async def rename(workspace_id: UUID, value: RenameInput, current: Current):
    return await perform(await service(current, workspace_id), 'rename', value.path, value.target, value.identity)


@router.post('/workspaces/{workspace_id}/trash', status_code=201)
async def recycle(workspace_id: UUID, value: DeleteInput, current: Current):
    return await perform(await service(current, workspace_id), 'recycle', value.path, value.identity)


@router.get('/workspaces/{workspace_id}/trash')
async def trash(workspace_id: UUID, current: Current):
    return await perform(await service(current, workspace_id), 'trash')


@router.post('/workspaces/{workspace_id}/trash/{record}/restore')
async def restore(workspace_id: UUID, record: UUID, value: RestoreInput, current: Current):
    return await perform(await service(current, workspace_id), 'restore', str(record), value.target)


def choose_directory():
    if sys.platform != 'darwin':
        raise HTTPException(503, '系统目录选择器不可用，请手工输入路径')
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', 'try\nPOSIX path of (choose folder with prompt "选择 Chagent 工作区")\non error number -128\nreturn ""\nend try'], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(503, '目录选择器不可用或超时，请手工输入路径') from None
    if result.returncode:
        raise HTTPException(503, '目录选择器不可用，请手工输入路径')
    return result.stdout.strip()


@router.post('/workspace-picker')
async def picker(current: Current):
    from chagent.web.deps import open_workspace
    from chagent.web.schemas import WorkspaceCreate
    path = await asyncio.to_thread(choose_directory)
    if not path:
        return dict(cancelled=True)
    opened = await open_workspace(WorkspaceCreate(path=path), current)
    return dict(cancelled=False, workspace=opened)


class OutputInput(FileInput):
    content: StrictStr = Field(max_length=1_048_576)
    request_id: UUID


class OutputDecision(InputModel):
    token: UUID
    choice: Literal['overwrite', 'keep-both', 'cancel']


@router.post('/workspaces/{workspace_id}/outputs')
async def output(workspace_id: UUID, value: OutputInput, current: Current):
    from chagent.outputs import OutputService
    fs = await service(current, workspace_id)
    try:
        return await OutputService(current.runner.conversations.database).submit(workspace_id,fs.root,value.path,
            value.content.encode('utf-8'),source='save-as',request_id=value.request_id)
    except (FileConflict, FileExistsError):
        raise HTTPException(409,'输出请求冲突，请刷新') from None
    except OSError:
        raise HTTPException(400,'无法写入目标，请检查目录与权限') from None


@router.get('/workspaces/{workspace_id}/outputs')
async def outputs(workspace_id: UUID, current: Current):
    from chagent.outputs import OutputService
    await service(current, workspace_id)
    return {'items':await OutputService(current.runner.conversations.database).pending(workspace_id)}


@router.get('/workspaces/{workspace_id}/outputs/{output_id}')
async def output_status(workspace_id: UUID, output_id: UUID, current: Current):
    from chagent.outputs import public
    await service(current, workspace_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute('SELECT * FROM file_outputs WHERE id=%s AND workspace_id=%s', (output_id,workspace_id))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404,'输出记录不存在')
    return public(row)


@router.post('/workspaces/{workspace_id}/outputs/{output_id}/decision')
async def output_decision(workspace_id: UUID, output_id: UUID, value: OutputDecision, current: Current):
    from chagent.outputs import OutputService
    fs = await service(current, workspace_id)
    try:
        return await OutputService(current.runner.conversations.database).decide(workspace_id,fs.root,output_id,value.token,value.choice)
    except FileConflict as error:
        raise HTTPException(409,str(error)) from None
    except FileNotFoundError:
        raise HTTPException(404,'目标或输出记录已不存在') from None
    except OSError:
        raise HTTPException(400,'输出写入失败，请检查目录与权限') from None


@router.get('/workspaces/{workspace_id}/content')
async def download(workspace_id: UUID, current: Current, path: str):
    from fastapi.responses import Response
    from urllib.parse import quote
    from pathlib import PurePosixPath
    fs = await service(current,workspace_id)
    data = await perform(fs,'read',path,limit=20*1024*1024)
    return Response(data,media_type='application/octet-stream',headers={
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(PurePosixPath(path).name,safe=''),
        'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})


@router.get('/workspaces/{workspace_id}/outputs/{output_id}/proposal')
async def proposed_download(workspace_id: UUID, output_id: UUID, current: Current):
    from fastapi.responses import Response
    from pathlib import PurePosixPath
    from urllib.parse import quote
    await service(current,workspace_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute("SELECT path,payload FROM file_outputs WHERE workspace_id=%s AND id=%s AND status='pending' AND expires_at>now()", (workspace_id,output_id))
        row = await cursor.fetchone()
    if row is None or row['payload'] is None:
        raise HTTPException(404,'待写入内容已不存在')
    return Response(bytes(row['payload']),media_type='application/octet-stream',headers={
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(PurePosixPath(row['path']).name,safe=''),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


async def preview_service(current, workspace_id):
    from chagent.previews import PreviewFS
    fs=await service(current,workspace_id)
    return PreviewFS(fs.root,expected_identity=fs.identity)


async def preview_call(fs,method,*args):
    from chagent.previews import PreviewUnavailable
    try:
        return await perform(fs,method,*args)
    except PreviewUnavailable as error:
        raise HTTPException(503,str(error)) from None


@router.get('/workspaces/{workspace_id}/file-state')
async def file_state(workspace_id: UUID, current: Current, path: str):
    return await preview_call(await preview_service(current,workspace_id),'state',path)


@router.get('/workspaces/{workspace_id}/preview')
async def preview(workspace_id: UUID, current: Current, path: str):
    return await preview_call(await preview_service(current,workspace_id),'information',path)


@router.get('/workspaces/{workspace_id}/preview/page')
async def preview_page(workspace_id: UUID, current: Current, path: str,
                       version: str=Query(pattern='^[0-9a-f]{64}$'), page: int=Query(1,ge=1,le=2000)):
    from fastapi.responses import Response
    data=await preview_call(await preview_service(current,workspace_id),'raster',path,version,page)
    return Response(data,media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
