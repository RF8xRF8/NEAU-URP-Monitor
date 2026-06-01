"""
东北农业大学教务系统 - 课程表 & 成绩定时监控
功能：
    1. 复用 app.py 的登录流程（直连 CAS / WebVPN CAS），无重复代码
    2. 抓取到的所有数据按原样保存至 ./data/，同时记录抓取时间
    3. 课程表按「课程号+课序号」去重，记录总课程数
    4. 任何字段变动（GPA 生成时间除外）触发归档 + 记录变动详情
    5. 通知渠道：仅 PushDeer
"""

import json
import logging
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import requests

# ══════════════════════════════════════════════════════════════════
# 配置加载（复用与 app.py 相同的 config.json）
# ══════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    config_file = Path(__file__).parent / "config.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise RuntimeError(f"读取 config.json 失败: {e}")
    raise RuntimeError(
        "未找到 config.json。\n"
        "请参照 config.example.json 创建配置文件并填写学号、密码等信息。"
    )

CONFIG = _load_config()
DIRECT_CAS_AUTH = CONFIG.get("direct_cas_auth", "https://authserver.neau.edu.cn/authserver")
DIRECT_CAS_SERVICE = CONFIG.get("direct_cas_service", "https://zhjwxs.neau.edu.cn/login")

# ══════════════════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("neau_monitor")


# ══════════════════════════════════════════════════════════════════
# 字段标签加载（用于格式化输出）
# ══════════════════════════════════════════════════════════════════
def _load_field_labels() -> dict:
    """加载 field_labels.json，如果不存在返回空字典"""
    labels_file = Path(__file__).parent / "field_labels.json"
    if labels_file.exists():
        try:
            with open(labels_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 排除特殊的 "说明" 键
                return {k: v for k, v in data.items() if k != "说明"}
        except Exception as e:
            log.warning(f"加载 field_labels.json 失败: {e}，将使用原始字段名")
            return {}
    return {}

_FIELD_LABELS = _load_field_labels()

def label(key: str) -> str:
    """获取字段的中文标签，如果不存在返回原始键名"""
    return _FIELD_LABELS.get(key, key)


# ══════════════════════════════════════════════════════════════════
# 登录 —— 完全复用 app.py 中的函数
# ══════════════════════════════════════════════════════════════════
# app.py 中核心登录逻辑的精简复刻（保持与 app.py 完全一致的行为）
# 包含：WebVPN 登录、AES 加密、二次认证、CAS 直连登录

import base64
import re
import random
import pickle
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

UA_PC = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
UA_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.5 Mobile/15E148 Safari/604.1 Edg/146.0.0.0"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def _random_str(n: int) -> str:
    return "".join(random.choice(_AES_CHARS) for _ in range(n))


def _aes_encrypt(password: str, salt: str) -> str:
    key = salt.encode().ljust(16, b"\x00")[:16]
    iv = b"\x00" * 16
    plain = (_random_str(64) + password).encode()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(plain, 16))).decode()


def _first_group_match(text: str, patterns: list) -> str:
    for p in patterns:
        m = re.search(p, text)
        if m and m.group(1):
            return m.group(1)
    return ""


def _get_page_params(sess: requests.Session, url: str, ua: dict | None = None) -> dict:
    """访问 CAS 登录页，提取 execution / pwdEncryptSalt，对齐 app.py 逻辑。"""
    ua = ua or UA_MOBILE
    r = sess.get(url, headers=ua, allow_redirects=True, timeout=10)
    html = r.text
    result: dict = {}

    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("input", {"name": "execution"})
    if tag and tag.get("value"):
        result["execution"] = tag["value"]
    for name in ("pwdEncryptSalt", "rsaPublicKey"):
        tag2 = soup.find("input", {"id": name}) or soup.find("input", {"name": name})
        if tag2 and tag2.get("value"):
            result["pub_key"] = tag2["value"]
            break

    if not result.get("execution"):
        result["execution"] = _first_group_match(html, [
            r'(?:name|id)=["\']execution["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']execution["\']',
            r'"execution"\s*[:=]\s*"([^"]{8,})"',
            r"'execution'\s*[:=]\s*'([^']{8,})'",
        ])
    if not result.get("pub_key"):
        result["pub_key"] = _first_group_match(html, [
            r'(?:name|id)=["\']pwdEncryptSalt["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']pwdEncryptSalt["\']',
            r'(?:name|id)=["\']rsaPublicKey["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*(?:name|id)=["\']rsaPublicKey["\']',
            r'"pwdEncryptSalt"\s*[:=]\s*"([^"]{8,64})"',
        ])

    result["final_url"] = r.url
    log.info(f"登录页参数: execution={'有' if result.get('execution') else '无'} "
             f"salt={'有' if result.get('pub_key') else '无'} url={r.url}")
    return result


