import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from chagent.web.workspaces import validate_workspace


class WorkspaceBoundaryTests(unittest.TestCase):
    def test_web_demo_registers_only_work_tools(self):
        """The web's own assembly: one face, reads only, no host execution.

        ``create_demo_runner`` defaults to allowing writes, like every other
        caller; the browser is the one entry point that passes the switch off
        because it cannot ask a human before a file changes.
        """
        from chagent.bootstrap import create_demo_runner
        runner = create_demo_runner(MagicMock(), work_only=True, allow_workspace_writes=False)
        names = {tool.name for tool in runner.tools.registry.definitions()}
        self.assertIn("echo", names)
        self.assertFalse(names & {"write_file", "edit_file", "apply_patch", "exec_command", "process", "run_tests"})

    def test_source_parent_child_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'app'
            source.mkdir()
            (source / 'src').mkdir()
            alias = root / 'alias'
            alias.symlink_to(source, target_is_directory=True)
            workspace = root / 'workspace'
            workspace.mkdir()
            with patch('chagent.boundaries.protected_source', return_value=source):
                for path in (source, source / 'src', root, alias):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        validate_workspace(path)
                self.assertEqual(validate_workspace(workspace), workspace)
