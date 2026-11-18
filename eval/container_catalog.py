"""Operator-owned frozen task catalog and tenant-private platform bindings."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tarfile

from eval.container_trial import PROFILE, load_task
from eval.manifests import sha256_text

BACKEND = 'container-task-v1'
# Exactly the five files load_task validates; any other file in the directory is
# copied verbatim but cannot change the binding.
BOUND_FILES = ('task.json', 'case.json', 'snapshot.tar', 'verifier/plan.json', 'verifier/verifier.tar')
# Bounded so a long-lived web process holding many archived plans cannot grow
# without limit.  Eviction is cheapest-first: the oldest signature, recomputed
# on the next read.
MAX_CACHED_TASKS = 256
VERIFIED = {}


def _signature(directory):
    """Change detector for the files load_task reads; no content hashing here."""
    stamps = []
    for name in BOUND_FILES:
        stat = (directory / name).stat()
        stamps.append((name, stat.st_mtime_ns, stat.st_size, stat.st_ino))
    return tuple(stamps)


def verified_task(directory):
    """load_task with a signature cache, returning a private copy each time.

    Every platform poll re-read and re-hashed every frozen snapshot, so a detail
    refresh cost grew with catalog size and snapshot bytes.  The signature is the
    stat of exactly the validated files: an edit, a replacement or an in-place
    rewrite changes mtime_ns/size/inode and forces a full re-validation.  A
    deliberate attacker who restores mtimes also owns the plan directory, which
    is outside this check's threat model.
    """
    directory = Path(directory).resolve(strict=True)
    signature = _signature(directory)
    cached = VERIFIED.get(str(directory))
    if cached is not None and cached[0] == signature:
        return deepcopy(cached[1])
    value = load_task(directory)
    if len(VERIFIED) >= MAX_CACHED_TASKS:
        VERIFIED.pop(next(iter(VERIFIED)))
    VERIFIED[str(directory)] = (signature, value)
    return deepcopy(value)


def capabilities(document):
    env = document['environment']
    return {'backend': BACKEND, 'tool_profile': PROFILE, 'tools': ['exec_command'],
            'boundary': 'container', 'memory': False, 'compaction': False,
            'network': env['network'], 'resources': env['resources'],
            'services': [s['name'] for s in env['services']],
            'verifier': 'verify-function-v1', 'verifier_network': 'none'}


def preflight(document):
    """Only inspect required frozen image IDs; never build, pull or change tags."""
    env = document['environment']
    images = {env['image_id'], document['verifier']['image_id'],
              *(s['image_id'] for s in env['services'])}
    for image in sorted(images):
        try:
            result = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                    capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('Docker 预检不可用；请检查 daemon 与本机执行镜像') from error
        if result.returncode or result.stdout.strip() != image:
            raise ValueError('缺少冻结任务所需镜像或镜像身份不符；请显式准备已登记的镜像 ID')


def _directories(root):
    for directory in sorted(Path(root).glob('*')):
        if directory.is_dir() and not directory.is_symlink():
            yield directory


def entries(root):
    """A catalog is a trusted directory, never an HTTP-supplied filesystem path.

    Duplicates raise here, so a plan can never copy two versions of one Case.
    The catalog itself keeps them visible instead (see ``catalog``): an operator
    needs to see which directory collides, not a page that refuses to load.
    """
    result, sources = {}, {}
    for directory in _directories(root):
        try:
            case, doc, digest = verified_task(directory)
        except (OSError, ValueError, KeyError, tarfile.TarError):
            continue
        if case.key in result:
            raise ValueError(f'容器目录存在重复用例版本：{case.key}'
                             f'（{sources[case.key].name}、{directory.name}）')
        result[case.key] = (directory, case, doc, digest)
        sources[case.key] = directory
    return result


def _row(directory):
    """One catalog row; unreadable tasks stay visible without breaking the rest."""
    try:
        case, doc, _ = verified_task(directory)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        return {'key': 'unavailable:' + directory.name, 'title': directory.name,
                'capability': 'project-work', 'sha256': None, 'requires_sandbox': False,
                'backend': BACKEND, 'supported': False,
                'reason': '冻结任务不可读或绑定无效：' + str(error)}
    row = {'key': case.key, 'title': case.title, 'capability': case.capability,
           'sha256': case.sha256, 'requires_sandbox': False,
           'execution': capabilities(doc), 'backend': BACKEND}
    try:
        preflight(doc)
        row.update(supported=True, reason='')
    except ValueError as error:
        row.update(supported=False, reason=str(error))
    return row


def catalog(root):
    rows = [_row(directory) for directory in _directories(root)]
    counts = {}
    for row in rows:
        if not row['key'].startswith('unavailable:'):
            counts[row['key']] = counts.get(row['key'], 0) + 1
    # Both copies are disabled: the operator must fix the directory, and picking
    # either one would only fail later in freeze_selected.
    for row in rows:
        count = counts.get(row['key'], 0)
        if count > 1:
            row.update(supported=False,
                       reason='同一用例版本在目录中出现 %d 次：%s；请只保留一份' % (count, row['key']))
    return rows


def freeze_selected(root, keys, folder):
    sources = entries(root)
    bindings, cases = {}, []
    for key in sorted(keys):
        if key not in sources:
            raise ValueError('容器用例已变更或不存在，请刷新目录')
        source, case, doc, digest = sources[key]
        preflight(doc)
        relative = '.platform/tasks/' + sha256_text(key)[:24]
        target = Path(folder) / relative
        shutil.copytree(source, target)
        frozen_case, frozen, frozen_hash = verified_task(target)
        if frozen_hash != digest or frozen_case.sha256 != case.sha256:
            raise ValueError('冻结复制期间任务输入发生变化')
        bindings[key] = {'path': relative, 'sha256': digest,
                         'case_sha256': case.sha256, 'execution': capabilities(frozen)}
        cases.append(frozen_case)
    return cases, bindings


def frozen_cases(folder, bindings, *, inspect_images=False):
    cases = {}
    for key, binding in bindings.items():
        expected = '.platform/tasks/' + sha256_text(key)[:24]
        if binding.get('path') != expected:
            raise ValueError('冻结任务路径绑定不合法')
        directory = Path(folder) / expected
        if not directory.resolve().is_relative_to(Path(folder).resolve()) or directory.is_symlink():
            raise ValueError('冻结任务路径越界')
        case, doc, digest = verified_task(directory)
        if (case.key != key or case.sha256 != binding['case_sha256'] or digest != binding['sha256']
                or capabilities(doc) != binding['execution']):
            raise ValueError('冻结容器任务已变化，尚未调用模型')
        if inspect_images:
            preflight(doc)
        cases[key] = case
    return cases
