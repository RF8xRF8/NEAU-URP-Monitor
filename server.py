"""
东北农业大学教务监控 — Web 查看端
运行: python server.py
默认 http://127.0.0.1:8080
鉴权: 使用 CONFIG 中的学号 / 密码登录
"""

import json
import os
import sys
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect,
                   render_template_string, request, session, url_for)

# ── 将 monitor.py 所在目录加入 path，读取同一份 CONFIG ──────────────
MONITOR_DIR = Path(__file__).parent
sys.path.insert(0, str(MONITOR_DIR))

try:
    from monitor import CONFIG
    DATA_DIR  = CONFIG["data_dir"]
    USERNAME  = CONFIG["username"]
    PASSWORD  = CONFIG["password"]
    MONITOR_INTERVAL = int(CONFIG.get("interval", 1800))
except Exception as e:
    # fallback：从环境变量读取
    USERNAME = os.environ.get("NEAU_USERNAME")
    PASSWORD = os.environ.get("NEAU_PASSWORD")
    if not USERNAME or not PASSWORD:
        print(
            "\n=== 错误：缺少凭据配置 ===\n"
            "请创建 config.json（参照 config.example.json），或设置环境变量：\n"
            "  NEAU_USERNAME=你的学号\n"
            "  NEAU_PASSWORD=你的密码\n"
        )
        sys.exit(1)
    DATA_DIR = os.environ.get("NEAU_DATA_DIR", "./data")
    MONITOR_INTERVAL = int(os.environ.get("NEAU_INTERVAL", "1800"))

app = Flask(__name__)
app.secret_key = "neau_monitor_secret_2025"
app.permanent_session_lifetime = timedelta(hours=12)

# ─────────────────────────── 学期配置 ─────────────────────────────────
# 学期开始日期（周一为第1周开始）修改为实际的学期开始日期
SEMESTER_START = datetime(2026, 3, 2)  # 2026年春季学期开始日期（周一）

def get_current_week() -> int:
    """
    计算当前是学期的第几周
    """
    today = datetime.now()
    days_passed = (today - SEMESTER_START).days
    current_week = (days_passed // 7) + 1
    return max(1, current_week)

def is_course_in_week(skzc: str, target_week: int) -> bool:
    """
    判断课程是否在指定周次上课
    
    参数：
      skzc: 周次信息，支持：
        - 二进制字符串：如 "000011000000000000000000"（第5-6周）
        - 文本格式：如 "第1周"、"第1-8周"、"第1,3,5周" 等
      target_week: 目标周数（如当前周）
    
    返回：True 表示该周有课
    """
    if not skzc or not str(skzc).strip():
        return False
    
    skzc = str(skzc).strip()
    
    # 检查是否为二进制字符串
    if all(c in '01' for c in skzc):
        # 二进制：位置i对应第(i+1)周
        if target_week <= len(skzc):
            return skzc[target_week - 1] == '1'
        return False
    
    # 文本格式处理
    # 支持 "第1周"、"第1-8周"、"第1,3,5周" 等格式
    import re
    
    # 提取所有数字范围和单个数字
    # 匹配 "第X周" 或 "第X-Y周"
    ranges = re.findall(r'第(\d+)(?:-(\d+))?周', skzc)
    
    for start_str, end_str in ranges:
        start = int(start_str)
        end = int(end_str) if end_str else start
        if start <= target_week <= end:
            return True
    
    # 匹配 "X-Y周" 格式（没有"第"）
    ranges = re.findall(r'(\d+)-(\d+)周', skzc)
    for start_str, end_str in ranges:
        start = int(start_str)
        end = int(end_str)
        if start <= target_week <= end:
            return True
    
    # 匹配单个 "X周" 格式（没有"-"）
    single = re.findall(r'(?<!-)\b(\d+)周', skzc)
    for week_str in single:
        if int(week_str) == target_week:
            return True
    
    return False

# ─────────────────────────── 工具函数 ────────────────────────────────

def _load(name: str):
    p = Path(DATA_DIR) / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
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
    return list(reversed(lines))   # 最新在前


def _history_root() -> Path:
    return Path(DATA_DIR) / "archive"


def _list_history(data_type: str = "") -> list[dict]:
    root = _history_root()
    if not root.exists():
        return []

    types = [data_type] if data_type else [p.name for p in root.iterdir() if p.is_dir()]
    items: list[dict] = []
    for t in types:
        t_dir = root / t
        if not t_dir.exists() or not t_dir.is_dir():
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
                if isinstance(payload, list):
                    count = len(payload)
                elif isinstance(payload, dict):
                    count = len(payload)
            except Exception:
                count = None

            items.append({
                "type": t,
                "file": p.name,
                "time": display_time,
                "count": count,
                "size": p.stat().st_size,
            })

    items.sort(key=lambda x: (x.get("time") or "", x.get("file") or ""), reverse=True)
    return items


def _monitor_status() -> dict:
    """读取 monitor.log 最后几行，返回运行状态摘要。"""
    log_path = MONITOR_DIR / "monitor.log"
    if not log_path.exists():
        return {"last_run": "尚未运行", "next_run": "未知", "lines": []}
    try:
        with log_path.open(encoding="utf-8") as f:
            all_lines = f.readlines()
        last_lines = [l.rstrip() for l in all_lines[-30:]]

        # 找最近一次抓取时间（优先“开始抓取”）
        last_run = "未知"
        last_dt = None
        for line in reversed(all_lines):
            if "开始抓取" in line:
                ts = line[:23]
                last_run = ts
                try:
                    last_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
                except Exception:
                    last_dt = None
                break
            if "本次抓取完成" in line and last_run == "未知":
                ts = line[:23]
                last_run = ts
                try:
                    last_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
                except Exception:
                    last_dt = None

        next_run = "未知"
        if last_dt is not None:
            next_dt = last_dt + timedelta(seconds=MONITOR_INTERVAL)
            now = datetime.now()
            if next_dt <= now:
                next_run = "即将执行"
            else:
                delta = next_dt - now
                mins = int(delta.total_seconds() // 60)
                secs = int(delta.total_seconds() % 60)
                next_run = f"{next_dt.strftime('%H:%M:%S')}（{mins}分{secs}秒后）"

        return {"last_run": last_run, "next_run": next_run, "lines": last_lines}
    except Exception:
        return {"last_run": "读取失败", "next_run": "未知", "lines": []}


# ─────────────────────────── 鉴权 ────────────────────────────────────

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
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if u == USERNAME and p == PASSWORD:
            session.permanent = True
            session["logged_in"] = True
            session["user"] = u
            return redirect(url_for("dashboard"))
        error = "学号或密码错误"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ─────────────────────────── 页面路由 ────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    return render_template_string(MAIN_HTML, user=session.get("user", ""))


# ─────────────────────────── API ─────────────────────────────────────

@app.route("/api/schedule")
@login_required
def api_schedule():
    data = _load("schedule")
    current_week = get_current_week()
    
    # 筛选本周课程
    this_week_courses = [c for c in (data or []) if is_course_in_week(c.get("skzc", ""), current_week)]
    
    return jsonify({
        "ok": True,
        "current_week": current_week,
        "semester_start": SEMESTER_START.strftime("%Y-%m-%d"),
        "all_data": data or [],
        "data": this_week_courses,
        "count": len(this_week_courses),
        "total_count": len(data or [])
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


@app.route("/api/changes")
@login_required
def api_changes():
    changes = _load_changes()
    t = request.args.get("type", "")
    if t:
        changes = [c for c in changes if c.get("type") == t]
    page  = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 30))
    total = len(changes)
    sliced = changes[(page-1)*limit : page*limit]
    return jsonify({"ok": True, "data": sliced, "total": total, "page": page})


@app.route("/api/gpa")
@login_required
def api_gpa():
    gpa = _load("gpa")
    return jsonify({"ok": True, "data": gpa or {}})


@app.route("/api/status")
@login_required
def api_status():
    st = _monitor_status()
    schedule   = _load("schedule")
    term_score = _load("this_term_scores")
    all_score  = _load("all_scores")
    gpa_info   = _load("gpa")
    academic_info = _load("academic_info")
    changes    = _load_changes()

    # 课程总数优先使用教务系统学业信息中“待修课程”口径，其次按课程号去重
    schedule_list = schedule or []
    unique_courses = {
        (str(x.get("kch") or x.get("courseNumber") or "").strip(),
         str(x.get("kcm") or x.get("courseName") or "").strip())
        for x in schedule_list
    }
    unique_courses = {k for k in unique_courses if k[0] or k[1]}
    def _to_int(v):
      if isinstance(v, int):
        return v
      if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
      return None

    schedule_cnt = None
    if isinstance(academic_info, dict):
      # 优先待修课程字段（你期望的口径）
      for k in ("courseNum_bxqyxd", "courseNum", "course_num"):
        n = _to_int(academic_info.get(k))
        if n is not None:
          schedule_cnt = n
          break
    if schedule_cnt is None:
        schedule_cnt = len(unique_courses)

    return jsonify({
        "ok": True,
        "last_run":      st["last_run"],
        "next_run":      st.get("next_run", "未知"),
        "schedule_cnt":  schedule_cnt,
        "schedule_session_cnt": len(schedule_list),
        "term_score_cnt":len(term_score or []),
        "all_score_cnt": len(all_score  or []),
        "gpa":           (gpa_info or {}).get("gpa", "-"),
        "gpa_time":      (gpa_info or {}).get("generated_at", "-"),
        "changes_cnt":   len(changes),
    })


@app.route("/api/history")
@login_required
def api_history():
    data_type = request.args.get("type", "").strip()
    file_name = request.args.get("file", "").strip()

    if file_name:
        if not data_type:
            return jsonify({"ok": False, "error": "缺少 type 参数"}), 400
        if ".." in file_name or "/" in file_name or "\\" in file_name:
            return jsonify({"ok": False, "error": "非法文件名"}), 400
        target = _history_root() / data_type / file_name
        if not target.exists() or not target.is_file():
            return jsonify({"ok": False, "error": "文件不存在"}), 404
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        return jsonify({
            "ok": True,
            "type": data_type,
            "file": file_name,
            "data": payload,
        })

    rows = _list_history(data_type)
    return jsonify({
        "ok": True,
        "data": rows,
        "count": len(rows),
    })


# ─────────────────────────── HTML 模板 ───────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>教务监控 · 登录</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#0f1117;--paper:#f5f0e8;--accent:#c8432b;--accent2:#2b5fc8;
  --border:#d4c9b0;--shadow:rgba(15,17,23,.12);
  --mono:'JetBrains Mono',monospace;--serif:'Noto Serif SC',serif;
}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:var(--paper);font-family:var(--serif);
  background-image:radial-gradient(circle at 20% 20%, rgba(200,67,43,.06) 0%, transparent 50%),
    radial-gradient(circle at 80% 80%, rgba(43,95,200,.06) 0%, transparent 50%);
}
.wrap{width:100%;max-width:400px;padding:24px}
.card{background:#fff;border:1.5px solid var(--border);padding:44px 40px 40px;
  box-shadow:0 2px 0 var(--border),0 8px 32px var(--shadow);
  position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--accent),var(--accent2))}
