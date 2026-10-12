#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鱼娘 · 账本
=============
把 DSH 真实的 token 消耗换算成钱，供桌宠的 HUD 显示。

数据从哪来
----------
DSH 自己就有一份精确记账，不用猜也不用估：

  ~/.dsh/sessions/<项目>/<会话>/session.v4.jsonl.zstd
      每条 assistant/message 都带
        time:  毫秒时间戳
        usage: {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}

  ~/.dsh/storages/session_projcache/sessions/session-*.json
      record.rows.tokenUsage.val.{totals,last}（累计 + 最近一轮），
      当会话日志读不到时用它兜底。

于是：
  · 今日已用 = 今天（本地日期）所有会话里 usage 的合计，按当前峰谷价折算
  · 本轮消耗 = 最近一条带 usage 的记录
两个数都直接来自 DSH 的账，所以能和她实际花的对上。

计价
----
DeepSeek 价目表见 记忆/计价.json（首次运行会自动写一份模板）：

    "table": {
      "deepseek-flash":  {"cache_hit": [空闲, 高峰], "cache_miss": [...], "output": [...]},
      "deepseek-v4-pro": {...}
    },
    "peak_ranges": [["09:00","12:00"], ["14:00","18:00"]],
    "holidays": []

即：**每个价位给一对 [空闲价, 高峰价]**，高峰 = 工作日落在 peak_ranges 里的
那两段（北京时间为准），其余（含周末、holidays 里列的日期）全部按空闲价。

余额
----
DeepSeek 的余额接口要标准 API key（sk-...）。DSH 存的是 OAuth 凭证，
不是 API key，所以**不去动它**。想在 HUD 上看余额就自己配一个：
    记忆/计价.json 里填 "api_key": "sk-xxxx"
或者设环境变量 DEEPSEEK_API_KEY。没配就显示「—」，不编数字。
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.request

# ----------------------------------------------------------------------
#  位置
# ----------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
MEM = os.path.join(HERE, "记忆")
PRICE_FILE = os.path.join(MEM, "计价.json")

