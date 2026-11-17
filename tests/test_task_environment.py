import asyncio
from hashlib import sha256
import io
import json
import sys
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from chagent.hostexec.task_environment import TaskEnvironment, archive_manifest, relative_path, snapshot_workspace
from chagent.tools.sandbox import Sandbox


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_stable_and_preserves_executable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            root.mkdir()
            (root / 'run.sh').write_text('echo frozen\n')
            (root / 'run.sh').chmod(0o755)
            (root / 'empty').mkdir()
            first = snapshot_workspace(root, Path(temp) / 'one.tar')
            second = snapshot_workspace(root, Path(temp) / 'two.tar')
            self.assertEqual(first, second)
            self.assertEqual(first['files'][0]['sha256'], sha256(b'echo frozen\n').hexdigest())
            with tarfile.open(Path(temp) / 'one.tar') as archive:
                self.assertEqual(archive.getmember('run.sh').mode, 0o755)

    def test_rejects_links_traversal_duplicate_and_undeclared_artifacts(self):
        for name in ('../outside', '/absolute', 'a/../b', 'a//b', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                relative_path(name)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            root.mkdir()
            (root / 'link').symlink_to('/etc/hosts')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                snapshot_workspace(root, Path(temp) / 'source.tar')
            (root / 'link').unlink()
            outside = Path(temp) / 'external'
            outside.write_text('external')
            os.link(outside, root / 'hardlink')
            with self.assertRaisesRegex(PermissionError, 'hard-linked'):
                snapshot_workspace(root, Path(temp) / 'hardlink.tar')
            for kind in ('link', 'duplicate', 'undeclared', 'oversize'):
                with self.subTest(kind=kind):
                    path = Path(temp) / (kind + '.tar')
                    with tarfile.open(path, 'w') as archive:
                        entry = tarfile.TarInfo('file')
                        if kind == 'link':
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = '/etc/hosts'
                        entry.size = 2 if kind == 'oversize' else 0
                        archive.addfile(entry, io.BytesIO(b'ab'))
                        if kind == 'duplicate':
                            archive.addfile(entry)
                    with self.assertRaises(ValueError):
                        archive_manifest(path, limit=1, allowed=['other'] if kind == 'undeclared' else None)


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DOCKER') == '1', 'Set CHAGENT_TEST_DOCKER=1')
class TaskDockerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chagent-task-test-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'input.txt').write_text('frozen')
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if task.state.get('cleanup') != 'confirmed':
                await task.cleanup()
        self.temp.cleanup()

    async def create(self, name, **kwargs):
        task = await TaskEnvironment.create(self.root / name, **kwargs)
        self.tasks.append(task)
        return task

    async def test_private_volumes_frozen_repeat_and_declared_artifact(self):
        first = await self.create('first', workspace=self.source, artifacts=['result'])
        (self.source / 'input.txt').write_text('host-changed')
        result = await first.run('mkdir result; cat input.txt > result/output.txt; echo task-change > input.txt; '
                                 'echo private > /cache/private; echo not-declared > extra.txt')
        self.assertFalse(result.is_error, result)
        manifest = await first.collect()
        self.assertEqual((first.output / 'artifacts/result/output.txt').read_text(), 'frozen')
        self.assertEqual(manifest['files'][0]['sha256'], sha256(b'frozen').hexdigest())
        self.assertFalse((first.output / 'artifacts/extra.txt').exists())
        self.assertEqual((self.source / 'input.txt').read_text(), 'host-changed')
        self.assertTrue(all(m['Type'] == 'volume' for m in first.state['environment']['mounts']))
        second = await self.create('second', snapshot=first.output / 'snapshot.tar', artifacts=['copy.txt'])
        result = await second.run('test ! -e /cache/private && cp input.txt copy.txt')
        self.assertFalse(result.is_error, result)
        await second.collect()
        self.assertEqual((second.output / 'artifacts/copy.txt').read_text(), 'frozen')
        self.assertEqual(first.state['snapshot'], second.state['snapshot'])
        self.assertNotEqual(first.state['environment']['container_id'], second.state['environment']['container_id'])
        for task in (first, second):
            await task.cleanup()
            self.assertEqual(task.state['cleanup'], 'confirmed')
            self.assertNotEqual((await Sandbox._docker('inspect', task.name))[0], 0)

    async def test_symlink_and_missing_artifact_fail_without_host_write(self):
        for index, cmd in enumerate(('ln -s /etc/hosts out', 'true')):
            task = await self.create('bad' + str(index), workspace=self.source, artifacts=['out'])
            await task.run(cmd)
            with self.assertRaisesRegex(ValueError, 'collection failed'):
                await task.collect()
            self.assertEqual(task.state['phase'], 'collection_failed')
            self.assertFalse((task.output / 'artifacts').exists())

    async def test_timeout_cancellation_and_recovery_cleanup(self):
        task = await self.create('cancel', workspace=self.source)
        result = await task.run('sleep 5', timeout=.2)
        self.assertTrue(result.details['timed_out'])
        execution = asyncio.create_task(task.run('sleep 20', timeout=30))
        await asyncio.sleep(.3)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        recovered = TaskEnvironment(task.output)
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            await recovered.run('true')
        await recovered.cleanup()
        self.assertEqual(recovered.state['cleanup'], 'confirmed')
        task.state = recovered.state
        await recovered.cleanup()  # Idempotent absence checks.

    async def test_failure_does_not_touch_unrelated_container_or_reuse_output(self):
        task = await self.create('owned', workspace=self.source)
        with self.assertRaises(FileExistsError):
            await TaskEnvironment.create(task.output, workspace=self.source)
        code, unrelated = await Sandbox._docker('run', '-d', '--label', 'chagent.task=unrelated',
            '--entrypoint', '/bin/sleep', task.state['image_id'], 'infinity')
        self.assertEqual(code, 0)
        try:
            await task.cleanup()
            self.assertEqual((await Sandbox._docker('inspect', unrelated.strip()))[0], 0)
        finally:
            await Sandbox._docker('rm', '-f', unrelated.strip())

    async def test_artifact_limit_and_policy_drift_are_explicit_failures(self):
        task = await self.create('limited', workspace=self.source, artifacts=['out'])
        await task.run('printf abcdef > out')
        with self.assertRaisesRegex(ValueError, 'collection failed'):
            await task.collect(limit=2)
        self.assertIn('size limit', task.state['error'])
        other = await self.create('drift', workspace=self.source)
        await Sandbox._docker('update', '--cpus', '3', other.name)
        with self.assertRaisesRegex(RuntimeError, 'policy changed'):
            await other.run('true')

    async def test_cli_run_collect_cleanup_and_snapshot_replay(self):
        for index in range(2):
            output = self.root / ('cli' + str(index))
            source = (['--workspace', str(self.source)] if index == 0 else
                      ['--snapshot', str(self.root / 'cli0/snapshot.tar')])
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
                *source, '--out', str(output), '--cmd', 'cp input.txt answer.txt', '--artifact', 'answer.txt',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            self.assertEqual(process.returncode, 0, stderr.decode())
            state = json.loads(stdout)
            self.assertEqual(state['phase'], 'cleaned')
            self.assertEqual(state['cleanup'], 'confirmed')
            self.assertEqual((output / 'artifacts/answer.txt').read_text(), 'frozen')

    async def test_cli_evidence_inside_repository_is_not_a_workspace(self):
        evidence_root = Path(__file__).resolve().parents[1] / 'eval' / 'results'
        evidence_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='boundary-chain-', dir=evidence_root) as temp:
            output = Path(temp) / 'evidence'
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
                '--workspace', str(self.source), '--out', str(output),
                '--cmd', 'cp input.txt answer.txt', '--artifact', 'answer.txt',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)['cleanup'], 'confirmed')
            self.assertEqual((output / 'artifacts/answer.txt').read_text(), 'frozen')

    async def test_repair_execute_collect_replay_chain(self):
        (self.source / 'stats.py').write_text('def mean(values):\n    return sum(values) / (len(values) - 1)\n')
        (self.source / 'tests').mkdir()
        (self.source / 'tests/test_stats.py').write_text(
            'import unittest\nfrom stats import mean\nclass Stats(unittest.TestCase):\n'
            '    def test_mean(self): self.assertEqual(mean([2, 4]), 3)\n')
        before = snapshot_workspace(self.source, self.root / 'before.tar')
        task = await self.create('repair', workspace=self.source, artifacts=['delivery'])
        failed = await task.run('python3 -m unittest discover -s tests')
        self.assertTrue(failed.is_error)
        fixed = await task.run("sed -i 's/(len(values) - 1)/len(values)/' stats.py; "
                               'python3 -m unittest discover -s tests')
        self.assertFalse(fixed.is_error, fixed)
        delivered = await task.run('mkdir delivery; cp stats.py delivery/stats.py; '
            "python3 -c 'from stats import mean; print(mean([2,4]))' > delivery/answer.txt")
        self.assertFalse(delivered.is_error)
        manifest = await task.collect()
        self.assertEqual((task.output / 'artifacts/delivery/answer.txt').read_text().strip(), '3.0')
        self.assertEqual(len(manifest['files']), 2)
        for item in manifest['files']:
            self.assertEqual(sha256((task.output / 'artifacts' / item['path']).read_bytes()).hexdigest(), item['sha256'])
        self.assertEqual(snapshot_workspace(self.source, self.root / 'after.tar'), before)
        replay = await self.create('replay', snapshot=task.output / 'snapshot.tar')
        self.assertTrue((await replay.run('python3 -m unittest discover -s tests')).is_error)
        self.assertEqual(task.state['snapshot'], replay.state['snapshot'])

    async def test_preparation_failure_leaves_recovery_record_and_no_resources(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'CHAGENT_EXECUTOR_IMAGE': 'chagent:missing-batch-b'}):
            with self.assertRaises(RuntimeError):
                await self.create('failed', workspace=self.source)
        recovered = TaskEnvironment(self.root / 'failed')
        self.assertEqual(recovered.state['cleanup'], 'confirmed')
        self.assertEqual(recovered.state['phase_before_cleanup'], 'preparation_failed')
