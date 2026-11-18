import json
def verify(artifacts):
    try: data=json.loads((artifacts/'settlement.json').read_text())
    except (ValueError,UnicodeError): return False
    return data == {'settled_count':4,'total_cents':500,'pages':3} and all(type(v) is int for v in data.values())
