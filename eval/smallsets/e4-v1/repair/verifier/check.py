import subprocess


def verify(artifacts):
    if not (artifacts / 'sequence.py').is_file():
        return False
    program = "import runpy,json; f=runpy.run_path('/artifacts/sequence.py')['triangular']; print(json.dumps([f(n) for n in [0,1,5,10]]))"
    result = subprocess.run(['/app/.venv/bin/python', '-I', '-c', program],
                            capture_output=True, timeout=3)
    return {'passed': result.returncode == 0 and result.stdout.strip() == b'[0, 1, 15, 55]',
            'details': {'contract': 'triangular-four-inputs-v1', 'exit_code': result.returncode}}