.school{font-size:.7rem;letter-spacing:.15em;color:#888;text-transform:uppercase;
  font-family:var(--mono);margin-bottom:8px}
h1{font-size:1.6rem;font-weight:700;color:var(--ink);line-height:1.2;margin-bottom:32px}
h1 span{color:var(--accent)}
label{display:block;font-size:.75rem;letter-spacing:.08em;color:#666;
  font-family:var(--mono);margin-bottom:6px}
input{width:100%;border:1.5px solid var(--border);padding:11px 14px;font-size:.95rem;
  font-family:var(--mono);color:var(--ink);background:#faf8f4;outline:none;
  transition:border-color .2s,box-shadow .2s;margin-bottom:20px;border-radius:2px}
input:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(43,95,200,.1)}
.btn{width:100%;padding:12px;background:var(--ink);color:#fff;border:none;
  font-family:var(--mono);font-size:.9rem;letter-spacing:.05em;cursor:pointer;
  transition:background .2s;border-radius:2px}
.btn:hover{background:var(--accent)}
.err{background:#fff0ee;border:1px solid #f5b8b0;color:var(--accent);
  padding:10px 14px;font-size:.82rem;font-family:var(--mono);margin-bottom:20px;border-radius:2px}
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
<link rel="preconnect" href="https://fonts.googleapis.com">
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
/* ── Layout ── */
.layout{display:flex;height:100vh;overflow:hidden}
/* ── Sidebar ── */
.sidebar{width:var(--sidebar);flex-shrink:0;background:var(--ink);display:flex;
  flex-direction:column;overflow:hidden}
.logo{padding:28px 24px 20px;border-bottom:1px solid rgba(255,255,255,.08)}
.logo-school{font-size:.6rem;letter-spacing:.18em;color:rgba(255,255,255,.4);
  font-family:var(--mono);margin-bottom:4px}
