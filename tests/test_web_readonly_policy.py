import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from chagent.config import Settings
from chagent.config.dotenv import load_missing
from chagent.models.settings import ModelSettings
from chagent.web.policy import validate_web_settings, validate_web_runner


class WebReadonlyPolicyTests(unittest.TestCase):
    def test_all_required_switches_and_default_work(self):
        good=Settings(code_mode=True,allow_host_commands=True,sandbox='isolated',sandbox_writable=False)
        validate_web_settings(good)
        self.assertEqual(good.default_mode,'work')
        for args in ({'sandbox':'off'},{'sandbox':'workspace-write'},{'sandbox_writable':True},
                     {'sandbox_network':True},{'allow_host_commands':False}):
            with self.subTest(args=args),self.assertRaisesRegex(ValueError,'只读'):
                validate_web_settings(replace(good,**args))
        validate_web_settings(Settings())

    def test_actual_runner_cannot_disagree_with_config(self):
        from chagent.bootstrap import create_runner
        from chagent.models.fake import FakeModelClient
        from chagent.models.base import Model
        from chagent.tools.sandbox import Sandbox,SandboxSettings
        cfg=Settings(code_mode=True,allow_host_commands=True,sandbox='isolated',sandbox_writable=False)
        sb=Sandbox(SandboxSettings(mode='isolated',writable_workspace=False));self.addCleanup(sb.close)
        runner=create_runner(MagicMock(),Model('fake','local','fake',8192,512),FakeModelClient(),
                             code_mode=True,allow_host_commands=True,sandbox=sb,allow_workspace_writes=False)
        validate_web_runner(cfg,runner)
        runner.tool_faces['code'].registry.get('process').allowed_actions=('write',)
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
