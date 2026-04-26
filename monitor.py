"""
东北农业大学教务系统 - 课程表 & 成绩定时监控脚本
功能：
    1. 定时抓取课程表 / 本学期成绩 / 历史成绩 / GPA概览
  2. 与本地旧数据对比，发现变化时记录日志并推送通知
    3. 支持多种通知渠道：企业微信机器人、Server 酱（微信）、钉钉、Bark（iOS）、飞书、PushDeer
"""

import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import ddddocr
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ────────────────────────────── 配置区 ──────────────────────────────
# 从 config.json 或环境变量读取配置，不再 hardcode 凭据
def _load_config():
    """从 config.json 或环境变量读取配置。"""
    config_file = Path(__file__).parent / "config.json"
    
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"读取 config.json 失败: {e}，尝试从环境变量读取...")
    
    # 环境变量备选
    username = os.environ.get("NEAU_USERNAME")
    password = os.environ.get("NEAU_PASSWORD")
    
    if not username or not password:
        raise RuntimeError(
            "\n=== 配置错误 ===\n"
            "未找到 config.json 且环境变量 NEAU_USERNAME/NEAU_PASSWORD 未设置。\n"
            "\n请执行以下步骤：\n"
            "1. 复制 config.example.json 为 config.json\n"
            "2. 编辑 config.json，填入你的学号和密码\n"
            "3. 重新运行本脚本\n"
            "\n或设置环境变量：\n"
            "  NEAU_USERNAME=你的学号\n"
            "  NEAU_PASSWORD=你的密码\n"
        )
    
    return {
        "username": username,
        "password": password,
        "use_webvpn": os.environ.get("NEAU_USE_WEBVPN", "false").lower() == "true",
        "interval": int(os.environ.get("NEAU_INTERVAL", "1800")),
        "data_dir": os.environ.get("NEAU_DATA_DIR", "./data"),
        "notify": {
            "wecom_webhook": os.environ.get("NEAU_WECOM_WEBHOOK", ""),
            "serverchan_key": os.environ.get("NEAU_SERVERCHAN_KEY", ""),
            "dingtalk_webhook": os.environ.get("NEAU_DINGTALK_WEBHOOK", ""),
            "bark_key": os.environ.get("NEAU_BARK_KEY", ""),
            "feishu_webhook": os.environ.get("NEAU_FEISHU_WEBHOOK", ""),
            "pushdeer_key": os.environ.get("NEAU_PUSHDEER_KEY", ""),
            "telegram_token": os.environ.get("NEAU_TELEGRAM_TOKEN", ""),
            "telegram_chat_id": os.environ.get("NEAU_TELEGRAM_CHAT_ID", ""),
        },
        "base_url": os.environ.get("NEAU_BASE_URL", "https://zhjwxs.neau.edu.cn"),
        "webvpn_auth": os.environ.get("NEAU_WEBVPN_AUTH", "https://authserver-443.webvpn.neau.edu.cn/authserver"),
        "webvpn_base": os.environ.get("NEAU_WEBVPN_BASE", "https://zhjwxs-443.webvpn.neau.edu.cn"),
        "cas_service": os.environ.get("NEAU_CAS_SERVICE", "https://webvpn.neau.edu.cn/users/auth/cas"),
    }

CONFIG = _load_config()
# ─────────────────────────────────────────────────────────────────────

# ══════════════════════════ 日志初始化 ══════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("neau_monitor")


# ══════════════════════════ 常量 & UA ══════════════════════════
UA_PC = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
UA_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


# ══════════════════════════ 加密工具 ══════════════════════════
def _random_str(n: int) -> str:
    import random
    return "".join(random.choice(_AES_CHARS) for _ in range(n))


def _aes_encrypt(password: str, salt: str) -> str:
    key = salt.encode().ljust(16, b"\x00")[:16]
    iv = b"\x00" * 16
    plain = (_random_str(64) + password).encode()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(plain, 16))).decode()


def _extract_score_token(url: str, html: str, kind: str) -> str:
    """从 URL 或 HTML 中提取成绩接口 token 段。"""
    patterns = [
        rf"/scoreQuery/([^/]+)/{kind}",
        rf"scoreQuery/([^/]+)/{kind}",
    ]
    for src in (url, html):
        for pat in patterns:
            m = re.search(pat, src)
            if m and m.group(1):
                return m.group(1).strip()
    return ""


