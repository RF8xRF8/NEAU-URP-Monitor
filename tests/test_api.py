#!/usr/bin/env python3
"""Test API endpoints by simulating what the frontend does."""

import json
from pathlib import Path

DATA_DIR = Path("./data")

def _load(name):
    """Load and unwrap JSON data (same logic as server.py)."""
    p = DATA_DIR / f'{name}.json'
    if not p.exists():
        print(f'[MISSING] {name}.json')
        return None
    try:
        obj = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(obj, dict) and 'fetch_time' in obj:
            if name in ('gpa',):
                return {k: v for k, v in obj.items() if k != 'fetch_time'}
            if 'data' in obj:
                return obj['data']
            return {k: v for k, v in obj.items() if k != 'fetch_time'}
        return obj
    except Exception as e:
        print(f'[ERROR] {name}.json: {e}')
        return None

def _load_changes():
    """Load changes from JSONL file."""
    p = DATA_DIR / "changes.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding='utf-8').strip().split('\n')
        return [json.loads(line) for line in lines if line.strip()]
    except Exception as e:
        print(f'[ERROR] changes.jsonl: {e}')
        return []

print("=" * 50)
print("API 端点数据加载测试")
print("=" * 50)

# 测试 _load
print("\n1. 检查数据文件加载:")
schedule = _load('schedule')
print(f'  schedule: {type(schedule).__name__} - {len(schedule) if isinstance(schedule, list) else "N/A"} items')

term_scores = _load('this_term_scores')
print(f'  this_term_scores: {type(term_scores).__name__} - {len(term_scores) if isinstance(term_scores, list) else "N/A"} items')

all_scores = _load('all_scores')
print(f'  all_scores: {type(all_scores).__name__} - {len(all_scores) if isinstance(all_scores, list) else "N/A"} items')

gpa = _load('gpa')
print(f'  gpa: {type(gpa).__name__}', end='')
if isinstance(gpa, dict):
    if 'data' in gpa:
        print(f' - has "data" field: {type(gpa["data"]).__name__}')
    else:
        print(f' - fields: {list(gpa.keys())}')
else:
    print()

# 测试 _load_changes
print("\n2. 检查变更记录:")
changes = _load_changes()
print(f'  changes.jsonl: list - {len(changes)} items')
if changes:
    initial_count = sum(1 for c in changes if c.get("initial"))
    non_initial_count = sum(1 for c in changes if not c.get("initial"))
    print(f'    - initial: {initial_count}')
    print(f'    - non-initial: {non_initial_count}')

# 测试 API /api/status 需要的数据
print("\n3. /api/status 端点数据:")
print(f'  schedule_cnt (should count courses): {len(schedule) if isinstance(schedule, list) else 0}')
print(f'  term_score_cnt: {len(term_scores) if isinstance(term_scores, list) else 0}')
print(f'  all_score_cnt: {len(all_scores) if isinstance(all_scores, list) else 0}')
non_initial = [c for c in changes if not c.get("initial")]
print(f'  changes_cnt (non-initial): {len(non_initial)}')

# GPA 解析
print(f'  gpa data structure:')
if isinstance(gpa, dict):
    data_list = gpa.get("data")
    if isinstance(data_list, list) and data_list:
        print(f'    - data[0] type: {type(data_list[0]).__name__}')
        first = data_list[0]
        if isinstance(first, list) and len(first) >= 5:
            print(f'    - GPA value (index 1): {first[1]}')
            print(f'    - Time (index 3): {first[3]}')
        elif isinstance(first, dict):
            print(f'    - GPA field: {first.get("gpa")}')
            print(f'    - Time field: {first.get("generated_at")}')

# 测试 API /api/changes
print("\n4. /api/changes 端点数据:")
print(f'  total changes (non-initial): {len(non_initial)}')
if non_initial:
    print(f'  first 5 changes:')
    for i, c in enumerate(non_initial[:5]):
        print(f'    [{i}] type={c.get("type")}, has_timestamp={bool(c.get("timestamp"))}')

# 测试 API /api/field_labels
print("\n5. /api/field_labels 端点:")
print(f'  文件检查: ', end='')
labels_file = Path("field_labels.json")
if labels_file.exists():
    try:
        raw = json.loads(labels_file.read_text(encoding='utf-8'))
        print(f'{len(raw)} 个字段定义')
    except Exception as e:
        print(f'读取失败: {e}')
else:
    print('文件不存在 (将使用内置标签)')

print("\n" + "=" * 50)
print("测试完成")
print("=" * 50)
