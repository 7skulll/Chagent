import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from chagent.config import Settings
from chagent.config.dotenv import load_missing
from chagent.models.settings import ModelSettings
from chagent.web.policy import validate_web_settings, validate_web_runner


class WebProgrammablePolicyTests(unittest.TestCase):
    def test_configured_profile_cannot_be_silently_upgraded(self):
        configured = replace(Settings(), execution_profile='configured')
        with self.assertRaisesRegex(ValueError, '实际容器'):
            validate_web_settings(configured)
        with self.assertRaisesRegex(ValueError, '实际容器'):
            validate_web_settings(replace(configured, code_mode=False))

    def test_all_required_switches_and_default_work(self):
        good=Settings(execution_profile='containerized', container_declared=True,
                      allow_host_commands=True, sandbox_network=True)
        with patch('chagent.web.policy.Path.exists', return_value=True):
            validate_web_settings(good)
        self.assertEqual(good.axes.combination, ("container", "full", "none"))
        for args in ({'sandbox':'workspace-write'},{'sandbox_writable':False},
                     {'sandbox_network':False},{'allow_host_commands':False}):
            with self.subTest(args=args),self.assertRaises(ValueError):
                validate_web_settings(replace(good,**args))

    def test_actual_runner_cannot_disagree_with_config(self):
        from chagent.bootstrap import create_runner
        from chagent.models.fake import FakeModelClient
        from chagent.models.base import Model
        from chagent.tools.sandbox import Sandbox,SandboxSettings
        cfg=Settings(execution_profile='containerized', container_declared=True,
                     allow_host_commands=True, sandbox_network=True)
        sb=Sandbox(SandboxSettings(mode='off', network=True, writable_workspace=True, containerized=True));self.addCleanup(sb.close)
        runner=create_runner(MagicMock(),Model('fake','local','fake',8192,512),FakeModelClient(),
                             code_mode=True,allow_host_commands=True,sandbox=sb,allow_workspace_writes=True,
                             unified=True,containerized=True,process_stdin=False)
        with patch('chagent.web.policy.Path.exists', return_value=True):
            validate_web_runner(cfg,runner)
        runner.tool_faces['code'].registry.get('process').allowed_actions=('write',)
        with patch('chagent.web.policy.Path.exists', return_value=True):
            with self.assertRaisesRegex(ValueError,'read/stop'):validate_web_runner(cfg,runner)

    def test_dotenv_missing_only_and_provider_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'.env'
            path.write_text('DEEPSEEK_API_KEY=synthetic-only\nCHAGENT_DATABASE_URL=postgresql:///custom\nCHAGENT_MODEL=file-model\n')
            env={'CHAGENT_MODEL':'ambient-model'}
            load_missing(path,env)
            self.assertEqual(env['CHAGENT_MODEL'],'ambient-model')
            self.assertEqual(ModelSettings.from_env(env).provider,'deepseek')
            self.assertEqual(Settings.from_env(env).database_url,'postgresql:///custom')
            env['OPENAI_API_KEY']='synthetic-second'
            with self.assertRaises(ValueError):ModelSettings.from_env(env)
            env['CHAGENT_PROVIDER']='deepseek'
            self.assertEqual(ModelSettings.from_env(env).provider,'deepseek')
            load_missing(Path(tmp)/'missing',env)
