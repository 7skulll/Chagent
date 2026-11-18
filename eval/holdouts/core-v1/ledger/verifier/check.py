import json
def verify(artifacts):
    try:
        actual=json.loads((artifacts/'summary.json').read_text())
    except (ValueError,UnicodeError): return False
    return actual == {'east': {'count': 2, 'net': '0.30'}, 'west': {'count': 3, 'net': '24.00'}}
