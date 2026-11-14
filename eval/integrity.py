"""Local immutable publication and execution identity for P4 evidence."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import tempfile

from eval.manifests import sha256_json


def publish(path: Path, document: dict, *, identical_ok=False):
    """Publish a complete file atomically without replacing existing evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not identical_ok or path.is_symlink() or json.loads(path.read_text()) != document:
                raise ValueError(f'{path.name} already exists; evidence cannot be overwritten') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


@contextmanager
def experiment_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.execution.lock').open('a') as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another process is running this experiment') from None
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def identity(manifest: dict, settings, model_settings=None) -> dict:
    """Model-independent code/runtime identity; model choice belongs to the arm."""
    model_config = (None if model_settings is None else
                    {key: value for key, value in asdict(model_settings).items()
                     if key not in {'api_key', 'key_source', 'thinking_source',
                                    'model_id', 'provider', 'base_url'}})
    config = {'model_settings_sha256': sha256_json(model_config) if model_config is not None else None,
              'settings_sha256': sha256_json(asdict(settings)),
              'ambient_sha256': sha256_json({key: value for key, value in os.environ.items()
                  if key.startswith('CHAGENT_') and key not in {'CHAGENT_MODEL', 'CHAGENT_PROVIDER'}})}
    return {'repo': manifest['repo'], 'dependencies': manifest['dependencies'],
            'runtime': manifest['runtime'], 'skills': manifest['skills'],
            'prompt_source_sha256': manifest['prompts']['sha256'],
            'tool_source_sha256': manifest['tools']['sha256'], 'config': config}


def execution(identity_document: dict) -> dict:
    return {'version': 3, 'identity': identity_document,
            'identity_sha256': sha256_json(identity_document)}


def identity_difference(recorded, current) -> list[str]:
    """Which components of two execution identities differ, as dotted paths.

    The guard already refuses to continue; naming the component is what tells an
    operator whether to revert an edit, commit it, or start a new experiment.  Only
    one level is walked because that is the whole shape of an identity: five groups
    of flat hashes.  A missing component is reported, not skipped -- "absent" and
    "unchanged" are different facts.
    """
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return ["identity"]
    paths = []
    for key in sorted(set(recorded) | set(current)):
        left, right = recorded.get(key), current.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            for inner in sorted(set(left) | set(right)):
                if left.get(inner) != right.get(inner):
                    paths.append(f"{key}.{inner}")
        elif left != right:
            paths.append(key)
    return paths


def valid_identity(document):
    import re
    def digest(value, size=64):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is not None
    if not isinstance(document, dict) or set(document) != {
            'repo', 'dependencies', 'runtime', 'skills', 'prompt_source_sha256',
            'tool_source_sha256', 'config'}:
        return False
    repo = document['repo']
    deps, runtime, skills = document['dependencies'], document['runtime'], document['skills']
    return (isinstance(repo, dict) and digest(repo.get('commit'), 40)
            and type(repo.get('dirty')) is bool
            and (digest(repo.get('patch_sha256')) if repo['dirty'] else repo.get('patch_sha256') is None)
            and isinstance(deps, dict) and digest(deps.get('lock_sha256')) and bool(deps.get('python'))
            and isinstance(runtime, dict) and bool(runtime.get('platform'))
            and isinstance(skills, dict) and bool(skills) and all(digest(value) for value in skills.values())
            and digest(document['prompt_source_sha256']) and digest(document['tool_source_sha256'])
            and isinstance(document['config'], dict)
            and all(digest(document['config'].get(key)) for key in
                    ('settings_sha256', 'ambient_sha256', 'model_settings_sha256')))
