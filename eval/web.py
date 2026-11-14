"""Independent loopback-only evaluation UI: python -m eval.web --port 8766."""
import argparse
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
import json
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eval.platform import Busy, Platform


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    pricing_confirmed: Literal[True]
    cases: Annotated[list[str], Field(min_length=1, max_length=16)]
    provider: Annotated[str, Field(pattern='^(deepseek|openai|anthropic|qwen|custom)$')] = 'deepseek'
    baseline: Annotated[str, Field(min_length=1, max_length=128)] = 'deepseek-flash'
    candidate: Annotated[str, Field(min_length=1, max_length=128)] = 'deepseek-v4-pro'
    question: Annotated[str, Field(min_length=1, max_length=1000)] = '固定用例和预算下的 Agent 行为对照'
    repeats: Annotated[int, Field(ge=3, le=25)] = 3
    max_turns: Annotated[int, Field(ge=1, le=30)] = 10
    max_tool_calls: Annotated[int, Field(ge=0, le=100)] = 24
    wall_seconds: Annotated[int, Field(ge=1, le=600)] = 240
    cost_cap: Annotated[float, Field(gt=0, le=100)] = 1
    context_window: Annotated[int, Field(ge=1024, le=131072)] = 32768
    max_tokens: Annotated[int, Field(ge=128, le=16384)] = 4096
    threshold: Annotated[float, Field(ge=0, le=1)] = .3
    input_rate: Annotated[float, Field(ge=0, le=1000)] = 1.32
    output_rate: Annotated[float, Field(ge=0, le=1000)] = 3.96
    price_source: Annotated[str, Field(min_length=1, max_length=1000)] = (
        'https://api-docs.deepseek.com/quick_start/pricing/ checked 2026-11-14; '
        'Pro peak ceilings for both Flash/Pro, USD/M. Verify for other models.')


class LocalBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        expected = scope['scheme'].encode() + b'://' + headers.get(b'host', b'')
        if (headers.get(b'origin', expected) != expected
                or headers.get(b'sec-fetch-site') == b'cross-site'):
            return await JSONResponse({'error': 'Cross-origin access disabled'}, 403)(scope, receive, send)
        if scope['method'] in {'POST', 'PATCH', 'PUT', 'DELETE'}:
            if headers.get(b'content-type', b'').split(b';')[0] != b'application/json':
                return await JSONResponse({'error': 'Use application/json'}, 415)(scope, receive, send)
            content = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                content.extend(message.get('body', b''))
                if len(content) > 32768:
                    return await JSONResponse({'error': 'Request too large'}, 413)(scope, receive, send)
                if not message.get('more_body'):
                    break
            original = receive
            delivered = False
            async def buffered():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(content), 'more_body': False}
                return await original()
            receive = buffered
        await self.app(scope, receive, send)


def create_app(platform=None, accounts=None):
    from chagent.users.store import AccountStore
    from chagent.web.accounts import mount_accounts, account_page
    from eval.cli import _evaluation_env
    store = accounts or AccountStore(_evaluation_env().get("CHAGENT_ACCOUNT_DIR"))
    platforms = {}
    def current_platform(request):
        user = request.scope['account']
        root = (platform.root / 'users' / user['id'] if platform is not None else
                store.data_root(user['id']) / 'evaluations')
        if user['id'] not in platforms:
            platforms.setdefault(user['id'], Platform(root, platform.cases if platform is not None else None,
                        account_id=user['id'], account_root=store.root))
        return platforms[user['id']]
    app = FastAPI(title='Chagent 自主测试平台', docs_url=None, redoc_url=None)
    app.state.platform = platform
    app.state.platforms = platforms
    mount_accounts(app, store, admin_only=True)

    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        return JSONResponse({'error': '输入不合法，请检查范围与必填项'}, 400)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'error': str(error)}, 409 if isinstance(error, Busy) else 400)

    @app.exception_handler(FileNotFoundError)
    async def missing(request, error):
        return JSONResponse({'error': '计划不存在'}, 404)

    @app.get('/')
    def page(request: Request):
        user = request.scope.get('account')
        if user is None or user['role'] != 'admin':
            return account_page()
        return FileResponse(Path(__file__).with_name('platform_ui.html'), media_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/api/catalog')
    def catalog(request: Request):
        return {'cases': current_platform(request).catalog()}

    @app.get('/api/jobs')
    def jobs(request: Request):
        return {'jobs': current_platform(request).jobs()}

    @app.post('/api/plans', status_code=201)
    def plan(value: PlanRequest, request: Request):
        return current_platform(request).plan(value.model_dump())

    @app.get('/api/jobs/{identifier}')
    def detail(identifier: str, request: Request):
        return current_platform(request).detail(identifier)

    @app.post('/api/jobs/{identifier}/start')
    def start(identifier: str, request: Request):
        return current_platform(request).start(identifier)

    @app.post('/api/jobs/{identifier}/stop')
    def stop(identifier: str, request: Request):
        return current_platform(request).stop(identifier)

    @app.get('/api/jobs/{identifier}/report')
    def download(identifier: str, request: Request):
        value = current_platform(request).detail(identifier)
        return Response(json.dumps(value, ensure_ascii=False, indent=2),
                        media_type='application/json', headers={
                            'Content-Disposition': f'attachment; filename="{identifier}.json"',
                            'Cache-Control': 'no-store'})

    @app.get('/health')
    def health():
        return {'status': 'ok'}
    app.add_middleware(LocalBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit('port must be between 1024 and 65535')
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
