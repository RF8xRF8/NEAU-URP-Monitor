from copy import deepcopy
import json

from monitor import diff_schedule_dedup, _fmt_schedule_changes

old = {
    "courses": [
        {
            "kch": "001",
            "kxh": "A",
            "kcm": "微积分",
            "skjs": "张三",
            "sessions": [],
            "meta": {"credit": 3, "skjs": "张三", "classroomName": "101楼201"}
        }
    ]
}

new = deepcopy(old)
# 修改 meta 内部的某个字段（使用真实字段名）
new["courses"][0]["meta"]["credit"] = 4
new["courses"][0]["meta"]["classroomName"] = "102楼301"

changes = diff_schedule_dedup(old, new)
print(json.dumps(changes, ensure_ascii=False, indent=2))
print('\nFormatted summary:\n')
print(_fmt_schedule_changes(changes))
