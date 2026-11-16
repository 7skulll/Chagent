"""Bounded skill imports. Archives are inspected in memory, never extracted."""
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import stat
from uuid import uuid4
from zipfile import ZipFile, BadZipFile

import yaml
from psycopg.types.json import Jsonb

MAX_IMPORT = 2 * 1024 * 1024
MAX_TEXT = 64 * 1024


def parse_import(filename, data):
    if not data or len(data) > MAX_IMPORT:
        raise ValueError('技能文件必须为 1 字节到 2 MB')
    files = {}
    if filename.lower().endswith('.zip'):
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 100 or sum(i.file_size for i in entries) > MAX_IMPORT:
                    raise ValueError('ZIP 最多 100 项，解压后不得超过 2 MB')
                for info in entries:
                    path = PurePosixPath(info.filename)
                    if (path.is_absolute() or '..' in path.parts or '\\' in info.filename
                            or ':' in info.filename or stat.S_ISLNK(info.external_attr >> 16)):
                        raise ValueError('ZIP 包含不安全的路径或符号链接')
                    if info.is_dir():
                        continue
                    name = str(path)
                    if name in files:
                        raise ValueError('ZIP 包含重复路径')
                    files[name] = base64.b64encode(archive.read(info)).decode('ascii')
        except (BadZipFile, RuntimeError, NotImplementedError) as error:
            raise ValueError('ZIP 无法读取或已加密') from error
        roots = [name for name in files if PurePosixPath(name).name == 'SKILL.md']
        if len(roots) != 1:
            raise ValueError('ZIP 必须且只能包含一个 SKILL.md')
        root = PurePosixPath(roots[0]).parent
        if any(not PurePosixPath(name).is_relative_to(root) for name in files):
            raise ValueError('所有附属文件必须在 SKILL.md 所在目录内')
        files = {str(PurePosixPath(name).relative_to(root)): value for name, value in files.items()}
    elif filename.lower().endswith('.md'):
        files['SKILL.md'] = base64.b64encode(data).decode('ascii')
    else:
        raise ValueError('请选择 SKILL.md 或技能 ZIP 包')
    raw = base64.b64decode(files['SKILL.md'])
    if len(raw) > MAX_TEXT:
        raise ValueError('SKILL.md 不得超过 64 KB')
    try:
        content = raw.decode('utf-8-sig')
        parts = re.split(r'^---\s*$', content, maxsplit=2, flags=re.M)
        if len(parts) != 3 or parts[0].strip() or not parts[2].strip():
            raise ValueError('SKILL.md 需要 YAML 头部和非空正文')
        metadata = yaml.safe_load(parts[1])
        name, description = metadata['name'], metadata['description']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
            raise ValueError('name 须为 1–64 位小写字母、数字或连字符')
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1000:
            raise ValueError('description 须为 1–1000 个字符')
    except (UnicodeError, yaml.YAMLError, KeyError, TypeError, RecursionError) as error:
        raise ValueError('SKILL.md 必须为 UTF-8，且包含有效的 name 和 description') from error
    digest = sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return dict(name=name, description=description.strip(), content=content, files=files, sha256=digest)


class UserSkillStore:
    def __init__(self, database):
        self.database = database

    async def list(self, *, enabled=False):
        async with self.database.connection() as conn:
            cursor = await conn.execute('SELECT * FROM user_skills ' +
                ('WHERE enabled ' if enabled else '') + 'ORDER BY created_at, id')
            return await cursor.fetchall()

    async def import_skill(self, skill):
        async with self.database.transaction() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(1128813395)')
            cursor = await conn.execute('SELECT count(*) AS n FROM user_skills')
            if (await cursor.fetchone())['n'] >= 100:
                raise ValueError('每个用户最多保存 100 个技能，请先删除不需要的技能')
            cursor = await conn.execute('INSERT INTO user_skills '
                '(id,name,description,content,files,sha256) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',
                (uuid4(), skill['name'], skill['description'], skill['content'], Jsonb(skill['files']), skill['sha256']))
            return await cursor.fetchone()

    async def get(self, identity):
        async with self.database.connection() as conn:
            cursor = await conn.execute('SELECT * FROM user_skills WHERE id=%s', (identity,))
            row = await cursor.fetchone()
            if row is None:
                raise LookupError('用户技能不存在')
            return row

    async def change(self, identity, revision, *, enabled=None, delete=False):
        async with self.database.connection() as conn:
            if delete:
                cursor = await conn.execute('DELETE FROM user_skills WHERE id=%s AND revision=%s RETURNING id',
                                            (identity, revision))
            else:
                cursor = await conn.execute('UPDATE user_skills SET enabled=%s, revision=revision+1 '
                    'WHERE id=%s AND revision=%s RETURNING id', (enabled, identity, revision))
            if await cursor.fetchone() is None:
                from chagent.sessions.errors import StateConflictError
                raise StateConflictError('技能已变化或不存在，请刷新后重试')
