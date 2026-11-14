"""Account HTTP boundary shared by the local app and admin evaluation portal."""
from importlib.resources import files
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from chagent.users.store import AccountStore, AuthenticationError, COOKIE, SESSION_SECONDS

PUBLIC = {'/api/accounts/status', '/api/accounts/register', '/api/accounts/login', '/api/accounts/setup'}


class AccountBoundary:
    def __init__(self, app, store: AccountStore, admin_only=False):
        self.app, self.store, self.admin_only = app, store, admin_only

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        user = self.store.session(headers.get(b'cookie', b'').decode('latin-1'))
        scope['account'] = user
        path = scope['path']
        if (path.startswith('/api/') or path == '/openapi.json') and path not in PUBLIC:
            if user is None:
                return await JSONResponse({'error': '请先登录'}, 401)(scope, receive, send)
            if (self.admin_only and not path.startswith('/api/accounts')) or path.startswith('/api/accounts/users'):
                if user['role'] != 'admin':
                    return await JSONResponse({'error': '仅管理员可以使用测试平台或管理用户'}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


def account_page():
    return HTMLResponse(files('chagent.web').joinpath('static', 'accounts.html').read_text(),
                        headers={'Cache-Control': 'no-store'})


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class Setup(Credentials):
    setup_code: str = Field(min_length=32, max_length=128)


class UserEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['admin', 'user'] | None = None
    active: bool | None = None


def mount_accounts(app, store, changed=None, *, admin_only=False):
    app.state.accounts = store
    router = APIRouter(prefix='/api/accounts', tags=['本机账户'])

    @router.get('/status')
    def status(request: Request):
        return {'setup_required': store.setup_required(), 'user': request.scope.get('account'),
                'setup_code_path': str(store.setup_path) if store.setup_required() else None,
                'storage': 'local'}

    @router.post('/register', status_code=201)
    def register(value: Credentials):
        return {'user': store.register(value.username, value.password)}

    @router.post('/setup', status_code=201)
    def setup(value: Setup):
        try:
            return {'user': store.register(value.username, value.password, setup_code=value.setup_code)}
        except AuthenticationError as error:
            raise HTTPException(403, str(error)) from None

    @router.post('/login')
    def login(value: Credentials):
        try:
            token, user = store.login(value.username, value.password)
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        response = JSONResponse({'user': user})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            samesite='strict', path='/')
        return response

    @router.post('/logout')
    def logout(request: Request):
        store.logout(request.headers.get('cookie', ''))
        response = JSONResponse({'logged_out': True})
        response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict')
        return response

    @router.get('/me')
    def me(request: Request):
        user = request.scope['account']
        return {'user': user, 'workspace': str(store.workspace(user['id'])), 'storage': 'local'}

    @router.get('/users')
    def users():
        return {'users': store.users()}

    @router.patch('/users/{uid}')
    async def edit(uid: str, value: UserEdit, request: Request):
        try:
            user = store.update(request.scope['account']['id'], uid,
                                role=value.role, active=value.active)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from None
        except LookupError:
            raise HTTPException(404, '用户不存在') from None
        # Terminate this user's active runtime; evaluation workers check roles
        # again between paid slots, even if this process is not the eval server.
        if changed is not None:
            await changed(user)
        return {'user': user}

    @app.get('/account', include_in_schema=False)
    def portal():
        return account_page()

    @app.get('/account-assets/{name}', include_in_schema=False)
    def account_asset(name: str):
        types = {'accounts.css': 'text/css', 'accounts.js': 'text/javascript'}
        if name not in types:
            raise HTTPException(404)
        return Response(files('chagent.web').joinpath('static', name).read_bytes(),
                        media_type=types[name], headers={'Cache-Control': 'no-store'})

    app.include_router(router)
    app.add_middleware(AccountBoundary, store=store, admin_only=admin_only)
