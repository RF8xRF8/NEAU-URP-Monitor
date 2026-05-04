"""
东北农业大学教务监控 — Web 查看端
运行: python server.py
默认 http://127.0.0.1:8080
鉴权: 使用 config.json 中的学号 / 密码登录

说明：
  - 前端显示以中文为主，字段名→中文映射从 field_labels.json 读取，修改后重启生效
  - 课程列表按文件中记录顺序展示，展示时按「课序号+课程号」合并同一门课
  - 成绩区分等级制（展示等级）和百分制（展示分数）
  - 变动日志格式：[成绩/课程/GPA] 类型 变动描述
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect,
                   render_template_string, request, session, url_for)

# ── 配置加载 ──────────────────────────────────────────────────────

MONITOR_DIR = Path(__file__).parent
sys.path.insert(0, str(MONITOR_DIR))

try:
    _cfg_text = (MONITOR_DIR / "config.json").read_text(encoding="utf-8")
    _cfg = json.loads(_cfg_text)
    DATA_DIR = _cfg.get("data_dir", "./data")
    USERNAME = _cfg["username"]
    PASSWORD = _cfg["password"]
    MONITOR_INTERVAL = int(_cfg.get("interval", 1800))
except Exception as e:
    USERNAME = os.environ.get("NEAU_USERNAME")
    PASSWORD = os.environ.get("NEAU_PASSWORD")
    if not USERNAME or not PASSWORD:
        print("\n=== 错误：缺少凭据配置 ===\n请创建 config.json 或设置环境变量 NEAU_USERNAME / NEAU_PASSWORD\n")
        sys.exit(1)
    DATA_DIR = os.environ.get("NEAU_DATA_DIR", "./data")
    MONITOR_INTERVAL = int(os.environ.get("NEAU_INTERVAL", "1800"))

# ── 字段标签（从外部文件读取，方便修改） ─────────────────────────

def _load_field_labels() -> dict:
    label_file = MONITOR_DIR / "field_labels.json"
    if label_file.exists():
        try:
            raw = json.loads(label_file.read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if k != "说明"}
        except Exception:
            pass
    return {}

FIELD_LABELS = _load_field_labels()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())
# 缩短会话有效期为 1 小时，超过需重新登录
app.permanent_session_lifetime = timedelta(hours=1)

# ── 学期配置 ─────────────────────────────────────────────────────
SEMESTER_START = datetime(2026, 3, 2)   # 修改为实际的学期开始日期（周一）

def get_current_week() -> int:
    days_passed = (datetime.now() - SEMESTER_START).days
    return max(1, days_passed // 7 + 1)


def _extract_week_list(skzc: str) -> list[int]:
    """解析周次字符串，返回有课的周次列表。"""
    src = str(skzc or "").strip()
    if not src:
        return []
    if re.fullmatch(r"[01]+", src):
        return [i + 1 for i, c in enumerate(src) if c == "1"]
    weeks = set()
    for a, b in re.findall(r"(\d+)\s*[-~至]\s*(\d+)", src):
        weeks.update(range(int(a), int(b) + 1))
    for w in re.findall(r"(?<!\d)(\d+)(?!\d)", src):
        weeks.add(int(w))
    return sorted(w for w in weeks if w > 0)


def _is_course_in_week(skzc: str, week: int) -> bool:
    return week in _extract_week_list(skzc)


def _detect_max_week(schedule_data: dict) -> int:
    mx = 0
    for c in (schedule_data or {}).get("courses", []):
        for s in (c.get("sessions") or []):
            ws = _extract_week_list(s.get("skzc", ""))
            if ws:
                mx = max(mx, ws[-1])
    return max(mx, get_current_week())


def build_schedule_dedup_from_list(raw_list: list) -> dict:
    """从平铺的课程条目列表构建去重后的课程结构（按课序号+课程号合并）。"""
    groups = {}
    for item in raw_list or []:
      kch = str(item.get("kch") or item.get("courseNumber") or "").strip()
      kxh = str(item.get("kxh") or item.get("coureSequenceNumber") or "").strip()
      key = f"{kxh}_{kch}"
      if key not in groups:
        first_meta = {k: v for k, v in (item or {}).items() if k not in ("skxq", "skjc", "skzc", "jxdd", "raw_session")}
        groups[key] = {
          "kch": kch,
          "kxh": kxh,
          "kcm": str(item.get("kcm") or item.get("courseName") or ""),
          "skjs": str(item.get("skjs") or item.get("attendClassTeacher") or "").strip(),
          "sessions": [],
          "meta": first_meta,
        }
      groups[key]["sessions"].append({
        "skxq": str(item.get("skxq") or item.get("classDay") or ""),
        "skjc": str(item.get("skjc") or item.get("section") or ""),
        "skzc": str(item.get("skzc") or item.get("weekRange") or ""),
        "jxdd": str(item.get("jxdd") or item.get("classroom") or item.get("classroomName") or ""),
        "raw_session": item.get("raw_session"),
      })
    courses = list(groups.values())
    return {"total_course_count": len(courses), "courses": courses}


# ── 工具函数 ─────────────────────────────────────────────────────

def _load(name: str):
    p = Path(DATA_DIR) / f"{name}.json"
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        # 如果是我们新格式：dict 包含 fetch_time
        if isinstance(obj, dict) and 'fetch_time' in obj:
            # 对于 GPA 和其他原始 dict 响应，返回去掉 fetch_time 的整个 dict
            if name in ('gpa',):
                return {k: v for k, v in obj.items() if k != 'fetch_time'}
            # 对于有 'data' 字段的格式（schedule, scores），返回 data 内容
            if 'data' in obj:
                return obj['data']
            # 其他情况返回去掉 fetch_time 的 dict
            return {k: v for k, v in obj.items() if k != 'fetch_time'}
        return obj
    except Exception:
        return None


def _load_changes() -> list:
    p = Path(DATA_DIR) / "changes.jsonl"
    if not p.exists():
        return []
    lines = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except Exception:
                pass
    return list(reversed(lines))


def _list_history(data_type: str = "") -> list[dict]:
    root = Path(DATA_DIR) / "archive"
    if not root.exists():
        return []
    types = [data_type] if data_type else [p.name for p in root.iterdir() if p.is_dir()]
    items: list[dict] = []
    for t in types:
        t_dir = root / t
        if not t_dir.is_dir():
            continue
        for p in t_dir.glob("*.json"):
            stem = p.stem
            display_time = stem
            try:
                display_time = datetime.strptime(stem, "%Y%m%d_%H%M%S_%f").strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            count = None
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                count = len(payload) if isinstance(payload, (list, dict)) else None
            except Exception:
                pass
            items.append({"type": t, "file": p.name, "time": display_time,
                          "count": count, "size": p.stat().st_size})
    items.sort(key=lambda x: (x.get("time") or "", x.get("file") or ""), reverse=True)
    return items


def _monitor_status() -> dict:
    log_path = MONITOR_DIR / "monitor.log"
    if not log_path.exists():
        return {"last_run": "尚未运行", "next_run": "未知", "lines": []}
    try:
        with log_path.open(encoding="utf-8") as f:
            all_lines = f.readlines()
        last_lines = [l.rstrip() for l in all_lines[-30:]]
        last_run = "未知"
        last_dt = None
        for line in reversed(all_lines):
            if "开始抓取" in line:
                ts = line[:23]
                last_run = ts
                try:
                    last_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
                except Exception:
                    pass
                break
        next_run = "未知"
        if last_dt is not None:
            next_dt = last_dt + timedelta(seconds=MONITOR_INTERVAL)
            now = datetime.now()
            if next_dt <= now:
                next_run = "即将执行"
            else:
                delta = next_dt - now
                mins, secs = int(delta.total_seconds() // 60), int(delta.total_seconds() % 60)
                next_run = f"{next_dt.strftime('%H:%M:%S')}（{mins}分{secs}秒后）"
        return {"last_run": last_run, "next_run": next_run, "lines": last_lines}
    except Exception:
        return {"last_run": "读取失败", "next_run": "未知", "lines": []}


# ── 鉴权 ─────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = ""
    if request.method == "POST":
        import time # 确保顶部导入了 time
        u, p = request.form.get("username", "").strip(), request.form.get("password", "").strip()
        if u == USERNAME and p == PASSWORD:
            session.permanent = True
            session["logged_in"] = True
            session["user"] = u
            return redirect(url_for("dashboard"))
        
        # 密码错误时，强制延迟 2 秒响应，防御暴力破解
        time.sleep(2)
        error = "Authentication Failed."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ── 页面路由 ─────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    return render_template_string(MAIN_HTML, user=session.get("user", ""))


# ── API ──────────────────────────────────────────────────────────

@app.route("/api/schedule")
@login_required
def api_schedule():
    """返回课程表数据：平铺列表用于列表视图，去重结构用于网格视图。"""
    flat_schedule = _load("schedule")  # 加载平铺的课程列表
    if flat_schedule is None:
        flat_schedule = []
    
    # 从平铺列表构建去重结构
    dedup = build_schedule_dedup_from_list(flat_schedule if isinstance(flat_schedule, list) else [])
    
    current_week = get_current_week()
    max_week = _detect_max_week(dedup)

    return jsonify({
        "ok": True,
        "current_week": current_week,
        "max_week": max_week,
        "semester_start": SEMESTER_START.strftime("%Y-%m-%d"),
        "data": dedup or {"total_course_count": 0, "courses": []},
        "flat": flat_schedule if isinstance(flat_schedule, list) else [],
    })


@app.route("/api/scores/term")
@login_required
def api_scores_term():
    data = _load("this_term_scores")
    return jsonify({"ok": True, "data": data or [], "count": len(data or [])})


@app.route("/api/scores/all")
@login_required
def api_scores_all():
    data = _load("all_scores")
    return jsonify({"ok": True, "data": data or [], "count": len(data or [])})


@app.route("/api/gpa")
@login_required
def api_gpa():
    gpa = _load("gpa")
    return jsonify({"ok": True, "data": gpa or {}})


@app.route("/api/changes")
@login_required
def api_changes():
    changes = _load_changes()
    t = request.args.get("type", "")
    if t:
        changes = [c for c in changes if c.get("type") == t]
    # 不计入 initial（首次初始化）的变动次数
    non_initial = [c for c in changes if not c.get("initial")]
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 30))
    total = len(non_initial)
    sliced = non_initial[(page - 1) * limit: page * limit]
    return jsonify({"ok": True, "data": sliced, "total": total, "page": page})


@app.route("/api/status")
@login_required
def api_status():
    st = _monitor_status()
    schedule = _load("schedule")
    term_score = _load("this_term_scores")
    all_score = _load("all_scores")
    gpa_info = _load("gpa")
    changes = _load_changes()
    non_initial_cnt = sum(1 for c in changes if not c.get("initial"))

    # schedule 现在为平铺列表
    if isinstance(schedule, list):
        schedule_cnt = build_schedule_dedup_from_list(schedule).get("total_course_count", 0)
    else:
        schedule_cnt = 0

    # GPA 值提取（新版原始 JSON 格式：{"data":[[name, gpa, class_rank, time, grade_rank]]}）
    gpa_val = "-"
    gpa_time = "-"
    if isinstance(gpa_info, dict):
        data_list = gpa_info.get("data")
        if isinstance(data_list, list) and data_list:
            first = data_list[0]
            if isinstance(first, list) and len(first) >= 5:
                gpa_val = str(first[1] if first[1] is not None else "-")
                gpa_time = str(first[3] or "-")
            elif isinstance(first, dict):
                gpa_val = str(first.get("gpa") or first.get("绩点") or "-")
                gpa_time = str(first.get("generated_at") or first.get("生成时间") or "-")
        else:
            gpa_val = str(gpa_info.get("gpa") or gpa_info.get("绩点") or "-")
            gpa_time = str(gpa_info.get("generated_at") or gpa_info.get("生成时间") or "-")

    return jsonify({
        "ok": True,
        "last_run": st["last_run"],
        "next_run": st.get("next_run", "未知"),
        "schedule_cnt": schedule_cnt,
        "term_score_cnt": len(term_score or []),
        "all_score_cnt": len(all_score or []),
        "gpa": gpa_val,
        "gpa_time": gpa_time,
        "changes_cnt": non_initial_cnt,
    })


@app.route("/api/history")
@login_required
def api_history():
    data_type = request.args.get("type", "").strip()
    file_name = request.args.get("file", "").strip()
    
    if file_name:
        if not data_type: return jsonify({"ok": False, "error": "缺少 type 参数"}), 400
        
        # 严格防御：检查参数中是否包含非法路径字符
        if any(char in data_type or char in file_name for char in ("..", "/", "\\")):
            return jsonify({"ok": False, "error": "检测到非法路径访问"}), 403
            
        # 构建路径并强制解析为绝对路径
        archive_dir = (Path(DATA_DIR) / "archive").resolve()
        target = (archive_dir / data_type / file_name).resolve()
        
        # 安全校验：检查最终解析出的 target 是否仍然在 archive_dir 目录之下
        if not str(target).startswith(str(archive_dir)):
            return jsonify({"ok": False, "error": "越权访问被拒绝"}), 403

        if not target.exists(): return jsonify({"ok": False, "error": "文件不存在"}), 404
        try: payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception: payload = None
        return jsonify({"ok": True, "type": data_type, "file": file_name, "data": payload})
        
    rows = _list_history(data_type)
    return jsonify({"ok": True, "data": rows, "count": len(rows)})


@app.route("/api/field_labels")
@login_required
def api_field_labels():
    """返回字段标签映射，供前端使用。"""
    return jsonify({"ok": True, "data": FIELD_LABELS})


# ── HTML 模板 ─────────────────────────────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>教务监控 · 登录</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0f1117;--paper:#f5f0e8;--accent:#c8432b;--accent2:#2b5fc8;--border:#d4c9b0;--mono:'JetBrains Mono',monospace;--serif:'Noto Serif SC',serif}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--paper);font-family:var(--serif)}
.wrap{width:100%;max-width:400px;padding:24px}
.card{background:#fff;border:1.5px solid var(--border);padding:44px 40px 40px;box-shadow:0 8px 32px rgba(15,17,23,.12);position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.school{font-size:.7rem;letter-spacing:.15em;color:#888;font-family:var(--mono);margin-bottom:8px}
h1{font-size:1.6rem;font-weight:700;color:var(--ink);line-height:1.2;margin-bottom:32px}
h1 span{color:var(--accent)}
label{display:block;font-size:.75rem;color:#666;font-family:var(--mono);margin-bottom:6px}
input{width:100%;border:1.5px solid var(--border);padding:11px 14px;font-size:.95rem;font-family:var(--mono);color:var(--ink);background:#faf8f4;outline:none;transition:border-color .2s;margin-bottom:20px;border-radius:2px}
input:focus{border-color:var(--accent2)}
.btn{width:100%;padding:12px;background:var(--ink);color:#fff;border:none;font-family:var(--mono);font-size:.9rem;cursor:pointer;border-radius:2px;transition:background .2s}
.btn:hover{background:var(--accent)}
.err{background:#fff0ee;border:1px solid #f5b8b0;color:var(--accent);padding:10px 14px;font-size:.82rem;font-family:var(--mono);margin-bottom:20px;border-radius:2px}
.foot{margin-top:28px;text-align:center;font-size:.72rem;color:#aaa;font-family:var(--mono)}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="school">Northeast Agricultural University</div>
    <h1>教务<span>监控</span><br>数据中心</h1>
    {% if error %}<div class="err">⚠ {{ error }}</div>{% endif %}
    <form method="post">
      <label>学号</label>
      <input name="username" placeholder="输入学号" autocomplete="username" autofocus>
      <label>密码</label>
      <input name="password" type="password" placeholder="输入密码" autocomplete="current-password">
      <button class="btn" type="submit">进入系统 →</button>
    </form>
    <div class="foot">仅限本人使用 · 数据来自本地缓存</div>
  </div>
</div>
</body>
</html>"""


MAIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>教务监控 · 数据中心</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#0f1117;--paper:#f5f0e8;--card:#fff;
  --accent:#c8432b;--accent2:#2b5fc8;--green:#1a7a4a;
  --border:#ddd5bf;--border-light:#ede8de;
  --muted:#888;--mono:'JetBrains Mono',monospace;--serif:'Noto Serif SC',serif;
  --sidebar:240px;
}
html,body{height:100%;background:var(--paper);font-family:var(--serif);color:var(--ink)}
.layout{display:flex;height:100vh;overflow:hidden}
.sidebar{width:var(--sidebar);flex-shrink:0;background:var(--ink);display:flex;flex-direction:column;overflow:hidden}
.sidebar-mask{display:none}
.logo{padding:28px 24px 20px;border-bottom:1px solid rgba(255,255,255,.08)}
.logo-school{font-size:.6rem;letter-spacing:.18em;color:rgba(255,255,255,.4);font-family:var(--mono);margin-bottom:4px}
.logo-name{font-size:1.05rem;color:#fff;font-weight:600;line-height:1.3}
.logo-name span{color:#e07060}
.nav{flex:1;padding:16px 0;overflow-y:auto}
.nav-section{padding:8px 20px 4px;font-size:.6rem;letter-spacing:.15em;color:rgba(255,255,255,.3);font-family:var(--mono)}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 20px;color:rgba(255,255,255,.65);font-size:.82rem;cursor:pointer;transition:all .18s;border-left:2px solid transparent;font-family:var(--mono)}
.nav-item:hover{background:rgba(255,255,255,.06);color:#fff}
.nav-item.active{background:rgba(200,67,43,.15);color:#e07060;border-left-color:#e07060}
.nav-item .icon{width:16px;text-align:center;opacity:.7;flex-shrink:0}
.sidebar-foot{padding:16px 20px;border-top:1px solid rgba(255,255,255,.08)}
.user-badge{font-size:.72rem;color:rgba(255,255,255,.4);font-family:var(--mono)}
.logout-btn{display:block;margin-top:8px;font-size:.7rem;color:rgba(255,255,255,.3);font-family:var(--mono);cursor:pointer;text-decoration:none;transition:color .2s}
.logout-btn:hover{color:#e07060}
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column}
.topbar{padding:20px 32px;border-bottom:1px solid var(--border);background:var(--card);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.topbar-left{display:flex;align-items:flex-start;gap:12px}
.topbar-actions{display:flex;align-items:center;gap:12px}
.mobile-menu-btn{display:none;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid var(--border);background:transparent;border-radius:2px;font-size:1rem;line-height:1;color:var(--ink);cursor:pointer;flex-shrink:0}
.page-title{font-size:1.2rem;font-weight:700}
.page-subtitle{font-size:.75rem;color:var(--muted);font-family:var(--mono);margin-top:2px}
.status-pill{display:flex;align-items:center;gap:6px;font-size:.72rem;font-family:var(--mono);color:var(--muted);padding:6px 12px;background:var(--paper);border:1px solid var(--border);border-radius:20px}
.status-dot{width:7px;height:7px;border-radius:50%;background:#aaa}
.status-dot.ok{background:#1a7a4a;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.content{flex:1;padding:28px 32px}
.stats-row{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:28px}
.stat-card{background:var(--card);border:1.5px solid var(--border);padding:20px;position:relative;overflow:hidden}
.stat-card.clickable{cursor:pointer}
.stat-card.clickable:hover{transform:translateY(-2px);box-shadow:0 8px 18px rgba(15,17,23,.08)}
.stat-card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--accent)}
.stat-card:nth-child(2)::after{background:var(--accent2)}
.stat-card:nth-child(3)::after{background:var(--green)}
.stat-card:nth-child(4)::after{background:#9b6b2b}
.stat-card:nth-child(5)::after{background:#6b4fb3}
.stat-label{font-size:.65rem;letter-spacing:.1em;color:var(--muted);font-family:var(--mono)}
.stat-num{font-size:2rem;font-weight:700;font-family:var(--mono);line-height:1.2;margin-top:4px}
.stat-sub{font-size:.7rem;color:var(--muted);font-family:var(--mono);margin-top:4px}
.panel{background:var(--card);border:1.5px solid var(--border)}
.panel-head{padding:16px 20px;border-bottom:1px solid var(--border-light);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.panel-title{font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:8px}
.panel-title .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
.filter-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.filter-btn{font-size:.7rem;font-family:var(--mono);padding:4px 12px;border:1px solid var(--border);background:transparent;cursor:pointer;color:var(--muted);transition:all .18s;border-radius:2px}
.filter-btn.active,.filter-btn:hover{background:var(--ink);color:#fff;border-color:var(--ink)}
.search-box{font-size:.75rem;font-family:var(--mono);padding:5px 10px;border:1px solid var(--border);background:var(--paper);color:var(--ink);outline:none;width:180px;border-radius:2px}
.search-box:focus{border-color:var(--accent2)}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
thead tr{background:var(--paper)}
th{padding:10px 16px;text-align:left;font-size:.65rem;letter-spacing:.1em;color:var(--muted);font-family:var(--mono);font-weight:500;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:11px 16px;border-bottom:1px solid var(--border-light);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,0,0,.018)}
.click-row{cursor:pointer}
.click-row:hover td{background:rgba(43,95,200,.08)}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:2px;font-size:.65rem;font-family:var(--mono);font-weight:500}
.badge-add{background:#e8f5ee;color:var(--green)}
.badge-del{background:#fdf0ee;color:var(--accent)}
.badge-chg{background:#eef3fd;color:var(--accent2)}
.score-num{font-family:var(--mono);font-weight:600}
.score-exc{color:var(--green)}
.score-good{color:#2563eb}
.score-mid{color:#b97a00}
.score-pass{color:#cc5500}
.score-fail{color:#dd001b}
.hist-changed{background:rgba(208,36,36,.08);border-radius:3px;padding:1px 4px}
/* 课表 */
.kb-header{padding:16px 20px;background:#f5f5f5;border-bottom:2px solid var(--border);display:flex;align-items:center;justify-content:space-between;font-size:.9rem}
.week-info{font-weight:600;color:var(--ink)}
.week-nav{display:flex;align-items:center;gap:8px}
.week-nav-btn{font-size:.72rem;font-family:var(--mono);padding:5px 10px;border:1px solid var(--border);background:#fff;cursor:pointer;border-radius:2px;color:var(--ink)}
.week-nav-btn:hover{background:var(--ink);color:#fff}
.week-nav-btn:disabled{opacity:.35;cursor:default}
.kb-wrap{overflow-x:auto;padding:4px}
.kb{width:100%;border-collapse:collapse;background:#fff;table-layout:fixed}
.kb thead{background:#fafafa}
.kb th{padding:8px 6px;border:1px solid var(--border);text-align:center;font-size:.75rem;white-space:nowrap}
.kb th:not(.time-col){width:calc((100% - 45px) / 7)}
.kb th.time-col{background:#f5f5f5;width:45px}
.date-small{display:block;font-size:.65rem;color:var(--muted);font-weight:400;margin-top:2px}
.kb td{padding:6px;border:1px solid var(--border);text-align:center;height:65px;vertical-align:top;overflow:hidden}
.kb td.hdr{background:#f5f5f5;font-weight:600;color:var(--ink);width:45px;padding:8px 0;font-size:.75rem}
.kb td.empty{background:#fafafa}
.kb td.course-container{padding:2px}
.kb tr.slot-gap td{border-top:8px solid var(--card)}
.course-cell{background:linear-gradient(135deg,#eef3fd,#e6eeff);border-left:3px solid var(--accent2);padding:5px 7px;height:100%;font-size:.7rem;line-height:1.4;border-radius:2px;cursor:pointer}
.course-cell.c2{background:linear-gradient(135deg,#e8f5ee,#dff2e8);border-left-color:var(--green)}
.course-cell.c3{background:linear-gradient(135deg,#fdf5e8,#faeedd);border-left-color:#c8902b}
.course-cell.c4{background:linear-gradient(135deg,#fdf0ee,#fae6e3);border-left-color:var(--accent)}
.course-cell.c5{background:linear-gradient(135deg,#f5e8fd,#eeddf5);border-left-color:#8b2bc8}
.cc-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cc-sub{color:var(--muted);font-size:.62rem;font-family:var(--mono);margin-top:1px}
/* 变动时间线 */
.timeline{padding:20px}
.tl-item{display:flex;gap:16px;margin-bottom:20px;cursor:pointer}
.tl-item:hover .tl-card{border-color:var(--accent2)}
.tl-line{display:flex;flex-direction:column;align-items:center;flex-shrink:0}
.tl-dot{width:10px;height:10px;border-radius:50%;background:var(--accent);border:2px solid var(--card);box-shadow:0 0 0 2px var(--accent);flex-shrink:0;margin-top:3px}
.tl-dot.score{background:var(--accent2);box-shadow:0 0 0 2px var(--accent2)}
.tl-dot.sch{background:#9b6b2b;box-shadow:0 0 0 2px #9b6b2b}
.tl-dot.gpa{background:#6b4fb3;box-shadow:0 0 0 2px #6b4fb3}
.tl-vline{width:1px;background:var(--border);flex:1;margin-top:4px}
.tl-body{flex:1}
.tl-time{font-size:.65rem;font-family:var(--mono);color:var(--muted);margin-bottom:6px}
.tl-card{background:var(--paper);border:1px solid var(--border);padding:10px 14px;border-radius:2px;transition:border-color .15s}
.tl-log{font-size:.78rem;line-height:1.7;font-family:var(--mono)}
.tl-log .tag{font-weight:600;color:var(--accent2)}
.tl-log .arr{color:var(--muted)}
/* 弹窗 */
.modal-mask{position:fixed;inset:0;background:rgba(15,17,23,.48);display:none;align-items:center;justify-content:center;z-index:999}
.modal-mask.open{display:flex}
.modal-card{width:min(92vw,700px);max-height:min(88vh,900px);background:#fff;border:1.5px solid var(--border);box-shadow:0 8px 40px rgba(0,0,0,.18);display:flex;flex-direction:column}
.modal-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-size:.9rem;font-weight:700}
.modal-close{border:1px solid var(--border);background:transparent;padding:2px 8px;cursor:pointer;font-family:var(--mono)}
.modal-body{padding:16px 18px;font-size:.82rem;line-height:1.8;overflow:auto}
.kv{display:flex;justify-content:space-between;gap:16px;border-bottom:1px dashed var(--border-light);padding:6px 0}
.kv:last-child{border-bottom:none}
.kv .kl{flex:0 0 180px;white-space:nowrap;color:var(--muted);font-family:var(--mono);font-size:.75rem}
.kv .kv2{flex:1;word-break:break-word}
.raw-json{margin-top:12px;background:#f7f4ed;border:1px solid var(--border-light);padding:10px}
.raw-json summary{cursor:pointer;font-family:var(--mono);font-size:.72rem;color:var(--muted);margin-bottom:8px}
.raw-json pre{margin:0;white-space:pre;font-family:var(--mono);font-size:.68rem;line-height:1.5;max-height:300px;overflow:auto}
/* 通用 */
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-icon{font-size:2rem;margin-bottom:12px}
.empty-text{font-size:.85rem;font-family:var(--mono)}
.loading{display:flex;align-items:center;justify-content:center;padding:60px;gap:12px;color:var(--muted);font-family:var(--mono);font-size:.8rem}
.spinner{width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.pager{display:flex;justify-content:center;gap:6px;padding:16px}
.pager-btn{font-size:.72rem;font-family:var(--mono);padding:5px 12px;border:1px solid var(--border);background:transparent;cursor:pointer;color:var(--ink);transition:all .18s;border-radius:2px}
.pager-btn:hover,.pager-btn.active{background:var(--ink);color:#fff;border-color:var(--ink)}
.pager-btn:disabled{opacity:.35;cursor:default}
.view{display:none}
.view.active{display:block}
.ck-inline{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;color:var(--muted);font-family:var(--mono)}
.ck-inline input{accent-color:var(--accent2)}
.history-table td,.history-table th{white-space:nowrap}
/* 课程列表中的多时段 */
.time-slots{display:flex;flex-direction:column;gap:6px}
.time-slot{font-size:.75rem;font-family:var(--mono);white-space:normal;word-break:break-word;line-height:1.35}
.time-slot-week{font-size:.68rem;color:var(--muted);font-family:var(--mono);line-height:1.25}
.slot-group{border-bottom:1px dashed var(--border-light);padding:6px 0}
.slot-group:last-child{border-bottom:none}
.schedule-list-table{table-layout:fixed;width:100%}
.schedule-list-table th:nth-child(1),.schedule-list-table td:nth-child(1){width:56px}
.schedule-list-table th:nth-child(2),.schedule-list-table td:nth-child(2){width:92px}
.schedule-list-table th:nth-child(3),.schedule-list-table td:nth-child(3){width:92px}
.schedule-list-table th:nth-child(4),.schedule-list-table td:nth-child(4){width:220px}
.schedule-list-table th:nth-child(5),.schedule-list-table td:nth-child(5){width:120px}
.schedule-list-table th:nth-child(6),.schedule-list-table td:nth-child(6){width:auto}
@media(max-width:768px){
  :root{--sidebar:0px}
  .sidebar{position:fixed;left:-240px;top:0;bottom:0;width:240px;z-index:100;transition:left .25s}
  .sidebar.open{left:0}
  .sidebar-mask{display:none;position:fixed;inset:0;background:rgba(15,17,23,.45);z-index:90}
  .sidebar-mask.open{display:block}
  .mobile-menu-btn{display:inline-flex}
  .topbar{padding:12px 14px;gap:10px}
  .topbar-left{align-items:center;gap:8px;min-width:0}
  .topbar-actions{gap:8px}
  .topbar-actions .status-pill{max-width:56vw;overflow:hidden}
  .topbar-actions #status-text{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
  .page-title{font-size:1rem;line-height:1.25}
  .page-subtitle{display:none}
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .content{padding:12px}
  .panel-head{padding:12px 12px}
  th,td{padding:9px 10px}
  .kb-header{padding:10px 12px;display:block}
  .week-nav{margin-top:8px;flex-wrap:wrap}
  .timeline{padding:12px}
  .tl-item{gap:10px;margin-bottom:14px}
  .modal-card{width:96vw;max-height:92vh}
  .modal-head{padding:10px 12px}
  .modal-body{padding:12px;font-size:.78rem}
  .kv{display:block}
  .kv .kl{display:block;margin-bottom:4px;white-space:normal}
  #schedule-list .tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .schedule-list-table{table-layout:auto;min-width:760px}
  .schedule-list-table th,.schedule-list-table td{white-space:normal}
}
@media(max-width:460px){
  .stats-row{grid-template-columns:1fr}
  .topbar-actions .status-pill{display:none}
  .filter-row{width:100%}
  .search-box{width:100%}
  .week-nav-btn{padding:5px 8px;font-size:.68rem}
}
</style>
</head>
<body>
<div class="layout">

<div class="sidebar" id="sidebar">
  <div class="logo">
    <div class="logo-school">NEAU · Academic Monitor</div>
    <div class="logo-name">教务<span>监控</span><br>数据中心</div>
  </div>
  <nav class="nav">
    <div class="nav-section">数据总览</div>
    <div class="nav-item active" onclick="showView('overview')" id="nav-overview"><span class="icon">◈</span>概览仪表盘</div>
    <div class="nav-section">当前数据</div>
    <div class="nav-item" onclick="showView('schedule')" id="nav-schedule"><span class="icon">▦</span>本学期课程表</div>
    <div class="nav-item" onclick="showView('scores-term')" id="nav-scores-term"><span class="icon">◉</span>本学期成绩</div>
    <div class="nav-item" onclick="showView('scores-all')" id="nav-scores-all"><span class="icon">≡</span>历史全部成绩</div>
    <div class="nav-section">变动记录</div>
    <div class="nav-item" onclick="showView('changes')" id="nav-changes"><span class="icon">◷</span>变动日志</div>
    <div class="nav-item" onclick="showView('history')" id="nav-history"><span class="icon">◫</span>历史数据</div>
  </nav>
  <div class="sidebar-foot">
    <div class="user-badge">已登录：{{ user }}</div>
    <a class="logout-btn" href="/logout">退出登录</a>
  </div>
</div>

<div class="sidebar-mask" id="sidebar-mask" onclick="closeSidebar()"></div>

<div class="main">
  <div class="topbar">
    <div class="topbar-left">
      <button class="mobile-menu-btn" type="button" onclick="toggleSidebar()" aria-label="打开导航">☰</button>
      <div>
        <div class="page-title" id="topbar-title">概览仪表盘</div>
        <div class="page-subtitle" id="topbar-sub">教务系统数据概览</div>
      </div>
    </div>
    <div class="topbar-actions">
      <div class="status-pill">
        <div class="status-dot ok" id="status-dot"></div>
        <span id="status-text" style="font-family:var(--mono);font-size:.72rem">加载中…</span>
      </div>
      <button onclick="refreshCurrent()" style="font-size:.72rem;font-family:var(--mono);padding:6px 14px;border:1px solid var(--border);background:transparent;cursor:pointer;border-radius:2px">↻ 刷新</button>
    </div>
  </div>

  <div class="content">

    <!-- 概览 -->
    <div class="view active" id="view-overview">
      <div class="stats-row" id="stats-row">
        <div class="stat-card clickable" onclick="showView('schedule')"><div class="stat-label">课程总数</div><div class="stat-num" id="st-sch">—</div><div class="stat-sub">点击查看课程表</div></div>
        <div class="stat-card clickable" onclick="showView('scores-term')"><div class="stat-label">本学期成绩</div><div class="stat-num" id="st-ts">—</div><div class="stat-sub">点击查看详情</div></div>
        <div class="stat-card clickable" onclick="showView('scores-all')"><div class="stat-label">历史成绩</div><div class="stat-num" id="st-as">—</div><div class="stat-sub">点击查看详情</div></div>
        <div class="stat-card clickable" onclick="showView('changes')"><div class="stat-label">变动次数</div><div class="stat-num" id="st-ch">—</div><div class="stat-sub">点击查看日志</div></div>
        <div class="stat-card clickable" onclick="openGpaModal()"><div class="stat-label">实时 GPA</div><div class="stat-num" id="st-gpa">—</div><div class="stat-sub">点击查看详情</div></div>
      </div>
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot"></div>最近变动</div>
          <span style="font-size:.7rem;font-family:var(--mono);color:var(--muted)">最新 5 条</span>
        </div>
        <div id="overview-changes"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

    <!-- 课程表 -->
    <div class="view" id="view-schedule">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:var(--accent2)"></div>本学期课程表</div>
          <div class="filter-row">
            <button class="filter-btn active" onclick="setSchedView('grid',this)">课表视图</button>
            <button class="filter-btn" onclick="setSchedView('list',this)">列表视图</button>
          </div>
        </div>
        <div id="schedule-grid"><div class="loading"><div class="spinner"></div>加载中…</div></div>
        <div id="schedule-list" style="display:none"></div>
      </div>
    </div>

    <!-- 本学期成绩 -->
    <div class="view" id="view-scores-term">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:var(--green)"></div>本学期成绩</div>
          <div class="filter-row">
            <input class="search-box" placeholder="搜索课程名 / 课程号…" oninput="filterScores('term',this.value)">
          </div>
        </div>
        <!-- 本学期成绩统计信息区域 -->
        <!-- TODO: 待补充本学期成绩统计字段后启用
             可能的字段：课程平均分、课程最高分、课程最低分等
             需结合实际抓取到的数据结构确定对应 JSON key 后修改此处 -->
        <div id="term-stats" style="padding:12px 16px;background:#f9f6f0;border-bottom:1px solid var(--border-light);font-size:.78rem;color:var(--muted);font-family:var(--mono);display:none">
          <!-- 待接入统计数据后展示 -->
        </div>
        <div class="tbl-wrap" id="scores-term-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

    <!-- 历史成绩 -->
    <div class="view" id="view-scores-all">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:#9b6b2b"></div>历史全部成绩</div>
          <div class="filter-row">
            <input class="search-box" placeholder="搜索课程名 / 课程号…" oninput="filterScores('all',this.value)">
          </div>
        </div>
        <div class="tbl-wrap" id="scores-all-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

    <!-- 变动日志 -->
    <div class="view" id="view-changes">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:#9b6b2b"></div>变动日志</div>
          <div class="filter-row">
            <button class="filter-btn active" onclick="filterChanges('',this)">全部</button>
            <button class="filter-btn" onclick="filterChanges('schedule',this)">课程表</button>
            <button class="filter-btn" onclick="filterChanges('this_term_scores',this)">本学期成绩</button>
            <button class="filter-btn" onclick="filterChanges('all_scores',this)">历史成绩</button>
            <button class="filter-btn" onclick="filterChanges('gpa',this)">GPA</button>
          </div>
        </div>
        <div id="changes-content"><div class="loading"><div class="spinner"></div>加载中…</div></div>
        <div class="pager" id="changes-pager"></div>
      </div>
    </div>

    <!-- 历史归档 -->
    <div class="view" id="view-history">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:#6b4fb3"></div>历史数据归档</div>
          <div class="filter-row">
            <button class="filter-btn active" onclick="setHistoryType('',this)">全部</button>
            <button class="filter-btn" onclick="setHistoryType('schedule',this)">课程表</button>
            <button class="filter-btn" onclick="setHistoryType('this_term_scores',this)">本学期成绩</button>
            <button class="filter-btn" onclick="setHistoryType('all_scores',this)">历史成绩</button>
            <button class="filter-btn" onclick="setHistoryType('gpa',this)">GPA</button>
          </div>
        </div>
        <div class="tbl-wrap" id="history-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

  </div>
</div>
</div>

<!-- GPA 弹窗 -->
<div class="modal-mask" id="gpa-modal" onclick="closeModal('gpa-modal',event)">
  <div class="modal-card">
    <div class="modal-head">
      <div class="modal-title">GPA 详情</div>
      <button class="modal-close" onclick="closeModal('gpa-modal')">关闭</button>
    </div>
    <div class="modal-body" id="gpa-modal-body"></div>
  </div>
</div>

<!-- 通用详情弹窗 -->
<div class="modal-mask" id="detail-modal" onclick="closeModal('detail-modal',event)">
  <div class="modal-card">
    <div class="modal-head">
      <div class="modal-title" id="detail-modal-title">详情</div>
      <button class="modal-close" onclick="closeModal('detail-modal')">关闭</button>
    </div>
    <div class="modal-body" id="detail-modal-body"></div>
  </div>
</div>

<!-- 变动详情弹窗 -->
<div class="modal-mask" id="change-modal" onclick="closeModal('change-modal',event)">
  <div class="modal-card">
    <div class="modal-head">
      <div class="modal-title" id="change-modal-title">变动详情</div>
      <button class="modal-close" onclick="closeModal('change-modal')">关闭</button>
    </div>
    <div class="modal-body" id="change-modal-body"></div>
  </div>
</div>

<script>
// ══════════════════════════════════════════════════════════════════
// 状态
// ══════════════════════════════════════════════════════════════════
const S = {
  scheduleData: null,   // 完整的去重课程数据 {total_course_count, courses:[]}
  currentWeek: 1,
  viewWeek: 1,
  maxWeek: 1,
  semesterStart: null,
  schedView: 'grid',
  showAllCourses: false,
  gpaRaw: null,
  scoresTerm: null,
  scoresAll: null,
  renderedScores: { term: [], all: [] },
  changesPage: 1,
  changesType: '',
  changesTotal: 0,
  historyType: '',
  historyRows: [],
  LABELS: {},   // 从 /api/field_labels 加载
};

const DAYS = ['一','二','三','四','五','六','日'];
const COLORS = ['','c2','c3','c4','c5'];

// 加载字段标签
async function loadFieldLabels() {
  try {
    const d = await api('/api/field_labels');
    S.LABELS = d.data || {};
  } catch(e) { S.LABELS = {}; }
}

function label(key) {
  return S.LABELS[key] || key;
}

function formatGpaValue(v) {
  if (v === null || v === undefined) return '-';
  if (Array.isArray(v)) {
    const first = v.length ? v[0] : null;
    if (Array.isArray(first)) return first.map(x => String(x ?? '-')).join(' / ');
    if (first && typeof first === 'object') return JSON.stringify(first);
    return v.map(x => String(x ?? '-')).join(' / ');
  }
  if (typeof v === 'object') {
    if (Array.isArray(v.data)) return formatGpaValue(v.data);
    if ('gpa' in v || '绩点' in v) return String(v.gpa ?? v['绩点'] ?? '-');
    return JSON.stringify(v);
  }
  return String(v);
}

// ══════════════════════════════════════════════════════════════════
// 导航
// ══════════════════════════════════════════════════════════════════
const VIEW_META = {
  overview:      {title:'概览仪表盘',    sub:'教务系统数据概览'},
  schedule:      {title:'本学期课程表',  sub:'当前学期所有排课信息（按课序号+课程号去重）'},
  'scores-term': {title:'本学期成绩',    sub:'本学期已录入的成绩'},
  'scores-all':  {title:'历史全部成绩',  sub:'累计所有已通过课程成绩'},
  changes:       {title:'变动日志',      sub:'监控检测到的所有数据变动'},
  history:       {title:'历史数据归档',  sub:'抓取数据的历史归档快照'},
};

function isMobile() {
  return window.matchMedia('(max-width: 768px)').matches;
}

function openSidebar() {
  const sb = document.getElementById('sidebar');
  const mask = document.getElementById('sidebar-mask');
  if (!sb || !mask || !isMobile()) return;
  sb.classList.add('open');
  mask.classList.add('open');
}

function closeSidebar() {
  const sb = document.getElementById('sidebar');
  const mask = document.getElementById('sidebar-mask');
  if (!sb || !mask) return;
  sb.classList.remove('open');
  mask.classList.remove('open');
}

function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  if (!sb || !isMobile()) return;
  if (sb.classList.contains('open')) closeSidebar();
  else openSidebar();
}

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('view-'+id)?.classList.add('active');
  document.getElementById('nav-'+id)?.classList.add('active');
  if (isMobile()) closeSidebar();
  const m = VIEW_META[id] || {};
  document.getElementById('topbar-title').textContent = m.title || id;
  document.getElementById('topbar-sub').textContent   = m.sub   || '';
  loadView(id);
}

async function loadView(id) {
  if (id === 'overview')     await loadOverview();
  if (id === 'schedule')     await loadSchedule();
  if (id === 'scores-term')  await loadScoresTerm();
  if (id === 'scores-all')   await loadScoresAll();
  if (id === 'changes')      await loadChanges();
  if (id === 'history')      await loadHistory();
}

function refreshCurrent() {
  const active = document.querySelector('.view.active');
  if (!active) return;
  const id = active.id.replace('view-', '');
  if (id === 'schedule') S.scheduleData = null;
  if (id === 'scores-term') S.scoresTerm = null;
  if (id === 'scores-all') S.scoresAll = null;
  Promise.all([loadStatus(), loadView(id)]).catch(() => {});
}

// ══════════════════════════════════════════════════════════════════
// API 工具
// ══════════════════════════════════════════════════════════════════
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ══════════════════════════════════════════════════════════════════
// 状态栏
// ══════════════════════════════════════════════════════════════════
async function loadStatus() {
  try {
    const d = await api('/api/status');
    document.getElementById('st-sch').textContent = d.schedule_cnt ?? '—';
    document.getElementById('st-ts').textContent  = d.term_score_cnt ?? '—';
    document.getElementById('st-as').textContent  = d.all_score_cnt  ?? '—';
    document.getElementById('st-ch').textContent  = d.changes_cnt    ?? '—';
    document.getElementById('st-gpa').textContent = d.gpa || '—';
    document.getElementById('status-text').textContent = `上次: ${d.last_run||'未知'} | 下次: ${d.next_run||'未知'}`;
    document.getElementById('status-dot').className = 'status-dot ok';
  } catch(e) {
    document.getElementById('status-text').textContent = '连接失败';
    document.getElementById('status-dot').className = 'status-dot';
  }
}

// ══════════════════════════════════════════════════════════════════
// 概览
// ══════════════════════════════════════════════════════════════════
async function loadOverview() {
  await Promise.all([loadStatus(), loadGpaInfo()]);
  try {
    const d = await api('/api/changes?limit=5');
    const el = document.getElementById('overview-changes');
    if (!d.data.length) { el.innerHTML = emptyHtml('暂无变动记录'); return; }
    el.innerHTML = '<div class="timeline" style="padding:16px 20px">' +
      d.data.map((c, i) => tlItem(c, i, d.data)).join('') + '</div>';
  } catch(e) { document.getElementById('overview-changes').innerHTML = errHtml(); }
}

async function loadGpaInfo() {
  try {
    const d = await api('/api/gpa');
    S.gpaRaw = d.data;
  } catch(e) { S.gpaRaw = null; }
}

// ══════════════════════════════════════════════════════════════════
// GPA 弹窗
// ══════════════════════════════════════════════════════════════════
function _parseGpa(raw) {
  if (!raw) return {};
  // 新版格式: {data: [[name, gpa, class_rank, time, grade_rank]]}
  if (typeof raw === 'object' && Array.isArray(raw.data) && raw.data.length) {
    const f = raw.data[0];
    if (Array.isArray(f) && f.length >= 5) {
      return {name: f[0], gpa: f[1], class_rank: f[2], generated_at: f[3], grade_rank: f[4]};
    }
    if (typeof f === 'object') return f;
  }
  // 旧版 dict 格式
  return raw;
}

function openGpaModal() {
  const g = _parseGpa(S.gpaRaw) || {};
  const rows = [
    ['绩点名称', g.name || g.gpa_name || g['绩点名称'] || '-'],
    ['GPA', g.gpa || g['绩点'] || '-'],
    ['班级排名', g.class_rank || g['班级排名'] || '-'],
    ['年级排名', g.grade_rank || g['年级排名'] || '-'],
    ['生成时间', g.generated_at || g['生成时间'] || '-'],
  ];
  document.getElementById('gpa-modal-body').innerHTML =
    rows.map(([k, v]) => `<div class="kv"><span class="kl">${esc(k)}</span><span class="kv2">${esc(String(v))}</span></div>`).join('') +
    `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(S.gpaRaw||{}, null, 2))}</pre></details>`;
  document.getElementById('gpa-modal').classList.add('open');
}

// ══════════════════════════════════════════════════════════════════
// 通用弹窗
// ══════════════════════════════════════════════════════════════════
function closeModal(id, evt) {
  if (evt && evt.target && evt.target.id !== id) return;
  document.getElementById(id).classList.remove('open');
}

function openDetailModal(title, obj, preferred=[]) {
  const flat = _flatten(obj || {});
  const used = new Set();
  const rows = [];
  preferred.forEach(k => {
    const v = _deepGet(obj, k);
    if (v === undefined || v === null || String(v).trim() === '') return;
    used.add(k);
    rows.push(kvRow(k.split('.').pop(), v));
  });
  Object.entries(flat).forEach(([k, v]) => {
    if (used.has(k) || v === null || v === undefined || String(v).trim() === '') return;
    rows.push(kvRow(k.split('.').pop(), v));
  });
  document.getElementById('detail-modal-title').textContent = title;
  document.getElementById('detail-modal-body').innerHTML =
    (rows.join('') || '<div class="empty-text">暂无可展示字段</div>') +
    `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(obj||{}, null, 2))}</pre></details>`;
  document.getElementById('detail-modal').classList.add('open');
}

function kvRow(key, val) {
  let displayVal = val;
  if (Array.isArray(val)) {
    if (!val.length) {
      return `<div class="kv"><span class="kl">${esc(label(key))}</span><span class="kv2">-</span></div>`;
    }
    const blocks = val.map((item, idx) => {
      const head = `<div style="font-size:.72rem;color:var(--muted);font-family:var(--mono);margin-bottom:6px">${esc(label(key))} #${idx + 1}</div>`;
      if (item && typeof item === 'object') {
        const inner = Object.entries(_flatten(item)).map(([k2, v2]) => {
          const kk = k2.split('.').pop();
          const vv = (v2 === null || v2 === undefined || String(v2).trim() === '') ? '-' : v2;
          return `<div class="kv" style="margin:6px 0"><span class="kl">${esc(label(kk))}</span><span class="kv2">${esc(String(vv))}</span></div>`;
        }).join('');
        return `<div style="padding:10px 0;border-top:1px dashed rgba(0,0,0,.12);margin-top:10px">${head}${inner}</div>`;
      }
      return `<div style="padding:10px 0;border-top:1px dashed rgba(0,0,0,.12);margin-top:10px">${head}<div class="kv"><span class="kl">${esc(label(key))}</span><span class="kv2">${esc(String(item ?? '-'))}</span></div></div>`;
    }).join('');
    return `<div class="kv"><span class="kl">${esc(label(key))}</span><div class="kv2">${blocks}</div></div>`;
  }
  if (val && typeof val === 'object') displayVal = JSON.stringify(val, null, 2);
  return `<div class="kv"><span class="kl">${esc(label(key))}</span><span class="kv2">${esc(String(displayVal ?? '-'))}</span></div>`;
}

function _flatten(obj, prefix='') {
  const out = {};
  if (!obj || typeof obj !== 'object') return out;
  Object.entries(obj).forEach(([k, v]) => {
    const nk = prefix ? `${prefix}.${k}` : k;
    if (Array.isArray(v)) out[nk] = v;
    else if (v && typeof v === 'object') Object.assign(out, _flatten(v, nk));
    else out[nk] = v;
  });
  return out;
}

function _deepGet(obj, path) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj);
}

// ══════════════════════════════════════════════════════════════════
// 周次工具
// ══════════════════════════════════════════════════════════════════
function extractWeeks(skzc) {
  const src = String(skzc || '').trim();
  if (!src) return [];
  if (/^[01]+$/.test(src)) return [...src].map((c,i) => c==='1' ? i+1 : 0).filter(Boolean);
  const weeks = new Set();
  const rangeReg = /(\d+)\s*[-~至]\s*(\d+)/g;
  let m;
  while ((m = rangeReg.exec(src)) !== null) {
    const lo = Math.min(+m[1], +m[2]), hi = Math.max(+m[1], +m[2]);
    for (let w = lo; w <= hi; w++) weeks.add(w);
  }
  const singleReg = /(?<!\d)(\d+)(?!\d)/g;
  while ((m = singleReg.exec(src)) !== null) weeks.add(+m[1]);
  return [...weeks].filter(w => w > 0).sort((a,b) => a-b);
}

function parseWeekRange(skzc) {
  const weeks = extractWeeks(skzc);
  if (!weeks.length) return '-';
  const ranges = [];
  let start = weeks[0], prev = weeks[0];
  for (let i = 1; i < weeks.length; i++) {
    if (weeks[i] - prev === 1) { prev = weeks[i]; continue; }
    ranges.push(start === prev ? `第${start}周` : `第${start}-${prev}周`);
    start = prev = weeks[i];
  }
  ranges.push(start === prev ? `第${start}周` : `第${start}-${prev}周`);
  return ranges.join('、');
}

function isCourseInWeek(course, week) {
  // course 是去重后的格式 {sessions:[{skzc,...}]}
  return (course.sessions || []).some(s => extractWeeks(s.skzc || '').includes(week));
}

function getWeekDateRange(week) {
  if (!S.semesterStart) return { start: '-', end: '-', startDate: new Date() };
  const base = new Date(S.semesterStart);
  const start = new Date(base);
  start.setDate(base.getDate() + (week - 1) * 7);
  const end = new Date(start);
  end.setDate(start.getDate() + 6);
  return {
    start: `${start.getMonth()+1}月${start.getDate()}日`,
    end: `${end.getMonth()+1}月${end.getDate()}日`,
    startDate: start,
  };
}

function detectMaxWeek(scheduleData) {
  let mx = 0;
  (scheduleData?.courses || []).forEach(c => {
    (c.sessions || []).forEach(s => {
      const ws = extractWeeks(s.skzc || '');
      if (ws.length) mx = Math.max(mx, ws[ws.length-1]);
    });
  });
  return Math.max(mx, S.currentWeek || 1);
}

function formatSection(skjc) {
  skjc = String(skjc || '').trim();
  if (!skjc) return '';
  if (skjc.includes('-')) {
    const [a, b] = skjc.split('-').map(Number);
    return `第${a}-${b}节`;
  }
  return `第${skjc}节`;
}

function formatDay(skxq) {
  const n = parseInt(skxq, 10);
  return n >= 1 && n <= 7 ? `周${DAYS[n-1]}` : (skxq || '');
}

// ══════════════════════════════════════════════════════════════════
// 课程表加载
// ══════════════════════════════════════════════════════════════════
async function loadSchedule() {
  if (S.scheduleData) { renderSchedule(); return; }
  document.getElementById('schedule-grid').innerHTML = loadingHtml();
  try {
    const d = await api('/api/schedule');
    S.scheduleData = d.data;
    S.scheduleFlat = d.flat || [];
    S.currentWeek = d.current_week;
    S.viewWeek = d.current_week;
    S.semesterStart = d.semester_start;
    S.maxWeek = detectMaxWeek(S.scheduleData);
    renderSchedule();
  } catch(e) { document.getElementById('schedule-grid').innerHTML = errHtml(); }
}

function setSchedView(v, btn) {
  S.schedView = v;
  document.querySelectorAll('#view-schedule .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('schedule-grid').style.display = v === 'grid' ? '' : 'none';
  document.getElementById('schedule-list').style.display = v === 'list' ? '' : 'none';
  if (S.scheduleData) renderSchedule();
}

function toggleShowAllCourses(checked) {
  S.showAllCourses = !!checked;
  if (S.scheduleData) renderSchedule();
}

function renderSchedule() {
  if (S.schedView === 'grid') renderSchedGrid();
  else renderSchedList();
}

function schedWeekNavHtml() {
  const r = getWeekDateRange(S.viewWeek);
  return `<div class="kb-header">
    <div class="week-info">第 ${S.viewWeek} 周（${r.start} – ${r.end}）</div>
    <div class="week-nav">
      <button class="week-nav-btn" onclick="changeWeek(-1)" ${S.viewWeek<=1?'disabled':''}>← 上一周</button>
      <button class="week-nav-btn" onclick="goCurrentWeek()">回到本周</button>
      <button class="week-nav-btn" onclick="changeWeek(1)" ${S.viewWeek>=S.maxWeek?'disabled':''}>下一周 →</button>
    </div>
  </div>`;
}

function changeWeek(delta) {
  const next = Math.min(S.maxWeek, Math.max(1, S.viewWeek + delta));
  if (next !== S.viewWeek) { S.viewWeek = next; renderSchedule(); }
}
function goCurrentWeek() {
  if (S.viewWeek !== S.currentWeek) { S.viewWeek = S.currentWeek; renderSchedule(); }
}

// ── 课表网格视图 ──────────────────────────────────────────────────
function renderSchedGrid() {
  const el = document.getElementById('schedule-grid');
  el.style.display = '';
  const courses = S.showAllCourses
    ? (S.scheduleData?.courses || [])
    : (S.scheduleData?.courses || []).filter(c => isCourseInWeek(c, S.viewWeek));
  const nav = schedWeekNavHtml();

  if (!courses.length) { el.innerHTML = nav + emptyHtml('本周无课程'); return; }

  // 构建：每节课位置 -> 课程信息
  // 因为同一门课可能有多个 session（不同周次同一时间），只取本周有效的 session
  const startMap = {};  // [day][startSec] -> array of cells
  const covered = {};   // [day] -> Set of sections covered by rowspan
  const colorMap = {};
  let colorIdx = 0;
  const detailList = [];  // 对应 grid 中每个格子的完整课程信息
  const maxSec = 12;

  courses.forEach(course => {
    const kcm = course.kcm || '';
    if (!colorMap[kcm]) colorMap[kcm] = COLORS[colorIdx++ % COLORS.length];

    // 找本周有课的所有 session
    const activeSessions = S.showAllCourses
      ? (course.sessions || [])
      : (course.sessions || []).filter(s => extractWeeks(s.skzc || '').includes(S.viewWeek));

    activeSessions.forEach(s => {
      const xq = parseInt(s.skxq, 10);
      if (!xq) return;

      let secStart, secEnd;
      const rawJc = String(s.skjc || '').trim();
      if (rawJc.includes('-')) {
        [secStart, secEnd] = rawJc.split('-').map(Number);
      } else {
        secStart = parseInt(rawJc, 10) || 0;
        secEnd = secStart;
      }
      if (!secStart) return;
      secStart = Math.max(1, secStart);
      secEnd = Math.min(maxSec, secEnd || secStart);
      const span = Math.max(1, secEnd - secStart + 1);

      if (!startMap[xq]) startMap[xq] = {};
      if (!startMap[xq][secStart]) startMap[xq][secStart] = [];

      const idx = detailList.push({...course, _activeSession: s}) - 1;
      startMap[xq][secStart].push({
        course, session: s, _idx: idx, _color: colorMap[kcm],
        _span: span, _start: secStart, _end: secEnd,
      });

      if (!covered[xq]) covered[xq] = new Set();
      for (let sec = secStart + 1; sec <= secEnd; sec++) covered[xq].add(sec);
    });
  });

  const r = getWeekDateRange(S.viewWeek);
  const days7 = [1,2,3,4,5,6,7];
  let html = nav + '<div class="kb-wrap"><table class="kb"><thead><tr><th class="time-col">时间</th>';
  days7.forEach(d => {
    const date = new Date(r.startDate);
    date.setDate(date.getDate() + d - 1);
    html += `<th>周${DAYS[d-1]}<br><span class="date-small">${date.getMonth()+1}/${date.getDate()}</span></th>`;
  });
  html += '</tr></thead><tbody>';

  for (let sec = 1; sec <= maxSec; sec++) {
    const gapClass = (sec === 5 || sec === 9) ? 'slot-gap' : '';
    html += `<tr class="${gapClass}"><td class="hdr time-col">${sec}</td>`;
    days7.forEach(d => {
      const cells = (startMap[d] && startMap[d][sec]) || [];
      const isCovered = covered[d] && covered[d].has(sec);
      if (!cells.length && isCovered) return;  // 被 rowspan 覆盖
      if (!cells.length) { html += '<td class="empty"></td>'; return; }
      const maxSpan = Math.max(...cells.map(x => x._span));
      html += `<td class="course-container" rowspan="${maxSpan}">`;
      cells.forEach(cell => {
        const c = cell.course;
        const s = cell.session;
        html += `<div class="course-cell ${cell._color}" onclick="openCourseGridDetail(${cell._idx})" title="点击查看详情">
          <div class="cc-name" title="${esc(c.kcm)}">${esc(c.kcm)}</div>
          <div class="cc-sub">${esc(c.skjs || '')}</div>
          <div class="cc-sub">${esc(s.jxdd || '')}</div>
        </div>`;
      });
      html += '</td>';
    });
    html += '</tr>';
  }
  html += '</tbody></table></div>';
  el._detailList = detailList;
  el.innerHTML = html;
}

function openCourseGridDetail(idx) {
  const el = document.getElementById('schedule-grid');
  const item = (el._detailList || [])[idx];
  if (!item) return;
  openCourseDetailFull(item);
}

// ── 课程列表视图 ──────────────────────────────────────────────────
function renderSchedList() {
  const el = document.getElementById('schedule-list');
  el.style.display = '';
  const flat = S.scheduleFlat || [];
  const courses = (S.showAllCourses ? flat : flat.filter(it => extractWeeks(it.skzc || '').includes(S.viewWeek))).map(it => ({
    ...it,
    sessions: [{
      skxq: it.skxq || it.classDay || '',
      skjc: it.skjc || it.section || '',
      skzc: it.skzc || it.weekRange || '',
      jxdd: it.jxdd || it.classroom || it.classroomName || ''
    }],
    raw_course: it.raw_course || it,
    raw_session: it.raw_session || null,
  }));
  const toolbar = `<div class="list-toolbar" style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;border-bottom:1px solid #ddd;background:#fafafa">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:.72rem;color:var(--muted);font-family:var(--mono)">${S.showAllCourses ? '当前：全部课程' : `当前：第 ${S.viewWeek} 周`}</div>
    <label style="display:flex;align-items:center;gap:6px;font-size:.76rem;color:var(--muted);cursor:pointer;white-space:nowrap;margin-left:auto">
      <input id="show-all-courses" type="checkbox" onchange="toggleShowAllCourses(this.checked)">
      显示全部课程
    </label>
  </div>`;
  const nav = schedWeekNavHtml();

  if (!courses.length) {
    el.innerHTML = toolbar + nav + emptyHtml('暂无课程数据');
    const chk = document.getElementById('show-all-courses');
    if (chk) chk.checked = !!S.showAllCourses;
    return;
  }

  const rowsHtml = courses.map((c, idx) => {
    const slotsHtml = (c.sessions || []).map(s => {
      const timeStr = `${formatDay(s.skxq)} ${formatSection(s.skjc)}`.trim() || '-';
      const weekStr = parseWeekRange(s.skzc);
      const room = s.jxdd || '-';
      return `<div class="slot-group"><div class="time-slot">${esc(timeStr)} ｜ ${esc(room)}</div><div class="time-slot-week">${esc(weekStr)}</div></div>`;
    }).join('');

    return `<tr class="click-row" onclick="openCourseListDetail(${idx})" title="点击查看课程详情">
      <td style="font-family:var(--mono)">${idx+1}</td>
      <td style="font-family:var(--mono);font-size:.8rem">${esc(c.kch||'-')}</td>
      <td style="font-family:var(--mono);font-size:.8rem">${esc(c.kxh||'-')}</td>
      <td>${esc(c.kcm||'-')}</td>
      <td style="font-family:var(--mono);font-size:.8rem">${esc(c.skjs||'-')}</td>
      <td><div class="time-slots">${slotsHtml}</div></td>
    </tr>`;
  }).join('');

  el._courses = courses;
  el.innerHTML = toolbar + nav +
    `<div style="padding:12px 16px;background:#f5f5f5;border-bottom:1px solid #ddd;font-size:.85rem;color:#666">
      ${S.showAllCourses ? '全部课程' : `本周课程（第 ${S.viewWeek} 周）`} · 共 <strong>${courses.length}</strong> 条记录（展平列表）
    </div>
    <div class="tbl-wrap">
      <table class="schedule-list-table">
        <thead><tr>
          <th>序号</th><th>课程号</th><th>课序号</th><th>课程名称</th><th>任课教师</th>
          <th>上课安排（时间 ｜ 地点 / 周次）</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
  const chk = document.getElementById('show-all-courses');
  if (chk) chk.checked = !!S.showAllCourses;
  }

  function openCourseListDetail(idx) {
    const el = document.getElementById('schedule-list');
    const c = (el._courses || [])[idx];
    if (!c) return;
    openCourseDetailFull(c);
  }

  // 打开历史归档中的课程详情（history modal 使用）
  function openHistoryScheduleDetail(idx) {
    const c = (S.historyModalCourses || [])[idx];
    if (!c) return;
    openCourseDetailFull(c);
  }

  function openCourseDetailFull(c) {
    const activeSession = c && c._activeSession ? c._activeSession : null;
    const baseInfo = _courseBaseInfo(c);
    const sessionRows = _courseSessionRows(c, activeSession);
    const baseHtml = _renderKvBlock(baseInfo, ['timeAndPlaceList', 'sessions', 'raw_course', 'raw_session', 'meta', '_activeSession']);
    const sessionHtml = _renderSessionRows(sessionRows);
    const rawObj = {
      ...(c && c.raw_course && typeof c.raw_course === 'object' ? c.raw_course : {}),
      ...(c && c.meta && typeof c.meta === 'object' ? c.meta : {}),
      sessions: sessionRows,
      raw_course: c.raw_course || null,
      raw_session: activeSession ? (activeSession.raw_session || null) : null,
    };

    document.getElementById('detail-modal-title').textContent = '课程详情';
    document.getElementById('detail-modal-body').innerHTML = `
      <div class="detail-section" style="margin-bottom:14px">
        <div class="detail-section-title" style="font-size:.82rem;font-weight:700;margin-bottom:10px;color:var(--accent2)">基础信息</div>
        ${baseHtml || '<div class="empty-text">暂无字段</div>'}
      </div>
      <div class="detail-section" style="margin-bottom:14px">
        <div class="detail-section-title" style="font-size:.82rem;font-weight:700;margin-bottom:10px;color:var(--accent2)">时段信息</div>
        ${sessionHtml || '<div class="empty-text">暂无时段信息</div>'}
      </div>
      <details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(rawObj, null, 2))}</pre></details>`;
    document.getElementById('detail-modal').classList.add('open');
  }

  function _courseBaseInfo(c) {
    const out = {};
    const source = c && c.raw_course && typeof c.raw_course === 'object'
      ? c.raw_course
      : (c && typeof c === 'object' ? c : {});
    Object.entries(source).forEach(([k, v]) => {
      if (['timeAndPlaceList', 'sessions', 'raw_course', 'raw_session', 'meta', '_activeSession'].includes(k)) return;
      out[k] = v;
    });
    if (c && c.meta && typeof c.meta === 'object') {
      Object.entries(c.meta).forEach(([k, v]) => {
        if (out[k] === undefined) out[k] = v;
      });
    }
    return out;
  }

  function _courseSessionRows(c, activeSession) {
    const course = c && c.raw_course && typeof c.raw_course === 'object' ? c.raw_course : {};
    let sessions = [];
    if (Array.isArray(course.timeAndPlaceList) && course.timeAndPlaceList.length) {
      sessions = course.timeAndPlaceList;
    } else if (Array.isArray(c && c.sessions) && c.sessions.length) {
      sessions = c.sessions.map(s => (s && s.raw_session && typeof s.raw_session === 'object') ? s.raw_session : s);
    } else if (activeSession && activeSession.raw_session && typeof activeSession.raw_session === 'object') {
      sessions = [activeSession.raw_session];
    } else if (c && c.raw_session) {
      sessions = Array.isArray(c.raw_session) ? c.raw_session : [c.raw_session];
    }
    return sessions.filter(s => s && typeof s === 'object');
  }

  function _renderKvBlock(obj, excludeKeys=[]) {
    const flat = _flatten(obj || {});
    const rows = Object.entries(flat)
      .filter(([k, v]) => !excludeKeys.includes(k.split('.')[0]) && v !== null && v !== undefined && String(v).trim() !== '')
      .map(([k, v]) => {
        const key = k.split('.').pop();
        return `<div class="kv"><span class="kl">${esc(label(key))}</span><span class="kv2">${esc(String(v))}</span></div>`;
      }).join('');
    return rows;
  }

  function _renderSessionRows(rows) {
    if (!rows || !rows.length) return '';
    const keys = [];
    rows.forEach(row => {
      Object.keys(_flatten(row)).forEach(k => {
        const short = k.split('.').pop();
        if (!keys.includes(short)) keys.push(short);
      });
    });
    const header = keys.map(k => `<th>${esc(label(k))}</th>`).join('');
    const body = rows.map((row, idx) => {
      const flat = _flatten(row);
      const cols = keys.map(k => {
        const foundKey = Object.keys(flat).find(fk => fk.split('.').pop() === k);
        const val = foundKey ? flat[foundKey] : '';
        const display = (val === null || val === undefined || String(val).trim() === '') ? '-' : val;
        return `<td>${esc(String(display))}</td>`;
      }).join('');
      return `<tr><td style="font-family:var(--mono);color:var(--muted)">${idx + 1}</td>${cols}</tr>`;
    }).join('');
    return `<div style="overflow:auto"><table><thead><tr><th>序号</th>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function openRawDetailModal(title, obj) {
    const flat = _flatten(obj || {});
    const rows = Object.entries(flat).map(([k, v]) => {
      const val = (v === null || v === undefined || String(v).trim() === '') ? '-' : v;
      return `<div class="kv"><span class="kl">${esc(k)}</span><span class="kv2">${esc(String(val))}</span></div>`;
    }).join('');
    document.getElementById('detail-modal-title').textContent = title;
    document.getElementById('detail-modal-body').innerHTML =
      (rows || '<div class="empty-text">暂无字段</div>') +
      `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(obj || {}, null, 2))}</pre></details>`;
    document.getElementById('detail-modal').classList.add('open');
  }

  // ══════════════════════════════════════════════════════════════════
  // 成绩
  // ══════════════════════════════════════════════════════════════════
  function scoreColor(s) {
    const raw = String(s || '').trim();
    if (['优秀','优'].includes(raw)) return 'score-exc';
    if (['良好','良'].includes(raw)) return 'score-good';
    if (['中等','中'].includes(raw)) return 'score-mid';
    if (['及格','合格'].includes(raw)) return 'score-pass';
    if (['不及格','不合格'].includes(raw)) return 'score-fail';
    const n = parseFloat(raw);
    if (isNaN(n)) return '';
    if (n < 60) return 'score-fail';
    if (n < 70) return 'score-pass';
    if (n < 80) return 'score-mid';
    if (n >= 90) return 'score-exc';
    return 'score-good';
  }

function getScoreDisplay(s) {
  /**
   * 区分等级制和百分制：
   * - 等级制：scoreEntryModeCode != '001' 且 gradeName 不为空 → 显示 gradeName
   * - 百分制：显示 cj / score 等数字
   */
  const mode = String(s.scoreEntryModeCode || s.cjlrfsdm || '').trim();
  const gradeName = String(s.gradeName || s.grade || '').trim();
  if (mode && mode !== '001' && gradeName) return { display: gradeName, isGrade: true };
  const cj = String(s.cj || s.score || s.courseScore || s.gradeScore || '').trim();
  return { display: cj || '—', isGrade: false };
}

function scoresTable(kind, data, filter) {
  let rows = data;
  if (filter) {
    const q = filter.toLowerCase();
    rows = data.filter(s =>
      (s.kcm || s.courseName || '').toLowerCase().includes(q) ||
      (s.kch || s.courseNumber || '').toLowerCase().includes(q)
    );
  }
  S.renderedScores[kind] = rows;
  if (!rows.length) return emptyHtml('暂无成绩数据');

  const rowsHtml = rows.map((s, idx) => {
    const { display: cjDisp, isGrade } = getScoreDisplay(s);
    const jd = s.jd || s.gradePoint || s.gradePointScore || '—';
    const xf = s.xf || s.credit || '—';
    const kch = s.kch || s.courseNumber || (s.id && s.id.courseNumber) || '';
    const kcm = s.kcm || s.courseName || '';
    const cjHtml = `<span class="score-num ${scoreColor(cjDisp)}">${esc(cjDisp)}</span>` +
      (isGrade ? ` <span style="font-size:.65rem;color:var(--muted);font-family:var(--mono)">[等级]</span>` : '');
    return `<tr class="click-row" onclick="openScoreDetail('${kind}',${idx})" title="点击查看详情">
      <td>${esc(kcm)}</td>
      <td style="font-family:var(--mono);font-size:.75rem;color:var(--muted)">${esc(kch||'-')}</td>
      <td>${cjHtml}</td>
      <td style="font-family:var(--mono)">${esc(String(jd))}</td>
      <td style="font-family:var(--mono)">${esc(String(xf))}</td>
    </tr>`;
  }).join('');

  return `<table><thead><tr>
    <th>课程名称</th><th>课程号</th><th>成绩</th><th>绩点</th><th>学分</th>
  </tr></thead><tbody>${rowsHtml}</tbody></table>`;
}

function openScoreDetail(kind, idx) {
  const s = (S.renderedScores[kind] || [])[idx];
  if (!s) return;
  const title = kind === 'all' ? '历史成绩详情' : '本学期成绩详情';
  // 展示所有字段，空值用 - 代替
  const flat = _flatten(s);
  const rows = Object.entries(flat).map(([k, v]) => {
    const shortKey = k.split('.').pop();
    const val = (v === null || v === undefined || String(v).trim() === '') ? '-' : v;
    return `<div class="kv"><span class="kl">${esc(label(shortKey))}</span><span class="kv2">${esc(String(val))}</span></div>`;
  }).join('');
  document.getElementById('detail-modal-title').textContent = title;
  document.getElementById('detail-modal-body').innerHTML =
    (rows || '<div class="empty-text">暂无字段</div>') +
    `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(s, null, 2))}</pre></details>`;
  document.getElementById('detail-modal').classList.add('open');
}

async function loadScoresTerm() {
  if (S.scoresTerm) { renderScoresTerm(); return; }
  document.getElementById('scores-term-table').innerHTML = loadingHtml();
  try {
    const d = await api('/api/scores/term');
    S.scoresTerm = d.data;
    renderScoresTerm();
  } catch(e) { document.getElementById('scores-term-table').innerHTML = errHtml(); }
}

function renderScoresTerm(filter) {
  document.getElementById('scores-term-table').innerHTML = scoresTable('term', S.scoresTerm || [], filter || '');
}

async function loadScoresAll() {
  if (S.scoresAll) { renderScoresAll(); return; }
  document.getElementById('scores-all-table').innerHTML = loadingHtml();
  try {
    const d = await api('/api/scores/all');
    S.scoresAll = d.data;
    renderScoresAll();
  } catch(e) { document.getElementById('scores-all-table').innerHTML = errHtml(); }
}

function renderScoresAll(filter) {
  document.getElementById('scores-all-table').innerHTML = scoresTable('all', S.scoresAll || [], filter || '');
}

function filterScores(type, q) {
  if (type === 'term') renderScoresTerm(q);
  else renderScoresAll(q);
}

// ══════════════════════════════════════════════════════════════════
// 变动日志
// ══════════════════════════════════════════════════════════════════

/**
 * 日志格式：
 * 单项变动：  [成绩] "键" 新增/变动 "课程名" 旧值->新值
 * 多项变动：  [成绩] 多项变动 "课程名"
 * 课程：     [课程] "键" 新增/变动 "课程名" 旧值->新值
 * GPA：      [GPA] "键" 变动 旧->新
 * 点击后弹出所有变动前后数据
 */
function tlItem(c, idx, allData) {
  const typeMap = { schedule:'课程', this_term_scores:'成绩(本学期)', all_scores:'成绩(历史)', gpa:'GPA' };
  const dotClass = { schedule:'sch', this_term_scores:'score', all_scores:'score', gpa:'gpa' }[c.type] || '';
  const typeTag = typeMap[c.type] || c.type;
  const time = (c.time || '').replace('T', ' ').slice(0, 16);

  const logLines = buildLogLines(c);
  const linesHtml = logLines.slice(0, 3).map(l => `<div class="tl-log">${l}</div>`).join('');
  const moreHint = logLines.length > 3 ? `<div style="font-size:.68rem;color:var(--muted);font-family:var(--mono)">…共 ${logLines.length} 条变动，点击查看全部</div>` : '';

  return `<div class="tl-item" onclick="openChangeDetail(${idx})">
    <div class="tl-line">
      <div class="tl-dot ${dotClass}"></div>
      <div class="tl-vline"></div>
    </div>
    <div class="tl-body">
      <div class="tl-time">${esc(time)}</div>
      <div class="tl-card">
        <div style="font-size:.65rem;font-family:var(--mono);font-weight:600;margin-bottom:6px;color:var(--muted)">[${esc(typeTag)}]</div>
        ${linesHtml}
        ${moreHint}
      </div>
    </div>
  </div>`;
}

function buildLogLines(c) {
  // 优先读取预生成的摘要
  if (typeof c.summary === 'string' && c.summary.trim()) {
    return c.summary.split(/\r?\n/).filter(Boolean).map(line => esc(line));
  }
  // 如果没有摘要，返回空列表
  return [];
}

// 存储当前 changes 列表，供弹窗使用
let _currentChanges = [];

function openChangeDetail(idx) {
  const c = _currentChanges[idx];
  if (!c) return;
  const tag = { schedule:'课程', this_term_scores:'成绩(本学期)', all_scores:'成绩(历史)', gpa:'GPA' }[c.type] || c.type;
  const time = (c.time || '').replace('T', ' ').slice(0, 19);

  let html = `<div style="font-size:.8rem;color:var(--muted);font-family:var(--mono);margin-bottom:12px">变动时间: ${esc(time)}</div>`;

  (c.changes || []).forEach(ch => {
    if (ch.action === 'modified') {
      html += `<div style="font-weight:600;margin:8px 0 4px;font-size:.8rem">${esc(label(ch.key || ''))} — 字段变动详情</div>`;
      const fields = ch.fields || {};
      if (Object.keys(fields).length) {
        Object.entries(fields).forEach(([fk, fv]) => {
          html += `<div class="kv"><span class="kl">${esc(label(fk))}</span><span class="kv2">
            <span style="color:var(--accent)">${esc(String(fv.before??'-'))}</span>
            <span style="color:var(--muted)"> → </span>
            <span style="color:var(--green)">${esc(String(fv.after??'-'))}</span>
          </span></div>`;
        });
      } else {
        // schedule modified: compare before/after objects
        const b = ch.before || {}, a = ch.after || {};
        const allKeys = [...new Set([...Object.keys(b), ...Object.keys(a)])];
        allKeys.forEach(k => {
          const bv = JSON.stringify(b[k]), av = JSON.stringify(a[k]);
          if (bv !== av) {
            html += `<div class="kv"><span class="kl">${esc(label(k))}</span><span class="kv2">
              <span style="color:var(--accent)">${esc(String(b[k]??'-'))}</span>
              <span style="color:var(--muted)"> → </span>
              <span style="color:var(--green)">${esc(String(a[k]??'-'))}</span>
            </span></div>`;
          }
        });
      }
    } else if (ch.action === 'added') {
      const obj = ch.course || ch.score || {};
      const name = obj.kcm || obj.courseName || ch.key || '';
      html += `<div style="font-weight:600;margin:8px 0 4px;font-size:.8rem;color:var(--green)">+ 新增: ${esc(name)}</div>`;
      Object.entries(_flatten(obj)).forEach(([k, v]) => {
        if (v === null || v === undefined || String(v).trim() === '') return;
        html += `<div class="kv"><span class="kl">${esc(label(k.split('.').pop()))}</span><span class="kv2">${esc(String(v))}</span></div>`;
      });
    } else if (ch.action === 'removed') {
      const obj = ch.course || ch.score || {};
      const name = obj.kcm || obj.courseName || ch.key || '';
      html += `<div style="font-weight:600;margin:8px 0 4px;font-size:.8rem;color:var(--accent)">- 删除: ${esc(name)}</div>`;
    } else if (c.type === 'gpa') {
      // GPA 逐项变动
      const k = String(ch.key ?? '');
      html += `<div class="kv"><span class="kl">${esc(k || label(k))}</span><span class="kv2">
        <span style="color:var(--accent)">${esc(formatGpaValue(ch.before))}</span>
        <span style="color:var(--muted)"> → </span>
        <span style="color:var(--green)">${esc(formatGpaValue(ch.after))}</span>
      </span></div>`;
    }
  });

  html += `<details class="raw-json"><summary>原始变动数据</summary><pre>${esc(JSON.stringify(c, null, 2))}</pre></details>`;
  document.getElementById('change-modal-title').textContent = `[${tag}] 变动详情`;
  document.getElementById('change-modal-body').innerHTML = html;
  document.getElementById('change-modal').classList.add('open');
}

async function loadChanges(page, type) {
  if (page !== undefined) S.changesPage = page;
  if (type !== undefined) S.changesType = type;
  document.getElementById('changes-content').innerHTML = loadingHtml();
  try {
    const url = `/api/changes?page=${S.changesPage}&limit=15${S.changesType ? '&type=' + S.changesType : ''}`;
    const d = await api(url);
    S.changesTotal = d.total;
    _currentChanges = d.data;
    if (!d.data.length) {
      document.getElementById('changes-content').innerHTML = emptyHtml('暂无变动记录');
      document.getElementById('changes-pager').innerHTML = '';
      return;
    }
    document.getElementById('changes-content').innerHTML =
      '<div class="timeline">' + d.data.map((c, i) => tlItem(c, i, d.data)).join('') + '</div>';
    renderPager();
  } catch(e) { document.getElementById('changes-content').innerHTML = errHtml(); }
}

function filterChanges(type, btn) {
  document.querySelectorAll('#view-changes .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  S.changesPage = 1;
  loadChanges(1, type);
}

function renderPager() {
  const perPage = 15;
  const pages = Math.ceil(S.changesTotal / perPage);
  const cur = S.changesPage;
  if (pages <= 1) { document.getElementById('changes-pager').innerHTML = ''; return; }
  let html = '';
  if (cur > 1) html += `<button class="pager-btn" onclick="loadChanges(${cur-1})">← 上一页</button>`;
  const start = Math.max(1, cur-2), end = Math.min(pages, cur+2);
  for (let i = start; i <= end; i++)
    html += `<button class="pager-btn ${i===cur?'active':''}" onclick="loadChanges(${i})">${i}</button>`;
  if (cur < pages) html += `<button class="pager-btn" onclick="loadChanges(${cur+1})">下一页 →</button>`;
  html += `<span style="font-size:.65rem;font-family:var(--mono);color:var(--muted);align-self:center">共 ${S.changesTotal} 条</span>`;
  document.getElementById('changes-pager').innerHTML = html;
}

// ══════════════════════════════════════════════════════════════════
// 历史归档
// ══════════════════════════════════════════════════════════════════
function typeLabel(t) {
  return {schedule:'课程表', this_term_scores:'本学期成绩', all_scores:'历史成绩', gpa:'GPA'}[t] || t || '-';
}

async function loadHistory() {
  const el = document.getElementById('history-table');
  el.innerHTML = loadingHtml();
  try {
    const q = S.historyType ? `?type=${encodeURIComponent(S.historyType)}` : '';
    const d = await api(`/api/history${q}`);
    S.historyRows = d.data || [];
    renderHistory();
  } catch(e) { el.innerHTML = errHtml(); }
}

function setHistoryType(t, btn) {
  S.historyType = t || '';
  document.querySelectorAll('#view-history .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadHistory();
}

function renderHistory() {
  const el = document.getElementById('history-table');
  const rows = S.historyRows || [];
  if (!rows.length) { el.innerHTML = emptyHtml('暂无归档数据'); return; }
  const body = rows.map((r, idx) => `<tr class="click-row" onclick="openHistoryItem(${idx})">
    <td>${esc(typeLabel(r.type))}</td>
    <td style="font-family:var(--mono)">${esc(r.time||'-')}</td>
    <td style="font-family:var(--mono)">${esc(r.file||'-')}</td>
    <td style="font-family:var(--mono)">${esc(r.count==null?'-':String(r.count))}</td>
    <td style="font-family:var(--mono)">${esc(r.size==null?'-':r.size+' B')}</td>
  </tr>`).join('');
  el.innerHTML = `<table class="history-table"><thead><tr>
    <th>数据类型</th><th>归档时间</th><th>文件名</th><th>条数/字段数</th><th>文件大小</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

async function openHistoryItem(idx) {
  const row = (S.historyRows || [])[idx];
  if (!row) return;
  try {
    const q = `?type=${encodeURIComponent(row.type)}&file=${encodeURIComponent(row.file)}`;
    const d = await api(`/api/history${q}`);
    const payload = d.data;
    document.getElementById('detail-modal-title').textContent = `归档 · ${typeLabel(row.type)} · ${row.file}`;
    let bodyHtml = '';
    if (row.type === 'this_term_scores' || row.type === 'all_scores') {
      // 使用成绩表格渲染（复用 scoresTable）并确保点击行能打开详情
      const kind = row.type === 'this_term_scores' ? 'term' : 'all';
      // 规范 payload 为数组（支持 payload、payload.data、payload.scores 等格式）
      let arr = [];
      if (Array.isArray(payload)) arr = payload;
      else if (payload && Array.isArray(payload.data)) arr = payload.data;
      else if (payload && Array.isArray(payload.scores)) arr = payload.scores;
      else if (payload && typeof payload === 'object') {
        for (const k in payload) {
          if (Array.isArray(payload[k])) { arr = payload[k]; break; }
        }
      }
      S.historyScores = S.historyScores || {};
      S.historyScores[kind] = arr || [];
      S.renderedScores[kind] = arr || [];
      bodyHtml = `<div style="max-height:60vh;overflow:auto">${scoresTable(kind, arr || [], '')}</div>`;
      bodyHtml += `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(payload||{}, null, 2))}</pre></details>`;
    } else if (row.type === 'schedule') {
      // payload 可能为平铺列表或去重结构，构建与“课程列表视图”一致的数据结构并支持点击查看详情
      let courses = [];
      if (Array.isArray(payload)) {
        courses = payload.map(it => {
          const raw = (it && it.raw_course) ? it.raw_course : it;
          return Object.assign({}, it, {
            sessions: [{
              skxq: it.skxq || it.classDay || '',
              skjc: it.skjc || it.section || '',
              skzc: it.skzc || it.weekRange || '',
              jxdd: it.jxdd || it.classroom || it.classroomName || ''
            }],
            raw_course: raw,
            raw_session: it.raw_session || null,
          });
        });
      } else if (payload && Array.isArray(payload.data)) {
        courses = payload.data.map(it => {
          const raw = (it && it.raw_course) ? it.raw_course : it;
          return Object.assign({}, it, {
            sessions: [{
              skxq: it.skxq || it.classDay || '',
              skjc: it.skjc || it.section || '',
              skzc: it.skzc || it.weekRange || '',
              jxdd: it.jxdd || it.classroom || it.classroomName || ''
            }],
            raw_course: raw,
            raw_session: it.raw_session || null,
          });
        });
      } else if (payload && payload.courses) {
        courses = payload.courses.map(c => ({...c, raw_course: c}));
      }

      if (courses.length) {
        // 规范字段，确保详情视图能读取到常用简称字段
        const norm = courses.map(it => {
          const raw = (it && it.raw_course) ? it.raw_course : it;
          const kch = it.kch || raw.coureNumber || raw.coureNumber || it.courseNumber || (raw && raw.id && raw.id.coureNumber) || '';
          const kxh = it.kxh || raw.coureSequenceNumber || it.coureSequenceNumber || '';
          const kcm = it.kcm || raw.courseName || it.courseName || '';
          const skjs = it.skjs || raw.attendClassTeacher || raw.courseTeacher || raw.teacherName || it.attendClassTeacher || '';
          return Object.assign({}, it, {kch, kxh, kcm, skjs, raw_course: raw});
        });
        S.historyModalCourses = norm;
        const rows = norm.map((it, i) => {
          const slots = (it.sessions || []).map(s => `${formatDay(s.skxq||'')} ${formatSection(s.skjc||'')} | ${s.jxdd||'-'} | ${parseWeekRange(s.skzc||'')}`).join('\n');
          return `<tr class="click-row" onclick="openHistoryScheduleDetail(${i})" title="点击查看课程详情"><td>${i+1}</td><td>${esc(it.kch||'-')}</td><td>${esc(it.kxh||'-')}</td><td>${esc(it.kcm||'-')}</td><td>${esc(it.skjs||'-')}</td><td><pre style="white-space:pre-wrap">${esc(slots)}</pre></td></tr>`;
        }).join('');
        bodyHtml = `<table><thead><tr><th>序号</th><th>课程号</th><th>课序号</th><th>课程名称</th><th>任课教师</th><th>上课安排</th></tr></thead><tbody>${rows}</tbody></table>`;
        bodyHtml += `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(payload||{}, null, 2))}</pre></details>`;
      }
    } else if (row.type === 'gpa') {
      bodyHtml = `<pre style="max-height:60vh;overflow:auto;font-family:var(--mono);font-size:.8rem">${esc(JSON.stringify(payload||{}, null, 2))}</pre>`;
    } else {
      bodyHtml = `<pre style="max-height:60vh;overflow:auto;font-family:var(--mono);font-size:.7rem">${esc(JSON.stringify(payload||{}, null, 2))}</pre>`;
    }

    document.getElementById('detail-modal-body').innerHTML = bodyHtml;
    document.getElementById('detail-modal').classList.add('open');
  } catch(e) {
    alert('读取归档失败');
  }
}

// ══════════════════════════════════════════════════════════════════
// 工具
// ══════════════════════════════════════════════════════════════════
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function emptyHtml(msg) {
  return `<div class="empty"><div class="empty-icon">◌</div><div class="empty-text">${msg}</div></div>`;
}
function loadingHtml() {
  return '<div class="loading"><div class="spinner"></div>加载中…</div>';
}
function errHtml() {
  return `<div class="empty"><div class="empty-icon">⚠</div><div class="empty-text">加载失败，请刷新重试</div></div>`;
}

// ══════════════════════════════════════════════════════════════════
// 初始化
// ══════════════════════════════════════════════════════════════════
(async () => {
  window.addEventListener('resize', () => {
    if (!isMobile()) closeSidebar();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSidebar();
  });
  await loadFieldLabels();
  await loadStatus();
  await loadOverview();
})();
</script>
</body>
</html>"""


# ── 入口 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 52)
    print("  东北农业大学教务监控 Web 端")
    print(f"  http://127.0.0.1:{port}")
    print(f"  学号: {USERNAME}   数据目录: {DATA_DIR}")
    print("=" * 52)
    app.run(host="0.0.0.0", port=port, debug=False)
