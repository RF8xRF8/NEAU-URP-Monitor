#!/usr/bin/env python3
"""
课程表变动检测脚本：
1. 保存一份"参考"去重结构到 data/schedule_dedup.json
2. 每次运行时读取参考文件作为 old，读取 schedule.json 并去重作为 new
3. 对比输出变动，然后更新参考文件
"""
import json
from pathlib import Path
from monitor import build_schedule_dedup, diff_schedule_dedup, _fmt_schedule_changes

SCHEDULE_FILE = Path('data/schedule.json')
DEDUP_REFERENCE_FILE = Path('data/schedule_dedup_ref.json')

# 读取当前 schedule.json
with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
    sched_data = json.load(f)

current_flat_list = sched_data.get('data', [])
print(f"当前数据：{len(current_flat_list)} 条课次\n")

# 构建当前的去重结构
current_dedup = build_schedule_dedup(current_flat_list)
print(f"去重后：{current_dedup['total_course_count']} 门课\n")

# 读取参考去重结构（如果存在）
if DEDUP_REFERENCE_FILE.exists():
    with open(DEDUP_REFERENCE_FILE, 'r', encoding='utf-8') as f:
        old_dedup = json.load(f)
    print(f"参考去重：{old_dedup['total_course_count']} 门课\n")
    
    # 对比
    changes = diff_schedule_dedup(old_dedup, current_dedup)
    
    print(f"检测到 {len(changes)} 条变动\n")
    
    if changes:
        print("变动详情（JSON）:")
        print(json.dumps(changes, ensure_ascii=False, indent=2))
        print("\n格式化输出:")
        summary = _fmt_schedule_changes(changes)
        print(summary)
    else:
        print("（无变动）")
else:
    print("首次运行，无参考基准。已保存当前状态为参考。")
    print(f"下次运行时将对比当前的 {current_dedup['total_course_count']} 门课与参考基准。")

# 更新参考文件（无论有无变动）
with open(DEDUP_REFERENCE_FILE, 'w', encoding='utf-8') as f:
    json.dump(current_dedup, f, ensure_ascii=False, indent=2)
print(f"\n✓ 参考基准已更新到 {DEDUP_REFERENCE_FILE}")

print("\n格式化输出:")
if changes:
    summary = _fmt_schedule_changes(changes)
    print(summary)
else:
    print("（无变动）")
