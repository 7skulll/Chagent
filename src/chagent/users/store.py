"""Local identity store; plaintext passwords and session tokens are never persisted."""
from contextlib import contextmanager
from hashlib import scrypt, sha256
import hmac
from http.cookies import SimpleCookie, CookieError
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from time import time
from uuid import uuid4

COOKIE = 'chagent_user_session'
SESSION_SECONDS = 7 * 24 * 3600


class AuthenticationError(ValueError):
    pass


class AccountStore:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('CHAGENT_ACCOUNT_DIR') or
            Path.home() / 'Library/Application Support/Chagent/accounts').expanduser()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('Account directory must not be a symlink')
        info = self.root.stat()
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('Account directory must be owner-only (0700)')
        self.root = self.root.resolve()
        self.path = self.root / 'accounts.sqlite3'
        self.setup_path = self.root / 'setup-code'
        for path, content in ((self.path, None), (self.setup_path, secrets.token_urlsafe(32))):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w') as f:
                    if content:
                        f.write(content)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and
                    (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise ValueError('Account files must be owner-only regular files')
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, salt TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    username TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL);
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def username(value):
        normalized = value.strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,31}', normalized):
            raise ValueError('用户名须为 3–32 位字母、数字、下划线、点或短横线')
        return normalized

    @staticmethod
    def password_hash(password, salt):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('密码须为 12–256 个字符')
        return scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'username', 'role', 'active', 'created_at')}

    def setup_required(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone() is None

    def register(self, username, password, *, setup_code=None):
        name = self.username(username)
        salt = secrets.token_hex(16)
        hashed = self.password_hash(password, salt)
        with self.connection(write=True) as db:
            role = 'user'
            if setup_code is not None:
                if not hmac.compare_digest(setup_code.encode('utf-8'), self.setup_path.read_text().strip().encode('utf-8')):
                    raise AuthenticationError('首次管理员配置码无效')
                if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                    raise ValueError('首次管理员已配置，请使用管理员账户管理用户')
                role = 'admin'
            uid = uuid4().hex
            try:
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,1,?)',
                           (uid, name, hashed, salt, role, time()))
            except sqlite3.IntegrityError:
                raise ValueError('用户名已被使用') from None
            row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            return self.public(row)

    def login(self, username, password):
        name = self.username(username)
        # Always do one KDF, including unknown usernames.
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
            attempt = db.execute('SELECT * FROM attempts WHERE username=?', (name,)).fetchone()
        if attempt and attempt['locked_until'] > time():
            raise AuthenticationError('登录尝试过多，请稍后再试')
        try:
            hashed = self.password_hash(password, row['salt'] if row else '00' * 16)
        except ValueError:
            hashed = self.password_hash('invalid-password-marker', '00' * 16)
        valid = row and row['active'] and hmac.compare_digest(hashed, row['password_hash'])
        with self.connection(write=True) as db:
            # Recheck under the write lock: a disabled account cannot race a login.
            current = db.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone() if row else None
            if not valid or current is None or not current['active'] or current['password_hash'] != row['password_hash']:
                old = db.execute('SELECT * FROM attempts WHERE username=?', (name,)).fetchone()
                failures = (old['failures'] if old else 0) + 1
                db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)',
                           (name, failures, time() + 60 if failures >= 5 else 0))
                failed = True
            else:
                failed = False
                db.execute('DELETE FROM attempts WHERE username=?', (name,))
                db.execute('DELETE FROM sessions WHERE expires<=?', (time(),))
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO sessions VALUES (?,?,?)',
                           (sha256(token.encode()).hexdigest(), current['id'], time() + SESSION_SECONDS))
        if failed:
            raise AuthenticationError('用户名或密码错误')
        return token, self.public(current)

    def session(self, raw_cookie):
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
            token = cookie[COOKIE].value
        except (KeyError, ValueError, CookieError):
            return None
        with self.connection() as db:
            row = db.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id '
                'WHERE s.token_hash=? AND s.expires>? AND u.active=1',
                (sha256(token.encode()).hexdigest(), time())).fetchone()
            return self.public(row) if row else None

    def logout(self, raw_cookie):
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
            token = cookie[COOKIE].value
        except (KeyError, ValueError, CookieError):
            return
        with self.connection(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (sha256(token.encode()).hexdigest(),))

    def user(self, uid):
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
            return self.public(row) if row else None

    def users(self):
        with self.connection() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM users ORDER BY created_at')]

    def update(self, actor_id, uid, *, role=None, active=None):
        with self.connection(write=True) as db:
            actor = db.execute("SELECT * FROM users WHERE id=? AND active=1 AND role='admin'", (actor_id,)).fetchone()
            if actor is None:
                raise PermissionError('仅管理员可管理用户')
            row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if row is None:
                raise LookupError('用户不存在')
            role = row['role'] if role is None else role
            active = bool(row['active']) if active is None else active
            if role not in ('user', 'admin') or type(active) is not bool:
                raise ValueError('无效用户权限')
            if row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
                count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError('不能停用或降级最后一位管理员')
            db.execute('UPDATE users SET role=?,active=? WHERE id=?', (role, int(active), uid))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            return self.public(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())

    def data_root(self, uid):
        if not re.fullmatch(r'[0-9a-f]{32}', uid):
            raise ValueError('invalid account identity')
        path = self.root / 'data' / uid
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.resolve() != path:
            raise PermissionError('用户资源目录不能是链接')
        return path

    def workspace(self, uid):
        path = self.data_root(uid) / 'workspace'
        path.mkdir(exist_ok=True, mode=0o700)
        self.guard(uid, path)
        return path

    def guard(self, uid, path):
        root = self.data_root(uid) / 'workspace'
        if not Path(path).resolve().is_relative_to(root):
            raise PermissionError('只能访问当前用户的本机工作区及其子目录')
