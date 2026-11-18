"""Operator-owned frozen task catalog and tenant-private platform bindings."""
from pathlib import Path
import shutil
import subprocess
import tarfile

from eval.container_trial import PROFILE, load_task
from eval.manifests import sha256_text

BACKEND = 'container-task-v1'


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


def entries(root):
    """A catalog is a trusted directory, never an HTTP-supplied filesystem path."""
    result = {}
    for directory in sorted(Path(root).glob('*')):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            case, doc, digest = load_task(directory)
        except (OSError, ValueError, KeyError, tarfile.TarError):
            continue
        if case.key in result:
            raise ValueError('容器目录存在重复用例版本：' + case.key)
        result[case.key] = (directory, case, doc, digest)
    return result


def catalog(root):
    rows = []
    # Keep broken tasks visible without preventing other valid choices.
    seen = set()
    for directory in sorted(Path(root).glob('*')):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            case, doc, _ = load_task(directory)
            if case.key in seen:
                raise ValueError('容器目录存在重复用例版本：' + case.key)
            seen.add(case.key)
            row = {'key': case.key, 'title': case.title, 'capability': case.capability,
                   'sha256': case.sha256, 'requires_sandbox': False,
                   'execution': capabilities(doc), 'backend': BACKEND}
            try:
                preflight(doc)
                row.update(supported=True, reason='')
            except ValueError as error:
                row.update(supported=False, reason=str(error))
        except (OSError, ValueError, KeyError, tarfile.TarError) as error:
            row = {'key': 'unavailable:' + directory.name, 'title': directory.name,
                   'capability': 'project-work', 'sha256': None, 'requires_sandbox': False,
                   'backend': BACKEND, 'supported': False, 'reason': '冻结任务不可读或绑定无效：' + str(error)}
        rows.append(row)
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
        frozen_case, frozen, frozen_hash = load_task(target)
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
        case, doc, digest = load_task(directory)
        if (case.key != key or case.sha256 != binding['case_sha256'] or digest != binding['sha256']
                or capabilities(doc) != binding['execution']):
            raise ValueError('冻结容器任务已变化，尚未调用模型')
        if inspect_images:
            preflight(doc)
        cases[key] = case
    return cases
