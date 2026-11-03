"""Workspace file service. Disk is authoritative; all paths are directory-relative.

Coordination covers this process's file tools and HTTP operations, not Shell or
external editors. Trash metadata lives with its payload and survives restarts.
"""
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timezone
from uuid import uuid4, UUID

from chagent.tools.workspace import WorkspaceFS, coordinated
from chagent.boundaries import validate_file_identity


class FileConflict(ValueError):
    pass


def rename_exclusive(source_fd, source, target_fd, target):
    """Atomic no-replace rename, including directories; never use check + replace."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        result = libc.renameatx_np(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 4)
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1)
    else:
        raise ValueError('此平台暂不支持安全重命名')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


class FileService(WorkspaceFS):
    def __init__(self, root, *, allow_absolute=False, require_versions=False, versions=None, **kwargs):
        super().__init__(root, **kwargs)
        self.allow_absolute = allow_absolute
        self.require_versions = require_versions
        self.versions = versions or {}

    @coordinated
    def write(self, path, data, *, overwrite=False, expected_hash=None, **kwargs):
        if overwrite and self.require_versions:
            expected = self.versions.get('/'.join(self.parts(path)))
            if expected is None:
                raise FileConflict('Read the latest file first and provide its SHA256 before editing')
            if expected_hash is not None and expected_hash != expected:
                raise FileConflict('File revision does not match the supplied read version')
            expected_hash = expected
        return super().write(path, data, overwrite=overwrite, expected_hash=expected_hash, **kwargs)

    @coordinated
    def remove(self, path, *, expected_hash=None):
        if self.require_versions:
            expected = self.versions.get('/'.join(self.parts(path)))
            if expected is None or (expected_hash is not None and expected != expected_hash):
                raise FileConflict('Provide the read version before deleting a file')
            expected_hash = expected
        # Model patch deletions are recoverable through the same trash service.
        with self.parent(path) as (parent, name):
            data, _ = self._read_at(parent, name)
            if expected_hash is not None and sha256(data).hexdigest() != expected_hash:
                raise FileConflict('File changed since it was read')
            info = self.object_info(parent, name)
        self.recycle(path, self.token(info))

    def parts(self, path):
        if not isinstance(path, str) or '\\' in path or '\x00' in path or (Path(path).is_absolute() and not self.allow_absolute):
            raise ValueError('必须使用工作区相对路径')
        parts = super().parts(path)
        if any(p == '.chagent' or any(ord(c) < 32 for c in p) for p in parts):
            raise PermissionError('内部目录不可操作')
        return parts

    def token(self, info):
        return f'{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}'

    def object_info(self, parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            validate_file_identity(info)
        elif not stat.S_ISDIR(info.st_mode):
            raise PermissionError('仅支持普通文件和目录，不跟随符号链接')
        return info

    @coordinated
    def listing(self, path='', offset=0, limit=100, hidden=False):
        with self.directory(self.parts(path)) as descriptor:
            with os.scandir(descriptor) as entries:
                entries = sorted(entries, key=lambda e: e.name)
            entries = [e for e in entries if e.name != '.chagent' and (hidden or not e.name.startswith('.'))]
            items = []
            for entry in entries[offset:offset+limit]:
                info = entry.stat(follow_symlinks=False)
                kind = 'link' if stat.S_ISLNK(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'unsupported'
                items.append(dict(name=entry.name, path='/'.join((*self.parts(path), entry.name)), type=kind,
                                  size=info.st_size, identity=self.token(info)))
        return dict(items=items, next_offset=offset+limit if len(entries)>offset+limit else None)

    @coordinated
    def text(self, path):
        with self.parent(path) as (parent, name):
            data, _ = self._read_at(parent, name)
            info = self.object_info(parent, name)
        try:
            content = data.decode('utf-8')
            if '\x00' in content:
                raise UnicodeError()
        except UnicodeError:
            raise ValueError('该文件不是可编辑的 UTF-8 文本') from None
        rest = content.replace('\r\n', '')
        kinds = [name for name, present in [('crlf', '\r\n' in content), ('lf', '\n' in rest), ('cr', '\r' in rest)] if present]
        newline = 'mixed' if len(kinds)>1 else kinds[0] if kinds else 'lf'
        return dict(path=path, content=content, version=sha256(data).hexdigest(), identity=self.token(info), encoding='utf-8', newline=newline)

    @coordinated
    def save_text(self, path, content, version, identity):
        current = self.text(path)
        if current['version'] != version or current['identity'] != identity:
            raise FileConflict('文件已变化，请读取磁盘版本；草稿未保存')
        self.write(path, content.encode('utf-8'), overwrite=True, expected_hash=version)
        return self.text(path)

    @coordinated
    def create(self, path, kind, content=''):
        with self.parent(path) as (parent, name):
            if kind == 'directory':
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            else:
                self.write(path, content.encode('utf-8'))
        return dict(path=path)

    @coordinated
    def rename(self, path, target, identity):
        with self.parent(path) as (source_fd, name), self.parent(target) as (target_fd, dest):
            info = self.object_info(source_fd, name)
            if self.token(info) != identity:
                raise FileConflict('目标身份已变化，请刷新')
            rename_exclusive(source_fd, name, target_fd, dest)
            os.fsync(source_fd)
            os.fsync(target_fd)
        return dict(path=target)

    def trash_directory(self):
        # Internal paths intentionally bypass public parts(), but use no-follow FDs.
        for parts, name in [((), '.chagent'), (('.chagent',), 'trash')]:
            with self.directory(parts) as descriptor:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
        return ('.chagent', 'trash')

    @coordinated
    def recycle(self, path, identity):
        with self.parent(path) as (parent, name):
            if self.token(self.object_info(parent, name)) != identity:
                raise FileConflict('目标身份已变化，请刷新')
            trash = self.trash_directory()
            record = str(uuid4())
            with self.directory(trash) as descriptor:
                os.mkdir(record, 0o700, dir_fd=descriptor)
            with self.directory((*trash, record)) as descriptor:
                metadata = dict(id=record, path=path, recycled_at=datetime.now(timezone.utc).isoformat())
                fd = os.open('metadata.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(metadata, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                rename_exclusive(parent, name, descriptor, 'payload')
                os.fsync(parent)
                os.fsync(descriptor)
        return metadata

    @coordinated
    def trash(self):
        try:
            with self.directory(('.chagent', 'trash')) as descriptor:
                names = os.listdir(descriptor)
        except FileNotFoundError:
            return dict(items=[])
        items = []
        for name in names:
            try:
                UUID(name)
                with self.directory(('.chagent', 'trash', name)) as descriptor:
                    self.object_info(descriptor, 'payload')
                    data, _ = self._read_at(descriptor, 'metadata.json')
                    item = json.loads(data)
                    self.parts(item['path'])
                    items.append(item)
            except (ValueError, OSError, KeyError):
                continue
        return dict(items=items)

    @coordinated
    def restore(self, record, target=None):
        record = str(UUID(record))
        with self.directory(('.chagent', 'trash', record)) as descriptor:
            data, _ = self._read_at(descriptor, 'metadata.json')
            path = target or json.loads(data)['path']
            with self.parent(path) as (parent, name):
                self.object_info(descriptor, 'payload')
                rename_exclusive(descriptor, 'payload', parent, name)
                os.fsync(parent)
                os.fsync(descriptor)
        return dict(path=path)