def _extract_list_payload(payload: Any, keys: tuple[str, ...] = ("list", "kbList", "data", "rows", "content", "result")) -> list | None:
    """尽量从常见 JSON 包装结构中提取列表。无法识别时返回 None。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None

    # 广度优先：先看顶层，再看一层嵌套，避免误解析太深层的非业务字段。
    queue: list[Any] = [payload]
    visited: set[int] = set()

    for _ in range(2):
        next_level: list[Any] = []
        for node in queue:
            if id(node) in visited or not isinstance(node, dict):
                continue
            visited.add(id(node))

            for k in keys:
                if k in node:
                    val = node[k]
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        next_level.append(val)

            for v in node.values():
                if isinstance(v, dict):
                    next_level.append(v)
                elif isinstance(v, list):
                    # 某些接口直接放在非标准键里，但值是列表。
                    return v
        queue = next_level

    return None


def _json_shape(payload: Any) -> str:
    if isinstance(payload, list):
        return f"list(len={len(payload)})"
    if isinstance(payload, dict):
        return f"dict(keys={list(payload.keys())[:12]})"
    return type(payload).__name__


def _flatten_schedule_payload(payload: Any) -> list | None:
    """适配新版课程表结构，将 xkxx 展平为课次列表。"""
    if not isinstance(payload, dict):
        return _extract_list_payload(payload)

    # 新版接口主数据在 xkxx[0]，其 value 是课程信息 dict。
    xkxx = payload.get("xkxx")
    if isinstance(xkxx, list) and xkxx and isinstance(xkxx[0], dict):
        rows: list[dict[str, Any]] = []
        for course in xkxx[0].values():
            if not isinstance(course, dict):
                continue
            cid_obj = course.get("id")
            cid: dict[str, Any] = cid_obj if isinstance(cid_obj, dict) else {}
            kch = str(
                course.get("courseNumber")
                or course.get("coureNumber")
                or cid.get("courseNumber")
                or cid.get("coureNumber")
                or ""
            )
            kcm = str(course.get("courseName") or "")
            skjs = str(course.get("attendClassTeacher") or "").strip()
            skzc = str(course.get("skzcs") or "")

            tps = course.get("timeAndPlaceList") if isinstance(course.get("timeAndPlaceList"), list) else []
            if not tps:
                rows.append({
                    "kch": kch,
                    "kcm": kcm,
                    "skjs": skjs,
                    "skxq": "",
                    "skjc": "",
                    "skzc": skzc,
                    "jxdd": "",
                })
                continue

            for tp in tps:
                if not isinstance(tp, dict):
                    continue
                start = tp.get("classSessions")
                span = tp.get("continuingSession")
                if isinstance(start, int) and isinstance(span, int) and span > 1:
                    skjc = f"{start}-{start + span - 1}"
                else:
                    skjc = str(start or tp.get("classSessionsName") or "")

                rows.append({
                    "kch": kch or str(tp.get("coureNumber") or ""),
                    "kcm": kcm or str(tp.get("coureName") or ""),
                    "skjs": skjs,
                    "skxq": str(tp.get("classDay") or ""),
                    "skjc": skjc,
                    "skzc": str(tp.get("classWeek") or skzc),
                    "jxdd": str(tp.get("classroomName") or ""),
                })
        return rows

    return _extract_list_payload(payload)


def _flatten_all_scores_payload(payload: Any) -> list | None:
    """适配新版历史成绩结构，将 lnList[].cjList[] 扁平化。"""
    if isinstance(payload, dict) and isinstance(payload.get("lnList"), list):
        rows: list[dict[str, Any]] = []
        for term in payload.get("lnList", []):
            if not isinstance(term, dict):
                continue
            cj_list = term.get("cjList")
            if isinstance(cj_list, list):
                rows.extend([x for x in cj_list if isinstance(x, dict)])
        return rows

    return _extract_list_payload(payload)


# ══════════════════════════ 登录流程 ══════════════════════════
def _first_group_match(text: str, patterns: list) -> str:
    """依次尝试 patterns，返回第一个匹配的 group(1)，全部失败返回空串。"""
    for pattern in patterns:
        match = re.search(pattern, text)
        if match and match.group(1):
            return match.group(1)
    return ""


def _get_page_params(sess: requests.Session, url: str, ua: dict | None = None) -> dict:
    """访问 CAS 登录页，提取 execution 和 pwdEncryptSalt，同时返回最终 URL。
    完全对齐 app.py 中的 _get_login_page_params 逻辑（多模式 fallback 正则）。
    """
    if ua is None:
        ua = UA_MOBILE
    r = sess.get(url, headers=ua, allow_redirects=True, timeout=10)
    final_url = r.url
    html = r.text
    result = {}
    soup = BeautifulSoup(html, "html.parser")

    # execution
    tag = soup.find("input", {"name": "execution"})
    if tag and tag.get("value"):
        result["execution"] = tag["value"]

    # pwdEncryptSalt / rsaPublicKey
    for name in ("pwdEncryptSalt", "rsaPublicKey"):
        tag2 = soup.find("input", {"id": name}) or soup.find("input", {"name": name})
        if tag2 and tag2.get("value"):
            result["pub_key"] = tag2["value"]
            break

    # fallback：多模式宽松正则（兼容页面结构变化）
    if not result.get("execution"):
        execution_patterns = [
            r'(?:name|id)=["\']execution["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']execution["\']',
            r'"execution"\s*[:=]\s*"([^"]{8,})"',
            r"'execution'\s*[:=]\s*'([^']{8,})'",
        ]
        result["execution"] = _first_group_match(html, execution_patterns)

    if not result.get("pub_key"):
        salt_patterns = [
            r'(?:name|id)=["\']pwdEncryptSalt["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']pwdEncryptSalt["\']',
            r'(?:name|id)=["\']rsaPublicKey["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']rsaPublicKey["\']',
            r'"pwdEncryptSalt"\s*[:=]\s*"([^"]{8,64})"',
            r"'pwdEncryptSalt'\s*[:=]\s*'([^']{8,64})'",
        ]
        result["pub_key"] = _first_group_match(html, salt_patterns)

    result["final_url"] = final_url
    ex_info = f"有(len={len(result['execution'])})" if result.get("execution") else "无！"
    sk_info = f"有({result['pub_key'][:8]}...)" if result.get("pub_key") else "无"
    log.info(f"登录页参数: execution={ex_info} salt={sk_info} final_url={final_url[:60]}")
    return result


def _recognize_captcha(sess: requests.Session, base_url: str, ua: dict) -> tuple[str, bytes]:
    r = sess.get(f"{base_url}/img/captcha.jpg", headers=ua, timeout=10)
    r.raise_for_status()
    ocr  = ddddocr.DdddOcr(show_ad=False)
    raw  = ocr.classification(r.content)
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    log.info(f"验证码识别: {text}")
    return text, r.content


def login_direct(sess: requests.Session, username: str, password: str,
                 base_url: str) -> bool:
    """直连教务系统登录（带验证码）。"""
    log.info("尝试直连登录…")
    try:
        page = sess.get(f"{base_url}/login", headers=UA_PC, timeout=10)
    except Exception as e:
        log.error(f"无法访问登录页: {e}")
        return False

    # token
    m = re.search(r'(?:name|id)=["\']tokenValue["\'][^>]*value=["\']([^"\']{10,})["\']', page.text)
    tok = m.group(1) if m else ""

    # 验证码，最多重试 5 次
    for attempt in range(5):
        cap_text, _ = _recognize_captcha(sess, base_url, UA_PC)
        r = sess.post(
            f"{base_url}/j_spring_security_check",
            data={"lang": "zh", "tokenValue": tok,
                  "j_username": username, "j_password": password,
                  "j_captcha": cap_text},
            headers={**UA_PC,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{base_url}/login"},
            allow_redirects=True, timeout=15,
        )
        if "login" not in r.url and "j_spring_security_check" not in r.url:
            log.info("直连登录成功")
            return True
        if "密码" in r.text or "password" in r.text.lower():
            log.error("账号或密码错误，请检查配置")
            return False
        log.warning(f"验证码错误，重试 {attempt + 1}/5…")
    log.error("验证码识别连续失败")
    return False


def _do_webvpn_reauth(sess: requests.Session, reauth_url: str, password: str,
                      webvpn_auth: str, cas_service: str) -> bool:
    """完成 WebVPN 二次认证（reAuthType=2，密码验证）。对齐 app.py _do_reauth 逻辑。"""
    if not reauth_url.startswith("http"):
        # 补全相对 URL（authserver origin）
        reauth_url = f"{webvpn_auth.rsplit('/authserver', 1)[0]}{reauth_url}"
    log.info(f"二次认证页: {reauth_url[:80]}")
    params = _get_page_params(sess, reauth_url)
    pub_key = params.get("pub_key", "")
    try:
        enc_pwd = _aes_encrypt(password, pub_key) if pub_key else password
    except Exception as ex:
        log.warning(f"二次认证 AES 加密失败({ex})，明文提交")
        enc_pwd = password

    r = sess.post(
        f"{webvpn_auth}/reAuthCheck/reAuthSubmit.do",
        data={"service":    f"{cas_service}/callback?url",
              "reAuthType": "2",
              "password":   enc_pwd},
        headers={**UA_MOBILE,
                 "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json, text/javascript, */*; q=0.01",
                 "Referer": reauth_url},
        timeout=15,
    )
    text = r.text.strip()
    log.info(f"二次认证响应: {text[:200]}")
    try:
        d = json.loads(text)
        code   = str(d.get("code",   "")).lower()
        msg    = str(d.get("msg",    ""))
        status = str(d.get("status", "")).lower()
        if (
            d.get("resultCode") in ("0", 0)
            or d.get("code")    in ("0", 0)
            or d.get("success")
            or status == "success"
            or code   in ("reauth_success", "success")
            or "成功" in msg
        ):
            log.info("二次认证成功")
            return True
        redirect = d.get("url") or d.get("redirectUrl") or d.get("location")
        if redirect:
            sess.get(redirect, headers=UA_MOBILE, allow_redirects=True, timeout=10)
            log.info("二次认证成功（跟随跳转）")
            return True
        log.error(f"二次认证失败: {d}")
        return False
    except Exception:
        if any(k in text for k in ("success", "成功", "redirect")):
            return True
        log.error(f"二次认证响应无法解析: {text[:100]}")
        return False


def login_webvpn(sess: requests.Session, username: str, password: str,
                 webvpn_auth: str, webvpn_base: str, cas_service: str) -> bool:
    """
    WebVPN 账号密码登录流程（手机 UA），完全对齐 app.py do_login_webvpn：
      1. 预热 authserver 会话，依次请求 getLanguageTypes / tenant/info
      2. GET login 页面提取 execution 与 pwdEncryptSalt
      3. AES 加密密码，POST /authserver/login（带完整 Sec-Fetch-* 头）
      4. 若首次提交返回 200 则提取新 execution/salt 重试一次
      5. 跟随 301/302 跳转，若落地到 reAuthCheck 则完成二次认证
      6. 探测教务登录页可访问后才算成功
    """
    from urllib.parse import urljoin

    sess.headers.update(UA_MOBILE)
    base_auth = webvpn_auth.rsplit("/authserver", 1)[0]   # e.g. https://authserver-443.webvpn.neau.edu.cn
    login_url = f"{webvpn_auth}/login?service={cas_service}/callback?url"

    log.info("WebVPN 使用手机 UA 进行账号密码登录")

    # ── 1. 预热（获取 route / JSESSIONID）──
    try:
        ajax_h = {**UA_MOBILE, "X-Requested-With": "XMLHttpRequest",
                  "Referer": base_auth + "/"}
        sess.get(base_auth, headers=UA_MOBILE, allow_redirects=True, timeout=10)
        sess.post(f"{base_auth}/authserver/common/getLanguageTypes.htl",
                  data={}, headers=ajax_h, timeout=10)
        sess.get(f"{base_auth}/authserver/tenant/info", headers=ajax_h, timeout=10)
        sess.post(f"{base_auth}/authserver/common/getLanguageTypes.htl",
                  data={}, headers=ajax_h, timeout=10)
    except Exception as ex:
        log.warning(f"WebVPN 会话预热失败(可忽略): {ex}")

    # ── 2. 获取 execution / salt ──
    params    = _get_page_params(sess, login_url)
    execution = params.get("execution", "")
    salt      = params.get("pub_key", "")
    post_url  = params.get("final_url", login_url)

    if not execution:
        log.error("WebVPN 登录失败：未获取 execution")
        return False

    # ── 3. 构造并提交登录表单 ──
    def _submit(ex_value: str, salt_value: str):
        try:
            enc_pwd = _aes_encrypt(password, salt_value) if salt_value else password
        except Exception as ex:
            log.warning(f"WebVPN AES 加密失败({ex})，使用明文提交")
            enc_pwd = password

        data = {
            "username":  username,
            "password":  enc_pwd,
            "captcha":   "",
            "_eventId":  "submit",
            "lt":        "",
            "cllt":      "userNameLogin",
            "dllt":      "generalLogin",
            "execution": ex_value,
        }
        headers = {
            **UA_MOBILE,
            "Content-Type":            "application/x-www-form-urlencoded",
            "Referer":                 post_url,
            "Origin":                  base_auth,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":          "document",
            "Sec-Fetch-Mode":          "navigate",
            "Sec-Fetch-Site":          "same-origin",
            "Sec-Fetch-User":          "?1",
            "Cache-Control":           "max-age=0",
        }
        return sess.post(post_url, data=data, headers=headers,
                         allow_redirects=False, timeout=15)

    r = _submit(execution, salt)

    # ── 4. 首次 200 → 用新 execution 重试 ──
    if r.status_code == 200:
        params2    = _get_page_params(sess, post_url)
        execution2 = params2.get("execution", "")
        salt2      = params2.get("pub_key", "")
        if execution2 and execution2 != execution:
            log.info("WebVPN 首次提交返回 200，使用新 execution/salt 重试")
            r = _submit(execution2, salt2)

    if r.status_code not in (301, 302):
        tip = re.sub(r"\s+", " ", r.text)[:160]
        log.error(f"WebVPN 登录失败: HTTP {r.status_code} {tip}")
        return False

    # ── 5. 跟随跳转 ──
    landing_url = getattr(r, "url", "")
    location    = r.headers.get("Location", "")
    if location:
        next_url    = urljoin(post_url, location)
        r_next      = sess.get(next_url, headers=UA_MOBILE, allow_redirects=True, timeout=15)
        landing_url = r_next.url

    if "reAuthCheck" in landing_url or "reAuthLoginView" in landing_url:
        if not _do_webvpn_reauth(sess, landing_url, password, webvpn_auth, cas_service):
            return False

    # ── 6. 探测教务登录页 ──
    try:
        probe = sess.get(f"{webvpn_base}/login", headers=UA_MOBILE,
                         allow_redirects=True, timeout=10)
        if probe.status_code != 200:
            log.error(f"WebVPN 通道建立后访问教务登录页失败: HTTP {probe.status_code}")
            return False
    except Exception as ex:
        log.error(f"WebVPN 通道建立后访问教务登录页异常: {ex}")
        return False

    log.info("WebVPN 通道已建立，准备复用教务登录流程")
    return True


def do_login(config: dict) -> requests.Session | None:
    """
    根据配置执行登录，返回已认证的 Session，失败返回 None。
    对齐 app.py sniper_main 逻辑：
      - WebVPN 只做一次（失败直接返回）
      - 教务系统验证码登录最多重试 5 次
    """
    sess = requests.Session()
    username   = config["username"]
    password   = config["password"]
    use_webvpn = config.get("use_webvpn", False)
    base_url   = config["webvpn_base"] if use_webvpn else config["base_url"]

    if use_webvpn:
        log.info("通过 WebVPN 登录…")
        ok = login_webvpn(sess, username, password,
                          config["webvpn_auth"], config["webvpn_base"],
                          config["cas_service"])
        if not ok:
            log.error("WebVPN 认证失败，请检查账号密码或网络")
            return None
        log.info("WebVPN 已就绪，开始教务系统登录（仅重试教务部分）…")
    else:
        log.info("直连登录…")

    # 教务系统本身的验证码登录，最多 5 次
    for attempt in range(1, 6):
        try:
            if login_direct(sess, username, password, base_url):
                return sess
        except Exception as e:
            log.warning(f"教务登录异常({attempt}/5): {e}")
        if attempt < 5:
            time.sleep(2)

    log.error("教务系统登录连续失败，请检查账号密码后重试")
    return None


# ══════════════════════════ 数据抓取 ══════════════════════════
def fetch_schedule(sess: requests.Session, base_url: str) -> list | None:
    """
    抓取本学期课程表。
    接口: GET /student/courseSelect/thisSemesterCurriculum/ajaxStudentSchedule/callback
    """
    ref = f"{base_url}/student/courseSelect/thisSemesterCurriculum/index"
    try:
        # 先访问页面（服务器有时需要先初始化 session 状态）
        r_idx = sess.get(ref, headers=UA_PC, timeout=10)
        log.info(f"[课程表] index status={r_idx.status_code} final_url={r_idx.url}")
        r = sess.get(
            f"{base_url}/student/courseSelect/thisSemesterCurriculum/ajaxStudentSchedule/callback",
            headers={**UA_PC,
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": ref},
            timeout=15,
        )
        log.info(f"[课程表] callback status={r.status_code} url={r.url}")
        if "login" in r.url:
            log.warning("fetch_schedule: Session 已失效")
            return None
        data = r.json()
        log.info(f"[课程表] json结构={_json_shape(data)}")
        rows = _flatten_schedule_payload(data)
        if rows is None:
            log.warning("[课程表] 无法识别响应结构，本次记为抓取失败")
            return None
        log.info(f"[课程表] 解析条数={len(rows)}")
        return rows
    except Exception as e:
        log.error(f"抓取课程表失败: {e}")
        return None


def fetch_this_term_scores(sess: requests.Session, base_url: str) -> list | None:
    """
    抓取本学期成绩。
    接口: GET /student/integratedQuery/scoreQuery/{token}/thisTermScores/data
    """
    index_url = f"{base_url}/student/integratedQuery/scoreQuery/thisTermScores/index"
    try:
        r_idx = sess.get(index_url, headers=UA_PC, timeout=10)
        log.info(f"[本学期成绩] index status={r_idx.status_code} final_url={r_idx.url}")
        if "login" in r_idx.url:
            return None
        # 从页面或跳转 URL 中提取随机 token 段
        token_seg = _extract_score_token(r_idx.url, r_idx.text, "thisTermScores")
        log.info(f"[本学期成绩] token={'有' if token_seg else '无'} len={len(token_seg)}")
        data_url = (f"{base_url}/student/integratedQuery/scoreQuery"
                    f"/{token_seg}/thisTermScores/data" if token_seg
                    else f"{base_url}/student/integratedQuery/scoreQuery/thisTermScores/data")
        r = sess.get(data_url, headers={**UA_PC,
                                        "X-Requested-With": "XMLHttpRequest",
                                        "Referer": index_url}, timeout=15)
        log.info(f"[本学期成绩] data status={r.status_code} url={r.url}")
        if "login" in r.url:
            log.warning("[本学期成绩] data 请求被重定向到登录页")
            return None
        raw = r.json()
        log.info(f"[本学期成绩] json结构={_json_shape(raw)}")
        # 兼容旧格式: [{"state":"1","list":[...]}]
        if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "list" in raw[0]:
            rows = raw[0].get("list", [])
            rows = rows if isinstance(rows, list) else []
            log.info(f"[本学期成绩] 解析条数={len(rows)} (legacy list[0].list)")
            return rows

        rows = _extract_list_payload(raw)
        if rows is None:
            log.warning("[本学期成绩] 无法识别响应结构，本次记为抓取失败")
            return None
        log.info(f"[本学期成绩] 解析条数={len(rows)}")
        return rows
    except Exception as e:
        log.error(f"抓取本学期成绩失败: {e}")
        return None


def fetch_all_scores(sess: requests.Session, base_url: str) -> list | None:
    """
    抓取历史全部成绩（已通过课程）。
    接口: GET /student/integratedQuery/scoreQuery/{token}/allPassingScores/callback
    """
    index_url = f"{base_url}/student/integratedQuery/scoreQuery/allPassingScores/index"
    try:
        r_idx = sess.get(index_url, headers=UA_PC, timeout=10)
        log.info(f"[历史成绩] index status={r_idx.status_code} final_url={r_idx.url}")
        if "login" in r_idx.url:
            return None
        token_seg = _extract_score_token(r_idx.url, r_idx.text, "allPassingScores")
        log.info(f"[历史成绩] token={'有' if token_seg else '无'} len={len(token_seg)}")
        cb_url = (f"{base_url}/student/integratedQuery/scoreQuery"
                  f"/{token_seg}/allPassingScores/callback" if token_seg
                  else f"{base_url}/student/integratedQuery/scoreQuery/allPassingScores/callback")
        r = sess.get(cb_url, headers={**UA_PC,
                                      "X-Requested-With": "XMLHttpRequest",
                                      "Referer": index_url}, timeout=15)
        log.info(f"[历史成绩] callback status={r.status_code} url={r.url}")
        if "login" in r.url:
            log.warning("[历史成绩] callback 请求被重定向到登录页")
            return None
        raw = r.json()
        log.info(f"[历史成绩] json结构={_json_shape(raw)}")
        rows = _flatten_all_scores_payload(raw)
        if rows is None:
            log.warning("[历史成绩] 无法识别响应结构，本次记为抓取失败")
            return None
        log.info(f"[历史成绩] 解析条数={len(rows)}")
        return rows
    except Exception as e:
        log.error(f"抓取历史成绩失败: {e}")
        return None


def fetch_gpa_overview(sess: requests.Session, base_url: str) -> dict | None:
    """按 HAR 中真实接口抓取 GPA：POST /main/showMoreGPA。"""
    url = f"{base_url}/main/showMoreGPA"
    ref = f"{base_url}/"
    try:
        r = sess.post(url,
                      headers={**UA_PC,
                               "X-Requested-With": "XMLHttpRequest",
                               "Referer": ref},
                      timeout=12)
        if r.status_code != 200 or "login" in r.url.lower():
            log.warning(f"[GPA] showMoreGPA 请求异常 status={r.status_code} url={r.url}")
            return None
        raw = r.json()
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list) or not data:
            log.warning(f"[GPA] showMoreGPA 返回结构异常: {str(raw)[:200]}")
            return None

        first = data[0]
        # 典型格式: ["智育学分绩",3.0712,"14/33","2026-04-26 03:00:26","90/266"]
        if isinstance(first, list) and len(first) >= 5:
            return {
                "gpa_name": str(first[0] or "GPA"),
                "gpa": str(first[1] if first[1] is not None else "-"),
                "class_rank": str(first[2] or "-"),
                "generated_at": str(first[3] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "grade_rank": str(first[4] or "-"),
                "source_url": url,
            }

        # 兼容对象格式
        if isinstance(first, dict):
            return {
                "gpa_name": str(first.get("gpaName") or first.get("name") or "GPA"),
                "gpa": str(first.get("gpa") or first.get("value") or "-"),
                "class_rank": str(first.get("classRank") or first.get("class_rank") or "-"),
                "generated_at": str(first.get("time") or first.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "grade_rank": str(first.get("gradeRank") or first.get("grade_rank") or "-"),
                "source_url": url,
            }
    except Exception as e:
        log.warning(f"[GPA] 抓取失败 {url}: {e}")

    log.warning("[GPA] 未在 showMoreGPA 中提取到 GPA 信息")
    return None


def fetch_academic_info(sess: requests.Session, base_url: str) -> dict | None:
    """抓取首页学业信息（包含课程总数等统计）。"""
    url = f"{base_url}/main/academicInfo"
    try:
        r = sess.post(url,
                      data={"flag": ""},
                      headers={**UA_PC,
                               "X-Requested-With": "XMLHttpRequest",
                               "Referer": f"{base_url}/"},
                      timeout=12)
        if r.status_code != 200 or "login" in r.url.lower():
            return None
        raw = r.json()
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return raw[0]
        if isinstance(raw, dict):
            return raw
        return None
    except Exception as e:
        log.warning(f"[学业信息] 抓取失败: {e}")
        return None


# ══════════════════════════ 本地存储 ══════════════════════════
def _data_path(data_dir: str, name: str) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{name}.json"


def load_local(data_dir: str, name: str) -> list | None:
    path = _data_path(data_dir, name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_local(data_dir: str, name: str, data: Any):
    path = _data_path(data_dir, name)
    old_data = None
    if path.exists():
        try:
            old_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old_data = None

    # 覆盖写入前把旧版本归档，便于后续核验历史数据。
    if path.exists() and old_data != data:
        archive_dir = Path(data_dir) / "archive" / name
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        archive_path = archive_dir / f"{ts}.json"
        archive_path.write_text(
            json.dumps(old_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_change(data_dir: str, name: str, diff: dict):
    """记录变更历史到单独文件。"""
    p = Path(data_dir) / "changes.jsonl"
    entry = {"time": datetime.now().isoformat(), "type": name, **diff}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ══════════════════════════ 数据对比 ══════════════════════════
def _norm_course(c: dict) -> dict:
    """标准化课程表条目，只保留关键字段用于比较。"""
    return {
        "kch": str(c.get("kch", c.get("courseNumber", ""))),
        "kcm": str(c.get("kcm", c.get("courseName", ""))),
        "skjs": str(c.get("skjs", c.get("teacherName", ""))).strip(),
        "skxq": str(c.get("skxq", c.get("weekDay", ""))),
        "skjc": str(c.get("skjc", c.get("section", ""))),
        "skzc": str(c.get("skzc", c.get("weekRange", ""))),
        "jxdd": str(c.get("jxdd", c.get("classroom", ""))),
    }


def _course_key(c: dict) -> str:
    n = _norm_course(c)
    return f"{n['kch']}_{n['skxq']}_{n['skjc']}_{n['skzc']}"


def diff_schedule(old: list, new: list) -> dict:
    """返回课程表变动：新增、删除、修改的课程。"""
    old_map = {_course_key(c): _norm_course(c) for c in old}
    new_map = {_course_key(c): _norm_course(c) for c in new}
    added = [new_map[k] for k in new_map if k not in old_map]
    removed = [old_map[k] for k in old_map if k not in new_map]
    modified = []
    for k in old_map:
        if k in new_map and old_map[k] != new_map[k]:
            modified.append({"before": old_map[k], "after": new_map[k]})
    return {"added": added, "removed": removed, "modified": modified}


def _norm_score(s: dict) -> dict:
    sid_obj = s.get("id")
    sid: dict[str, Any] = sid_obj if isinstance(sid_obj, dict) else {}
    return {
        "kch": str(
            s.get("kch")
            or s.get("courseNumber")
            or sid.get("courseNumber")
            or sid.get("kch_zj")
            or ""
        ),
        "kcm": str(s.get("kcm") or s.get("courseName") or ""),
        "cj": str(
            s.get("cj")
            or s.get("score")
            or s.get("grade")
            or s.get("courseScore")
            or s.get("gradeScore")
            or ""
        ).strip(),
        "xf": str(s.get("xf") or s.get("credit") or ""),
        "jd": str(s.get("jd") or s.get("gradePoint") or s.get("gradePointScore") or ""),
    }


def _score_key(s: dict) -> str:
    n = _norm_score(s)
    return f"{n['kch']}_{n['kcm']}"


def diff_scores(old: list, new: list) -> dict:
    """返回成绩变动：新增、分数变化的条目。"""
    old_map = {_score_key(s): _norm_score(s) for s in old}
    new_map = {_score_key(s): _norm_score(s) for s in new}
    added = [new_map[k] for k in new_map if k not in old_map]
    changed = []
    for k in old_map:
        if k in new_map and old_map[k]["cj"] != new_map[k]["cj"]:
            changed.append({"before": old_map[k], "after": new_map[k]})
    return {"added": added, "changed": changed}


def has_changes(diff: dict) -> bool:
    return any(bool(v) for v in diff.values())


# ══════════════════════════ 通知推送 ══════════════════════════
def _fmt_schedule_diff(diff: dict) -> str:
    lines = []
    for c in diff.get("added", []):
        lines.append(f"📗 新增课程: {c['kcm']} | 教师: {c['skjs']} | "
                     f"周{c['skxq']} 第{c['skjc']}节 | 地点: {c['jxdd']}")
    for c in diff.get("removed", []):
        lines.append(f"📕 删除课程: {c['kcm']} | 教师: {c['skjs']} | "
                     f"周{c['skxq']} 第{c['skjc']}节")
    for m in diff.get("modified", []):
        lines.append(f"📙 课程变更: {m['before']['kcm']}\n"
                     f"   变更前: 周{m['before']['skxq']} 第{m['before']['skjc']}节 {m['before']['jxdd']}\n"
                     f"   变更后: 周{m['after']['skxq']} 第{m['after']['skjc']}节 {m['after']['jxdd']}")
    return "\n".join(lines)


def _fmt_score_diff(diff: dict, label: str) -> str:
    lines = []
    for s in diff.get("added", []):
        lines.append(f"🆕 [{label}] 新成绩: {s['kcm']} | 成绩: {s['cj']} | 学分: {s['xf']} | 绩点: {s['jd']}")
    for ch in diff.get("changed", []):
        b, a = ch["before"], ch["after"]
        lines.append(f"✏️  [{label}] 成绩变动: {b['kcm']} | {b['cj']} → {a['cj']}")
    return "\n".join(lines)


def send_notify(config: dict, title: str, content: str):
    """向所有已配置的渠道推送通知。"""
    nc = config.get("notify", {})
    log.info(f"[通知] {title}\n{content}")

    # 企业微信
    if nc.get("wecom_webhook"):
        try:
            requests.post(nc["wecom_webhook"],
                          json={"msgtype": "text",
                                "text": {"content": f"【{title}】\n{content}"}},
                          timeout=10)
        except Exception as e:
            log.warning(f"企业微信推送失败: {e}")

    # Server 酱
    if nc.get("serverchan_key"):
        try:
            requests.post(f"https://sctapi.ftqq.com/{nc['serverchan_key']}.send",
                          data={"title": title, "desp": content.replace("\n", "\n\n")},
                          timeout=10)
        except Exception as e:
            log.warning(f"Server 酱推送失败: {e}")

    # 钉钉
    if nc.get("dingtalk_webhook"):
        try:
            requests.post(nc["dingtalk_webhook"],
                          json={"msgtype": "text",
                                "text": {"content": f"【{title}】\n{content}"}},
                          timeout=10)
        except Exception as e:
            log.warning(f"钉钉推送失败: {e}")

    # Bark（iOS）
    if nc.get("bark_key"):
        try:
            requests.get(f"https://api.day.app/{nc['bark_key']}/{title}/{content}",
                         timeout=10)
        except Exception as e:
            log.warning(f"Bark 推送失败: {e}")

    # 飞书
    if nc.get("feishu_webhook"):
        try:
            requests.post(nc["feishu_webhook"],
                          json={"msg_type": "text",
                                "content": {"text": f"【{title}】\n{content}"}},
                          timeout=10)
        except Exception as e:
            log.warning(f"飞书推送失败: {e}")

    # PushDeer
    if nc.get("pushdeer_key"):
        try:
            requests.get(
                "https://api2.pushdeer.com/message/push",
                params={
                    "pushkey": nc["pushdeer_key"],
                    "text": title,
                    "desp": content,
                    "type": "markdown",
                },
                timeout=10,
            )
        except Exception as e:
            log.warning(f"PushDeer 推送失败: {e}")

    # Telegram
    if nc.get("telegram_token") and nc.get("telegram_chat_id"):
        try:
            requests.post(
                f"https://api.telegram.org/bot{nc['telegram_token']}/sendMessage",
                json={"chat_id": nc["telegram_chat_id"],
                      "text": f"<b>{title}</b>\n{content}",
                      "parse_mode": "HTML"},
                timeout=10)
        except Exception as e:
            log.warning(f"Telegram 推送失败: {e}")


# ══════════════════════════ 主监控循环 ══════════════════════════
def run_once(config: dict):
    """执行一次完整的抓取 + 对比 + 通知流程。"""
    log.info("═" * 50)
    log.info("开始抓取…")
    data_dir = config["data_dir"]
    use_webvpn = config.get("use_webvpn", False)
    base_url = config["webvpn_base"] if use_webvpn else config["base_url"]

    sess = do_login(config)
    if sess is None:
        log.error("登录失败，本次跳过")
        send_notify(config, "⚠️ 教务监控登录失败", "请检查账号密码或网络，已跳过本次抓取。")
        return

    results = {}
    bootstrap_msgs: list[str] = []

    # 1. 课程表
    schedule = fetch_schedule(sess, base_url)
    if schedule is not None:
        log.info(f"[课程表] 本次抓取结果: {len(schedule)} 条")
        old_sched = load_local(data_dir, "schedule")
        if old_sched is None:
            log.info(f"首次抓取课程表，共 {len(schedule)} 条，已保存。")
            save_local(data_dir, "schedule", schedule)
            init_diff = {
                "added": [_norm_course(x) for x in schedule],
                "removed": [],
                "modified": [],
                "initial": True,
            }
            archive_change(data_dir, "schedule", init_diff)
            bootstrap_msgs.append(f"课程表 {len(schedule)} 条")
        else:
            diff = diff_schedule(old_sched, schedule)
            if has_changes(diff):
                log.info(f"课程表有变动: {diff}")
                archive_change(data_dir, "schedule", diff)
                save_local(data_dir, "schedule", schedule)
                results["schedule"] = diff
            else:
                log.info("课程表无变动。")

    # 2. 本学期成绩
    this_scores = fetch_this_term_scores(sess, base_url)
    if this_scores is not None:
        log.info(f"[本学期成绩] 本次抓取结果: {len(this_scores)} 条")
        old_ts = load_local(data_dir, "this_term_scores")
        if old_ts is None:
            log.info(f"首次抓取本学期成绩，共 {len(this_scores)} 条，已保存。")
            save_local(data_dir, "this_term_scores", this_scores)
            init_diff = {
                "added": [_norm_score(x) for x in this_scores],
                "changed": [],
                "initial": True,
            }
            archive_change(data_dir, "this_term_scores", init_diff)
            bootstrap_msgs.append(f"本学期成绩 {len(this_scores)} 条")
        else:
            diff = diff_scores(old_ts, this_scores)
            if has_changes(diff):
                log.info(f"本学期成绩有变动: {diff}")
                archive_change(data_dir, "this_term_scores", diff)
                save_local(data_dir, "this_term_scores", this_scores)
                results["this_term_scores"] = diff
            else:
                log.info("本学期成绩无变动。")

    # 3. 历史全部成绩
    all_scores = fetch_all_scores(sess, base_url)
    if all_scores is not None:
        log.info(f"[历史成绩] 本次抓取结果: {len(all_scores)} 条")
        old_as = load_local(data_dir, "all_scores")
        if old_as is None:
            log.info(f"首次抓取历史成绩，共 {len(all_scores)} 条，已保存。")
            save_local(data_dir, "all_scores", all_scores)
            init_diff = {
                "added": [_norm_score(x) for x in all_scores],
                "changed": [],
                "initial": True,
            }
            archive_change(data_dir, "all_scores", init_diff)
            bootstrap_msgs.append(f"历史成绩 {len(all_scores)} 条")
        else:
            diff = diff_scores(old_as, all_scores)
            if has_changes(diff):
                log.info(f"历史成绩有变动: {diff}")
                archive_change(data_dir, "all_scores", diff)
                save_local(data_dir, "all_scores", all_scores)
                results["all_scores"] = diff
            else:
                log.info("历史成绩无变动。")

    # 4. GPA 概览（每次都刷新）
    gpa_info = fetch_gpa_overview(sess, base_url)
    if gpa_info is not None:
        save_local(data_dir, "gpa", gpa_info)
        log.info(f"[GPA] 已更新: GPA={gpa_info.get('gpa','-')} 班级={gpa_info.get('class_rank','-')} 年级={gpa_info.get('grade_rank','-')}")

    academic_info = fetch_academic_info(sess, base_url)
    if academic_info is not None:
        save_local(data_dir, "academic_info", academic_info)

    sess.close()

    # 汇总推送
    if results:
        parts = []
        if "schedule" in results:
            parts.append(_fmt_schedule_diff(results["schedule"]))
        if "this_term_scores" in results:
            parts.append(_fmt_score_diff(results["this_term_scores"], "本学期"))
        if "all_scores" in results:
            parts.append(_fmt_score_diff(results["all_scores"], "历史"))
        msg = "\n\n".join(p for p in parts if p)
        send_notify(config, "📚 教务系统有新变动", msg)
    elif bootstrap_msgs:
        send_notify(config,
                    "✅ 教务监控首次抓取成功",
                    "已完成首次数据抓取（用于推送通道测试）\n\n" + "\n".join(f"- {x}" for x in bootstrap_msgs))
    else:
        log.info("本次无变动，不推送通知。")

    log.info("本次抓取完成。")


def main():
    log.info("东北农业大学教务监控启动")
    log.info(f"抓取间隔: {CONFIG['interval']} 秒")
    log.info(f"数据目录: {CONFIG['data_dir']}")
    log.info(f"WebVPN 模式: {'开启' if CONFIG['use_webvpn'] else '关闭'}")

    while True:
        try:
            run_once(CONFIG)
        except KeyboardInterrupt:
            log.info("用户中断，退出。")
            break
        except Exception as e:
            log.error(f"未捕获异常: {e}", exc_info=True)
        log.info(f"等待 {CONFIG['interval']} 秒后进行下一次抓取…")
        time.sleep(CONFIG["interval"])


if __name__ == "__main__":
    main()
