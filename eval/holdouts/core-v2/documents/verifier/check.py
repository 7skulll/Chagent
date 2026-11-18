import json
def verify(artifacts):
    try: actual=json.loads((artifacts/"decision.json").read_text())
    except (ValueError,UnicodeError): return False
    return actual == {'as_of': '2026-11-18', 'ttl_seconds': 180, 'owner': 'runtime-team', 'timeout_action': 'bypass', 'serve_stale': False, 'max_retries': 2, 'rollback': {'error_ratio_gt': 0.02, 'window_minutes': 5, 'action': 'CACHE_ENABLED=false'}, 'sources': {'policy': 'notes/03-decision.md', 'rollout': 'notes/04-rollout.md'}}
