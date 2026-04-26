# 东北农业大学教务监控脚本

定时抓取**课程表**、**本学期成绩**、**历史成绩**，与本地旧数据对比，有变动时推送通知。

---

## 功能

| 模块 | 说明 |
|------|------|
| 课程表监控 | 检测新增 / 删除 / 时间地点变更 |
| 本学期成绩监控 | 检测新出成绩 / 成绩修改 |
| 历史成绩监控 | 检测补录 / 更正的历史成绩 |
| 多渠道通知 | 企业微信 / Server 酱 / 钉钉 / Bark / 飞书 / Telegram |
| 本地存档 | 每次变更写入 `data/changes.jsonl`，原始数据保存为 JSON |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 如果 `ddddocr` 安装报错，先执行：`pip install onnxruntime`

### 2. 修改配置

打开 `monitor.py`，找到顶部的 `CONFIG` 字典，填写：

```python
CONFIG = {
    "username": "你的学号",       # ← 改这里
    "password": "你的密码",       # ← 改这里

    "use_webvpn": False,          # 校外访问改为 True
    "interval": 1800,             # 抓取间隔，单位秒（默认 30 分钟）
    "data_dir": "./data",         # 数据保存目录

    "notify": {
        # 至少配置一个通知渠道：
        "wecom_webhook": "",      # 企业微信群机器人 Webhook URL
        "serverchan_key": "",     # Server 酱 Turbo SendKey
        "dingtalk_webhook": "",   # 钉钉机器人 Webhook URL
        "bark_key": "",           # Bark iOS 设备 Key
        "feishu_webhook": "",     # 飞书机器人 Webhook URL
        "telegram_token": "",     # Telegram Bot Token
        "telegram_chat_id": "",   # Telegram Chat ID
    },
    ...
}
```

### 3. 运行

```bash
python monitor.py
```

---

## 通知渠道配置指引

### Server 酱（微信推送，最简单）

1. 访问 https://sct.ftqq.com/ 用微信扫码登录
2. 创建 Key，复制 SendKey
3. 填入 `"serverchan_key": "SCT..."`

### 企业微信群机器人

1. 群聊 → 添加机器人 → 复制 Webhook URL
2. 填入 `"wecom_webhook": "https://qyapi.weixin.qq.com/..."`

### Bark（iOS）

1. App Store 下载 Bark
2. 打开 App，复制设备 Key（一串字母数字）
3. 填入 `"bark_key": "AbCdEfGhIj"`

---

## WebVPN 模式（校外使用）

将 `"use_webvpn": True`，脚本会先通过东农 WebVPN 的 CAS 认证，再访问教务系统。  
账号密码与教务系统相同（统一身份认证）。

---

## 数据文件说明

```
data/
├── schedule.json            # 最新课程表
├── this_term_scores.json    # 本学期最新成绩
├── all_scores.json          # 历史最新成绩
└── changes.jsonl            # 每次变动的详细记录（JSONL 格式）
```

---

## 定时运行（可选）

### Linux / macOS — crontab

```cron
# 每 30 分钟运行一次（脚本内部已有循环，也可以直接用 python monitor.py 持续运行）
# 如果想用 crontab 调度单次运行：
*/30 * * * * cd /path/to/neau_monitor && python monitor.py --once
```

在脚本中添加 `--once` 支持：在 `main()` 尾部判断 `sys.argv`，
或者直接让脚本持续运行（推荐，已内置循环）。

### Windows — 任务计划程序

创建基本任务 → 触发器"每天/重复执行" → 操作"启动程序" → `python monitor.py`

---

## 注意事项

- 验证码使用 `ddddocr` 离线 OCR，识别率约 85%，失败会自动重试最多 5 次。
- 请勿将学号密码提交到公开仓库。
- 建议 `interval` 不低于 600 秒（10 分钟），避免频繁请求给服务器造成压力。