DSH_DIR = os.environ.get("DSH_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".dsh")
SESSION_GLOB = os.path.join(DSH_DIR, "sessions", "*", "*", "session.v4.jsonl.zstd")
PROJCACHE_GLOB = os.path.join(DSH_DIR, "storages", "session_projcache", "sessions", "*.json")

DEFAULT_PRICE = {
    # 用哪个模型的价目表；"auto" = 从会话日志里读实际模型名
    "model": "auto",
    # 元 / 百万 tokens，[空闲时段, 高峰时段]
    "table": {
        "deepseek-flash":  {"cache_hit": [0.02, 0.04], "cache_miss": [1.0, 2.0],  "output": [4.0, 8.0]},
        "deepseek-v4-pro": {"cache_hit": [0.15, 0.30], "cache_miss": [4.5, 9.0],  "output": [13.5, 27.0]},
    },
    # 认不出来的模型按 flash 算（DSH 默认就是 deepseek-flash）
    "fallback_model": "deepseek-flash",
    # 高峰时段：工作日这两个区间；其余（含周末）都是空闲
    "peak_ranges": [["09:00", "12:00"], ["14:00", "18:00"]],
    # 法定节假日全天按空闲算。官方文档说得很清楚，但节假日表我没法可靠拿到，
    # 所以留空 —— 想精确就自己往里加日期，格式 "2026-10-01"。
    "holidays": [],
    "currency": "CNY",
    "api_key": "",
    "note": "DeepSeek 官方价；工作日 9-12 / 14-18 为高峰，其余（含周末）五折",
}

_lock = threading.Lock()
_price_cache: dict | None = None


def load_price(force: bool = False) -> dict:
    global _price_cache
    with _lock:
        if _price_cache is not None and not force:
            return _price_cache
    p = dict(DEFAULT_PRICE)
    try:
        with open(PRICE_FILE, "r", encoding="utf-8-sig") as f:
            p.update(json.load(f))
    except FileNotFoundError:
        save_price(p)
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        _price_cache = p
    return p


def save_price(p: dict) -> None:
    try:
        os.makedirs(MEM, exist_ok=True)
        with open(PRICE_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def reload_price() -> dict:
    return load_price(force=True)


# ----------------------------------------------------------------------
#  峰谷
# ----------------------------------------------------------------------
def _hm(s: str) -> int:
    """'08:30' -> 510（当天的分钟数）"""
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return 0


def peak_state(now: float | None = None, price: dict | None = None) -> tuple[bool, int]:
    """返回 (是否高峰, 距下次切换的秒数)。

    高峰 = 工作日（周一至周五，且不在 holidays 里）落在 peak_ranges 里的时段；
    其余（含周末、节假日全天）都是空闲（谷）时段。

    ⚠️ 单位必须统一 —— 这里踩过一次坑：
      _hm() 给的是**当天的分钟数**（'09:00' -> 540），可原来写成了
      `a = _hm(r[0]) * 60`（当成秒 = 32400），又拿它去跟 `sec_of_day // 60`
      （分钟，0~1439）比 —— 区间判断**永远不成立**，于是高峰时段也一律报"谷"
      （主人看到的「峰段显示谷段」就是这个）；倒计时那边还多乘了一次 60，
      算出来是 190 万秒 ≈ 22 天。现在全程按**分钟**算，只在返回时换算成秒。

    ⚠️ 时间基准：DeepSeek 按**北京时间**计费，所以这里固定用 UTC+8，
      不用 time.localtime()（本机时区要不是 +8，判出来的峰谷就是错的）。
    """
    p = price or load_price()
    now = time.time() if now is None else float(now)
    lt = time.gmtime(now + 8 * 3600)              # 北京时间的"墙上时钟"
    date_s = time.strftime("%Y-%m-%d", lt)
    minute = lt.tm_hour * 60 + lt.tm_min          # 当天的分钟数 0~1439

    holidays = set(p.get("holidays") or [])
    ranges = []
    for r in p.get("peak_ranges", [["09:00", "12:00"], ["14:00", "18:00"]]):
        try:
            a, b = _hm(r[0]), _hm(r[1])
        except Exception:  # noqa: BLE001
            continue
        if b > a:
            ranges.append((a, b))
    ranges.sort()

    def is_workday(t) -> bool:
        return t.tm_wday < 5 and time.strftime("%Y-%m-%d", t) not in holidays

    in_peak = is_workday(lt) and any(a <= minute < b for a, b in ranges)

    # ——— 距下一次"峰↔谷"变化还有多少分钟 ———
    left_min = None
    if in_peak:
        for a, b in ranges:                       # 正在峰里 → 等这一段结束
            if a <= minute < b:
                left_min = b - minute
                break
    else:
        if is_workday(lt):
            for a, b in ranges:                   # 今天稍后还有峰段吗
                if minute < a:
                    left_min = a - minute
                    break
        if left_min is None:                      # 都不行：等下一个工作日的峰段
            first = ranges[0][0] if ranges else 9 * 60
            for d in range(1, 8):
                t = time.gmtime(now + 8 * 3600 + d * 86400)
                if is_workday(t):
                    left_min = d * 24 * 60 + first - minute
                    break
        if left_min is None:
            left_min = 24 * 60 - minute

    return bool(in_peak), max(0, int(left_min * 60))

def price_for(model: str | None, price: dict | None = None) -> tuple[str, dict]:
    """挑出这个模型该用哪一档价目"""
    p = price or load_price()
    table = p.get("table") or {}
    want = (model or "").strip().lower()
    if not want or p.get("model") not in (None, "", "auto"):
        want = str(p.get("model", "")).lower()
    if want in table:
        return want, table[want]
    # 前缀匹配：deepseek-v4-flash-vision-exp 之类也算 flash
    for k in table:
        if want and (k in want or want in k):
            return k, table[k]
    fb = p.get("fallback_model", "deepseek-flash")
    return fb, table.get(fb, {"cache_hit": [0.02, 0.04], "cache_miss": [1.0, 2.0], "output": [4.0, 8.0]})


def _pick(v, peak):
    """[空闲, 高峰] -> 取对应那一档；也接受单个数字"""
    if isinstance(v, (list, tuple)):
        return float(v[1] if peak else v[0])
    return float(v)


def cost_of(usage: dict, peak: bool, model: str | None = None) -> float:
    """把一条 usage 按模型价目表换算成元"""
    _, t = price_for(model)
    hit = float(usage.get("cacheReadTokens") or 0)
    miss = float(usage.get("inputTokens") or usage.get("uncachedInputTokens") or 0)
    out = float(usage.get("outputTokens") or 0)
    return (hit / 1e6) * _pick(t["cache_hit"], peak) \
        + (miss / 1e6) * _pick(t["cache_miss"], peak) \
        + (out / 1e6) * _pick(t["output"], peak)


def tokens_of(usage: dict) -> int:
    return int((usage.get("inputTokens") or 0)
               + (usage.get("outputTokens") or 0)
               + (usage.get("cacheReadTokens") or 0)
               + (usage.get("cacheWriteTokens") or 0))


# ----------------------------------------------------------------------
#  读会话日志
# ----------------------------------------------------------------------
_file_cache: dict[str, tuple] = {}
_scan_lock = threading.Lock()
_scan_cache: dict | None = None
_scan_at = 0.0
SCAN_TTL = 15.0   # 秒；HUD 点得再快也不会反复解析


def _read_session(path: str) -> list:
    """从一个会话日志里抽出 [(毫秒时间戳, usage)]。按 (mtime,size) 缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    hit = _file_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    recs: list = []
    try:
        from compression import zstd
        with open(path, "rb") as f:
            raw = zstd.decompress(f.read())
        for ln in raw.decode("utf-8", "replace").splitlines():
            if '"usage"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            data = d.get("data") or {}
            u = data.get("usage")
            t = d.get("time")
            if isinstance(u, dict) and t:
                recs.append((int(t), u))
    except Exception:  # noqa: BLE001
        pass
    _file_cache[path] = (key, recs)
    return recs


_model_cache = None
_model_lock = threading.Lock()


def detect_model(force: bool = False) -> str:
    """从会话日志里找出实际用的模型名（request/header 里记着）。
    否则价目表对不上 —— flash 和 pro 的价差近 10 倍。"""
    global _model_cache
    with _model_lock:
        if _model_cache is not None and not force:
            return _model_cache
    best = (0, None)
    fs = glob.glob(SESSION_GLOB)
    fs.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)
    for path in fs[:3]:
        try:
            from compression import zstd
            raw = zstd.decompress(open(path, "rb").read()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for ln in raw.splitlines():
            if '"model"' not in ln or '"request/header"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            t = int(d.get("time") or 0)
            hdr = ((d.get("data") or {}).get("header") or {})
            m = ((hdr.get("config") or {}).get("model") or "").strip()
            if m and t >= best[0]:
                best = (t, m)
    with _model_lock:
        _model_cache = best[1] or ""
    return _model_cache


def _fallback_projcache() -> tuple[dict | None, int]:
    """会话日志读不到时的兜底：用 projcache 的 last 和 totals"""
    last_u, last_t = None, 0
    for f in glob.glob(PROJCACHE_GLOB):
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                d = json.load(fh)
            val = ((d.get("record") or {}).get("rows") or {}).get("tokenUsage", {}).get("val") or {}
            la = val.get("last") or {}
            b = la.get("buckets") or {}
            if b and la.get("turn"):
                # 没有时间戳，只能拿最后写的那个文件当"最近"
                if os.path.getmtime(f) >= last_t:
                    last_t = os.path.getmtime(f)
                    last_u = b
        except Exception:  # noqa: BLE001
            continue
    return last_u, int(last_t)


def scan(max_age: float = SCAN_TTL) -> dict:
    """汇总 DSH 的真实用量。带缓存，避免每次点 HUD 都重解析十几 MB。"""
    global _scan_cache, _scan_at
    now = time.time()
    with _scan_lock:
        if _scan_cache is not None and now - _scan_at < max_age:
            return _scan_cache

    model = detect_model()
    today = time.strftime("%Y-%m-%d")
    today_tokens = 0
    today_cost = 0.0
    last_rec = None          # (ms, usage)
    span_start = None
    n_files = 0

    for path in glob.glob(SESSION_GLOB):
        recs = _read_session(path)
        if not recs:
            continue
        n_files += 1
        for ms, u in recs:
            if last_rec is None or ms > last_rec[0]:
                last_rec = (ms, u)
            if span_start is None or ms < span_start:
                span_start = ms
            d = time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
            if d == today:
                peak, _ = peak_state(ms / 1000)
                today_tokens += tokens_of(u)
                today_cost += cost_of(u, peak, model)

    if last_rec is None:
        u, mt = _fallback_projcache()
        if u:
            last_rec = (int(mt * 1000), u)

    # 最近一轮的价按那条记录当时的时间算
    turn = None
    if last_rec:
        ms, u = last_rec
        peak, _ = peak_state(ms / 1000)
        turn = {
            "ok": True,
            "model": model,
            "seq": int(ms // 1000),
            "turn": time.strftime("%H:%M:%S", time.localtime(ms / 1000)),
            "amount": round(cost_of(u, peak), 4),
            "tokens": tokens_of(u),
            "ts": ms,
        }

    price = load_price()
    out = {
        "today_date": today,
        "today_tokens": today_tokens,
        "today_amount": round(today_cost, 4),
        "turn": turn,
        "sessions_read": n_files,
        "model": model,
        "price": price,
    }
    with _scan_lock:
        _scan_cache = out
        _scan_at = time.time()
    return out


# ----------------------------------------------------------------------
#  余额（可选）
# ----------------------------------------------------------------------
_bal_cache: dict | None = None
_bal_at = 0.0


def balance(force: bool = False) -> dict:
    """查 DeepSeek 余额。没配 API key 就老老实实说没有，不编数字。"""
    global _bal_cache, _bal_at
    p = load_price()
    key = (p.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "code": "NO_KEY",
                "error": "没配 API key。想在 HUD 上看余额，就在 记忆/计价.json 里填 api_key"}
    now = time.time()
    if _bal_cache is not None and not force and now - _bal_at < 120:
        return _bal_cache
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        infos = d.get("balance_infos") or []
        info = infos[0] if infos else {}
        out = {"ok": True,
               "totalBalance": float(info.get("total_balance") or 0),
               "currency": info.get("currency") or p.get("currency", "CNY"),
               "version": "deepseek-api"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "code": "NET", "error": f"余额没查到：{type(e).__name__}"}
    _bal_cache, _bal_at = out, now
    return out


# ----------------------------------------------------------------------
#  HUD 载荷（pet.js 认的格式）
# ----------------------------------------------------------------------
def hud_payload(force: bool = False) -> dict:
    s = scan(0 if force else SCAN_TTL)
    b = balance(force)
    peak, left = peak_state()
    p = s["price"]
    return {
        "ok": True,
        "version": "dsh",
        "isPeak": peak,
        "peakNextChangeAt": int(time.time()) + left,
        "balance": b,
        "today": {"date": s["today_date"],
                  "amount": s["today_amount"],
                  "tokens": s["today_tokens"]},
        "turn": s["turn"] or {"ok": False},
        "priceNote": p.get("note", ""),
        "model": s.get("model") or "",
        "stats": {"sessions_read": s["sessions_read"], "model": s.get("model") or ""},
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 66)
    print("  账本自检")
    print("=" * 66)
    print(f"  DSH 目录 : {DSH_DIR}  ({'存在' if os.path.isdir(DSH_DIR) else '不存在'})")
    print(f"  计价文件 : {PRICE_FILE}")
    print(f"  会话日志 : {len(glob.glob(SESSION_GLOB))} 个")
    print(f"  计价     : {json.dumps(load_price(), ensure_ascii=False)}")
    t0 = time.time()
    h = hud_payload(force=True)
    print(f"\n  扫描耗时 {time.time()-t0:.2f}s，读了 {h['stats']['sessions_read']} 个会话")
    print(f"  峰谷     : {'峰' if h['isPeak'] else '谷'}  距切换 {h['peakNextChangeAt']-int(time.time())}s")
    print(f"  余额     : {json.dumps(h['balance'], ensure_ascii=False)[:110]}")
    print(f"  今日已用 : ¥{h['today']['amount']}  ({h['today']['tokens']:,} tokens)  {h['today']['date']}")
    print(f"  本轮消耗 : {json.dumps(h['turn'], ensure_ascii=False)}")
