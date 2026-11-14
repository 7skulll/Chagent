"""Controller-owned six-vector contract; never import candidate code as a module.

This intentionally supports a small pure Python subset for the statistics case.
AST rejection happens before *any* candidate instruction executes. In particular
candidate functions cannot reach stdout, unittest, imports, or Python internals.
Other project languages/dependencies need separately reviewed controller checks.
"""
import ast
from pathlib import Path


ALLOWED = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AugAssign, ast.Raise, ast.For, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple,
    ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.IfExp,
)
BUILTINS = {'len': len, 'sum': sum, 'max': max, 'min': min, 'abs': abs, 'range': range, 'ValueError': ValueError}


def validate(source):
    tree = ast.parse(source)
    functions = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, ast.FunctionDef) or node.name not in {'mean', 'largest'}:
            raise ValueError('only mean/largest function definitions are supported')
        if (node.decorator_list or node.returns or node.type_comment or node.type_params
                or node.args.defaults or node.args.kw_defaults or node.args.kwonlyargs
                or node.args.posonlyargs or node.args.vararg or node.args.kwarg
                or len(node.args.args) != 1 or node.args.args[0].annotation):
            raise ValueError('unsupported function signature')
        functions.append(node.name)
    if sorted(functions) != ['largest', 'mean']:
        raise ValueError('missing or duplicate functions')
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED):
            raise ValueError('unsupported Python syntax')
        if isinstance(node, ast.FunctionDef) and node not in tree.body:
            raise ValueError('nested definitions are not supported')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('reserved name')
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                or node.func.id not in BUILTINS or node.keywords):
            raise ValueError('only fixed pure builtins may be called')
        if isinstance(node, ast.Raise) and (node.cause is not None
                or not isinstance(node.exc, ast.Call) or not isinstance(node.exc.func, ast.Name)
                or node.exc.func.id != 'ValueError'):
            raise ValueError('only explicit ValueError guards are supported')
        if isinstance(node, ast.Constant) and type(node.value) not in (int, float, str, bool, type(None)):
            raise ValueError('unsupported literal')
    return tree


def main():
    path = Path('stats.py')
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('invalid source file')
    tree = validate(path.read_text())
    scope = {'__builtins__': dict(BUILTINS)}
    exec(compile(tree, '<candidate-stats>', 'exec'), scope)
    vectors = [('mean', [1, 2, 3], 2.), ('mean', [4, 8], 6.), ('mean', [7], 7.),
               ('largest', [3, 9, 4], 9), ('largest', [9, 1], 9), ('largest', [], None)]
    for function, arguments, expected in vectors:
        actual = scope[function](arguments)
        if type(actual) not in (int, float, type(None)) or actual != expected:
            raise ValueError('functional contract failed')
    print('stats-contract-v2:6/6')


if __name__ == '__main__':
    main()