def _do_webvpn_reauth(sess: requests.Session, reauth_url: str,
                      password: str, webvpn_auth: str, cas_service: str) -> bool:
    if not reauth_url.startswith("http"):
        reauth_url = f"{webvpn_auth.rsplit('/authserver', 1)[0]}{reauth_url}"
    log.info(f"二次认证页: {reauth_url}")
    params = _get_page_params(sess, reauth_url)
    try:
        enc_pwd = _aes_encrypt(password, params.get("pub_key", "")) if params.get("pub_key") else password
    except Exception as ex:
        log.warning(f"二次认证 AES 加密失败({ex})，明文提交")
        enc_pwd = password

    r = sess.post(
        f"{webvpn_auth}/reAuthCheck/reAuthSubmit.do",
        data={"service": f"{cas_service}/callback?url", "reAuthType": "2", "password": enc_pwd},
        headers={**UA_MOBILE, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Referer": reauth_url},
        timeout=15,
    )
    text = r.text.strip()
    try:
        d = json.loads(text)
        if (d.get("resultCode") in ("0", 0) or d.get("code") in ("0", 0)
                or d.get("success") or str(d.get("status", "")).lower() == "success"
                or "成功" in str(d.get("msg", ""))):
            log.info("二次认证成功")
            return True
        redirect = d.get("url") or d.get("redirectUrl") or d.get("location")
        if redirect:
            sess.get(redirect, headers=UA_MOBILE, allow_redirects=True, timeout=10)
            return True
        log.error(f"二次认证失败: {d}")
        return False
    except Exception:
        if any(k in text for k in ("success", "成功", "redirect")):
            return True
        log.error(f"二次认证响应无法解析: {text[:100]}")
        return False


def _analyze_login_failure(resp_text: str, resp_url: str) -> tuple[bool, str]:
    text = re.sub(r"\s+", " ", (resp_text or ""))
    password_pats = (
        "用户名或密码错误", "账号或密码错误", "学号或密码错误", "密码错误", "用户名错误"
    )
    captcha_pats = (
        "验证码错误", "验证码不正确", "验证码有误", "安全验证", "滑块", "拼图", "captcha", "验证码"
    )

    if any(p in text for p in password_pats):
        return False, "教务登录失败：账号或密码错误"
    if any(p in text.lower() for p in captcha_pats) or "captcha" in resp_url.lower():
        return True, "教务登录失败：验证码识别失败，准备重试"
    return True, "教务登录失败：未知原因，准备重试"


def _response_needs_slider_captcha(resp_text: str, resp_url: str) -> bool:
    """判断登录响应是否真的弹出了滑块验证码。"""
    retryable, reason = _analyze_login_failure(resp_text, resp_url)
    return retryable and "验证码识别失败" in reason


def _slider_move_length(match_result: dict, tag_width: float | int | None,
                        source_width: float | int | None, canvas_length: float | int = 280) -> float:
    """把 ddddocr 的匹配结果转成验证接口需要的拖动距离。"""
    target_x = match_result.get("target_x")
    if target_x is None:
        target = match_result.get("target")
        if isinstance(target, (list, tuple)) and target:
            target_x = target[0]
    move_length = float(target_x or 0.0)
    if source_width:
        scale = float(canvas_length) / float(source_width)
        move_length *= scale
        if tag_width:
            move_length -= float(tag_width) * scale / 2.0
    elif tag_width:
        move_length -= float(tag_width) / 2.0
    return max(0.0, move_length)


def _solve_slider_captcha(sess: requests.Session, authserver_base: str, login_url: str,
                          ua: dict) -> bool:
    """识别并校验 authserver 的拼图滑块验证码。"""
    import os

    os.environ.setdefault("ORT_DISABLE_ALL_OPTIMIZATIONS", "1")
    os.environ.setdefault("ORT_DISABLE_GRAPH_OPTIMIZATION", "1")

    try:
        import ddddocr
    except ImportError:
        log.error("滑块验证码需要 ddddocr，请先安装依赖")
        return False
    except Exception as exc:
        log.error(f"初始化 ddddocr 失败: {exc}")
        return False

    origin_base = authserver_base.rsplit("/authserver", 1)[0]
    open_url = f"{authserver_base}/common/openSliderCaptcha.htl"
    verify_url = f"{authserver_base}/common/verifySliderCaptcha.htl"
    ajax_headers = {
        **ua,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    verify_headers = {
        **ajax_headers,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": origin_base,
        "Referer": login_url,
    }

    try:
        ocr = ddddocr.DdddOcr(show_ad=False, ocr=False)
    except Exception as exc:
        log.error(f"ddddocr 初始化失败: {exc}")
        return False

    for round_idx in range(2):
        try:
            response = sess.get(open_url, headers={**ajax_headers, "Referer": login_url}, timeout=10)
            payload = response.json()
        except Exception as exc:
            log.warning(f"滑块验证码获取失败({round_idx + 1}/2): {exc}")
            continue

        big_image_b64 = payload.get("bigImage") or ""
        small_image_b64 = payload.get("smallImage") or ""
        if not big_image_b64 or not small_image_b64:
            return True

        try:
            big_image = base64.b64decode(big_image_b64)
            small_image = base64.b64decode(small_image_b64)
            try:
                from io import BytesIO
                from PIL import Image

                source_width = Image.open(BytesIO(big_image)).size[0]
            except Exception:
                source_width = None
            match_result = ocr.slide_match(small_image, big_image, simple_target=False)
        except Exception as exc:
            log.warning(f"滑块验证码识别失败({round_idx + 1}/2): {exc}")
            continue

        tag_width = payload.get("tagWidth")
        base_move = _slider_move_length(match_result, tag_width, source_width)
        move_candidates = []
        for delta in (0.0, -1.0, 1.0, -2.0, 2.0):
            candidate = max(0.0, base_move + delta)
            if candidate not in move_candidates:
                move_candidates.append(candidate)

        for move_length in move_candidates:
            try:
                verify_response = sess.post(
                    verify_url,
                    data={"canvasLength": 280, "moveLength": move_length},
                    headers=verify_headers,
                    timeout=10,
                )
                verify_payload = verify_response.json()
            except Exception as exc:
                log.warning(f"滑块验证码校验请求失败: {exc}")
                continue

            if str(verify_payload.get("errorCode")) == "1":
                log.info(f"滑块验证码识别成功，拖动距离约为 {move_length:.2f}")
                return True

        log.warning("滑块验证码校验失败，准备重新获取")

    return False


def _login_webvpn(sess: requests.Session, username: str, password: str,
                  webvpn_auth: str, webvpn_base: str, cas_service: str) -> bool:
    from urllib.parse import urljoin
    base_auth = webvpn_auth.rsplit("/authserver", 1)[0]
    login_url = f"{webvpn_auth}/login?service={cas_service}/callback?url"
    sess.headers.update(UA_MOBILE)
    log.info("WebVPN 使用手机 UA 登录")

    try:
        ajax_h = {**UA_MOBILE, "X-Requested-With": "XMLHttpRequest", "Referer": base_auth + "/"}
        sess.get(base_auth, headers=UA_MOBILE, allow_redirects=True, timeout=10)
        sess.post(f"{base_auth}/authserver/common/getLanguageTypes.htl", data={}, headers=ajax_h, timeout=10)
        sess.get(f"{base_auth}/authserver/tenant/info", headers=ajax_h, timeout=10)
        sess.post(f"{base_auth}/authserver/common/getLanguageTypes.htl", data={}, headers=ajax_h, timeout=10)
    except Exception as ex:
        log.warning(f"WebVPN 预热失败(可忽略): {ex}")

    params = _get_page_params(sess, login_url)
    execution = params.get("execution", "")
    salt = params.get("pub_key", "")
    post_url = params.get("final_url", login_url)

    if not execution:
        log.error("WebVPN 登录失败：未获取 execution")
        return False

    def _submit(ex: str, sl: str):
        try:
            enc_pwd = _aes_encrypt(password, sl) if sl else password
        except Exception as e:
            log.warning(f"AES 加密失败({e})，明文提交")
            enc_pwd = password
        return sess.post(post_url, data={
            "username": username, "password": enc_pwd, "captcha": "",
            "_eventId": "submit", "lt": "", "cllt": "userNameLogin",
            "dllt": "generalLogin", "execution": ex,
        }, headers={**UA_MOBILE, "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": post_url, "Origin": base_auth,
                    "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1", "Cache-Control": "max-age=0"},
            allow_redirects=False, timeout=15)

    r = _submit(execution, salt)

    if r.status_code in (200, 401) and _response_needs_slider_captcha(r.text, r.url):
        log.info("WebVPN 登录响应包含滑块验证码，开始识别")
        if not _solve_slider_captcha(sess, webvpn_auth, login_url, UA_MOBILE):
            log.error("WebVPN 登录失败：滑块验证码校验未通过")
            return False
        params2 = _get_page_params(sess, post_url)
        execution2 = params2.get("execution", "")
        salt2 = params2.get("pub_key", "")
        if execution2 or salt2:
            r = _submit(execution2 or execution, salt2 or salt)

    if r.status_code == 200:
        params2 = _get_page_params(sess, post_url)
        ex2, sl2 = params2.get("execution", ""), params2.get("pub_key", "")
        if ex2 and ex2 != execution:
            log.info("首次提交返回 200，用新 execution 重试")
            r = _submit(ex2, sl2)

    if r.status_code not in (301, 302):
        log.error(f"WebVPN 登录失败: HTTP {r.status_code}")
        return False

    location = r.headers.get("Location", "")
    landing_url = r.url
    if location:
        next_url = urljoin(post_url, location)
        r_next = sess.get(next_url, headers=UA_MOBILE, allow_redirects=True, timeout=15)
        landing_url = r_next.url

    if "reAuthCheck" in landing_url or "reAuthLoginView" in landing_url:
        if not _do_webvpn_reauth(sess, landing_url, password, webvpn_auth, cas_service):
            return False

    try:
        probe = sess.get(f"{webvpn_base}/login", headers=UA_MOBILE, allow_redirects=True, timeout=10)
        if probe.status_code != 200:
            log.error(f"WebVPN 建立后探测教务页失败: HTTP {probe.status_code}")
            return False
    except Exception as ex:
        log.error(f"WebVPN 探测教务页异常: {ex}")
        return False

    log.info("WebVPN 通道已建立")
    return True


def _login_direct_cas(sess: requests.Session, username: str, password: str,
                      base_url: str) -> bool:
    """直连教务系统的 CAS 登录。"""
    from urllib.parse import urljoin

    login_url = f"{DIRECT_CAS_AUTH}/login?service={DIRECT_CAS_SERVICE}"
    log.info("通过 CAS 统一认证直连登录…")

    try:
        base_auth = "https://authserver.neau.edu.cn"
        ajax_h = {**UA_PC, "X-Requested-With": "XMLHttpRequest", "Referer": base_auth + "/"}
        sess.get(base_auth, headers=UA_PC, allow_redirects=True, timeout=10)
        sess.post(f"{base_auth}/authserver/common/getLanguageTypes.htl", data={}, headers=ajax_h, timeout=10)
        sess.get(f"{base_auth}/authserver/tenant/info", headers=ajax_h, timeout=10)
    except Exception as ex:
        log.warning(f"CAS 会话预热失败(可忽略): {ex}")

    params = _get_page_params(sess, login_url, ua=UA_PC)
    execution = params.get("execution", "")
    salt = params.get("pub_key", "")
    post_url = params.get("final_url", login_url)

    if not execution:
        log.error("CAS 登录失败：未获取 execution")
        return False

    def _submit(ex_value: str, salt_value: str):
        try:
            enc_pwd = _aes_encrypt(password, salt_value) if salt_value else password
        except Exception as ex:
            log.warning(f"CAS AES 加密失败({ex})，使用明文提交")
            enc_pwd = password

        data = {
            "username": username,
            "password": enc_pwd,
            "captcha": "",
            "_eventId": "submit",
            "lt": "",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "execution": ex_value,
        }
        headers = {
            **UA_PC,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": post_url,
            "Origin": "https://authserver.neau.edu.cn",
            "Upgrade-Insecure-Requests": "1",
        }
        return sess.post(post_url, data=data, headers=headers, allow_redirects=False, timeout=15)

    r = _submit(execution, salt)

    if r.status_code in (200, 401) and _response_needs_slider_captcha(r.text, r.url):
        log.info("CAS 登录响应包含滑块验证码，开始识别")
        if not _solve_slider_captcha(sess, DIRECT_CAS_AUTH, login_url, UA_PC):
            log.error("CAS 登录失败：滑块验证码校验未通过")
            return False
        params2 = _get_page_params(sess, post_url, ua=UA_PC)
        execution2 = params2.get("execution", "")
        salt2 = params2.get("pub_key", "")
        if execution2 or salt2:
            r = _submit(execution2 or execution, salt2 or salt)

    if r.status_code == 200:
        params2 = _get_page_params(sess, post_url, ua=UA_PC)
        execution2 = params2.get("execution", "")
        salt2 = params2.get("pub_key", "")
        if execution2 and execution2 != execution:
            log.info("CAS 首次提交返回 200，使用新 execution/salt 重试")
            r = _submit(execution2, salt2)

    if r.status_code not in (301, 302):
        tip = re.sub(r"\s+", " ", r.text)[:180]
        log.error(f"CAS 登录失败: HTTP {r.status_code} {tip}")
        return False

    location = r.headers.get("Location", "")
    if location:
        next_url = urljoin(post_url, location)
        sess.get(next_url, headers=UA_PC, allow_redirects=True, timeout=15)

    try:
        probe = sess.get(f"{base_url}/login", headers=UA_PC, allow_redirects=True, timeout=10)
    except Exception as ex:
        log.error(f"CAS 登录后探测教务系统异常: {ex}")
        return False

    final_url = probe.url.lower()
    if "authserver.neau.edu.cn/authserver/login" in final_url:
        retryable, reason = _analyze_login_failure(probe.text, probe.url)
        log.error(reason or "CAS 登录失败：仍停留在统一认证页")
        return False

    log.info("CAS 登录成功")
    return True


def _login_direct_legacy(sess: requests.Session, username: str, password: str,
                         base_url: str) -> bool:
    """5 月 6 日前的旧版教务登录：登录页 + 验证码 OCR。"""
    try:
        import os
        # 禁止 ddddocr 使用 SIMD 优化，避免在某些 Docker 环境里 Illegal instruction 崩溃
        os.environ['ORT_DISABLE_ALL_OPTIMIZATIONS'] = '1'
        os.environ['ORT_DISABLE_GRAPH_OPTIMIZATION'] = '1'
        import ddddocr
    except ImportError:
        log.error("旧版教务登录需要 ddddocr，请先安装依赖后再设置 use_cas=false")
        return False
    except Exception as e:
        log.error(f"初始化 ddddocr 失败: {e}")
        return False

    log.info("使用旧版教务登录流程…")
    try:
        page = sess.get(f"{base_url}/login", headers=UA_PC, timeout=10)
    except Exception as e:
        log.error(f"无法访问登录页: {e}")
        return False

    m = re.search(r'(?:name|id)=["\']tokenValue["\'][^>]*value=["\']([^"\']{10,})["\']', page.text)
    tok = m.group(1) if m else ""

    try:
        ocr = ddddocr.DdddOcr(show_ad=False)
    except Exception as e:
        log.error(f"ddddocr 初始化失败: {e}")
        return False

    for attempt in range(5):
        try:
            r_cap = sess.get(f"{base_url}/img/captcha.jpg", headers=UA_PC, timeout=10)
            r_cap.raise_for_status()
            cap_raw = ocr.classification(r_cap.content)
            cap_text = cap_raw.strip() if isinstance(cap_raw, str) else str(cap_raw).strip()
            log.info(f"验证码识别: {cap_text}")
        except Exception as e:
            log.warning(f"验证码获取失败({attempt + 1}/5): {e}")
            time.sleep(1)
            continue

        try:
            r = sess.post(
                f"{base_url}/j_spring_security_check",
                data={"lang": "zh", "tokenValue": tok, "j_username": username,
                      "j_password": password, "j_captcha": cap_text},
                headers={**UA_PC, "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": f"{base_url}/login"},
                allow_redirects=True, timeout=15,
            )
        except Exception as e:
            log.warning(f"教务登录请求异常({attempt + 1}/5): {e}")
            time.sleep(1)
            continue

        if "login" not in r.url and "j_spring_security_check" not in r.url:
            log.info("旧版教务登录成功")
            return True
        if "密码" in r.text or "password" in r.text.lower():
            log.error("账号或密码错误")
            return False
        log.warning(f"验证码识别错误，重试 {attempt + 1}/5…")
        time.sleep(1)

    log.error("验证码识别连续失败")
    return False

# ══════════════════════════════════════════════════════════════════
# Cookie 持久化
# ══════════════════════════════════════════════════════════════════

def _cookie_path(data_dir: str) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "cookies.pkl"

def save_cookies(sess: requests.Session, data_dir: str):
    """将当前 Session 的 Cookie 保存到本地"""
    try:
        with open(_cookie_path(data_dir), "wb") as f:
            pickle.dump(sess.cookies, f)
        log.info("已保存当前会话 Cookie。")
    except Exception as e:
        log.warning(f"保存 Cookie 失败: {e}")

def load_cookies(sess: requests.Session, data_dir: str) -> bool:
    """尝试从本地加载 Cookie"""
    path = _cookie_path(data_dir)
    if path.exists():
        try:
            with open(path, "rb") as f:
                sess.cookies.update(pickle.load(f))
            return True
        except Exception as e:
            log.warning(f"读取 Cookie 失败: {e}")
    return False

def do_login(config: dict) -> requests.Session | None:
    """
    带状态缓存的登录封装。优先使用本地 Cookie，失效则发起全新登录。
    """
    sess = requests.Session()
    data_dir = config["data_dir"]
    use_webvpn = config.get("use_webvpn", False)
    use_cas = config.get("use_cas", True)
    base_url = config["webvpn_base"] if use_webvpn else config["base_url"]

    # 尝试加载历史 Cookie
    if load_cookies(sess, data_dir):
        log.info("检测到历史 Cookie，探测其有效性...")
        try:
            # 探测教务系统首页，禁止重定向。如果返回 200 且未被拦截，说明 Cookie 仍存活
            probe_url = f"{base_url}/student/courseSelect/thisSemesterCurriculum/index"
            r = sess.get(probe_url, headers=UA_PC, allow_redirects=False, timeout=5)
            
            if r.status_code == 200 and "login" not in r.text.lower():
                log.info("历史会话仍有效，成功跳过登录流程。")
                return sess
            else:
                log.info(f"会话已过期 (HTTP {r.status_code})，准备重新登录。")
        except Exception as e:
            log.info(f"探测请求异常 ({e})，降级为重新登录。")
            
    # 清空可能残留的无效 Cookie
    sess.cookies.clear()
    
    # ── 以下为原有的全新登录流程 ──
    username = config["username"]
    password = config["password"]

    if use_webvpn:
        log.info("通过 WebVPN 登录…")
        ok = _login_webvpn(sess, username, password,
                           config["webvpn_auth"], config["webvpn_base"], config["cas_service"])
        if not ok:
            log.error("WebVPN 认证失败")
            return None
        if use_cas:
            log.info("WebVPN 已通过 CAS 认证，教务首页可直接访问")
            save_cookies(sess, data_dir)
            return sess

        log.info("WebVPN 通道已建立，使用旧版教务登录…")
        for attempt in range(1, 6):
            try:
                if _login_direct_legacy(sess, username, password, config["webvpn_base"]):
                    save_cookies(sess, data_dir)
                    return sess
            except Exception as e:
                log.warning(f"教务登录异常({attempt}/5): {e}")
            if attempt < 5:
                time.sleep(2)
        log.error("WebVPN 通道下的旧版教务登录连续失败")
        return None

    if use_cas:
        log.info("直连 CAS 登录…")
        for attempt in range(1, 6):
            try:
                if _login_direct_cas(sess, username, password, base_url):
                    save_cookies(sess, data_dir)
                    return sess
            except Exception as e:
                log.warning(f"教务登录异常({attempt}/5): {e}")
            if attempt < 5:
                time.sleep(2)
    else:
        log.info("使用旧版教务登录…")
        for attempt in range(1, 6):
            try:
                if _login_direct_legacy(sess, username, password, base_url):
                    save_cookies(sess, data_dir)
                    return sess
            except Exception as e:
                log.warning(f"教务登录异常({attempt}/5): {e}")
            if attempt < 5:
                time.sleep(2)

    log.error("教务系统登录连续失败")
    return None

# ══════════════════════════════════════════════════════════════════
# 数据抓取（原样返回服务器 JSON，不做额外解析/兼容）
# ══════════════════════════════════════════════════════════════════

def _extract_score_token(url: str, html: str, kind: str) -> str:
    for src in (url, html):
        for pat in (rf"/scoreQuery/([^/]+)/{kind}", rf"scoreQuery/([^/]+)/{kind}"):
            m = re.search(pat, src)
            if m and m.group(1):
                return m.group(1).strip()
    return ""


def fetch_schedule(sess: requests.Session, base_url: str) -> dict | None:
    """
    抓取课程表，原样返回服务器 JSON（dict 或 list）。
    返回 None 表示抓取失败。
    """
    ref = f"{base_url}/student/courseSelect/thisSemesterCurriculum/index"
    try:
        sess.get(ref, headers=UA_PC, timeout=10)
        r = sess.get(
            f"{base_url}/student/courseSelect/thisSemesterCurriculum/ajaxStudentSchedule/callback",
            headers={**UA_PC, "X-Requested-With": "XMLHttpRequest", "Referer": ref},
            timeout=15,
        )
        if "login" in r.url:
            log.warning("fetch_schedule: Session 失效")
            return None
        data = r.json()
        log.info(f"[课程表] 抓取成功，结构类型={type(data).__name__}")
        return data
    except Exception as e:
        log.error(f"抓取课程表失败: {e}")
        return None


def fetch_this_term_scores(sess: requests.Session, base_url: str) -> dict | list | None:
    """抓取本学期成绩，原样返回服务器 JSON。"""
    index_url = f"{base_url}/student/integratedQuery/scoreQuery/thisTermScores/index"
    try:
        r_idx = sess.get(index_url, headers=UA_PC, timeout=10)
        if "login" in r_idx.url:
            return None
        token_seg = _extract_score_token(r_idx.url, r_idx.text, "thisTermScores")
        data_url = (f"{base_url}/student/integratedQuery/scoreQuery/{token_seg}/thisTermScores/data"
                    if token_seg else f"{base_url}/student/integratedQuery/scoreQuery/thisTermScores/data")
        r = sess.get(data_url, headers={**UA_PC, "X-Requested-With": "XMLHttpRequest",
                                         "Referer": index_url}, timeout=15)
        if "login" in r.url:
            return None
        data = r.json()
        log.info(f"[本学期成绩] 抓取成功，结构类型={type(data).__name__}")
        return data
    except Exception as e:
        log.error(f"抓取本学期成绩失败: {e}")
        return None


def _normalize_all_score_record(record: dict) -> dict:
    """将 allTermScores 返回的字段归一化为项目内部常用成绩结构。"""
    kch = str(record.get("KCH") or record.get("courseNumber") or record.get("kch") or "").strip()
    kcm = str(record.get("KCM") or record.get("courseName") or record.get("kcm") or "").strip()
    xf = str(record.get("XF") or record.get("credit") or record.get("xf") or "").strip()
    cj = str(record.get("KCCJ") or record.get("score") or record.get("cj") or record.get("courseScore") or "").strip()
    grade_name = str(record.get("DJM") or record.get("gradeName") or record.get("grade") or "").strip()
    cjlrfsdm = str(record.get("CJLRFSDM") or record.get("scoreEntryModeCode") or record.get("cjlrfsdm") or "").strip()
    jd = str(record.get("JD") or record.get("gradePoint") or record.get("gradePointScore") or "").strip()
    normalized = dict(record)
    normalized.update({
        "kch": kch,
        "courseNumber": kch,
        "kcm": kcm,
        "courseName": kcm,
        "xf": xf,
        "credit": xf,
        "cj": cj,
        "score": cj,
        "gradeName": grade_name,
        "grade": grade_name,
        "cjlrfsdm": cjlrfsdm,
        "scoreEntryModeCode": cjlrfsdm,
    })
    if jd:
        normalized["jd"] = jd
        normalized["gradePoint"] = jd
        normalized["gradePointScore"] = jd
    return normalized


def fetch_all_scores(sess: requests.Session, base_url: str) -> list[dict] | None:
    """抓取历史全部成绩，返回归一化后的成绩列表。"""
    index_url = f"{base_url}/student/integratedQuery/scoreQuery/allTermScores/index"
    try:
        r_idx = sess.get(index_url, headers=UA_PC, timeout=10)
        if "login" in r_idx.url:
            return None
        token_seg = _extract_score_token(r_idx.url, r_idx.text, "allTermScores")
        data_url = (f"{base_url}/student/integratedQuery/scoreQuery/{token_seg}/allTermScores/data"
                    if token_seg else f"{base_url}/student/integratedQuery/scoreQuery/allTermScores/data")
        base_payload = {
            "zxjxjhh": "",
            "cjlx": "1",
            "kch": "",
            "kcm": "",
            "pageSize": "300",
        }

        def _post_scores(page_num: int):
            payload = dict(base_payload)
            payload["pageNum"] = str(page_num)
            return sess.post(
                data_url,
                data=payload,
                headers={**UA_PC, "X-Requested-With": "XMLHttpRequest", "Referer": index_url},
                timeout=15,
            )

        r = _post_scores(1)
        if "login" in r.url:
            return None
        data = r.json()

        if isinstance(data, dict):
            score_list = data.get("list")
            if isinstance(score_list, dict):
                total_count = None
                page_context = score_list.get("pageContext")
                if isinstance(page_context, dict):
                    total_count = page_context.get("totalCount")
                records = score_list.get("records")
                if isinstance(total_count, int) and total_count > 0:
                    merged_records = list(records) if isinstance(records, list) else []
                    page_num = 2
                    while len(merged_records) < total_count:
                        r_page = _post_scores(page_num)
                        if "login" in r_page.url:
                            return None
                        page_data = r_page.json()
                        page_list = page_data.get("list") if isinstance(page_data, dict) else None
                        page_records = page_list.get("records") if isinstance(page_list, dict) else None
                        if isinstance(page_records, list):
                            merged_records.extend(page_records)
                        else:
                            break
                        page_num += 1
                    normalized = [
                        _normalize_all_score_record(item)
                        for item in merged_records
                        if isinstance(item, dict)
                    ]
                    log.info(f"[历史成绩] 抓取成功，结构类型=list, 条数={len(normalized)}")
                    return normalized
                if isinstance(records, list):
                    normalized = [
                        _normalize_all_score_record(item)
                        for item in records
                        if isinstance(item, dict)
                    ]
                    log.info(f"[历史成绩] 抓取成功，结构类型=list, 条数={len(normalized)}")
                    return normalized
        if isinstance(data, list):
            normalized = [
                _normalize_all_score_record(item)
                for item in data
                if isinstance(item, dict)
            ]
            log.info(f"[历史成绩] 抓取成功，结构类型=list, 条数={len(normalized)}")
            return normalized
        log.info(f"[历史成绩] 抓取成功，结构类型={type(data).__name__}")
        return []
    except Exception as e:
        log.error(f"抓取历史成绩失败: {e}")
        return None


def fetch_gpa_overview(sess: requests.Session, base_url: str) -> dict | None:
    """抓取 GPA 概览，原样返回服务器 JSON 中的第一条数据项。"""
    url = f"{base_url}/main/showMoreGPA"
    try:
        r = sess.post(url, headers={**UA_PC, "X-Requested-With": "XMLHttpRequest",
                                     "Referer": f"{base_url}/"}, timeout=12)
        if r.status_code != 200 or "login" in r.url.lower():
            log.warning(f"[GPA] 请求异常 status={r.status_code}")
            return None
        data = r.json()
        log.info(f"[GPA] 抓取成功")
        return data
    except Exception as e:
        log.error(f"抓取 GPA 失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 本地存储
# ══════════════════════════════════════════════════════════════════

def _data_path(data_dir: str, name: str) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{name}.json"


def _payload_core(payload):
    """提取用于比较的核心数据，忽略 fetch_time 这类包装字段。"""
    if isinstance(payload, dict):
        if 'data' in payload:
            return payload['data']
        return {k: v for k, v in payload.items() if k != 'fetch_time'}
    return payload


def _normalize_gpa_payload(obj):
    """提取 GPA 比较用数据，并忽略生成时间字段。"""
    if isinstance(obj, dict):
        if isinstance(obj.get('data'), (list, dict)):
            return _normalize_gpa_payload(obj.get('data'))
        return {
            k: _normalize_gpa_payload(v)
            for k, v in obj.items()
            if k not in {'fetch_time', 'generated_at', '生成时间', 'time', 'status', 'msg'}
        }
    if isinstance(obj, list):
        normalized = []
        for item in obj:
            if isinstance(item, list) and len(item) >= 4:
                row = list(item)
                if len(row) >= 4:
                    del row[3]
                normalized.append(row)
            else:
                normalized.append(_normalize_gpa_payload(item))
        return normalized
    return obj


def _gpa_rank_missing(raw_gpa) -> bool:
    """判断 GPA 抓取结果是否处于排名空缺的重算窗口。"""
    payload = raw_gpa
    if isinstance(raw_gpa, dict) and isinstance(raw_gpa.get('data'), list) and raw_gpa.get('data'):
        payload = raw_gpa['data']

    def _blank(value) -> bool:
        text = str(value).strip()
        return text == "" or text == "-" or text.lower() == "none"

    class_rank = None
    grade_rank = None

    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, list):
            class_rank = first[2] if len(first) > 2 else None
            grade_rank = first[4] if len(first) > 4 else None
        elif isinstance(first, dict):
            class_rank = first.get('class_rank') or first.get('班级排名')
            grade_rank = first.get('grade_rank') or first.get('年级排名')
    elif isinstance(payload, dict):
        class_rank = payload.get('class_rank') or payload.get('班级排名')
        grade_rank = payload.get('grade_rank') or payload.get('年级排名')

    return _blank(class_rank) or _blank(grade_rank)


def load_data(data_dir: str, name: str):
    """加载本地 JSON 文件，不存在返回 None。"""
    path = _data_path(data_dir, name)
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        # 兼容 wrapper: lists are returned as-is; dicts containing fetch_time
        # may either be a wrapper with key 'data' or a payload where we strip fetch_time
        if isinstance(obj, dict) and 'fetch_time' in obj:
            if 'data' in obj:
                return obj['data']
            # strip fetch_time and return remaining dict
            return {k: v for k, v in obj.items() if k != 'fetch_time'}
        return obj
    except Exception:
        return None


def save_data(data_dir: str, name: str, payload, fetch_time: str):
    """
    将 payload 原样写入 data/{name}.json，同时在保存的 JSON 中嵌入抓取时间 `fetch_time`。
    若数据有变化，先将旧版本归档到 data/archive/{name}/ 下。
    """
    path = _data_path(data_dir, name)

    # 归档旧版本
    if path.exists():
        try:
            old_text = path.read_text(encoding="utf-8")
            try:
                old_obj = json.loads(old_text)
            except Exception:
                old_obj = None

            # 抽取旧的 core payload 以便比较（兼容带 fetch_time 的格式）
            old_core = None
            if isinstance(old_obj, dict) and 'fetch_time' in old_obj:
                if 'data' in old_obj:
                    old_core = old_obj['data']
                else:
                    old_core = {k: v for k, v in old_obj.items() if k != 'fetch_time'}
            else:
                old_core = old_obj

            if name == "gpa":
                old_core = _normalize_gpa_payload(old_core)
                new_core = _normalize_gpa_payload(payload)
            else:
                new_core = _payload_core(payload)

            if old_core is None or json.dumps(old_core, ensure_ascii=False, sort_keys=True) != json.dumps(new_core, ensure_ascii=False, sort_keys=True):
                archive_dir = Path(data_dir) / "archive" / name
                archive_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                (archive_dir / f"{ts}.json").write_text(old_text, encoding="utf-8")
        except Exception as e:
            log.warning(f"归档旧数据失败: {e}")

    # 将 payload 包装或注入抓取时间后写入文件：
    # - 若 payload 为 list，则写为 {fetch_time, data: [...]}
    # - 若 payload 为 dict，则在不覆盖原有键的前提下添加 fetch_time
    if isinstance(payload, list):
        out = {"fetch_time": fetch_time, "data": payload}
    elif isinstance(payload, dict):
        out = dict(payload)
        out['fetch_time'] = fetch_time
    else:
        out = {"fetch_time": fetch_time, "data": payload}

    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def record_change(data_dir: str, entry: dict):
    """追加一条变动记录到 changes.jsonl。"""
    p = Path(data_dir) / "changes.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════════════════════════════
# 课程表解析与去重
# ══════════════════════════════════════════════════════════════════

def _flatten_schedule(raw) -> list[dict]:
    """
    将服务器返回的课程表 JSON 展平为课次列表，不做过多兼容。
    支持两种常见格式：
      - list[dict]：直接使用
      - dict with xkxx[0]（新版接口）：展平每门课的 timeAndPlaceList
    """
    if isinstance(raw, list):
        # 保留原始条目，若条目为 dict，包装为 raw_course
        out = []
        for it in raw:
            if isinstance(it, dict):
                item = dict(it)
                item.setdefault('raw_course', deepcopy(it))
                item.setdefault('raw_session', None)
                out.append(item)
            else:
                out.append(it)
        return out

    if isinstance(raw, dict):
        xkxx = raw.get("xkxx")
        if isinstance(xkxx, list) and xkxx and isinstance(xkxx[0], dict):
            rows: list[dict] = []
            for course in xkxx[0].values():
                if not isinstance(course, dict):
                    continue
                cid = course.get("id") if isinstance(course.get("id"), dict) else {}
                kch = str(course.get("courseNumber") or course.get("coureNumber")
                          or cid.get("courseNumber") or cid.get("coureNumber") or "")
                kcm = str(course.get("courseName") or "")
                kxh = str(cid.get("coureSequenceNumber") or course.get("kxh") or "")
                skjs = str(course.get("attendClassTeacher") or "").strip()
                skzc = str(course.get("skzcs") or "")

                tps = course.get("timeAndPlaceList") or []
                if not isinstance(tps, list) or not tps:
                    item = {
                        "kch": kch,
                        "kcm": kcm,
                        "kxh": kxh,
                        "skjs": skjs,
                        "skxq": "",
                        "skjc": "",
                        "skzc": skzc,
                        "jxdd": "",
                        "raw_course": deepcopy(course),
                        "raw_session": None,
                    }
                    rows.append(item)
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
                    item = {
                        "kch": kch or str(tp.get("coureNumber") or ""),
                        "kcm": kcm or str(tp.get("coureName") or ""),
                        "kxh": kxh,
                        "skjs": skjs,
                        "skxq": str(tp.get("classDay") or ""),
                        "skjc": skjc,
                        "skzc": str(tp.get("classWeek") or skzc),
                        "jxdd": str(tp.get("classroomName") or ""),
                        "raw_course": deepcopy(course),
                        "raw_session": deepcopy(tp),
                    }
                    rows.append(item)
            return rows

        # 其他 dict 包装格式（尝试常见 key）
        for key in ("list", "kbList", "data", "rows"):
            if isinstance(raw.get(key), list):
                return raw[key]

    return []


def _flatten_all_scores(raw) -> list[dict]:
    """将历史成绩展平为列表。支持 lnList[].cjList[] 嵌套格式和直接列表。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("lnList"), list):
            rows = []
            for term in raw["lnList"]:
                if isinstance(term, dict) and isinstance(term.get("cjList"), list):
                    rows.extend(x for x in term["cjList"] if isinstance(x, dict))
            return rows
        for key in ("list", "data", "rows"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def _flatten_this_term_scores(raw) -> list[dict]:
    """展平本学期成绩列表。"""
    if isinstance(raw, list):
        # 旧格式: [{"state":"1","list":[...]}]
        if raw and isinstance(raw[0], dict) and "list" in raw[0]:
            return raw[0].get("list") or []
        return raw
    if isinstance(raw, dict):
        for key in ("list", "data", "rows"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def build_schedule_dedup(raw_list: list[dict]) -> dict:
    """
    按「课程号 + 课序号」去重，返回包含去重后课程列表和总课程数的结构。
    同一门课（相同课程号+课序号）的多个上课时间条目会合并到 sessions 字段。
    """
    groups: dict[str, dict] = {}
    for item in raw_list:
        kch = str(item.get("kch") or item.get("courseNumber") or "").strip()
        kxh = str(item.get("kxh") or item.get("coureSequenceNumber")
                  or (item.get("id") or {}).get("coureSequenceNumber") or "").strip()
        key = f"{kxh}_{kch}"
        if key not in groups:
            # 复制一些通用字段，并保存 first_meta 以保留课程级别的其它原始字段
            # 排除 raw_course 和 raw_session，它们包含完整的原始 API 数据，太冗长
            first_meta = {k: v for k, v in item.items() if k not in ("skxq", "skjc", "skzc", "jxdd", "raw_session", "raw_course")}
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
    return {
        "total_course_count": len(courses),
        "courses": courses,
    }


# ══════════════════════════════════════════════════════════════════
# 变动检测
# ══════════════════════════════════════════════════════════════════

def _deep_equal(a, b) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == \
           json.dumps(b, ensure_ascii=False, sort_keys=True)


def diff_schedule_dedup(old: dict, new: dict) -> list[dict]:
    """
    比较去重后的课程表，返回变动列表。
    每条变动包含 action（added/removed/modified）和相关字段。
    """
    changes = []
    old_map = {f"{c['kxh']}_{c['kch']}": c for c in (old.get("courses") or [])}
    new_map = {f"{c['kxh']}_{c['kch']}": c for c in (new.get("courses") or [])}

    for key, nc in new_map.items():
        if key not in old_map:
            changes.append({"action": "added", "key": key, "course": deepcopy(nc)})
        elif not _deep_equal(old_map[key], nc):
            oc = old_map[key]
            # 捕获字段级别的变动
            all_keys = set(oc.keys()) | set(nc.keys())
            field_changes = {}
            for fk in all_keys:
                # 如果是 meta 字段，展开内部键逐一比较，避免把整个 meta 当成单一字段
                if fk == "meta":
                    om = oc.get("meta") or {}
                    nm = nc.get("meta") or {}
                    inner_keys = set(om.keys()) | set(nm.keys())
                    for ik in inner_keys:
                        ov, nv = om.get(ik), nm.get(ik)
                        if str(ov) != str(nv):
                            field_changes[ik] = {"before": ov, "after": nv}
                    continue
                # sessions 是一个课次列表，按索引逐项比较其内部字段
                if fk == "sessions":
                    osessions = oc.get("sessions") or []
                    nsessions = nc.get("sessions") or []
                    max_len = max(len(osessions), len(nsessions))
                    for idx in range(max_len):
                        os = osessions[idx] if idx < len(osessions) else None
                        ns = nsessions[idx] if idx < len(nsessions) else None
                        if os is None and ns is not None:
                            for sk, sv in ns.items():
                                if sk == "raw_session":
                                    continue
                                field_changes[f"session[{idx}].{sk}"] = {"before": None, "after": sv}
                            continue
                        if ns is None and os is not None:
                            for sk, sv in os.items():
                                if sk == "raw_session":
                                    continue
                                field_changes[f"session[{idx}].{sk}"] = {"before": sv, "after": None}
                            continue
                        if os is None or ns is None or _deep_equal(os, ns):
                            continue
                        session_keys = set(os.keys()) | set(ns.keys())
                        for sk in session_keys:
                            if sk == "raw_session":
                                continue
                            ov, nv = os.get(sk), ns.get(sk)
                            if str(ov) != str(nv):
                                field_changes[f"session[{idx}].{sk}"] = {"before": ov, "after": nv}
                    continue
                ov, nv = oc.get(fk), nc.get(fk)
                if str(ov) != str(nv):
                    field_changes[fk] = {"before": ov, "after": nv}
            changes.append({"action": "modified", "key": key,
                            "before": deepcopy(oc), "after": deepcopy(nc),
                            "fields": field_changes})
    for key, oc in old_map.items():
        if key not in new_map:
            changes.append({"action": "removed", "key": key, "course": deepcopy(oc)})
    return changes


def _score_key(item: dict) -> str:
    kch = str(item.get("kch") or item.get("courseNumber")
               or (item.get("id") or {}).get("courseNumber")
               or (item.get("id") or {}).get("kch_zj") or "").strip()
    kcm = str(item.get("kcm") or item.get("courseName") or "").strip()
    return f"{kch}_{kcm}"


def diff_scores(old_list: list, new_list: list) -> list[dict]:
    """
    比较成绩列表，返回变动列表。
    检查所有字段（不只是成绩字段），忽略无意义差异。
    """
    changes = []
    old_map = {_score_key(s): s for s in old_list}
    new_map = {_score_key(s): s for s in new_list}

    for key, ns in new_map.items():
        if key not in old_map:
            changes.append({"action": "added", "key": key, "score": deepcopy(ns)})
        else:
            os_ = old_map[key]
            if not _deep_equal(os_, ns):
                # 找出哪些字段变了
                all_keys = set(os_.keys()) | set(ns.keys())
                field_changes = {}
                for fk in all_keys:
                    ov, nv = os_.get(fk), ns.get(fk)
                    if str(ov) != str(nv):
                        field_changes[fk] = {"before": ov, "after": nv}
                changes.append({"action": "modified", "key": key, "fields": field_changes,
                                 "before": deepcopy(os_), "after": deepcopy(ns)})
    for key, os_ in old_map.items():
        if key not in new_map:
            changes.append({"action": "removed", "key": key, "score": deepcopy(os_)})
    return changes


# GPA 对比字段（生成时间不计入变动判断）
_GPA_IGNORE_KEYS = {"generated_at", "生成时间", "time"}




def diff_gpa(old_raw, new_raw) -> list[dict]:
    """GPA 对比，排除生成时间的影响"""
    old_cmp = _normalize_gpa_payload(deepcopy(old_raw)) if old_raw else None
    new_cmp = _normalize_gpa_payload(deepcopy(new_raw)) if new_raw else None

    if json.dumps(old_cmp, sort_keys=True) == json.dumps(new_cmp, sort_keys=True):
        return []

    changes = []
    if isinstance(old_cmp, dict) and isinstance(new_cmp, dict):
        for k in set(old_cmp.keys()) | set(new_cmp.keys()):
            ov, nv = old_cmp.get(k), new_cmp.get(k)
            if str(ov) != str(nv):
                changes.append({"key": k, "before": ov, "after": nv})
    elif isinstance(old_cmp, list) and isinstance(new_cmp, list):
        for i, (o, n) in enumerate(zip(old_cmp, new_cmp)):
            if json.dumps(o, sort_keys=True) != json.dumps(n, sort_keys=True):
                changes.append({"index": i, "before": o, "after": n})
        if len(new_cmp) > len(old_cmp):
            for i in range(len(old_cmp), len(new_cmp)):
                changes.append({"index": i, "before": None, "after": new_cmp[i]})
    else:
        changes.append({"before": old_cmp, "after": new_cmp})

    return changes


# ══════════════════════════════════════════════════════════════════
# 通知 —— 仅 PushDeer
# ══════════════════════════════════════════════════════════════════

def send_notify(config: dict, title: str, content: str):
    """通过 PushDeer 推送通知（Markdown 格式）。"""
    key = config.get("notify", {}).get("pushdeer_key", "")
    log.info(f"[通知] {title}\n{content}")
    if not key:
        log.info("PushDeer key 未配置，跳过推送")
        return
    try:
        r = requests.get(
            "https://api2.pushdeer.com/message/push",
            params={"pushkey": key, "text": title, "desp": content, "type": "markdown"},
            timeout=10,
        )
        if r.status_code == 200:
            log.info("PushDeer 推送成功")
        else:
            log.warning(f"PushDeer 推送失败: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"PushDeer 推送异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 通知内容格式化
# ══════════════════════════════════════════════════════════════════

def _fmt_schedule_changes(changes: list[dict]) -> str:
    """
    格式化课程表变动。格式：
    [课程] "课程名" 新增/删除课程
    [课程] "键" 变动 "课程名" 值1->值2
    [课程] 多项变动 "课程名"
    """
    lines = []
    for c in changes:
        action = c.get("action")
        if action == "added":
            course = c.get("course") or c.get("after") or {}
            kcm = course.get("kcm") or ""
            lines.append(f'[课程] "{kcm}" 新增课程')
        elif action == "removed":
            course = c.get("course") or c.get("before") or {}
            kcm = course.get("kcm") or ""
            lines.append(f'[课程] "{kcm}" 删除课程')
        elif action == "modified":
            before = c.get("before") or {}
            after = c.get("after") or {}
            kcm = after.get("kcm") or before.get("kcm") or ""
            # 优先使用 diff 函数生成的 fields（如果存在），否则回退到对比 before/after
            fields = c.get("fields")
            if not fields:
                all_keys = set(before.keys()) | set(after.keys())
                fields = {k: {"before": before.get(k), "after": after.get(k)}
                          for k in all_keys if str(before.get(k)) != str(after.get(k))}

            fkeys = list(fields.keys())
            if len(fkeys) == 1:
                fk = fkeys[0]
                fv = fields[fk]
                bv = fv.get("before", "")
                av = fv.get("after", "")
                lines.append(f'[课程] "{_fmt_schedule_field_label(fk)}" 变动 "{kcm}" {bv}->{av}')
            elif len(fkeys) > 1:
                # 列出所有字段的具体变动
                for fk in sorted(fkeys):
                    fv = fields[fk]
                    bv = fv.get("before", "")
                    av = fv.get("after", "")
                    lines.append(f'[课程] "{_fmt_schedule_field_label(fk)}" 变动 "{kcm}" {bv}->{av}')
    return "\n".join(lines)


def _fmt_schedule_field_label(key: str) -> str:
    """把课程变动里的字段名转换成更适合展示的中文。"""
    if key.startswith("session[") and "." in key:
        head, subkey = key.split(".", 1)
        try:
            idx = int(head[len("session["):-1]) + 1
        except Exception:
            idx = 1
        sub_label = {
            "skxq": "上课日",
            "skjc": "上课节次",
            "skzc": "上课周次",
            "jxdd": "教室名称",
        }.get(subkey, label(subkey))
        return f"第{idx}个课时·{sub_label}"
    if key == "jxdd":
        return "教室名称"
    return label(key)


def _fmt_score_changes(changes: list[dict], tag: str = "成绩") -> str:
    """
    格式化成绩变动。格式：
    [成绩] "课程名" 新增成绩 成绩值
    [成绩] "课程名" 删除成绩
    [成绩] "键" 变动 "课程名" 值1->值2
    [成绩] 多项变动 "课程名"
    """
    lines = []
    for c in changes:
        action = c.get("action")
        if action == "added":
            score = c.get("score") or c.get("after") or {}
            kcm = score.get("kcm") or score.get("courseName") or ""
            cj = score.get("cj") or score.get("score") or score.get("grade") or ""
            lines.append(f'[{tag}] "{kcm}" 新增成绩 {cj}')
        elif action == "removed":
            score = c.get("score") or c.get("before") or {}
            kcm = score.get("kcm") or score.get("courseName") or ""
            lines.append(f'[{tag}] "{kcm}" 删除成绩')
        elif action == "modified":
            before = c.get("before") or {}
            after = c.get("after") or {}
            kcm = after.get("kcm") or after.get("courseName") or before.get("kcm") or before.get("courseName") or ""
            
            fields = c.get("fields") or {}
            if len(fields) == 1:
                fk, fv = next(iter(fields.items()))
                bv = fv.get("before", "")
                av = fv.get("after", "")
                lines.append(f'[{tag}] "{label(fk)}" 变动 "{kcm}" {bv}->{av}')
            elif len(fields) > 1:
                # 列出所有字段的具体变动
                for fk in sorted(fields.keys()):
                    fv = fields[fk]
                    bv = fv.get("before", "")
                    av = fv.get("after", "")
                    lines.append(f'[{tag}] "{label(fk)}" 变动 "{kcm}" {bv}->{av}')
    return "\n".join(lines)


def _fmt_gpa_changes(changes: list[dict]) -> str:
    """格式化 GPA 变动，确保提取有效信息"""
    lines = []
    for c in changes:
        # 兼容 fields 字典格式
        fields = c.get("fields") or {}
        if fields:
            for fk, fv in fields.items():
                if "gpa" in str(fk).lower() or "绩点" in label(fk):
                    bv, av = fv.get("before", "-"), fv.get("after", "-")
                    lines.append(f'[GPA] "{label(fk)}" 变动: {bv} -> {av}')
        # 兼容 key/before/after 扁平格式
        elif "key" in c:
            k = c["key"]
            if "gpa" in str(k).lower() or "绩点" in label(k):
                bv, av = c.get("before", "-"), c.get("after", "-")
                lines.append(f'[GPA] "{label(k)}" 变动: {bv} -> {av}')
        elif "index" in c:
            lines.append(f'[GPA] 数据项更新')
            
    return "\n".join(lines) if lines else "[GPA] 发生变动"


# ══════════════════════════════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════════════════════════════

def run_once(config: dict):
    log.info("═" * 50)
    log.info("开始抓取…")
    data_dir = config["data_dir"]
    use_webvpn = config.get("use_webvpn", False)
    base_url = config["webvpn_base"] if use_webvpn else config["base_url"]
    fetch_time = datetime.now().isoformat()

    sess = do_login(config)
    if sess is None:
        log.error("登录失败，本次跳过")
        send_notify(config, "⚠️ 教务监控登录失败", "请检查账号密码或网络。")
        return

    notify_parts: list[str] = []
    is_first_run_any = False

    # ── 1. 课程表 ─────────────────────────────────────────────────
    raw_schedule = fetch_schedule(sess, base_url)
    if raw_schedule is not None:
        # 展平为课次列表
        flat_list = _flatten_schedule(raw_schedule)
        log.info(f"[课程表] 展平后 {len(flat_list)} 条课次")

        # 去重
        dedup = build_schedule_dedup(flat_list)
        log.info(f"[课程表] 去重后 {dedup['total_course_count']} 门课")

        # 先读取旧的平铺列表并构建去重结构进行比对，再保存当前抓取结果
        old_sched_raw = load_data(data_dir, "schedule")
        old_sched = build_schedule_dedup(old_sched_raw if isinstance(old_sched_raw, list) else [])

        if old_sched_raw is None:
            # 首次运行：记录初始化，不计入变动次数
            is_first_run_any = True
            entry = {
                "time": fetch_time,
                "type": "schedule",
                "initial": True,
                "count": dedup["total_course_count"],
                "note": f"首次抓取，共 {dedup['total_course_count']} 门课",
            }
            record_change(data_dir, entry)
            log.info(f"[课程表] 首次抓取，记录 {dedup['total_course_count']} 门课")
        else:
            changes = diff_schedule_dedup(old_sched, dedup)
            if changes:
                msg = _fmt_schedule_changes(changes)
                entry = {
                    "time": fetch_time, "type": "schedule",
                    "changes": changes,
                    "changes_count": len(changes),
                    "summary": msg,
                }
                record_change(data_dir, entry)
                notify_parts.append(msg)
                log.info(f"[课程表] {len(changes)} 条变动")
            else:
                log.info("[课程表] 无变动")

        # 保存展平后的课程记录到 schedule.json（包含所有原始字段和 fetch_time）
        save_data(data_dir, "schedule", flat_list, fetch_time)

    # ── 2. 本学期成绩 ──────────────────────────────────────────────
    raw_term = fetch_this_term_scores(sess, base_url)
    if raw_term is not None:
        flat_term = _flatten_this_term_scores(raw_term)
        log.info(f"[本学期成绩] {len(flat_term)} 条")
        old_term = load_data(data_dir, "this_term_scores")

        if old_term is None:
            is_first_run_any = True
            entry = {"time": fetch_time, "type": "this_term_scores", "initial": True,
                     "count": len(flat_term), "note": f"首次抓取，共 {len(flat_term)} 条"}
            record_change(data_dir, entry)
            log.info(f"[本学期成绩] 首次抓取 {len(flat_term)} 条")
        else:
            changes = diff_scores(old_term if isinstance(old_term, list) else [], flat_term)
            if changes:
                msg = _fmt_score_changes(changes, "成绩")
                entry = {"time": fetch_time, "type": "this_term_scores",
                         "changes": changes, "changes_count": len(changes),
                         "summary": msg}
                record_change(data_dir, entry)
                notify_parts.append(msg)
                log.info(f"[本学期成绩] {len(changes)} 条变动")
            else:
                log.info("[本学期成绩] 无变动")

        save_data(data_dir, "this_term_scores", flat_term, fetch_time)

    # ── 3. 历史成绩 ────────────────────────────────────────────────
    raw_all = fetch_all_scores(sess, base_url)
    if raw_all is not None:
        flat_all = _flatten_all_scores(raw_all)
        log.info(f"[历史成绩] {len(flat_all)} 条")
        old_all = load_data(data_dir, "all_scores")

        if old_all is None:
            is_first_run_any = True
            entry = {"time": fetch_time, "type": "all_scores", "initial": True,
                     "count": len(flat_all), "note": f"首次抓取，共 {len(flat_all)} 条"}
            record_change(data_dir, entry)
            log.info(f"[历史成绩] 首次抓取 {len(flat_all)} 条")
        else:
            changes = diff_scores(old_all if isinstance(old_all, list) else [], flat_all)
            if changes:
                msg = _fmt_score_changes(changes, "成绩")
                entry = {"time": fetch_time, "type": "all_scores",
                         "changes": changes, "changes_count": len(changes),
                         "summary": msg}
                record_change(data_dir, entry)
                notify_parts.append(msg)
                log.info(f"[历史成绩] {len(changes)} 条变动")
            else:
                log.info("[历史成绩] 无变动")

        save_data(data_dir, "all_scores", flat_all, fetch_time)

    # ── 4. GPA ────────────────────────────────────────────────────
    raw_gpa = fetch_gpa_overview(sess, base_url)
    if raw_gpa is not None:
        if _gpa_rank_missing(raw_gpa):
            log.info("[GPA] 发现空排名，跳过本次记录")
        else:
            old_gpa = load_data(data_dir, "gpa")

            if old_gpa is None:
                is_first_run_any = True
                entry = {"time": fetch_time, "type": "gpa", "initial": True,
                         "note": "首次抓取 GPA"}
                record_change(data_dir, entry)
            else:
                gpa_changes = diff_gpa(old_gpa, raw_gpa)
                if gpa_changes:
                    msg = _fmt_gpa_changes(gpa_changes)
                    entry = {"time": fetch_time, "type": "gpa",
                             "changes": gpa_changes, "changes_count": len(gpa_changes),
                             "summary": msg}
                    record_change(data_dir, entry)
                    notify_parts.append(msg)
                    log.info(f"[GPA] {len(gpa_changes)} 条变动")
                else:
                    log.info("[GPA] 无变动（或仅生成时间更新）")

            save_data(data_dir, "gpa", raw_gpa, fetch_time)

    sess.close()

    # ── 推送 ─────────────────────────────────────────────────────
    if notify_parts:
        send_notify(config, "📚 教务系统有新变动", "\n".join(notify_parts))
    elif is_first_run_any:
        send_notify(config, "✅ 教务监控首次抓取成功",
                    "已完成首次数据抓取，后续变动将第一时间推送。")
    else:
        log.info("本次无变动，不推送通知。")

    log.info("本次抓取完成。")


def main():
    log.info("东北农业大学教务监控启动")
    log.info(f"抓取间隔: {CONFIG['interval']} 秒")
    log.info(f"数据目录: {CONFIG['data_dir']}")
    log.info(f"WebVPN 模式: {'开启' if CONFIG.get('use_webvpn') else '关闭'}")

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
