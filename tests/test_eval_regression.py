"""Reserved fixture oracles, drift protection and simultaneous direction checks."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from eval import regression


class RegressionTests(unittest.TestCase):
    def test_all_correct_and_incorrect_artifacts_are_distinguished(self):
        suite=json.loads((regression.SUITE/'suite.json').read_text())
        for task in suite['tasks']:
            with self.subTest(task=task['name']), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                check=runpy.run_path(str(regression.SUITE/task['name']/'verifier/check.py'))['verify']
                # Decode the controller command's write payload; never execute shell.
                import ast, shlex
                code=shlex.split(task['controls']['correct'])[2]
                tree=ast.parse(code)
                content=ast.literal_eval(tree.body[-1].value.args[0])
                (root/task['artifacts'][0]).write_text(content)
                self.assertTrue(check(root))
                wrong='{}' if task['artifacts'][0].endswith('.json') else 'def merge_intervals(intervals):\n    return []\n'
                (root/task['artifacts'][0]).write_text(wrong)
                self.assertFalse(check(root))

    def test_input_trees_do_not_contain_controller_answers(self):
        for path in regression.SUITE.glob('*/input'):
            self.assertFalse(list(path.rglob('check.py')))
            self.assertFalse(list(path.rglob('suite.json')))

    def test_source_changes_and_symlinks_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'data').write_text('one')
            before=regression.fingerprint(root)
            (root/'data').write_text('two')
            self.assertNotEqual(before,regression.fingerprint(root))
            (root/'link').symlink_to(root/'data')
            with self.assertRaises(ValueError): regression.fingerprint(root)

    def test_simultaneous_intervals_are_wider_and_direction_is_honest(self):
        from eval.report import wilson,newcombe
        def cell(success):
            return {'pass':success,'denominator':20,'rate':success/20}
        cases=[{'case':str(i),'arms':{'baseline':cell(6),'candidate':cell(16)}} for i in range(4)]
        corrected=regression.family_deltas({'cases':cases})[0]
        ordinary=newcombe(wilson(6,20),wilson(16,20),.3,.8)
        self.assertLess(corrected['interval'][0],ordinary[0])
        self.assertGreater(corrected['interval'][1],ordinary[1])
        self.assertEqual(corrected['direction'],'improved')
        cases[0]['arms']['candidate']=cell(6)
        self.assertEqual(regression.family_deltas({'cases':cases})[0]['direction'],'no_difference_observed')
        cases[0]['arms']['baseline']=cell(18);cases[0]['arms']['candidate']=cell(2)
        self.assertEqual(regression.family_deltas({'cases':cases})[0]['direction'],'regressed')

    def test_design_is_fixed_and_arms_only_change_prompt(self):
        from eval.casefile import Case
        c=Case(id='test',version=1,title='t',capability='project-work',prompt='p',authority={'boundary':'container'},budget={},graders=(),cleanup='none',sha256='a'*64)
        protocol=regression.protocol_for('test-reserved',[c])
        self.assertEqual(protocol.repeats,20)
        self.assertEqual(protocol.arms[0].model,protocol.arms[1].model)
        self.assertEqual(protocol.arms[0].prompt['content'],'')
        self.assertEqual(protocol.arms[1].prompt['content'],regression.CANDIDATE)
        self.assertEqual(protocol.budget['total_cost_usd'],8)
        docs=next(t for t in json.loads((regression.SUITE/'suite.json').read_text())['tasks'] if t['name']=='documents')
        self.assertIn('without descriptive words',docs['prompt'])