.logo-name{font-size:1.05rem;color:#fff;font-weight:600;line-height:1.3}
.logo-name span{color:#e07060}
.nav{flex:1;padding:16px 0;overflow-y:auto}
.nav-section{padding:8px 20px 4px;font-size:.6rem;letter-spacing:.15em;
  color:rgba(255,255,255,.3);font-family:var(--mono)}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 20px;
  color:rgba(255,255,255,.65);font-size:.82rem;cursor:pointer;
  transition:all .18s;border-left:2px solid transparent;font-family:var(--mono)}
.nav-item:hover{background:rgba(255,255,255,.06);color:#fff}
.nav-item.active{background:rgba(200,67,43,.15);color:#e07060;
  border-left-color:#e07060}
.nav-item .icon{width:16px;text-align:center;opacity:.7;flex-shrink:0}
.sidebar-foot{padding:16px 20px;border-top:1px solid rgba(255,255,255,.08)}
.user-badge{font-size:.72rem;color:rgba(255,255,255,.4);font-family:var(--mono)}
.logout-btn{display:block;margin-top:8px;font-size:.7rem;color:rgba(255,255,255,.3);
  font-family:var(--mono);cursor:pointer;text-decoration:none;transition:color .2s}
.logout-btn:hover{color:#e07060}
/* ── Main ── */
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column}
.topbar{padding:20px 32px;border-bottom:1px solid var(--border);background:var(--card);
  display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.page-title{font-size:1.2rem;font-weight:700}
.page-subtitle{font-size:.75rem;color:var(--muted);font-family:var(--mono);margin-top:2px}
.status-pill{display:flex;align-items:center;gap:6px;font-size:.72rem;
  font-family:var(--mono);color:var(--muted);padding:6px 12px;
  background:var(--paper);border:1px solid var(--border);border-radius:20px}
.status-dot{width:7px;height:7px;border-radius:50%;background:#aaa}
.status-dot.ok{background:#1a7a4a;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.content{flex:1;padding:28px 32px}
/* ── Cards ── */
.stats-row{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:28px}
.stat-card{background:var(--card);border:1.5px solid var(--border);padding:20px;
  position:relative;overflow:hidden}
.stat-card.clickable{cursor:pointer}
.stat-card.clickable:hover{transform:translateY(-2px);box-shadow:0 8px 18px rgba(15,17,23,.08)}
.stat-card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;
  background:var(--accent)}
.stat-card:nth-child(2)::after{background:var(--accent2)}
.stat-card:nth-child(3)::after{background:var(--green)}
.stat-card:nth-child(4)::after{background:#9b6b2b}
.stat-card:nth-child(5)::after{background:#6b4fb3}
.stat-label{font-size:.65rem;letter-spacing:.1em;color:var(--muted);font-family:var(--mono)}
.stat-num{font-size:2rem;font-weight:700;font-family:var(--mono);line-height:1.2;margin-top:4px}
.stat-sub{font-size:.7rem;color:var(--muted);font-family:var(--mono);margin-top:4px}
/* ── Panel ── */
.panel{background:var(--card);border:1.5px solid var(--border)}
.panel-head{padding:16px 20px;border-bottom:1px solid var(--border-light);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.panel-title{font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:8px}
.panel-title .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
.filter-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.filter-btn{font-size:.7rem;font-family:var(--mono);padding:4px 12px;border:1px solid var(--border);
  background:transparent;cursor:pointer;color:var(--muted);transition:all .18s;border-radius:2px}
.filter-btn.active,.filter-btn:hover{background:var(--ink);color:#fff;border-color:var(--ink)}
.search-box{font-size:.75rem;font-family:var(--mono);padding:5px 10px;border:1px solid var(--border);
  background:var(--paper);color:var(--ink);outline:none;width:160px;border-radius:2px}
.search-box:focus{border-color:var(--accent2)}
/* ── Table ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
thead tr{background:var(--paper)}
th{padding:10px 16px;text-align:left;font-size:.65rem;letter-spacing:.1em;
  color:var(--muted);font-family:var(--mono);font-weight:500;
  border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:11px 16px;border-bottom:1px solid var(--border-light);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,0,0,.018)}
.click-row{cursor:pointer}
.click-row:hover td{background:rgba(43,95,200,.08)}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:2px;
  font-size:.65rem;font-family:var(--mono);font-weight:500}
.badge-add{background:#e8f5ee;color:var(--green)}
.badge-del{background:#fdf0ee;color:var(--accent)}
.badge-chg{background:#eef3fd;color:var(--accent2)}
.badge-sch{background:#f5f0e8;color:#9b6b2b}
.score-num{font-family:var(--mono);font-weight:600}
.score-exc{color:var(--green)}
.score-good{color:#2563eb}
.score-mid{color:#5b21b6}
.score-pass{color:#d9480f}
.score-fail{color:var(--accent)}
/* ── Course table grid ── */
.kb-wrap{overflow-x:auto;padding:4px}
.kb{width:100%;border-collapse:collapse;min-width:640px}
.kb th{padding:8px 6px;font-size:.65rem;font-family:var(--mono);
  color:var(--muted);text-align:center;border-bottom:1px solid var(--border);
  letter-spacing:.05em}
.kb td{vertical-align:top;padding:4px;border:1px solid var(--border-light);
  min-width:80px;height:64px}
.kb td.hdr{background:var(--paper);text-align:center;font-size:.65rem;
  font-family:var(--mono);color:var(--muted);font-weight:600;width:44px;
  padding:4px 2px;border-color:var(--border)}
.course-cell{background:linear-gradient(135deg,#eef3fd,#e6eeff);
  border-left:3px solid var(--accent2);padding:5px 7px;height:100%;
  font-size:.7rem;line-height:1.4;border-radius:2px;cursor:pointer}
.course-cell.c2{background:linear-gradient(135deg,#e8f5ee,#dff2e8);border-left-color:var(--green)}
.course-cell.c3{background:linear-gradient(135deg,#fdf5e8,#faeedd);border-left-color:#c8902b}
.course-cell.c4{background:linear-gradient(135deg,#fdf0ee,#fae6e3);border-left-color:var(--accent)}
.course-cell.c5{background:linear-gradient(135deg,#f5e8fd,#eeddf5);border-left-color:#8b2bc8}
.cc-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cc-teacher{color:var(--muted);font-size:.62rem;font-family:var(--mono);margin-top:1px}
.cc-room{color:#666;font-size:.6rem;font-family:var(--mono)}
.cc-week{color:#666;font-size:.6rem;font-family:var(--mono);margin-top:1px;font-style:italic}
/* ── 周视图样式 ── */
.kb-header{padding:16px 20px;background:#f5f5f5;border-bottom:2px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;font-size:.9rem}
.week-info{font-weight:600;color:var(--ink)}
.week-nav{display:flex;align-items:center;gap:8px}
.week-nav-btn{font-size:.72rem;font-family:var(--mono);padding:5px 10px;border:1px solid var(--border);
  background:#fff;cursor:pointer;border-radius:2px;color:var(--ink)}
.week-nav-btn:hover{background:var(--ink);color:#fff;border-color:var(--ink)}
.week-nav-btn:disabled{opacity:.35;cursor:default;background:#f3f3f3;color:#999;border-color:var(--border-light)}
.kb-wrap{overflow-x:auto}
.kb{width:100%;border-collapse:collapse;background:#fff;table-layout:fixed}
.kb thead{background:#fafafa;font-weight:600}
.kb th{padding:8px 6px;border:1px solid var(--border);text-align:center;font-size:.75rem;
  white-space:nowrap}
.kb th:not(.time-col){width:calc((100% - 45px) / 7)}
.kb th.time-col{background:#f5f5f5;width:45px;padding:8px 0}
.date-small{display:block;font-size:.65rem;color:var(--muted);font-weight:400;margin-top:2px}
.kb td{padding:6px;border:1px solid var(--border);text-align:center;height:65px;
  vertical-align:top;overflow:hidden;position:relative}
.kb td.hdr{background:#f5f5f5;font-weight:600;color:var(--ink);width:45px;padding:8px 0;
  font-size:.75rem}
.kb td.empty{background:#fafafa}
.kb td.course-container{padding:2px}
.kb td.merged-follow{background:#fcfcfc}
.kb tr.slot-gap td{border-top:8px solid var(--card)}
.ck-inline{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;color:var(--muted);font-family:var(--mono)}
.ck-inline input{accent-color:var(--accent2)}
.course-span{display:block;overflow:hidden}
/* ── Changes timeline ── */
.timeline{padding:20px}
.tl-item{display:flex;gap:16px;margin-bottom:24px}
.tl-line{display:flex;flex-direction:column;align-items:center;flex-shrink:0}
.tl-dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
  border:2px solid var(--card);box-shadow:0 0 0 2px var(--accent);flex-shrink:0;margin-top:3px}
.tl-dot.score{background:var(--accent2);box-shadow:0 0 0 2px var(--accent2)}
.tl-dot.sch{background:#9b6b2b;box-shadow:0 0 0 2px #9b6b2b}
.tl-vline{width:1px;background:var(--border);flex:1;margin-top:4px}
.tl-body{flex:1}
.tl-time{font-size:.65rem;font-family:var(--mono);color:var(--muted);margin-bottom:6px}
.tl-card{background:var(--paper);border:1px solid var(--border);padding:12px 16px;border-radius:2px}
.tl-type{font-size:.65rem;font-family:var(--mono);margin-bottom:8px;font-weight:600}
.change-row{font-size:.78rem;padding:4px 0;border-bottom:1px solid var(--border-light);
  display:flex;align-items:flex-start;gap:8px}
.change-row:last-child{border-bottom:none;padding-bottom:0}
.change-arrow{color:var(--muted);font-family:var(--mono);font-size:.7rem;flex-shrink:0;margin-top:2px}
/* ── Empty / Loading ── */
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-icon{font-size:2rem;margin-bottom:12px}
.empty-text{font-size:.85rem;font-family:var(--mono)}
.loading{display:flex;align-items:center;justify-content:center;padding:60px;gap:12px;
  color:var(--muted);font-family:var(--mono);font-size:.8rem}
.spinner{width:20px;height:20px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* ── Pagination ── */
.pager{display:flex;justify-content:center;gap:6px;padding:16px}
.pager-btn{font-size:.72rem;font-family:var(--mono);padding:5px 12px;
  border:1px solid var(--border);background:transparent;cursor:pointer;
  color:var(--ink);transition:all .18s;border-radius:2px}
.pager-btn:hover,.pager-btn.active{background:var(--ink);color:#fff;border-color:var(--ink)}
.pager-btn:disabled{opacity:.35;cursor:default}
/* ── Modal ── */
.modal-mask{position:fixed;inset:0;background:rgba(15,17,23,.48);display:none;align-items:center;justify-content:center;z-index:999}
.modal-mask.open{display:flex}
.modal-card{width:min(92vw,660px);max-height:min(86vh,860px);background:#fff;border:1.5px solid var(--border);box-shadow:0 8px 40px rgba(0,0,0,.18);display:flex;flex-direction:column}
.modal-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-size:.9rem;font-weight:700}
.modal-close{border:1px solid var(--border);background:transparent;padding:2px 8px;cursor:pointer;font-family:var(--mono)}
.modal-body{padding:16px 18px;font-size:.82rem;line-height:1.8;overflow:auto}
.kv{display:flex;justify-content:space-between;gap:16px;border-bottom:1px dashed var(--border-light);padding:6px 0}
.kv:last-child{border-bottom:none}
.kv span:first-child{flex:0 0 190px;white-space:nowrap;color:var(--muted);font-family:var(--mono);font-size:.75rem}
.kv span:last-child{flex:1;word-break:break-word}
.raw-json{margin-top:12px;background:#f7f4ed;border:1px solid var(--border-light);padding:10px}
.raw-json summary{cursor:pointer;font-family:var(--mono);font-size:.72rem;color:var(--muted);margin-bottom:8px}
.raw-json pre{margin:0;white-space:pre;font-family:var(--mono);font-size:.68rem;line-height:1.5;max-height:260px;overflow:auto}
.history-table td,.history-table th{white-space:nowrap}
/* ── Views hidden by default ── */
.view{display:none}
.view.active{display:block}
/* ── Responsive ── */
@media(max-width:768px){
  :root{--sidebar:0px}
  .sidebar{position:fixed;left:-240px;top:0;bottom:0;width:240px;z-index:100;transition:left .25s}
  .sidebar.open{left:0}
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .content{padding:16px}
  .topbar{padding:14px 16px}
}
</style>
</head>
<body>
<div class="layout">

<!-- ══ Sidebar ══ -->
<div class="sidebar" id="sidebar">
  <div class="logo">
    <div class="logo-school">NEAU · Academic Monitor</div>
    <div class="logo-name">教务<span>监控</span><br>数据中心</div>
  </div>
  <nav class="nav">
    <div class="nav-section">数据总览</div>
    <div class="nav-item active" onclick="showView('overview')" id="nav-overview">
      <span class="icon">◈</span>概览仪表盘
    </div>
    <div class="nav-section">当前数据</div>
    <div class="nav-item" onclick="showView('schedule')" id="nav-schedule">
      <span class="icon">▦</span>本学期课程表
    </div>
    <div class="nav-item" onclick="showView('scores-term')" id="nav-scores-term">
      <span class="icon">◉</span>本学期成绩
    </div>
    <div class="nav-item" onclick="showView('scores-all')" id="nav-scores-all">
      <span class="icon">≡</span>历史全部成绩
    </div>
    <div class="nav-section">变动记录</div>
    <div class="nav-item" onclick="showView('changes')" id="nav-changes">
      <span class="icon">◷</span>变动日志
    </div>
    <div class="nav-item" onclick="showView('history')" id="nav-history">
      <span class="icon">◫</span>历史数据
    </div>
  </nav>
  <div class="sidebar-foot">
    <div class="user-badge">已登录：{{ user }}</div>
    <a class="logout-btn" href="/logout">退出登录</a>
  </div>
</div>

<!-- ══ Main ══ -->
<div class="main">
  <div class="topbar">
    <div>
      <div class="page-title" id="topbar-title">概览仪表盘</div>
      <div class="page-subtitle" id="topbar-sub">教务系统数据概览</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="status-pill">
        <div class="status-dot ok" id="status-dot"></div>
        <span id="status-text" style="font-family:var(--mono);font-size:.72rem">加载中…</span>
      </div>
      <button onclick="refreshCurrent()" style="font-size:.72rem;font-family:var(--mono);
        padding:6px 14px;border:1px solid var(--border);background:transparent;
        cursor:pointer;border-radius:2px;transition:all .18s" onmouseover="this.style.background='var(--ink)';this.style.color='#fff'" onmouseout="this.style.background='transparent';this.style.color='inherit'">↻ 刷新</button>
    </div>
  </div>

  <div class="content">

    <!-- ── 概览 ── -->
    <div class="view active" id="view-overview">
      <div class="stats-row" id="stats-row">
        <div class="stat-card clickable" onclick="showView('schedule')"><div class="stat-label">课程总数</div><div class="stat-num" id="st-sch">—</div><div class="stat-sub">点击查看课程表</div></div>
        <div class="stat-card clickable" onclick="showView('scores-term')"><div class="stat-label">本学期成绩</div><div class="stat-num" id="st-ts">—</div><div class="stat-sub">点击查看详情</div></div>
        <div class="stat-card clickable" onclick="showView('scores-all')"><div class="stat-label">历史成绩</div><div class="stat-num" id="st-as">—</div><div class="stat-sub">点击查看详情</div></div>
        <div class="stat-card clickable" onclick="showView('changes')"><div class="stat-label">变动记录</div><div class="stat-num" id="st-ch">—</div><div class="stat-sub">点击查看日志</div></div>
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

    <!-- ── 课程表 ── -->
    <div class="view" id="view-schedule">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:var(--accent2)"></div>本学期课程表</div>
          <div class="filter-row">
            <button class="filter-btn active" onclick="setSchedView('grid',this)">课表视图</button>
            <button class="filter-btn" onclick="setSchedView('list',this)">列表视图</button>
            <label class="ck-inline"><input type="checkbox" id="sched-show-all" onchange="toggleSchedShowAll(this.checked)">列表显示全部课程</label>
          </div>
        </div>
        <div id="schedule-grid"><div class="loading"><div class="spinner"></div>加载中…</div></div>
        <div id="schedule-list" style="display:none"></div>
      </div>
    </div>

    <!-- ── 本学期成绩 ── -->
    <div class="view" id="view-scores-term">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:var(--green)"></div>本学期成绩</div>
          <div class="filter-row">
            <input class="search-box" placeholder="搜索课程…" oninput="filterScores('term',this.value)" id="search-term">
          </div>
        </div>
        <div class="tbl-wrap" id="scores-term-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

    <!-- ── 历史成绩 ── -->
    <div class="view" id="view-scores-all">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:#9b6b2b"></div>历史全部成绩</div>
          <div class="filter-row">
            <input class="search-box" placeholder="搜索课程…" oninput="filterScores('all',this.value)" id="search-all">
          </div>
        </div>
        <div class="tbl-wrap" id="scores-all-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

    <!-- ── 变动日志 ── -->
    <div class="view" id="view-changes">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title"><div class="dot" style="background:#9b6b2b"></div>变动日志</div>
          <div class="filter-row">
            <button class="filter-btn active" onclick="filterChanges('',this)">全部</button>
            <button class="filter-btn" onclick="filterChanges('schedule',this)">课程表</button>
            <button class="filter-btn" onclick="filterChanges('this_term_scores',this)">本学期成绩</button>
            <button class="filter-btn" onclick="filterChanges('all_scores',this)">历史成绩</button>
          </div>
        </div>
        <div id="changes-content"><div class="loading"><div class="spinner"></div>加载中…</div></div>
        <div class="pager" id="changes-pager"></div>
      </div>
    </div>

    <!-- ── 历史归档 ── -->
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
            <button class="filter-btn" onclick="setHistoryType('academic_info',this)">学业信息</button>
          </div>
        </div>
        <div class="tbl-wrap" id="history-table"><div class="loading"><div class="spinner"></div>加载中…</div></div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->
</div><!-- /layout -->

  <div class="modal-mask" id="gpa-modal" onclick="closeGpaModal(event)">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title">GPA 详情</div>
        <button class="modal-close" onclick="closeGpaModal()">关闭</button>
      </div>
      <div class="modal-body" id="gpa-modal-body">
        <div class="kv"><span>GPA</span><span id="gpa-v">-</span></div>
        <div class="kv"><span>班级排名</span><span id="gpa-cr">-</span></div>
        <div class="kv"><span>年级排名</span><span id="gpa-gr">-</span></div>
        <div class="kv"><span>生成时间</span><span id="gpa-t">-</span></div>
        <div class="kv"><span>来源页面</span><span id="gpa-src">-</span></div>
      </div>
    </div>
  </div>

  <div class="modal-mask" id="detail-modal" onclick="closeDetailModal(event)">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title" id="detail-modal-title">详情</div>
        <button class="modal-close" onclick="closeDetailModal()">关闭</button>
      </div>
      <div class="modal-body" id="detail-modal-body"></div>
    </div>
  </div>

<script>
// ── State ──────────────────────────────────────────────────────────
const S = {
  schedule: null,          // 本周课程
  allSchedule: null,       // 全部课程
  currentWeek: 1,          // 当前周数
  viewWeek: 1,             // 当前查看周
  maxWeek: 1,              // 课表可浏览最大周
  semesterStart: null,     // 学期开始日期
  gpaInfo: null,
  showAllInList: false,
  scoresTerm: null,
  scoresAll: null,
  renderedScores: { term: [], all: [] },
  gridDetailRows: [],
  listDetailRows: [],
  schedView: 'grid',
  changesPage: 1,
  changesType: '',
  changesTotal: 0,
  historyType: '',
  historyRows: [],
};

const DAYS  = ['一','二','三','四','五','六','日'];
const SECTIONS = ['第1节','第2节','第3节','第4节','第5节','第6节','第7节','第8节','第9节','第10节','第11节','第12节'];
const COLORS = ['','c2','c3','c4','c5'];
const FIELD_LABELS = {
  kcm: '课程名称',
  courseName: '课程名称',
  kch: '课程号',
  courseNumber: '课程号',
  skjs: '任课教师',
  teacherName: '任课教师',
  skxq: '上课星期',
  skjc: '上课节次',
  skzc: '上课周次',
  jxdd: '上课地点',
  kxh: '班序号',
  coureSequenceNumber: '班序号',
  classNo: '行政班',
  cj: '成绩',
  courseScore: '成绩分数',
  gradeName: '等级',
  gradePointScore: '绩点',
  credit: '学分',
  entryStatusCode: '成绩状态',
  scoreEntryModeCode: '成绩录入方式',
  examTypeCode: '考试类型',
  studyModeCode: '学习方式',
  courseAttributeName: '课程属性',
  courseAttributeCode: '课程属性编码',
  academicYearCode: '学年',
  termName: '学期',
  termCode: '学期编码',
  termTypeName: '学期类型',
  termTypeCode: '学期类型编码',
  examTime: '考试时间',
  operatingTime: '录入时间',
  planName: '培养方案',
  planName2: '培养方案说明',
  planNO: '培养方案编号',
  classNo: '行政班',
  cycle: '学时周期',
  zscj: '折算成绩',
  wclyscj: '原始成绩',
  xkcsxdm: '选课属性编码',
  xkcsxmc: '选课属性名称',
  tdkcm: '替代课程名',
  cjlrfsdm: '成绩录入方式编码',
  bm: '编码',
  xkkzm: '选课控制码',
  notByReasonCode: '不通过原因编码',
  notByReasonName: '不通过原因',
  substituteCourseNo: '替代课程号',
  englishCourseName: '英文课程名',
  remark: '备注',
  executiveEducationPlanNumber: '执行方案编号',
  startTime: '开始时间',
  studentId: '学号',
  operator: '操作人',
  kch_zj: '课程号（主）',
  gradeScore: '等级分数',
  slots: '节次',
  rawCount: '原始课程数',
  raw: '原始课程'
};

// ── Navigation ────────────────────────────────────────────────────
const VIEW_META = {
  overview:     {title:'概览仪表盘',     sub:'教务系统数据概览'},
  schedule:     {title:'本学期课程表',   sub:'当前学期所有排课信息'},
  'scores-term':{title:'本学期成绩',     sub:'本学期已录入的成绩'},
  'scores-all': {title:'历史全部成绩',   sub:'累计所有已通过课程成绩'},
  changes:      {title:'变动日志',       sub:'监控检测到的所有数据变动记录'},
  history:      {title:'历史数据',       sub:'抓取数据的归档快照与核验记录'},
};

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('view-'+id)?.classList.add('active');
  document.getElementById('nav-'+id)?.classList.add('active');
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
  const id = active.id.replace('view-','');

  // 强制刷新当前视图对应缓存，避免按钮“看起来没反应”
  if (id === 'schedule') {
    S.schedule = null;
    S.allSchedule = null;
  }
  if (id === 'scores-term') S.scoresTerm = null;
  if (id === 'scores-all') S.scoresAll = null;

  const st = document.getElementById('status-text');
  if (st) st.textContent = '刷新中...';

  Promise.all([loadStatus(), loadView(id)]).catch(() => {}).finally(() => {
    // loadStatus 会在成功后恢复状态文本
  });
}

// ── Fetch helpers ─────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ── Status pill ───────────────────────────────────────────────────
async function loadStatus() {
  try {
    const d = await api('/api/status');
    document.getElementById('st-sch').textContent = d.schedule_cnt;
    document.getElementById('st-ts').textContent  = d.term_score_cnt;
    document.getElementById('st-as').textContent  = d.all_score_cnt;
    document.getElementById('st-ch').textContent  = d.changes_cnt;
    document.getElementById('st-gpa').textContent = d.gpa || '-';
    const t = d.last_run || '未知';
    const n = d.next_run || '未知';
    document.getElementById('status-text').textContent = `上次: ${t} | 下次: ${n}`;
    document.getElementById('status-dot').className = 'status-dot ok';
  } catch(e) {
    document.getElementById('status-text').textContent = '连接失败';
    document.getElementById('status-dot').className = 'status-dot';
  }
}

// ── Overview ──────────────────────────────────────────────────────
async function loadOverview() {
  await loadStatus();
  await loadGpaInfo();
  try {
    const d = await api('/api/changes?limit=5');
    const el = document.getElementById('overview-changes');
    if (!d.data.length) { el.innerHTML = emptyHtml('暂无变动记录'); return; }
    el.innerHTML = '<div class="timeline" style="padding:16px 20px">' +
      d.data.map(c => tlItem(c)).join('') + '</div>';
  } catch(e) { document.getElementById('overview-changes').innerHTML = errHtml(); }
}

async function loadGpaInfo() {
  try {
    const d = await api('/api/gpa');
    S.gpaInfo = d.data || {};
  } catch(e) {
    S.gpaInfo = {};
  }
}

function openGpaModal() {
  const g = S.gpaInfo || {};
  document.getElementById('gpa-v').textContent = g.gpa || '-';
  document.getElementById('gpa-cr').textContent = g.class_rank || '-';
  document.getElementById('gpa-gr').textContent = g.grade_rank || '-';
  document.getElementById('gpa-t').textContent = g.generated_at || '-';
  document.getElementById('gpa-src').textContent = g.source_url || '-';
  document.getElementById('gpa-modal').classList.add('open');
}

function closeGpaModal(evt) {
  if (evt && evt.target && evt.target.id !== 'gpa-modal') return;
  document.getElementById('gpa-modal').classList.remove('open');
}

function labelOfKey(key) {
  return FIELD_LABELS[key] || key;
}

function prettyValue(v) {
  if (v === null || v === undefined || v === '') return '-';
  if (typeof v === 'object') return JSON.stringify(v, null, 2);
  return String(v);
}

function flattenObject(obj, prefix='') {
  const out = {};
  if (!obj || typeof obj !== 'object') return out;
  Object.entries(obj).forEach(([k, v]) => {
    const nk = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      Object.assign(out, flattenObject(v, nk));
    } else {
      out[nk] = v;
    }
  });
  return out;
}

function openDetailModal(title, dataObj, preferred=[]) {
  const flat = flattenObject(dataObj || {});
  const used = new Set();
  const rows = [];
  preferred.forEach(k => {
    if (k in flat && prettyValue(flat[k]) !== '-') {
      used.add(k);
      rows.push(`<div class="kv"><span>${esc(labelOfKey(k.split('.').slice(-1)[0]))}</span><span>${esc(prettyValue(flat[k]))}</span></div>`);
    }
  });
  Object.keys(flat).forEach(k => {
    if (used.has(k)) return;
    if (prettyValue(flat[k]) === '-') return;
    rows.push(`<div class="kv"><span>${esc(labelOfKey(k.split('.').slice(-1)[0]))}</span><span>${esc(prettyValue(flat[k]))}</span></div>`);
  });

  document.getElementById('detail-modal-title').textContent = title || '详情';
  document.getElementById('detail-modal-body').innerHTML =
    (rows.join('') || '<div class="empty-text">暂无可展示字段</div>') +
    `<details class="raw-json"><summary>原始数据</summary><pre>${esc(JSON.stringify(dataObj || {}, null, 2))}</pre></details>`;
  document.getElementById('detail-modal').classList.add('open');
}

function closeDetailModal(evt) {
  if (evt && evt.target && evt.target.id !== 'detail-modal') return;
  document.getElementById('detail-modal').classList.remove('open');
}

function extractWeeks(skzc) {
  const src = String(skzc || '').trim();
  if (!src) return [];
  if (/^[01]+$/.test(src)) {
    const arr = [];
    for (let i = 0; i < src.length; i++) if (src[i] === '1') arr.push(i + 1);
    return arr;
  }
  const nums = [];
  const rangeReg = /(\d+)\s*[-~至]\s*(\d+)/g;
  let m;
  while ((m = rangeReg.exec(src)) !== null) {
    const a = parseInt(m[1], 10);
    const b = parseInt(m[2], 10);
    if (!Number.isInteger(a) || !Number.isInteger(b)) continue;
    const lo = Math.min(a, b), hi = Math.max(a, b);
    for (let w = lo; w <= hi; w++) nums.push(w);
  }
  const singleReg = /(?:第)?(\d+)(?:周)?/g;
  while ((m = singleReg.exec(src)) !== null) {
    const v = parseInt(m[1], 10);
    if (Number.isInteger(v)) nums.push(v);
  }
  return [...new Set(nums.filter(x => x > 0))].sort((a, b) => a - b);
}

function isCourseInWeekClient(course, week) {
  const weeks = extractWeeks(course.skzc || course.weekRange || '');
  return weeks.includes(week);
}

function detectMaxWeek(courses) {
  let maxW = 0;
  (courses || []).forEach(c => {
    const ws = extractWeeks(c.skzc || c.weekRange || '');
    if (ws.length) maxW = Math.max(maxW, ws[ws.length - 1]);
  });
  return Math.max(maxW, S.currentWeek || 1);
}

function getWeekDateRange(week) {
  if (!S.semesterStart) return { start: '-', end: '-' };
  const semesterStart = new Date(S.semesterStart);
  const weekStartDate = new Date(semesterStart);
  weekStartDate.setDate(weekStartDate.getDate() + (week - 1) * 7);
  const weekEndDate = new Date(weekStartDate);
  weekEndDate.setDate(weekEndDate.getDate() + 6);
  return {
    start: `${weekStartDate.getMonth()+1}月${weekStartDate.getDate()}日`,
    end: `${weekEndDate.getMonth()+1}月${weekEndDate.getDate()}日`,
    startDate: weekStartDate,
  };
}

function getCoursesByWeek(week) {
  const src = S.allSchedule || [];
  return src.filter(c => isCourseInWeekClient(c, week));
}

function scheduleWeekHeaderHtml() {
  const r = getWeekDateRange(S.viewWeek);
  const prevDisabled = S.viewWeek <= 1 ? 'disabled' : '';
  const nextDisabled = S.viewWeek >= S.maxWeek ? 'disabled' : '';
  return `<div class="kb-header">
    <div class="week-info">第 ${S.viewWeek} 周（${r.start} - ${r.end}）</div>
    <div class="week-nav">
      <button class="week-nav-btn" onclick="changeWeek(-1)" ${prevDisabled}>← 上一周</button>
      <button class="week-nav-btn" onclick="goCurrentWeek()">回到本周</button>
      <button class="week-nav-btn" onclick="changeWeek(1)" ${nextDisabled}>下一周 →</button>
    </div>
  </div>`;
}

function changeWeek(delta) {
  if (!S.allSchedule) return;
  const next = Math.min(S.maxWeek, Math.max(1, S.viewWeek + delta));
  if (next === S.viewWeek) return;
  S.viewWeek = next;
  renderSchedule();
}

function goCurrentWeek() {
  if (S.viewWeek === S.currentWeek) return;
  S.viewWeek = S.currentWeek;
  renderSchedule();
}

// ── Schedule 工具函数 ────────────────────────────────────────────
/**
 * 解析上课周次
 * 支持两种格式：
 * 1. 二进制字符串：如 "000011000000000000000000"（第4-5周）
 * 2. 文本格式：如 "第1周"、"第1-3周"
 */
function parseWeekRange(skzc) {
  if (!skzc) return '-';
  skzc = String(skzc).trim();
  
  // 如果已经是文本格式，直接返回
  if (skzc.includes('第') || skzc.includes('周') || skzc.includes('~')) {
    return skzc;
  }
  
  // 检查是否是二进制字符串（全为0和1）
  if (/^[01]+$/.test(skzc)) {
    const weeks = [];
    // 从左到右遍历，记录每一位对应的周次
    for (let i = 0; i < skzc.length; i++) {
      if (skzc[i] === '1') {
        weeks.push(i + 1);
      }
    }
    
    if (weeks.length === 0) return '-';
    
    // 合并连续的周次
    const ranges = [];
    let start = weeks[0];
    let prev = weeks[0];
    
    for (let i = 1; i < weeks.length; i++) {
      if (weeks[i] - prev === 1) {
        prev = weeks[i];
      } else {
        ranges.push(start === prev ? `第${start}周` : `第${start}-${prev}周`);
        start = weeks[i];
        prev = weeks[i];
      }
    }
    ranges.push(start === prev ? `第${start}周` : `第${start}-${prev}周`);
    
    return ranges.join('、');
  }
  
  return skzc;
}

/**
 * 格式化上课时间
 */
function formatClassTime(skxq, skjc) {
  skxq = (skxq || '').toString().trim();
  skjc = (skjc || '').toString().trim();
  
  if (!skxq && !skjc) return '—';
  if (skxq && !skjc) return `周${['一','二','三','四','五','六','日'][parseInt(skxq)-1]||skxq}`;
  if (!skxq && skjc) return `第${skjc}节`;
  
  // 解析节次范围
  let sectionStr = skjc;
  if (skjc.includes('-')) {
    const [a, b] = skjc.split('-').map(Number);
    if (a && b) {
      sectionStr = `第${a}-${b}节`;
    } else {
      sectionStr = `第${skjc}节`;
    }
  } else if (skjc) {
    sectionStr = `第${skjc}节`;
  }
  
  const dayStr = `周${['一','二','三','四','五','六','日'][parseInt(skxq)-1]||skxq}`;
  return `${dayStr} ${sectionStr}`;
}

// ── Schedule ──────────────────────────────────────────────────────
async function loadSchedule() {
  if (S.schedule) { renderSchedule(); return; }
  const el = document.getElementById('schedule-grid');
  el.innerHTML = loadingHtml();
  try {
    const d = await api('/api/schedule');
    S.schedule = d.data;           // 本周课程
    S.allSchedule = d.all_data;    // 全部课程
    S.currentWeek = d.current_week;
    S.viewWeek = d.current_week;
    S.semesterStart = d.semester_start;
    S.maxWeek = detectMaxWeek(S.allSchedule);
    renderSchedule();
  } catch(e) { el.innerHTML = errHtml(); }
}

function setSchedView(v, btn) {
  S.schedView = v;
  document.querySelectorAll('#view-schedule .filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('schedule-grid').style.display = v==='grid' ? '' : 'none';
  document.getElementById('schedule-list').style.display = v==='list' ? '' : 'none';
  if (S.schedule) renderSchedule();
}

function toggleSchedShowAll(checked) {
  S.showAllInList = !!checked;
  if (S.schedView === 'list' && S.schedule) renderSchedList();
}

function getScheduleSource() {
  if (S.schedView === 'list' && S.showAllInList) return S.allSchedule || [];
  return getCoursesByWeek(S.viewWeek);
}

function renderSchedule() {
  if (S.schedView === 'grid') renderSchedGrid();
  else renderSchedList();
}

function parseSectionRange(course) {
  const raw = String(course.skjc || course.section || '').trim();
  if (!raw) return null;
  if (raw.includes('-')) {
    const [a, b] = raw.split('-').map(Number);
    if (Number.isInteger(a) && Number.isInteger(b) && a > 0 && b >= a) {
      return { start: a, end: b };
    }
  }
  const start = parseInt(raw, 10);
  if (!Number.isInteger(start) || start <= 0) return null;
  const dur = parseInt(course.continuingSession || course.duration || 1, 10);
  const span = Number.isInteger(dur) && dur > 1 ? dur : 1;
  return { start, end: start + span - 1 };
}

function renderSchedGrid() {
  const el = document.getElementById('schedule-grid');
  el.style.display = '';
  const coursesData = getCoursesByWeek(S.viewWeek);
  if (!coursesData.length) {
    el.innerHTML = scheduleWeekHeaderHtml() + emptyHtml('本周无课程'); 
    return; 
  }

  // Build start map: [day][startSec] -> courses[] and covered map for rowspan display.
  const startMap = {};
  const covered = {};
  const colorMap = {};
  const detailRows = [];
  let colorIdx = 0;
  const maxSec = 12;

  coursesData.forEach(c => {
    const xq  = parseInt(c.skxq  || c.weekDay  || 0);
    const kcm = c.kcm || c.courseName || '';
    if (!xq) return;
    const range = parseSectionRange(c);
    if (!range) return;
    const start = Math.max(1, range.start);
    const end = Math.min(maxSec, range.end);
    const span = Math.max(1, end - start + 1);

    if (!colorMap[kcm]) { colorMap[kcm] = COLORS[colorIdx++ % COLORS.length]; }

    if (!startMap[xq]) startMap[xq] = {};
    if (!startMap[xq][start]) startMap[xq][start] = [];
    const idx = detailRows.push(c) - 1;
    startMap[xq][start].push({ ...c, _idx: idx, _color: colorMap[kcm], _span: span, _start: start, _end: end });

    if (!covered[xq]) covered[xq] = new Set();
    for (let s = start + 1; s <= end; s++) covered[xq].add(s);
  });

  const range = getWeekDateRange(S.viewWeek);
  const weekStartDate = range.startDate;

  // 生成表格
  const days7 = [1,2,3,4,5,6,7];
  let html = scheduleWeekHeaderHtml() + '<div class="kb-wrap"><table class="kb"><thead><tr><th class="time-col">时间</th>';
  
  days7.forEach(d => { 
    const date = new Date(weekStartDate);
    date.setDate(date.getDate() + d - 1);
    const dateStr = `${date.getMonth()+1}/${date.getDate()}`;
    html += `<th>周${DAYS[d-1]}<br><span class="date-small">${dateStr}</span></th>`; 
  });
  html += '</tr></thead><tbody>';

  for (let sec=1; sec<=maxSec; sec++) {
    const gapClass = (sec === 5 || sec === 9) ? 'slot-gap' : '';
    html += `<tr class="${gapClass}"><td class="hdr time-col">${sec}</td>`;
    days7.forEach(d => {
      const courses = (startMap[d] && startMap[d][sec]) || [];
      const isCovered = covered[d] && covered[d].has(sec);
      if (!courses.length && isCovered) { return; }
      if (!courses.length) { html += '<td class="empty"></td>'; return; }
      const maxSpan = Math.max(...courses.map(x => x._span || 1));
      html += `<td class="course-container" rowspan="${maxSpan}">`;
      courses.forEach(c => {
        const name = c.kcm || c.courseName || '';
        const teacher = c.skjs || c.teacherName || '';
        const room = c.jxdd || c.classroom || '';
        const secText = `第${c._start}-${c._end}节`;
        html += `<div class="course-cell ${c._color}" onclick="openCourseDetail('grid', ${c._idx})" title="点击查看课程详情">
          <div class="cc-name" title="${esc(name)}">${esc(name)}</div>
          ${teacher ? `<div class="cc-teacher">${esc(teacher)}</div>` : ''}
          <div class="cc-teacher">${secText}</div>
          ${room    ? `<div class="cc-room">${esc(room)}</div>` : ''}
        </div>`;
      });
      html += '</td>';
    });
    html += '</tr>';
  }
  html += '</tbody></table></div>';
  S.gridDetailRows = detailRows;
  el.innerHTML = html;
}

function renderSchedList() {
  const el = document.getElementById('schedule-list');
  el.style.display = '';
  const coursesData = getScheduleSource();
  if (!coursesData.length) {
    el.innerHTML = emptyHtml(S.showAllInList ? '暂无课程数据' : '本周无课程');
    return;
  }
  const range = getWeekDateRange(S.viewWeek);
  const startStr = range.start;
  const endStr = range.end;

  const groups = {};
  coursesData.forEach(c => {
    const kch = String(c.kch || c.courseNumber || '').trim();
    const kcm = String(c.kcm || c.courseName || '').trim();
    const key = kch || kcm;
    if (!groups[key]) {
      groups[key] = {
        kch,
        kcm,
        teacher: new Set(),
        seq: new Set(),
        slots: [],
        raw: [],
      };
    }

    const teacher = String(c.skjs || c.teacherName || '').trim();
    if (teacher) groups[key].teacher.add(teacher);
    const seq = String(c.kxh || c.classSequenceNumber || c.courseSequenceNumber || ((c.id || {}).coureSequenceNumber) || '').trim();
    if (seq) groups[key].seq.add(seq);
    groups[key].raw.push(c);

    groups[key].slots.push({
      time: formatClassTime(c.skxq || c.weekDay, c.skjc || c.section),
      week: parseWeekRange(c.skzc || c.weekRange),
      room: String(c.jxdd || c.classroom || '').trim(),
      skxq: parseInt(c.skxq || c.weekDay || 0, 10) || 0,
      sec: String(c.skjc || c.section || '').trim(),
    });
  });

  const arr = Object.values(groups).sort((a, b) => (a.kch || a.kcm).localeCompare(b.kch || b.kcm, 'zh-CN'));
  S.listDetailRows = arr;
  const rows = arr.map((g, idx) => {
    const teacher = [...g.teacher].filter(Boolean).join(' / ') || '-';
    const seqNo = [...g.seq].filter(Boolean).join(' / ') || '-';

    const uniq = new Set();
    const slots = (g.slots || []).filter(s => {
      const key = `${s.time}||${s.week}||${s.room}`;
      if (uniq.has(key)) return false;
      uniq.add(key);
      return true;
    }).sort((a, b) => {
      const d = (a.skxq || 0) - (b.skxq || 0);
      if (d !== 0) return d;
      const aStart = parseInt(String(a.sec || '').split('-')[0], 10) || 0;
      const bStart = parseInt(String(b.sec || '').split('-')[0], 10) || 0;
      return aStart - bStart;
    });

    const time = slots.map(s => `<div>${esc(s.time || '-')}</div>`).join('') || '-';
    const week = slots.map(s => `<div>${esc(s.week || '-')}</div>`).join('') || '-';
    const room = slots.map(s => `<div>${esc(s.room || '-')}</div>`).join('') || '-';
    return `<tr class="click-row" onclick="openCourseDetail('list', ${idx})" title="点击查看课程详情">
    <td style="font-family:var(--mono)">${idx + 1}</td>
    <td style="font-family:var(--mono)">${esc(g.kch || '-')}</td>
    <td style="font-family:var(--mono)">${esc(seqNo)}</td>
    <td>${esc(g.kcm || '-')}</td>
    <td>${esc(teacher)}</td>
    <td style="font-family:var(--mono);font-size:.9rem">${time}</td>
    <td style="font-family:var(--mono);font-size:.82rem">${week}</td>
    <td>${room}</td>
  </tr>`;
  }).join('');
  
  const title = S.showAllInList ? '全部课程' : '本周课程';
  const navBlock = S.showAllInList ? '' : scheduleWeekHeaderHtml();
  const headerStr = `${navBlock}<div style="padding:12px 16px;background:#f5f5f5;border-bottom:1px solid #ddd;margin-bottom:0;font-size:.9rem;color:#666">
    <strong>${title}</strong>${S.showAllInList ? '' : '课表'} · 共 ${arr.length} 门
  </div>`;
  
  el.innerHTML = headerStr + `<table><thead><tr>
    <th>课序号</th><th>课程号</th><th>班序号</th><th>课程名称</th><th>教师</th><th>上课时间</th><th>上课周次</th><th>教室</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function openCourseDetail(mode, idx) {
  if (mode === 'grid') {
    const c = (S.gridDetailRows || [])[idx];
    if (!c) return;
    openDetailModal('课程详情', c, [
      'kcm', 'kch', 'kxh', 'skjs', 'skxq', 'skjc', 'skzc', 'jxdd'
    ]);
    return;
  }

  const g = (S.listDetailRows || [])[idx];
  if (!g) return;
  const seqNo = [...(g.seq || [])].filter(Boolean).join(' / ') || '-';
  const detail = {
    kcm: g.kcm || '-',
    kch: g.kch || '-',
    kxh: seqNo,
    skjs: [...(g.teacher || [])].filter(Boolean).join(' / ') || '-',
    slots: (g.slots || []).map(s => `${s.time} | ${s.week} | ${s.room || '-'}`),
    rawCount: (g.raw || []).length,
    raw: g.raw || [],
  };
  openDetailModal('课程详情', detail, ['kcm', 'kch', 'kxh', 'skjs', 'slots', 'rawCount']);
}

// ── Scores ────────────────────────────────────────────────────────
function scoreColor(s) {
  const raw = String(s || '').trim();
  if (raw === '优秀') return 'score-exc';
  if (raw === '良好') return 'score-good';
  if (raw === '中等') return 'score-mid';
  if (raw === '及格') return 'score-pass';
  if (raw === '不及格') return 'score-fail';
  const n = parseFloat(s);
  if (isNaN(n)) return '';
  if (n < 60) return 'score-fail';
  if (n < 70) return 'score-pass';
  if (n < 80) return 'score-mid';
  if (n >= 90) return 'score-exc';
  return 'score-good';
}

function scoreDisplayValue(s, kind) {
  const mode = String(s.scoreEntryModeCode || s.cjlrfsdm || '').trim();
  const gradeName = s.gradeName || s.grade || '';
  // 等级制课程优先显示等级，不显示折算分
  if (mode && mode !== '001' && gradeName) return gradeName;
  return s.cj||s.score||s.grade||s.courseScore||'';
}

function scoresTable(kind, data, filter) {
  let rows = data;
  if (filter) {
    const q = filter.toLowerCase();
    rows = data.filter(s => {
      const name = (s.kcm||s.courseName||'').toLowerCase();
      const kch  = (s.kch||s.courseNumber||'').toLowerCase();
      return name.includes(q) || kch.includes(q);
    });
  }
  S.renderedScores[kind] = rows;
  if (!rows.length) return emptyHtml('暂无成绩数据');
  return `<table><thead><tr>
    <th>课程名称</th><th>课程号</th><th>成绩</th><th>绩点</th><th>学分</th>
  </tr></thead><tbody>` +
  rows.map((s, idx) => {
    const cj = scoreDisplayValue(s, kind);
    const jd = s.jd||s.gradePoint||s.gradePointScore||'';
    const xf = s.xf||s.credit||'';
    const kch = s.kch||s.courseNumber||(s.id&&s.id.courseNumber)||'';
    const kcm = s.kcm||s.courseName||'';
    return `<tr class="click-row" onclick="openScoreDetail('${kind}', ${idx})" title="点击查看成绩详情">
      <td>${esc(kcm)}</td>
      <td style="font-family:var(--mono);font-size:.75rem;color:var(--muted)">${esc(kch)}</td>
      <td><span class="score-num ${scoreColor(cj)}">${esc(cj)||'—'}</span></td>
      <td style="font-family:var(--mono)">${esc(jd)||'—'}</td>
      <td style="font-family:var(--mono)">${esc(xf)||'—'}</td>
    </tr>`;
  }).join('') + '</tbody></table>';
}

function openScoreDetail(kind, idx) {
  const s = ((S.renderedScores || {})[kind] || [])[idx];
  if (!s) return;
  const title = kind === 'all' ? '历史成绩详情' : '本学期成绩详情';
  openDetailModal(title, s, [
    'courseName', 'kcm', 'id.courseNumber', 'kch', 'id.coureSequenceNumber', 'classNo',
    'scoreEntryModeCode', 'gradeName', 'cj', 'courseScore', 'gradePointScore', 'credit',
    'academicYearCode', 'termName', 'examTime', 'operatingTime', 'remark'
  ]);
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
  document.getElementById('scores-term-table').innerHTML = scoresTable('term', S.scoresTerm||[], filter||'');
}
function filterScores(type, q) {
  if (type==='term') renderScoresTerm(q);
  else renderScoresAll(q);
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
  document.getElementById('scores-all-table').innerHTML = scoresTable('all', S.scoresAll||[], filter||'');
}

// ── Changes ───────────────────────────────────────────────────────
async function loadChanges(page, type) {
  if (page  !== undefined) S.changesPage = page;
  if (type  !== undefined) S.changesType = type;
  document.getElementById('changes-content').innerHTML = loadingHtml();
  try {
    const url = `/api/changes?page=${S.changesPage}&limit=15${S.changesType?'&type='+S.changesType:''}`;
    const d = await api(url);
    S.changesTotal = d.total;
    if (!d.data.length) {
      document.getElementById('changes-content').innerHTML = emptyHtml('暂无变动记录');
      document.getElementById('changes-pager').innerHTML = '';
      return;
    }
    document.getElementById('changes-content').innerHTML =
      '<div class="timeline">' + d.data.map(c => tlItem(c)).join('') + '</div>';
    renderPager();
  } catch(e) { document.getElementById('changes-content').innerHTML = errHtml(); }
}

function filterChanges(type, btn) {
  document.querySelectorAll('#view-changes .filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  S.changesPage = 1;
  loadChanges(1, type);
}

function renderPager() {
  const total = S.changesTotal;
  const perPage = 15;
  const pages = Math.ceil(total / perPage);
  const cur = S.changesPage;
  if (pages <= 1) { document.getElementById('changes-pager').innerHTML = ''; return; }
  let html = '';
  if (cur > 1) html += `<button class="pager-btn" onclick="loadChanges(${cur-1})">← 上一页</button>`;
  const start = Math.max(1, cur-2), end = Math.min(pages, cur+2);
  for (let i=start;i<=end;i++) {
    html += `<button class="pager-btn ${i===cur?'active':''}" onclick="loadChanges(${i})">${i}</button>`;
  }
  if (cur < pages) html += `<button class="pager-btn" onclick="loadChanges(${cur+1})">下一页 →</button>`;
  html += `<span style="font-size:.65rem;font-family:var(--mono);color:var(--muted);align-self:center">共 ${total} 条</span>`;
  document.getElementById('changes-pager').innerHTML = html;
}

// ── History archives ──────────────────────────────────────────────
function typeLabel(t) {
  const m = {
    schedule: '课程表',
    this_term_scores: '本学期成绩',
    all_scores: '历史成绩',
    gpa: 'GPA',
    academic_info: '学业信息',
  };
  return m[t] || t || '-';
}

async function loadHistory() {
  const el = document.getElementById('history-table');
  el.innerHTML = loadingHtml();
  try {
    const q = S.historyType ? `?type=${encodeURIComponent(S.historyType)}` : '';
    const d = await api(`/api/history${q}`);
    S.historyRows = d.data || [];
    renderHistory();
  } catch (e) {
    el.innerHTML = errHtml();
  }
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
  if (!rows.length) {
    el.innerHTML = emptyHtml('暂无归档数据（有新数据覆盖时会自动归档）');
    return;
  }

  const body = rows.map((r, idx) => `<tr class="click-row" onclick="openHistorySnapshot(${idx})" title="点击查看归档详情">
    <td>${esc(typeLabel(r.type))}</td>
    <td style="font-family:var(--mono)">${esc(r.time || '-')}</td>
    <td style="font-family:var(--mono)">${esc(r.file || '-')}</td>
    <td style="font-family:var(--mono)">${esc(r.count == null ? '-' : r.count)}</td>
    <td style="font-family:var(--mono)">${esc(r.size == null ? '-' : r.size + ' B')}</td>
  </tr>`).join('');

  el.innerHTML = `<table class="history-table"><thead><tr>
    <th>数据类型</th><th>归档时间</th><th>文件名</th><th>条数/字段数</th><th>文件大小</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

async function openHistorySnapshot(idx) {
  const row = (S.historyRows || [])[idx];
  if (!row) return;
  try {
    const q = `?type=${encodeURIComponent(row.type)}&file=${encodeURIComponent(row.file)}`;
    const d = await api(`/api/history${q}`);
    openDetailModal(`归档详情 · ${typeLabel(row.type)}`, d.data || {}, []);
  } catch (e) {
    openDetailModal('归档详情', { error: '读取归档失败', type: row.type, file: row.file }, []);
  }
}

// ── Timeline item renderer ────────────────────────────────────────
function tlItem(c) {
  const typeMap = {
    schedule:'课程表变动', this_term_scores:'本学期成绩变动', all_scores:'历史成绩变动'
  };
  const dotClass = c.type === 'schedule' ? 'sch' : (c.type==='this_term_scores'?'score':'');
  const typeLabel = typeMap[c.type] || c.type;
  const time = c.time ? c.time.replace('T',' ').slice(0,16) : '';

  let rows = '';
  const kvDiff = (before, after) => {
    const keys = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])];
    const diffs = keys.filter(k => String((before || {})[k] ?? '') !== String((after || {})[k] ?? ''));
    if (!diffs.length) return '';
    return diffs.map(k => {
      const bn = (before || {})[k] ?? '-';
      const an = (after || {})[k] ?? '-';
      return `<div style="font-family:var(--mono);font-size:.68rem;color:var(--muted)">${esc(labelOfKey(k))}: ${esc(bn)} <span class="change-arrow">→</span> ${esc(an)}</div>`;
    }).join('');
  };
  // Added items
  (c.added||[]).forEach(item => {
    const name = item.kcm||item.courseName||'';
    const detail = item.cj ? `成绩: ${item.cj}  绩点: ${item.jd||'—'}  学分: ${item.xf||'—'}` :
      `周${item.skxq} 第${item.skjc}节  ${item.jxdd||''}`;
    rows += `<div class="change-row"><span class="badge badge-add">+新增</span>
      <span>${esc(name)} &nbsp;<span style="color:var(--muted);font-size:.72rem;font-family:var(--mono)">${esc(detail)}</span></span></div>`;
  });
  // Removed
  (c.removed||[]).forEach(item => {
    const name = item.kcm||item.courseName||'';
    rows += `<div class="change-row"><span class="badge badge-del">−删除</span>
      <span>${esc(name)} &nbsp;<span style="color:var(--muted);font-size:.72rem;font-family:var(--mono)">周${item.skxq} 第${item.skjc}节</span></span></div>`;
  });
  // Modified (schedule)
  (c.modified||[]).forEach(item => {
    const name = (item.before||{}).kcm||(item.after||{}).kcm||'';
    const b = item.before||{}, a = item.after||{};
    const details = kvDiff(b, a);
    rows += `<div class="change-row"><span class="badge badge-chg">≠变更</span>
      <span style="flex:1">${esc(name)}<br>
      <span style="font-family:var(--mono);font-size:.68rem;color:var(--muted)">
        ${b.jxdd||''} 周${b.skxq} 第${b.skjc}节
        <span class="change-arrow">→</span>
        ${a.jxdd||''} 周${a.skxq} 第${a.skjc}节
      </span>
      ${details || ''}
      </span></div>`;
  });
  // Changed scores
  (c.changed||[]).forEach(item => {
    const b = item.before||{}, a = item.after||{};
    const name = b.kcm||a.kcm||'';
    const details = kvDiff(b, a);
    rows += `<div class="change-row"><span class="badge badge-chg">≠变更</span>
      <span>${esc(name)} &nbsp;
      <span style="font-family:var(--mono);font-size:.75rem">
        <span class="${scoreColor(b.cj)}">${esc(b.cj)}</span>
        <span class="change-arrow"> → </span>
        <span class="${scoreColor(a.cj)}">${esc(a.cj)}</span>
      </span>
      ${details || ''}
      </span></div>`;
  });

  if (!rows) rows = '<div style="font-size:.75rem;color:var(--muted);font-family:var(--mono)">（无详细变动信息）</div>';

  return `<div class="tl-item">
    <div class="tl-line">
      <div class="tl-dot ${dotClass}"></div>
      <div class="tl-vline"></div>
    </div>
    <div class="tl-body">
      <div class="tl-time">${time}</div>
      <div class="tl-card">
        <div class="tl-type">${typeLabel}</div>
        ${rows}
      </div>
    </div>
  </div>`;
}

// ── Util ──────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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

// ── Init ──────────────────────────────────────────────────────────
loadStatus();
loadOverview();
</script>
</body>
</html>"""


# ─────────────────────────── 入口 ────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 52)
    print("  东北农业大学教务监控 Web 端")
    print(f"  http://127.0.0.1:{port}")
    print(f"  学号: {USERNAME}   数据目录: {DATA_DIR}")
    print("=" * 52)
    app.run(host="0.0.0.0", port=port, debug=False)
