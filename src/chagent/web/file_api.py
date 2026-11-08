"""Workspace-ID-based file endpoints, independent of Agent execution mode."""
import asyncio
import heapq
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Query
from starlette.exceptions import HTTPException
from pydantic import Field, StrictStr

from chagent.boundaries import validate_workspace, within_root
from chagent.files import FileService, FileConflict
from chagent.storage.repositories import ConversationRepository
from chagent.web.schemas import DirectoryEntry, DirectoryListing, FileOperationPage, InputModel
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
        cursor = await connection.execute("SELECT root_path, host_path FROM workspaces WHERE id = %s AND binding_status = 'bound'", (workspace_id,))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, '工作区不存在')
    try:
        from chagent.storage.workspace_identity import host_path_for
        root = current.root(row['root_path'])
        if host_path_for(root) != row['host_path']:
            raise ValueError("Workspace binding does not match this host mount")
        fs = FileService(root)
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


@router.get('/workspaces/{workspace_id}/operations', response_model=FileOperationPage)
async def operations(workspace_id: UUID, current: Current, limit: int = Query(50, ge=1, le=200)):
    """Recent writes to this workspace, from the agent's tools and the file panel.

    ``service`` is the gate: rows exist whether or not the directory is currently
    openable, and a caller must not learn about a workspace it cannot open.
    """
    fs = await service(current, workspace_id)
    from chagent.outputs import OutputService
    return {'items': await OutputService(fs.audit_database).operations(workspace_id, limit)}


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


#: A directory listing is a picker, not a file manager. A hundred children are
#: still explorable; ten thousand are a page nobody reads.
BROWSE_LIMIT = 200


#: The one tree the page may walk. A native dialog can reach anything the OS
#: user can read; the page cannot, because it has no way to learn a path the
#: server never mentioned. ``CHAGENT_WORKSPACE_ROOT`` is that tree when the
#: deployment sets one — the container mounts host directories under
#: ``/workspaces`` — and the home directory otherwise.
def picker_root() -> Path:
    setting = os.environ.get('CHAGENT_WORKSPACE_ROOT')
    raw = Path(setting).expanduser() if setting and setting.strip() else Path.home()
    try:
        root = raw.resolve(strict=True)
    except OSError:
        raise ValueError('工作区根目录不存在或不可访问') from None
    if not root.is_dir():
        raise ValueError('工作区根目录必须是目录')
    return root


def confined(target: Path, root: Path) -> Path:
    """Resolve one browse target and refuse anything above the published root."""
    try:
        resolved = target.expanduser().resolve(strict=True)
    except OSError:
        raise ValueError('目录不存在或不可访问') from None
    if not within_root(resolved, root):
        raise ValueError('只能浏览工作区根目录之内的文件夹')
    if not resolved.is_dir():
        raise ValueError('该路径不是目录')
    return resolved


def native_picker_available() -> bool:
    """Only macOS has a system dialog wired here; elsewhere the page navigates."""
    return sys.platform == 'darwin'


def choose_directory() -> str:
    """Ask the OS for a folder; an empty string means the user cancelled.

    A cancel and a failure must stay distinct: the first changes nothing, the
    second has to tell the page why, because ``osascript`` also fails when the
    host denies automation or the dialog cannot reach the display.
    """
    script = ('try\n'
              'POSIX path of (choose folder with prompt "选择 Chagent 工作区")\n'
              'on error number -128\n'
              'return ""\n'
              'end try')
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script],
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(503, '目录选择器超时，请重试或手工输入路径') from None
    except OSError:
        raise HTTPException(503, '无法启动目录选择器，请手工输入路径') from None
    if result.returncode:
        reason = [line for line in (result.stderr or '').splitlines() if line.strip()]
        detail = f'：{reason[-1].strip()[:200]}' if reason else ''
        raise HTTPException(503, f'目录选择器不可用{detail}')
    # ``POSIX path of`` prints one line and no trailing slash rule; only the
    # newline is ours to remove. Stripping whitespace would rename a folder
    # whose name legitimately ends in a space.
    return result.stdout.rstrip('\n')


@router.post('/workspace-picker')
async def picker(current: Current):
    """The system dialog when the host has one, otherwise a signal to navigate."""
    from chagent.web.deps import open_workspace
    from chagent.web.schemas import WorkspaceCreate
    if not native_picker_available():
        return dict(cancelled=True, native_picker=False)
    path = await asyncio.to_thread(choose_directory)
    if not path:
        return dict(cancelled=True, native_picker=True)
    opened = await open_workspace(WorkspaceCreate(path=path), current)
    return dict(cancelled=False, native_picker=True, workspace=opened)


@router.get('/workspace-browse', response_model=DirectoryListing)
async def browse(current: Current, path: str | None = Query(default=None, max_length=4096),
                 after: str | None = Query(default=None, max_length=255)):
    """List child directories of one target inside the published root.

    Discovery only: nothing is opened or registered here, so walking a tree has
    no side effect. The rows carry the same availability verdict as
    ``GET /api/workspaces``, computed now rather than on first use.
    """
    root = picker_root()
    target = confined(Path(path), root) if path else root
    known = {row['path'] for row in
             await ConversationRepository(current.runner.conversations.database).workspaces()}
    entries = []
    def order(name: str) -> tuple[str, str]:
        return name.casefold(), name

    def directories():
        # Scan the directory, but keep only one page in memory. A full sort can
        # allocate an unbounded list before BROWSE_LIMIT is ever checked.
        with os.scandir(target) as children:
            for child in children:
                try:
                    if child.is_dir() and (after is None or order(child.name) > order(after)):
                        yield child.name
                except OSError:
                    continue
    try:
        names = heapq.nsmallest(BROWSE_LIMIT + 1, directories(), key=order)
    except OSError:
        raise ValueError('目录不可读，请选择其他文件夹') from None
    more = len(names) > BROWSE_LIMIT
    for name in names[:BROWSE_LIMIT]:
        entry = target / name
        try:
            confined(entry, root)
            opened = validate_workspace(entry)
        except (ValueError, OSError, RuntimeError):
            entries.append(DirectoryEntry(name=entry.name, path=entry, available=False,
                                          reason='目录不可访问或不符合工作区准入要求',
                                          opened=False))
        else:
            entries.append(DirectoryEntry(name=entry.name, path=opened, available=True,
                                          opened=str(opened) in known))
    return DirectoryListing(path=target, root=root,
                            parent=None if target == root else target.parent,
                            entries=entries,
                            next_after=names[BROWSE_LIMIT - 1] if more else None)


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
