"""
查询 NEAU URP 分项成绩

用法:
  python subitem_score.py                          # 交互式输入参数
  python subitem_score.py <学年学期> <课程号> <课序号> <考试日期>

示例:
  python subitem_score.py 2025-2026-2-1 09610021j 10 20260625

注意: NEAU 的 URP 系统可能未开放分项成绩接口，返回空数据属于正常情况。
"""

import json
import logging
import sys
from pathlib import Path

import requests

from monitor import (
    CONFIG,
    UA_PC,
    _load_config,
    _login_direct_cas,
    _login_webvpn,
    load_cookies,
    save_cookies,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("subitem_score.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("subitem_score")

BASE_URL = CONFIG.get("base_url", "https://zhjwxs.neau.edu.cn")


def _extract_token(url: str, html: str) -> str:
    """从 index 页面的 URL 或 HTML 中提取 scoreQuery token。"""
    import re
    for src in (url, html):
        for pat in (r"/scoreQuery/([^/]+)/subitemScore", r"scoreQuery/([^/]+)/subitemScore"):
            m = re.search(pat, src)
            if m and m.group(1):
                return m.group(1).strip()
    return ""


def fetch_subitem_scores(
    sess: requests.Session,
    base_url: str,
    zxjxjhh: str,
    kch: str,
    kxh: str,
    kssj: str,
    param: str = "001",
) -> dict | list | None:
    """
    查询某门课程的分项成绩。

    参数:
      zxjxjhh — 学年学期，如 "2025-2026-2-1"
      kch      — 课程号，如 "09610021j"
      kxh      — 课序号，如 "10"
      kssj     — 考试日期，如 "20260625"
      param    — 固定 "001"

    返回:
      服务器原始 JSON 响应（含 records / pageContext），或 None 表示失败。
    """
    # 拼接 fxcjId（与 URP 前端 JS 一致）
    fxcj_id = f"{zxjxjhh}_{kch}_{kxh}_{kssj}_{param}"

    # 1. 访问 index 页面，获取 token 和 session cookie
    index_url = (
        f"{base_url}/student/integratedQuery/scoreQuery/subitemScore"
        f"/fxcjIndex/{zxjxjhh}/{kch}/{kxh}/{kssj}/{param}"
    )
    log.info(f"访问分项成绩 index: {index_url}")
    try:
        r_idx = sess.get(index_url, headers=UA_PC, timeout=15)
        if "login" in r_idx.url:
            log.warning("Session 失效，请重新登录")
            return None

        token = _extract_token(r_idx.url, r_idx.text)
        if not token:
            log.warning("未能从 index 页面提取 token，尝试无 token 请求")
        else:
            log.info(f"提取到 token: {token}")

        # 同时从 HTML 中提取 fxcjId（以防拼接的不准确）
        import re
        m = re.search(r'id="fxcjId"[^>]*value="([^"]*)"', r_idx.text)
        if m and m.group(1):
            fxcj_id = m.group(1)
            log.info(f"从页面提取到 fxcjId: {fxcj_id}")

    except Exception as e:
        log.error(f"访问 index 页面失败: {e}")
        return None

    # 2. POST 请求分项成绩数据
    search_url = (
        f"{base_url}/student/integratedQuery/scoreQuery"
        f"/{token}/subitemScore/searchFxcj"
        if token else
        f"{base_url}/student/integratedQuery/scoreQuery/subitemScore/searchFxcj"
    )
    headers = {
        **UA_PC,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": index_url,
    }
    data = {
        "fxcjId": fxcj_id,
        "pageNum": "1",
        "pageSize": "30",
    }

    log.info(f"POST 分项成绩数据: {search_url}")
    log.info(f"  fxcjId = {fxcj_id}")
    try:
        r = sess.post(search_url, headers=headers, data=data, timeout=15)
        if "login" in r.url:
            log.warning("Session 失效")
            return None

        log.info(f"响应状态: {r.status_code}, Content-Type: {r.headers.get('Content-Type', 'N/A')}")
        body = r.text.strip()

        # 如果返回 HTML 而非 JSON，说明接口可能不存在或参数有误
        if body.startswith("<"):
            log.warning("服务器返回 HTML 而非 JSON，分项成绩接口可能未开放")
            log.debug(f"响应前 500 字符:\n{body[:500]}")
            return {"raw_html": True, "snippet": body[:1000]}

        result = r.json()
        records = result.get("records") if isinstance(result, dict) else None
        count = len(records) if records else 0
        log.info(f"分项成绩查询成功，共 {count} 条记录")
        return result

    except requests.exceptions.JSONDecodeError:
        log.warning("响应不是有效 JSON")
        log.debug(f"响应内容:\n{r.text[:500]}")
        return {"raw_text": r.text[:1000]}
    except Exception as e:
        log.error(f"查询分项成绩失败: {e}")
        return None


def print_scores(result):
    """格式化输出分项成绩。"""
    if result is None:
        print("\n查询失败，无返回数据。")
        return

    if result.get("raw_html"):
        print("\n服务器返回了 HTML 页面（而非 JSON），分项成绩接口可能未对 NEAU 开放。")
        return

    if result.get("raw_text"):
        print(f"\n服务器返回了非 JSON 内容:\n{result['raw_text']}")
        return

    records = result.get("records") if isinstance(result, dict) else None
    if not records:
        page_ctx = result.get("pageContext", {})
        total = page_ctx.get("totalCount", 0) if page_ctx else 0
        print(f"\n查询成功，但无分项成绩记录（totalCount={total}）。")
        print("可能原因：该课程尚无分项成绩，或 NEAU 未开放此接口。")
        print(f"\n原始响应: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return

    print(f"\n{'序号':<6}{'分项名称':<20}{'分项成绩':<12}{'分项系数':<10}")
    print("-" * 48)
    for i, rec in enumerate(records, 1):
        name = rec.get("CJFXMC") or ""
        score = rec.get("FXCJ") or ""
        weight = rec.get("CJFXZB")
        weight_str = f"{float(weight):.4f}" if weight else ""
        print(f"{i:<6}{name:<20}{score:<12}{weight_str:<10}")


def main():
    if len(sys.argv) == 5:
        zxjxjhh, kch, kxh, kssj = sys.argv[1:5]
    elif len(sys.argv) == 1:
        print("=== NEAU 分项成绩查询 ===")
        zxjxjhh = input("学年学期 (如 2025-2026-2-1): ").strip()
        kch = input("课程号 (如 09610021j): ").strip()
        kxh = input("课序号 (如 10): ").strip()
        kssj = input("考试日期 (如 20260625): ").strip()
    else:
        print(__doc__)
        sys.exit(1)

    # 尝试复用已有 session
    sess = requests.Session()
    cookies_loaded = load_cookies(sess, BASE_URL)
    if cookies_loaded:
        log.info("已加载缓存的 cookies")
    else:
        log.info("无可用缓存 cookies，开始登录...")
        use_webvpn = CONFIG.get("use_webvpn", False)
        use_cas = CONFIG.get("use_cas", True)
        if use_webvpn:
            result = _login_webvpn(sess, CONFIG)
        elif use_cas:
            result = _login_direct_cas(sess, CONFIG)
        else:
            print("错误: config.json 中必须启用 use_cas 或 use_webvpn")
            sys.exit(1)

        if result is None:
            print("登录失败，请检查 config.json 中的学号和密码。")
            sys.exit(1)
        save_cookies(sess)
        log.info("登录成功")

    result = fetch_subitem_scores(sess, BASE_URL, zxjxjhh, kch, kxh, kssj)
    print_scores(result)

    # 保存原始结果
    out_path = Path("data") / "subitem_score.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n原始响应已保存到 {out_path}")


if __name__ == "__main__":
    main()
