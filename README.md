# 东北农业大学教务监控

教务系统本地监控工具，支持课程表、成绩、GPA 变动检测，提供 Web 查看界面。

## 功能

- **课程表监控**：检测新增、删除、时间地点变更
- **成绩监控**：本学期成绩、历史成绩变动
- **GPA 监控**：GPA 概览抓取和保存
- **本地存档**：变动记录写入 `data/changes.jsonl`
- **Web 界面**：浏览器查看课程、成绩、GPA 和变动历史

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `config.example.json` 为 `config.json`，填写以下信息：

```json
{
  "username": "学号",
  "password": "密码",
  "use_webvpn": false,
  "interval": 1800,
  "data_dir": "./data",
  "direct_cas_auth": "https://authserver.neau.edu.cn/authserver",
  "direct_cas_service": "https://zhjwxs.neau.edu.cn/login"
}
```

**可选配置**：
- `notify.pushdeer_key`：PushDeer 推送密钥
- `base_url`：教务系统地址（默认：`https://zhjwxs.neau.edu.cn`）
- `direct_cas_auth`：直连 CAS 认证地址
- `direct_cas_service`：直连 CAS service 地址
- `use_webvpn`：是否通过 WebVPN 访问

### 3. 运行

启动监控：

```bash
python monitor.py
```

启动 Web 查看端（可选）：

```bash
python server.py
```

通过浏览器访问 `http://localhost:5000` 查看监控数据。

## 登录方式

使用学校统一身份认证（CAS）。

- `use_webvpn: false`：直连教务系统
- `use_webvpn: true`：通过 WebVPN 访问

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

- 
- 通知目前仅支持 PushDeer。
- 请不要把真实学号和密码提交到公开仓库。
- 建议 `interval` 不低于 600 秒，避免请求过于频繁。
