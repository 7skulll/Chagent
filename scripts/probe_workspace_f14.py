"""Black-box HTTP acceptance probe for workspace F1-F4.

Runs against an already-started Chagent demo server. It creates fixtures in the
given workspace directories, exercises the real HTTP surface, and prints JSON.
It never touches business sessions or a non-temporary directory.

Example:
    python scripts/probe_workspace_f14.py --base http://127.0.0.1:8901 \
        --workspace /tmp/a --other /tmp/b --output docs/diagnostics/x.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import quote
from uuid import uuid4

import httpx

REPO = Path(__file__).resolve().parents[1]


class Probe:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.client = httpx.Client(base_url=self.base, timeout=30, trust_env=False)
        self.checks = []

    def check(self, name, function):
        try:
            detail = function()
            self.checks.append({'name': name, 'passed': True, 'detail': detail})
        except Exception as error:  # noqa: BLE001 - a probe reports, it does not crash
            self.checks.append({'name': name, 'passed': False, 'detail': f'{type(error).__name__}: {error}'})

    def require(self, condition, message):
        if not condition:
            raise AssertionError(message)


def fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'README.md').write_text('# Workspace\n\nHello.\n', encoding='utf-8')
    (root / 'notes-crlf.txt').write_bytes('一\r\n二\r\n'.encode('utf-8'))
    (root / 'data.csv').write_text('a,b\n1,2\n', encoding='utf-8')
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src' / 'main.py').write_text('print("hi")\n', encoding='utf-8')
    (root / 'uploads').mkdir(exist_ok=True)
    (root / 'uploads' / 'seed.txt').write_text('seed\n', encoding='utf-8')
    (root / '.hidden.txt').write_text('hidden\n', encoding='utf-8')
    (root / '.git').mkdir(exist_ok=True)
    (root / '.git' / 'config').write_text('[core]\n', encoding='utf-8')
    (root / 'binary.bin').write_bytes(b'\xff\x00\x01')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8901')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--other', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()

    root = args.workspace.resolve()
    other = args.other.resolve()
    fixtures(root)
    fixtures(other)

    probe = Probe(args.base)
    client = probe.client

    def open_workspace(path):
        response = client.post('/api/workspaces', json={'path': str(path)})
        probe.require(response.status_code == 201, f'open {path}: {response.status_code} {response.text}')
        return response.json()

    workspace = open_workspace(root)
    second = open_workspace(other)
    base = f"/api/workspaces/{workspace['id']}"
    second_base = f"/api/workspaces/{second['id']}"

    def workspace_identity():
        first = open_workspace(root)
        again = open_workspace(root)
        probe.require(first['id'] == again['id'] == workspace['id'], 'same directory must reuse one workspace ID')
        listing = client.get('/api/workspaces').json()['items']
        row = next(item for item in listing if item['id'] == workspace['id'])
        probe.require(row['available'] is True, 'opened workspace must be available')
        return {'id': header_id(first), 'sessions': row['sessions']}

    def header_id(row):
        return str(row['id'])

    def source_rejected():
        response = client.post('/api/workspaces', json={'path': str(REPO)})
        probe.require(response.status_code == 400, f'source directory: {response.status_code} {response.text}')
        rows = client.get('/api/workspaces').json()['items']
        probe.require(all(Path(row['path']) != REPO for row in rows), 'rejected source must not be registered')
        return response.status_code

    def root_listing():
        response = client.get(base + '/entries')
        probe.require(response.status_code == 200, response.text)
        items = {item['name']: item for item in response.json()['items']}
        for name in ['README.md', 'src', 'uploads', 'binary.bin']:
            probe.require(name in items, f'missing {name}')
        for name in ['.git', '.hidden.txt', '.chagent']:
            probe.require(name not in items, f'{name} must stay hidden by default')
        probe.require(items['src']['type'] == 'directory', 'src must be a directory')
        hidden = client.get(base + '/entries', params={'hidden': 'true'}).json()['items']
        names = {item['name'] for item in hidden}
        probe.require({'.git', '.hidden.txt'} <= names, 'hidden toggle must reveal dot paths')
        probe.require('.chagent' not in names, 'internal .chagent must never be listed')
        return sorted(items)

    def pagination():
        directory = root / 'paged'
        directory.mkdir(exist_ok=True)
        for index in range(5):
            (directory / f'f{index}.txt').write_text(str(index), encoding='utf-8')
        first = client.get(base + '/entries', params={'path': 'paged', 'limit': 2}).json()
        probe.require(len(first['items']) == 2, 'limit must bound one page')
        probe.require(first['next_offset'] == 2, f"next_offset {first['next_offset']}")
        last = client.get(base + '/entries', params={'path': 'paged', 'offset': 4, 'limit': 2}).json()
        probe.require(len(last['items']) == 1 and last['next_offset'] is None, 'final page must end paging')
        return {'first': 2, 'last': 1}

    def read_version():
        response = client.get(base + '/text', params={'path': 'README.md'})
        probe.require(response.status_code == 200, response.text)
        body = response.json()
        probe.require(body['content'].startswith('# Workspace'), 'content mismatch')
        probe.require(len(body['version']) == 64, 'version must be a SHA256 hex digest')
        probe.require(bool(body['identity']), 'identity required')
        probe.require(body['newline'] == 'lf', f"newline {body['newline']}")
        return {'version': body['version'][:12], 'newline': body['newline']}

    def save_conflict():
        read = client.get(base + '/text', params={'path': 'README.md'}).json()
        before = (root / 'README.md').read_bytes()
        stale = client.put(base + '/text', json={'path': 'README.md', 'content': 'stale', 'version': '0' * 64,
                                                 'identity': read['identity']})
        probe.require(stale.status_code == 409, f'stale write: {stale.status_code}')
        probe.require((root / 'README.md').read_bytes() == before, 'stale write must not touch disk')
        saved = client.put(base + '/text', json={'path': 'README.md', 'content': '# Workspace\n\nSaved.\n',
                                                 'version': read['version'], 'identity': read['identity']})
        probe.require(saved.status_code == 200, saved.text)
        probe.require((root / 'README.md').read_text() == '# Workspace\n\nSaved.\n', 'disk must match save')
        probe.require(saved.json()['version'] != read['version'], 'version must advance')
        return {'stale': 409}

    def crlf_roundtrip():
        read = client.get(base + '/text', params={'path': 'notes-crlf.txt'}).json()
        probe.require(read['newline'] == 'crlf', f"newline {read['newline']}")
        payload = {'path': 'notes-crlf.txt', 'content': '三\r\n二\r\n', 'version': read['version'],
                   'identity': read['identity']}
        saved = client.put(base + '/text', json=payload)
        probe.require(saved.status_code == 200, saved.text)
        probe.require((root / 'notes-crlf.txt').read_bytes() == '三\r\n二\r\n'.encode(), 'CRLF bytes must be preserved')
        return {'newline': 'crlf'}

    def binary_and_traversal():
        binary = client.get(base + '/text', params={'path': 'binary.bin'})
        probe.require(binary.status_code == 400, f'binary read: {binary.status_code}')
        for path in ['../escape', '/tmp/escape', '.chagent/trash/x']:
            response = client.post(base + '/entries', json={'path': path})
            probe.require(response.status_code in (400, 403), f'{path}: {response.status_code}')
        probe.require(not (root.parent / 'escape').exists(), 'traversal must not write outside root')
        return {'binary': 400, 'traversal': 'rejected'}

    def create_and_collide():
        created = client.post(base + '/entries', json={'path': 'new.txt', 'content': 'one'})
        probe.require(created.status_code == 201, created.text)
        probe.require((root / 'new.txt').read_text() == 'one', 'created file content')
        duplicate = client.post(base + '/entries', json={'path': 'new.txt', 'content': 'two'})
        probe.require(duplicate.status_code == 409, f'duplicate: {duplicate.status_code}')
        missing_parent = client.post(base + '/entries', json={'path': 'missing/child.txt'})
        probe.require(missing_parent.status_code == 404, f'missing parent: {missing_parent.status_code}')
        directory = client.post(base + '/entries', json={'path': 'newdir', 'type': 'directory'})
        probe.require(directory.status_code == 201, directory.text)
        return {'create': 201, 'duplicate': 409, 'missing_parent': 404}

    def identity_of(path):
        parent, _, name = path.rpartition('/')
        response = client.get(base + '/entries', params={'path': parent, 'limit': 500, 'hidden': 'true'})
        item = next(entry for entry in response.json()['items'] if entry['path'] == path)
        return item['identity']

    def rename_rules():
        client.post(base + '/entries', json={'path': 'rename-source.txt', 'content': 'x'})
        client.post(base + '/entries', json={'path': 'rename-target.txt', 'content': 'keep'})
        identity = identity_of('rename-source.txt')
        (root / 'rename-source.txt').write_text('changed externally', encoding='utf-8')
        stale = client.post(base + '/rename', json={'path': 'rename-source.txt', 'target': 'renamed.txt',
                                                    'identity': identity})
        probe.require(stale.status_code == 409, f'stale identity: {stale.status_code}')
        renamed = client.post(base + '/rename', json={'path': 'rename-source.txt', 'target': 'renamed.txt',
                                                      'identity': identity_of('rename-source.txt')})
        probe.require(renamed.status_code == 200, renamed.text)
        probe.require((root / 'renamed.txt').is_file(), 'rename must move the file')
        collision = client.post(base + '/rename', json={'path': 'renamed.txt', 'target': 'rename-target.txt',
                                                        'identity': identity_of('renamed.txt')})
        probe.require(collision.status_code == 409, f'rename collision: {collision.status_code}')
        probe.require((root / 'rename-target.txt').read_text() == 'keep', 'collision must not overwrite')
        return {'rename': 200, 'stale': 409, 'collision': 409}

    def trash_and_restore():
        client.post(base + '/entries', json={'path': 'trash-me.txt', 'content': 'gone'})
        identity = identity_of('trash-me.txt')
        recycled = client.post(base + '/trash', json={'path': 'trash-me.txt', 'identity': identity})
        probe.require(recycled.status_code == 201, recycled.text)
        record = recycled.json()['id']
        probe.require(not (root / 'trash-me.txt').exists(), 'recycle must remove the original path')
        probe.require(any(item['id'] == record for item in client.get(base + '/trash').json()['items']),
                      'trash listing must show the record')
        client.post(base + '/entries', json={'path': 'trash-me.txt', 'content': 'current'})
        blocked = client.post(base + f'/trash/{record}/restore', json={})
        probe.require(blocked.status_code == 409, f'restore collision: {blocked.status_code}')
        probe.require((root / 'trash-me.txt').read_text() == 'current', 'restore must not overwrite')
        restored = client.post(base + f'/trash/{record}/restore', json={'target': 'restored.txt'})
        probe.require(restored.status_code == 200, restored.text)
        probe.require((root / 'restored.txt').read_text() == 'gone', 'restored content')
        return {'recycle': 201, 'collision': 409, 'restore': 200}

    def concurrent_save():
        client.post(base + '/entries', json={'path': 'race.txt', 'content': 'original'})
        read = client.get(base + '/text', params={'path': 'race.txt'}).json()

        def save(value):
            with httpx.Client(base_url=probe.base, timeout=30, trust_env=False) as own:
                return own.put(base + '/text', json={'path': 'race.txt', 'content': value,
                                                     'version': read['version'],
                                                     'identity': read['identity']}).status_code

        with ThreadPoolExecutor(2) as pool:
            statuses = sorted(pool.map(save, ['one', 'two']))
        probe.require(statuses == [200, 409], f'concurrent saves: {statuses}')
        probe.require((root / 'race.txt').read_text() in ('one', 'two'), 'winner content must be on disk')
        return {'statuses': statuses}

    def download():
        response = client.get(base + '/content', params={'path': 'data.csv'})
        probe.require(response.status_code == 200, response.text)
        probe.require(response.content == b'a,b\n1,2\n', 'download bytes')
        probe.require('attachment' in response.headers.get('content-disposition', ''), 'attachment disposition')
        return {'bytes': len(response.content)}

    def session_binding():
        first = client.post('/api/sessions', json={'title': 'same title', 'workspace': str(root)}).json()
        second_session = client.post('/api/sessions', json={'title': 'same title', 'workspace': str(other)}).json()
        scoped = client.get('/api/sessions', params={'workspace': str(root)}).json()['items']
        probe.require([item['id'] for item in scoped] == [first['id']],
                      'workspace-scoped session list must not mix groups')
        probe.require(first['workspace'] != second_session['workspace'], 'session workspace binding')
        found = client.get('/api/sessions', params={'workspace': str(root), 'q': 'same'}).json()['items']
        probe.require([item['id'] for item in found] == [first['id']], 'title search must stay in workspace')
        return {'sessions': len(scoped)}

    def output_commit_and_pending():
        direct = client.post(base + '/outputs', json={'path': 'fresh.md', 'content': '# fresh',
                                                      'request_id': str(uuid4())}).json()
        probe.require(direct['status'] == 'committed', 'new path must commit directly')
        probe.require((root / 'fresh.md').read_text() == '# fresh', 'direct output content')
        (root / 'collision.md').write_text('old', encoding='utf-8')
        pending = client.post(base + '/outputs', json={'path': 'collision.md', 'content': 'proposed',
                                                       'request_id': str(uuid4())}).json()
        probe.require(pending['status'] == 'pending', 'collision must wait')
        probe.require((root / 'collision.md').read_text() == 'old', 'pending output must not write')
        proposal = client.get(base + f"/outputs/{pending['id']}/proposal")
        probe.require(proposal.status_code == 200 and proposal.content == b'proposed', 'proposal download')
        forged = client.post(base + f"/outputs/{pending['id']}/decision",
                             json={'token': str(uuid4()), 'choice': 'overwrite'})
        probe.require(forged.status_code == 409, f'forged token: {forged.status_code}')
        (root / 'collision.md').write_text('external', encoding='utf-8')
        stale = client.post(base + f"/outputs/{pending['id']}/decision",
                            json={'token': pending['token'], 'choice': 'overwrite'})
        probe.require(stale.status_code == 409, f'stale token: {stale.status_code}')
        latest = client.get(base + '/outputs').json()['items'][0]
        probe.require(latest['token'] != pending['token'], 'stale decision must rotate the token')
        old_again = client.post(base + f"/outputs/{pending['id']}/decision",
                                json={'token': pending['token'], 'choice': 'overwrite'})
        probe.require(old_again.status_code == 409, 'old token must stay dead')

        def decide():
            with httpx.Client(base_url=probe.base, timeout=30, trust_env=False) as own:
                return own.post(base + f"/outputs/{pending['id']}/decision",
                                json={'token': latest['token'], 'choice': 'overwrite'}).status_code

        with ThreadPoolExecutor(2) as pool:
            statuses = sorted(pool.map(lambda _: decide(), range(2)))
        probe.require(statuses == [200, 409], f'concurrent decisions: {statuses}')
        probe.require((root / 'collision.md').read_text() == 'proposed', 'confirmed output content')
        probe.require(client.get(base + '/outputs').json()['items'] == [], 'pending must clear after decision')
        return {'direct': 'committed', 'pending': 'confirmed', 'statuses': statuses}

    def output_keep_both_and_cancel():
        (root / 'keep.md').write_text('keep', encoding='utf-8')
        pending = client.post(base + '/outputs', json={'path': 'keep.md', 'content': 'new',
                                                       'request_id': str(uuid4())}).json()
        kept = client.post(base + f"/outputs/{pending['id']}/decision",
                           json={'token': pending['token'], 'choice': 'keep-both'})
        probe.require(kept.status_code == 200, kept.text)
        probe.require((root / 'keep.md').read_text() == 'keep', 'keep-both preserves original')
        probe.require((root / 'keep-1.md').read_text() == 'new', 'keep-both writes a new name')
        again = client.post(base + '/outputs', json={'path': 'keep.md', 'content': 'cancel me',
                                                     'request_id': str(uuid4())}).json()
        cancelled = client.post(base + f"/outputs/{again['id']}/decision",
                                json={'token': again['token'], 'choice': 'cancel'})
        probe.require(cancelled.status_code == 200, cancelled.text)
        probe.require((root / 'keep.md').read_text() == 'keep', 'cancel must not write')
        return {'keep_both': 'keep-1.md', 'cancel': 200}

    def output_idempotence_and_scope():
        identity = str(uuid4())
        body = {'path': 'again.md', 'content': 'same', 'request_id': identity}
        first = client.post(base + '/outputs', json=body).json()
        repeated = client.post(base + '/outputs', json=body).json()
        probe.require(first['id'] == repeated['id'], 'same request_id and content must be idempotent')
        changed = client.post(base + '/outputs', json={**body, 'content': 'different'})
        probe.require(changed.status_code == 409, f'changed content for same key: {changed.status_code}')
        probe.require(client.get(second_base + '/outputs').json()['items'] == [],
                      'pending outputs must be workspace-scoped')
        return {'idempotent': True}

    def uploads():
        first = client.post('/api/uploads', params={'name': 'note.txt', 'workspace': str(root)},
                            content=b'old', headers={'Content-Type': 'text/plain'})
        probe.require(first.status_code == 201, first.text)
        probe.require((root / 'uploads' / 'note.txt').read_text() == 'old', 'upload must land in uploads/')
        second = client.post('/api/uploads', params={'name': 'note.txt', 'workspace': str(root)},
                             content=b'new', headers={'Content-Type': 'text/plain'})
        probe.require(second.status_code == 202, f'upload collision: {second.status_code}')
        probe.require((root / 'uploads' / 'note.txt').read_text() == 'old', 'collision must not overwrite')
        item = client.get(base + '/outputs').json()['items'][0]
        decision = client.post(base + f"/outputs/{item['id']}/decision",
                               json={'token': item['token'], 'choice': 'cancel'})
        probe.require(decision.status_code == 200, decision.text)
        probe.require((root / 'uploads' / 'note.txt').read_text() == 'old', 'cancelled upload keeps original')
        return {'committed': 201, 'collision': 202, 'cancel': 200}

    def unavailable_workspace():
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            doomed = Path(folder).resolve()
            doomed_row = open_workspace(doomed)
            created = client.post('/api/sessions', json={'title': 'doomed', 'workspace': str(doomed)})
            probe.require(created.status_code == 201, created.text)
        rows = client.get('/api/workspaces').json()['items']
        row = next(item for item in rows if item['id'] == doomed_row['id'])
        probe.require(row['available'] is False and bool(row['reason']), 'missing directory must be unavailable')
        probe.require(client.get(f"/api/workspaces/{row['id']}/entries").status_code == 403,
                      'unavailable workspace must refuse file access')
        return {'available': False, 'files': 403}

    probe.check('F1 same directory reuses one workspace', workspace_identity)
    probe.check('F1 source directory is rejected and not registered', source_rejected)
    probe.check('F1 session list is workspace-scoped', session_binding)
    probe.check('F1 deleted directory stays listed but unavailable', unavailable_workspace)
    probe.check('F2 root listing hides internal paths', root_listing)
    probe.check('F2 directory pagination', pagination)
    probe.check('F2 read returns version, identity and newline', read_version)
    probe.check('F2 stale save is rejected without touching disk', save_conflict)
    probe.check('F2 CRLF bytes survive a save', crlf_roundtrip)
    probe.check('F2 binary and traversal are refused', binary_and_traversal)
    probe.check('F2 create, duplicate and missing parent', create_and_collide)
    probe.check('F2 rename is identity-checked and never overwrites', rename_rules)
    probe.check('F2 recycle, collision and restore as new name', trash_and_restore)
    probe.check('F2 concurrent saves have one winner', concurrent_save)
    probe.check('F2 download is an attachment with exact bytes', download)
    probe.check('F4 new output commits, collision waits and token rotates', output_commit_and_pending)
    probe.check('F4 keep-both and cancel do not overwrite', output_keep_both_and_cancel)
    probe.check('F4 output request is idempotent and scoped', output_idempotence_and_scope)
    probe.check('F4 upload collision is pending and cancellable', uploads)

    report = {'at': datetime.now(timezone.utc).isoformat(), 'base': probe.base,
              'workspace': str(root), 'other': str(other), 'checks': probe.checks,
              'passed': sum(check['passed'] for check in probe.checks), 'total': len(probe.checks)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'total': report['total'],
                      'failed': [check for check in probe.checks if not check['passed']]}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
