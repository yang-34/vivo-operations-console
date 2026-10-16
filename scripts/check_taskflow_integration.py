"""Offline source parity and executable-plan audit. Never contacts a vivo account."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))

from protocol.api import Account, VivoApi
from protocol.runner import PLAN_VERSION, step_plan
from protocol.source_bridge import task_catalog
from protocol.taskflow.vivo import LiveVivo


def audit(source=None):
    target = ROOT / 'server/protocol/taskflow'
    manifest = json.loads((target / 'source-manifest.json').read_text(encoding='utf-8'))
    files = []
    for name, entry in manifest.items():
        path = ROOT / name if name.startswith('scripts/') else target / name
        bundled = hashlib.sha256(path.read_text(encoding='utf-8-sig').encode()).hexdigest()
        expected = entry.get('bundled_sha256', entry['sha256'])
        item = {'file': name, 'bundled_matches': bundled == expected}
        if source:
            relative = Path('scripts') / Path(name).name if name.startswith('scripts/') else Path('apps/backend/modules/platforms') / name
            upstream = source / relative
            item['source_matches'] = upstream.exists() and hashlib.sha256(upstream.read_text(encoding='utf-8-sig').encode()).hexdigest() == entry['sha256']
        files.append(item)
    api = VivoApi(Account({'openid': 'offline-audit', 'vivotoken': 'not-real'}))
    try:
        plan = step_plan(api, 'full')
    finally:
        api.s.close()
    steps = {key for key, _, _ in plan} | {'balance.sync'}
    catalog = task_catalog()
    missing = [t['key'] for t in catalog if t['default_enabled'] and t['handler'] not in steps]
    report = {
        'adapter': LiveVivo.version, 'plan_version': PLAN_VERSION,
        'task_definitions': len(LiveVivo.tasks), 'execution_groups': len(plan),
        'catalog_entries': len(catalog),
        'legacy_sdk_aliases': [t.key for t in LiveVivo.tasks if t.key.startswith('sdk_') and not t.automatic],
        'automatic_sdk_groups': [key for key in steps if key.startswith('sdk.')],
        'missing_handlers': missing, 'files': files, 'network_requests': 0,
    }
    report['passed'] = not missing and not report['automatic_sdk_groups'] and len(steps) == len(plan) + 1 and all(all(v for k, v in f.items() if k.endswith('_matches')) for f in files)
    return report


if __name__ == '__main__':
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    report = audit(source)
    (ROOT / 'docs/taskflow-integration-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'files'}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
