"""F1–F4 文件接口的对抗性补充探针。

用途：补 `probe_workspace_f14.py` 之外的边界分支，验证「没有 5xx」以及契约明确处的
拒绝类别。产品决策类分支（没有唯一正确答案）只记为 informational，不做断言。

用法：
    PYTHONPATH=src .venv/bin/python scripts/probe_workspace_f14_extra.py \
      --base http://127.0.0.1:8912 --workspace /临时/A --other /临时/B \
      --output /临时/extra.json

需要已启动的服务（可用 `--demo`）和两个可写临时目录；脚本会在目录内创建文件、
软链接和硬链接，只应指向一次性目录。
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import httpx


class Probe:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.client = httpx.Client(base_url=self.base, timeout=60, trust_env=False)
        self.checks = []

    def check(self, name, function, expect=None):
        try:
            status, detail = function()
        except Exception as error:
            self.checks.append(dict(name=name, ok=False, status=None,
                                    detail=f'{type(error).__name__}: {error}'))
            return
        if status is not None and status >= 500:
            ok = False
        elif expect is None:
            ok = True
        else:
            ok = status in expect
        self.checks.append(dict(name=name, ok=ok, status=status, detail=detail,
                                expect=None if expect is None else sorted(expect)))

    def require(self, condition, message):
        if not condition:
            raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8912')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--other', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()

    root = args.workspace.resolve()
    other = args.other.resolve()
    root.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    (root / 'README.md').write_text('# extra\n', encoding='utf-8')
    (other / 'README.md').write_text('# other\n', encoding='utf-8')
    os.symlink('/etc/passwd', root / 'link-out')
    os.symlink('/tmp', root / 'link-dir')
    try:
        os.link('/etc/hosts', root / 'hard-out')
    except OSError:
        (root / 'hard-out').write_text('unavailable')
    (root / 'dir-a').mkdir(exist_ok=True)
    (root / 'dir-a' / 'inner').mkdir(exist_ok=True)

    probe = Probe(args.base)
    client = probe.client

    def open_(path):
        response = client.post('/api/workspaces', json={'path': str(path)})
        probe.require(response.status_code == 201, f'open: {response.status_code} {response.text}')
        return response.json()['id']

    ws = open_(root)
    ws2 = open_(other)
    base = f'/api/workspaces/{ws}'
    base2 = f'/api/workspaces/{ws2}'

    def get(url, **params):
        return client.get(base + url, params=params)

    def post(url, **body):
        return client.post(base + url, json=body)

    def put(url, **body):
        return client.put(base + url, json=body)

    def entry_identity(path):
        parent, _, _ = path.rpartition('/')
        items = client.get(base + '/entries',
                           params={'path': parent, 'limit': 500, 'hidden': 'true'}).json()['items']
        return next(item for item in items if item['path'] == path)['identity']

    # --- F2 边界 --------------------------------------------------------
    probe.check('F2 空文本路径被拒绝而不是报错',
                lambda: (get('/text', path='').status_code, 'empty path'), {400, 403, 404, 422})
    probe.check('F2 内部回收站路径不能通过 /text 读取',
                lambda: (get('/text', path='.chagent/trash/x').status_code, 'internal'), {400, 403})
    probe.check('F2 .chagent 不能作为新建目标',
                lambda: (post('/entries', path='.chagent').status_code, 'reserved dir'),
                {400, 403, 409, 422})
    # 1 MiB 超限走请求校验，本机把 RequestValidationError 统一映射成 400，不带请求体回显。
    probe.check('F2 超过 1 MiB 的保存被拒绝',
                lambda: (put('/text', path='README.md', content='x' * 1_048_577,
                             version='0' * 64, identity='i').status_code, 'oversize text'),
                {400, 413, 422})
    probe.check('F2 超过 6.4 MB 请求体被拒绝',
                lambda: (put('/text', path='README.md', content='x' * 6_600_000,
                             version='0' * 64, identity='i').status_code, 'oversize body'),
                {413, 422})
    probe.check('F2 新路径含 NUL 字符被拒绝',
                lambda: (post('/entries', path='bad\x00name.txt').status_code, 'NUL in name'),
                {400, 403, 422})
    probe.check('F2 下载目录被拒绝',
                lambda: (get('/content', path='dir-a').status_code, 'directory read'), {400, 403, 404})
    probe.check('F2 通过软链接下载被拒绝',
                lambda: (get('/content', path='link-out').status_code, 'symlink read'), {400, 403, 404})
    probe.check('F2 下载硬链接文件被拒绝',
                lambda: (get('/content', path='hard-out').status_code, 'hardlink read'), {400, 403, 404})
    probe.check('F2 列出软链接目录不跟随',
                lambda: (get('/entries', path='link-dir').status_code, 'symlink dir'), {400, 403, 404})
    probe.check('F2 目录改到自己的子目录被拒绝且原树保留',
                lambda: (post('/rename', path='dir-a', target='dir-a/inner/dir-a',
                              identity=entry_identity('dir-a')).status_code,
                         f"inner exists={(root / 'dir-a' / 'inner').is_dir()}"), {400, 403, 409})
    probe.check('F2 用裸点路径回收被拒绝',
                lambda: (post('/trash', path='.', identity='x').status_code, 'dot path'),
                {400, 403, 404, 422})
    probe.check('F2 恢复不能逃出工作区',
                lambda: (client.post(base + '/trash/' + str(uuid4()) + '/restore',
                                     json={'target': '../escape.txt'}).status_code, 'restore escape'),
                {403, 404, 409, 422})
    probe.check('F2 文本读取不跨工作区',
                lambda: (client.get(base2 + '/text', params={'path': 'no-such.md'}).status_code, 'other ws'),
                {404})

    # --- F4 确认边界 ----------------------------------------------------
    def submit(path, content, request_id=None):
        return client.post(base + '/outputs', json={'path': path, 'content': content,
                                                    'request_id': str(request_id or uuid4())})

    (root / 'guard.md').write_text('original', encoding='utf-8')
    first = submit('guard.md', 'one').json()
    second = submit('guard.md', 'two').json()

    def two_pending_same_path():
        first_decision = client.post(base + f"/outputs/{first['id']}/decision",
                                     json={'token': first['token'], 'choice': 'overwrite'}).status_code
        second_decision = client.post(base + f"/outputs/{second['id']}/decision",
                                      json={'token': second['token'], 'choice': 'overwrite'}).status_code
        content = (root / 'guard.md').read_text()
        probe.require(first_decision in (200, 409) and second_decision in (200, 409),
                      f'{first_decision}/{second_decision}')
        probe.require(content in ('one', 'two'), f'disk content {content!r}')
        return (None, f'first={first_decision} second={second_decision} disk={content}')

    probe.check('F4 同一路径第二张 pending 不能静默生效', two_pending_same_path)

    (root / 'keep2.md').write_text('keep', encoding='utf-8')
    keep = submit('keep2.md', 'proposed').json()
    (root / 'keep2.md').unlink()

    probe.check('F4 目标在确认前消失时 keep-both 被拒绝，不回落到旧名字',
                lambda: (client.post(base + f"/outputs/{keep['id']}/decision",
                                     json={'token': keep['token'], 'choice': 'keep-both'}).status_code,
                         'target deleted before keep-both'), {404, 409})

    (root / 'cancel2.md').write_text('keep', encoding='utf-8')
    cancel = submit('cancel2.md', 'proposed').json()
    cancelled = client.post(base + f"/outputs/{cancel['id']}/decision",
                            json={'token': cancel['token'], 'choice': 'cancel'})
    probe.require(cancelled.status_code == 200, cancelled.text)
    probe.check('F4 取消后提案不可再取',
                lambda: (client.get(base + f"/outputs/{cancel['id']}/proposal").status_code, 'after cancel'),
                {404, 409})
    probe.check('F4 重放已消费的确认被拒绝',
                lambda: (client.post(base + f"/outputs/{cancel['id']}/decision",
                                     json={'token': cancel['token'], 'choice': 'overwrite'}).status_code,
                         'replay'), {404, 409})
    probe.check('F4 待确认输出不跨工作区可见',
                lambda: (client.get(base2 + f"/outputs/{cancel['id']}").status_code, 'cross-workspace'),
                {404})
    probe.check('F4 同一 request_id 换路径被拒绝',
                lambda: (client.post(base + '/outputs', json={'path': 'other-name.md', 'content': 'x',
                                                              'request_id': first['id']}).status_code,
                         'request id reuse'), {409})
    probe.check('F4 pending 列表按工作区隔离',
                lambda: (None, f"ws={len(client.get(base + '/outputs').json()['items'])} "
                               f"other={len(client.get(base2 + '/outputs').json()['items'])}"))

    # --- F1 --------------------------------------------------------------
    probe.check('F1 未知工作区 ID 拒绝所有文件接口',
                lambda: (client.get('/api/workspaces/' + str(uuid4()) + '/entries').status_code, 'unknown id'),
                {403, 404})

    report = dict(at=datetime.now(timezone.utc).isoformat(), base=probe.base,
                  workspace=str(root), checks=probe.checks,
                  ok=sum(1 for check in probe.checks if check['ok']),
                  total=len(probe.checks))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': report['ok'], 'total': report['total'],
                      'problems': [c for c in probe.checks if not c['ok']],
                      'informational': [c for c in probe.checks
                                        if c.get('expect') is None and c['status'] is not None]},
                     ensure_ascii=False, indent=2))
    return 0 if report['ok'] == report['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
