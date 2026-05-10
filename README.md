# 东北农业大学教务监控

一个用于东北农业大学教务系统的本地监控工具，当前包含两部分：

- `monitor.py`：定时抓取课程表、本学期成绩、历史成绩和 GPA，并在有变动时记录到本地数据目录。
- `server.py`：读取本地数据，提供一个需要账号密码登录的 Web 查看端。

## 当前能力

- 课程表监控：检测新增、删除、时间地点变更。
- 本学期成绩监控：检测新出成绩、成绩修改。
- 历史成绩监控：检测补录、更正。
- GPA 监控：抓取 GPA 概览并保存。
- 本地存档：每次变更写入 `data/changes.jsonl`，原始数据按 JSON 保存。
- Web 查看：通过浏览器查看课程、成绩、GPA 和变动历史。

## 登录方式

当前登录流程已经更新为 CAS 认证，不再使用旧版验证码直连登录。

- `use_webvpn = false`：本地直连教务系统，走学校统一认证 CAS，成功后直接进入教务首页。
- `use_webvpn = true`：先通过 WebVPN 的 CAS 登录，再直接访问 WebVPN 下的教务首页。

账号和密码都使用学校统一身份认证的凭据。

## 安装

推荐使用 `uv`，也可以继续使用 `pip`。

```bash
uv sync
```

或者：

```bash
pip install -r requirements.txt
```

依赖会由 `uv` 或 `pip` 自动安装，不需要额外的 OCR 组件。

## 配置

复制 `config.example.json` 为 `config.json`，然后填写你的账号和密码。

### 必填项

- `username`：学号
- `password`：密码
- `use_webvpn`：是否通过 WebVPN 访问
- `interval`：监控间隔，单位秒
- `data_dir`：数据保存目录

### 可选项

- `notify.pushdeer_key`：PushDeer 推送密钥
- `base_url`：直连教务系统地址
- `webvpn_auth`：WebVPN CAS 地址
- `webvpn_base`：WebVPN 教务地址
- `cas_service`：WebVPN 认证服务地址

示例：

```json
{
  "username": "你的学号",
  "password": "你的密码",
  "use_webvpn": false,
  "interval": 1800,
  "data_dir": "./data",
  "notify": {
    "pushdeer_key": ""
  },
  "base_url": "https://zhjwxs.neau.edu.cn",
  "webvpn_auth": "https://authserver-443.webvpn.neau.edu.cn/authserver",
  "webvpn_base": "https://zhjwxs-443.webvpn.neau.edu.cn",
  "cas_service": "https://webvpn.neau.edu.cn/users/auth/cas"
}
```

## 运行

### 启动监控

```bash
uv run monitor.py
```

或者：

```bash
python monitor.py
```

### 启动 Web 查看端

```bash
uv run server.py
```

或者：

```bash
python server.py
```

默认地址：`http://127.0.0.1:8080`

## 数据文件

监控脚本会把数据写入 `data/`：

- `schedule.json`：最新课程表
- `this_term_scores.json`：本学期最新成绩
- `all_scores.json`：历史最新成绩
- `gpa.json`：GPA 概览
- `changes.jsonl`：每次变动的详细记录
- `archive/`：按类型归档的历史快照

## Web 查看端说明

`server.py` 只是一个本地查看界面，不负责登录教务系统。它使用 `config.json` 中的账号密码作为本地 Web 登录凭据。

主要接口包括：

- `/api/schedule`
- `/api/scores/term`
- `/api/scores/all`
- `/api/gpa`
- `/api/changes`
- `/api/status`
- `/api/history`
- `/api/field_labels`

## 常见问题

### 1. 登录失败

先确认 `config.json` 中的账号密码是否正确，再确认当前网络环境：

- 校内直连：保持 `use_webvpn = false`
- 校外访问：设置 `use_webvpn = true`

### 2. 监控没有数据

检查 `data_dir` 是否可写，以及教务系统当前是否已开放对应页面。

### 3. Web 查看端打不开

确认已经先运行了 `monitor.py`，并且 `data/` 目录里有实际数据。

## 注意事项

- 当前登录流程已切换为 CAS，不再依赖验证码 OCR。
- 通知目前仅支持 PushDeer。
- 请不要把真实学号和密码提交到公开仓库。
- 建议 `interval` 不低于 600 秒，避免请求过于频繁。
