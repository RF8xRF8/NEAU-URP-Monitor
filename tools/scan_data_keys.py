import json
from pathlib import Path
from collections import defaultdict

ROOT = Path('data')
keys = set()
paths_by_key = defaultdict(set)

def walk(v, prefix='', source=''):
    if isinstance(v, dict):
        for k, val in v.items():
            keys.add(k)
            if source:
                paths_by_key[k].add(source)
            walk(val, f"{prefix}.{k}" if prefix else k, source)
    elif isinstance(v, list):
        for item in v:
            walk(item, prefix + '[]' if prefix else '[]', source)

for p in sorted(ROOT.glob('**/*')):
    if not p.is_file():
        continue
    if p.suffix.lower() == '.json':
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            walk(data, source=str(p).replace('\\', '/'))
        except Exception:
            pass
    elif p.suffix.lower() == '.jsonl':
        try:
            for line in p.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                walk(obj, source=str(p).replace('\\', '/'))
        except Exception:
            pass

print('TOTAL_KEYS', len(keys))
for k in sorted(keys):
    src = ', '.join(sorted(paths_by_key.get(k, []))[:3])
    print(f'{k}\t{src}')
