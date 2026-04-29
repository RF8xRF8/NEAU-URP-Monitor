import json
from pathlib import Path
p=Path('data/schedule.json')
obj=json.loads(p.read_text(encoding='utf-8'))
data=obj.get('data')
print('items:', len(data) if isinstance(data,list) else type(data))
if isinstance(data,list) and data:
    print('first keys:', list(data[0].keys()))
    has_raw_course = any('raw_course' in it for it in data if isinstance(it,dict))
    print('has_raw_course:', has_raw_course)
    for it in data:
        if isinstance(it,dict) and 'raw_course' in it and it['raw_course']:
            print('example raw_course keys:', list(it['raw_course'].keys())[:10])
            break
