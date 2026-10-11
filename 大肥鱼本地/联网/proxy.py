#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地大模型 · 通用联网插件
=========================
在 8081 起一个 OpenAI 兼容代理：

    客户端 ──► 8095/proxy? 不，是 ──► 127.0.0.1:8081 ──► 判断要不要联网
                                                      ──► 搜（必应/百度）
                                                      ──► 把资料塞进上下文
                                                      ──► 转给 127.0.0.1:8080
                                                      ──► 把回答流式吐回客户端

为什么做成代理层而不是改 llama.cpp：
  · 任何客户端（浏览器界面 / Cherry Studio / ChatBox / 你自己写的脚本）只要把
    地址从 8080 改成 8081 就自动获得联网能力，其余一律不变。
  · 任何模型都能用 —— 代理只看 HTTP，不关心背后是 DeepSeek 还是 Qwen 还是将来新加的。
  · 不侵入 llama.cpp，升级运行时不会把联网功能搞丢。

用法：
    python proxy.py                 # 前台跑
    python proxy.py --port 8081
客户端接口地址填： http://127.0.0.1:8081/v1
调试页： http://127.0.0.1:8081/
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "联网配置.json")
HOST = "127.0.0.1"
PORT = int(os.environ.get("NET_PORT", "8081"))
UPSTREAM = os.environ.get("NET_UPSTREAM", "http://127.0.0.1:8080")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_CFG = {
    "enable": True,             # 总开关
    "always": False,            # True=每句都搜（慢但省心）
    "max_results": 5,           # 送进上下文的条数
    "timeout": 15,
    "engine": "bing",           # bing / baidu
    "show_note": True,          # 在回答前加一行"已联网检索"
}

# 命中这些词就认为需要联网（配合"是不是在问实时/事实性问题"）
TRIGGERS = [
    "搜一下", "搜索", "查一下", "查查", "帮我查", "帮我搜", "上网", "联网",
    "最新", "新闻", "今天", "昨天", "现在", "当前", "实时", "最近",
    "谁是", "是什么", "多少钱", "价格", "天气", "股价", "汇率", "几点",
    "什么时候", "哪一年", "多少年", "成立于", "发布了", "更新", "版本",
]

_cfg_lock = threading.Lock()
_stats = {"search_total": 0, "hit_total": 0, "last_query": "", "last_ms": 0,
          "last_count": 0, "last_error": ""}


def load_cfg() -> dict:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(CONFIG_FILE, "r", encoding=enc) as f:
                d = json.load(f)
            out = dict(DEFAULT_CFG)
            out.update(d)
            return out
        except FileNotFoundError:
            break
        except Exception:  # noqa: BLE001
            continue
    return dict(DEFAULT_CFG)


def save_cfg(c: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


CFG = load_cfg()


# ----------------------------------------------------------------------
#  搜索
# ----------------------------------------------------------------------
def _strip_tags(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    # 先剥标签再解实体：反过来的话 &#60;script&#62; 会被解成真标签漏过去
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fetch(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def search_bing(q: str, n: int, timeout: int) -> list[dict]:
    html = _fetch("https://cn.bing.com/search?q=" + urllib.parse.quote(q)
                  + "&setlang=zh-CN&ensearch=0", timeout)
    out = []
    # Bing 的结果块以 class="b_algo" 开头
    for block in re.split(r'class="b_algo"', html)[1:]:
        block = block[:6000]
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not m:
            continue
        url, title = m.group(1), _strip_tags(m.group(2))
        sm = re.search(r"<p[^>]*>([\s\S]*?)</p>", block)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def search_baidu(q: str, n: int, timeout: int) -> list[dict]:
    html = _fetch("https://www.baidu.com/s?wd=" + urllib.parse.quote(q)
                  + "&rn=" + str(n + 5), timeout)
    out = []
    # 百度：标题在 h3 里，摘要常见于 content-right_* 的 span
    for m in re.finditer(
            r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>',
            html):
        url, title = m.group(1), _strip_tags(m.group(2))
        tail = html[m.end():m.end() + 3000]
        sm = re.search(r'class="[^"]*content-right[^"]*"[^>]*>([\s\S]*?)</span>', tail)
        if not sm:
            sm = re.search(r"<span[^>]*>([\s\S]{20,300}?)</span>", tail)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        if title:
            out.append({"title": title, "url": url,
                        "snippet": snippet, "baidu_redirect": url.startswith("/link")})
        if len(out) >= n:
            break
    return out


def web_search(q: str) -> list[dict]:
    """联网检索，主引擎失败自动换备用"""
    q = (q or "").strip()
    if not q:
        return []
    n = int(CFG.get("max_results", 5))
    to = int(CFG.get("timeout", 15))
    engines = [CFG.get("engine", "bing")] + [e for e in ("bing", "baidu")
                                             if e != CFG.get("engine", "bing")]
    last_err = ""
    t0 = time.time()
    with _cfg_lock:
        _stats["search_total"] += 1
        _stats["last_query"] = q
    for eng in engines:
        try:
            res = search_bing(q, n, to) if eng == "bing" else search_baidu(q, n, to)
            if res:
                with _cfg_lock:
                    _stats.update(last_ms=int((time.time() - t0) * 1000),
                                  last_count=len(res), last_error="", hit_total=_stats["hit_total"] + 1)
                return res
            last_err = f"{eng}: 没解析出结果"
        except Exception as e:  # noqa: BLE001
            last_err = f"{eng}: {type(e).__name__}: {str(e)[:60]}"
    with _cfg_lock:
        _stats.update(last_ms=int((time.time() - t0) * 1000), last_count=0,
                      last_error=last_err)
    return []


def need_search(text: str) -> bool:
    if not CFG.get("enable", True):
        return False
    if CFG.get("always", False):
        return True
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith(("@搜索", "/搜索", "#搜索", "@web", "/web")):
        return True
    low = t.lower()
    if any(k in t for k in TRIGGERS):
        return True
    # 带问号且不太长的问句，多半在问事实
    if ("?" in t or "？" in t) and len(t) <= 60:
        return True
    return False


def build_context_block(q: str, results: list[dict]) -> str:
    lines = [f"【联网检索结果】查询：{q}（{time.strftime('%Y-%m-%d %H:%M')}）"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   来源：{r['url']}")
    lines.append("")
    lines.append("要求：优先依据以上资料回答；资料没提到就说没查到，绝不编造。"
                 "回答里不用列出网址，除非用户明确要。")
    return "\n".join(lines)


# 检索式清洗：疑问句和"只回答X"这类指令会严重拉低搜索结果质量。
# 实测同一个问题，"…是什么时候发布的？只回答日期。" 只有 1/4 条命中，
# 清洗成"…发布时间"后 4/4 条全中。所以送进搜索引擎的必须是关键词形态。
_Q_SUBS = [
    (r"(是什么时候|什么时候|啥时候|何时)\s*(发布|成立|上线|推出|开始|诞生)", r"\2时间"),
    (r"(发布|成立|上线|推出)\s*的", r"\1时间"),
    (r"(是什么时候|什么时候|啥时候|何时)", "时间"),
    (r"(多少钱|什么价格|价格是)", "价格"),
    (r"(是谁|谁是|是什么|什么是|是啥)", ""),
    (r"(是多少|多少个|多少)", ""),
    (r"(怎么样|如何|怎样)", ""),
    (r"(为什么|为啥)", "原因"),
]
_Q_DROP = ["只回答", "只给", "只输出", "一句话", "简单说", "简述", "请问", "帮我",
           "搜一下", "搜索", "查一下", "查查", "谢谢", "麻烦", "给我", "告诉我",
           "告诉我", "回答", "回复", "用中文", "简洁"]


def clean_query(text: str) -> str:
    """把口语问题压成适合搜索引擎的关键词"""
    q = strip_trigger(text)
    for pat, rep in _Q_SUBS:
        q = re.sub(pat, rep, q)
    for w in _Q_DROP:
        q = q.replace(w, "")
    # 去掉句末那一小段指令（"？只回答日期。"）
    q = re.sub(r"[，,。.！!？?、；;]+\s*[^，,。.！!？?、；;]{0,14}$", "", q)
    q = re.sub(r"[，,。.！!？?、；;]+", " ", q)
    q = re.sub(r"(时间|日期|时候)[的\s]*(日期|时间|时候)", r"\1", q)  # 收敛同义重复
    q = re.sub(r"^[的的了]+\s*", "", q)      # 去掉清洗后残留的"的"
    q = re.sub(r"\s+", " ", q).strip()
    return q or strip_trigger(text)
def strip_trigger(text: str) -> str:
    return re.sub(r"^@?(搜索|web|/搜索|#搜索)\s*[:：]?\s*", "", text or "").strip()


# ----------------------------------------------------------------------
#  HTTP
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/v1/" in (self.path or ""):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 通用 ----------
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:  # noqa: BLE001
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = (self.path or "/").split("?")[0]
        qs = urllib.parse.parse_qs((self.path or "").split("?")[1] if "?" in (self.path or "") else "")

        if path in ("/", "/index.html"):
            return self._json({"ok": True, "service": "本地模型联网插件",
                               "upstream": UPSTREAM,
                               "用法": "把客户端接口地址填成 http://127.0.0.1:%d/v1" % PORT,
                               "触发": "句子里含「搜索/最新/谁是/是什么…」或带问号；也可用 @搜索 开头强制",
                               "配置": CFG, "统计": _stats})

        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            if not q:
                return self._json({"ok": False, "error": "缺少 q"}, 400)
            res = web_search(q)
            return self._json({"ok": bool(res), "query": q, "count": len(res),
                               "results": res, "stats": _stats})

        if path == "/api/state":
            return self._json({"ok": True, "config": CFG, "stats": _stats,
                               "upstream": UPSTREAM})

        if path in ("/v1/models", "/models"):
            try:
                with urllib.request.urlopen(UPSTREAM + "/v1/models", timeout=10) as r:
                    b = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 502)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        path = (self.path or "").split("?")[0]
        if path == "/api/config":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                return self._json({"ok": False, "error": "bad json"}, 400)
            with _cfg_lock:
                for k in DEFAULT_CFG:
                    if k in body:
                        CFG[k] = body[k]
            save_cfg(CFG)
            return self._json({"ok": True, "config": CFG})

        if path != "/v1/chat/completions":
            return self._json({"ok": False, "error": "not found"}, 404)

        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)

        # 取出最后一条用户消息，判断要不要联网
        msgs = body.get("messages") or []
        last_user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                last_user = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                break

        results, searched = [], False
        if need_search(last_user):
            q = clean_query(last_user)
            results = web_search(q)
            if results:
                searched = True
                block = build_context_block(q, results)
                # 追加到【用户消息】后面，而不是塞进 system。
                # 实测：放 system 里模型经常当没看见（问 R1 发布时间，资料写着
                # 2025-01-20，它却答 2026 年）；跟着问题走才认得。
                placed = False
                for mm in reversed(msgs):
                    if mm.get("role") == "user":
                        c = mm.get("content")
                        if isinstance(c, str):
                            mm["content"] = c + "\n\n" + block
                        elif isinstance(c, list):      # 多模态格式
                            c.append({"type": "text", "text": "\n\n" + block})
                        placed = True
                        break
                if not placed:
                    msgs.append({"role": "user", "content": block})
                body["messages"] = msgs
            else:
                sys.stderr.write(f"[联网] 没搜到：{q}  {_stats.get('last_error')}\n")

        self._forward(body, searched, results)

    # ---------- 转发 ----------
    def _forward(self, body: dict, searched: bool, results: list[dict]):
        try:
            req = urllib.request.Request(
                UPSTREAM + "/v1/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            upstream = urllib.request.urlopen(req, timeout=600)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"连不上上游 {UPSTREAM}: {e}"}, 502)

        is_stream = bool(body.get("stream"))
        try:
            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                note_sent = False
                for raw in upstream:
                    line = raw.decode("utf-8", "replace")
                    if (searched and CFG.get("show_note") and not note_sent
                            and line.startswith("data:") and '"delta"' in line):
                        note = f"（已联网检索到 {len(results)} 条资料）\n"
                        d = {"choices": [{"delta": {"content": note}, "index": 0}]}
                        self.wfile.write(("data: " + json.dumps(d, ensure_ascii=False)
                                          + "\n\n").encode("utf-8"))
                        note_sent = True
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            else:
                data = upstream.read()
                try:
                    obj = json.loads(data.decode("utf-8", "replace"))
                    if searched and CFG.get("show_note") and isinstance(obj, dict):
                        ch = (obj.get("choices") or [{}])[0].get("message")
                        if isinstance(ch, dict) and isinstance(ch.get("content"), str):
                            ch["content"] = (f"（已联网检索到 {len(results)} 条资料）\n"
                                             + ch["content"])
                            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                upstream.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    print("=" * 62)
    print("  本地模型 · 联网插件")
    print("=" * 62)
    print(f"  监听      : http://{HOST}:{PORT}")
    print(f"  上游模型  : {UPSTREAM}")
    print(f"  开关      : {'开' if CFG.get('enable') else '关'}"
          f"   每句都搜: {'是' if CFG.get('always') else '否（按关键词判断）'}")
    print(f"  搜索引擎  : {CFG.get('engine')}  取 {CFG.get('max_results')} 条")
    print("-" * 62)
    print(f"  客户端接口地址填： http://{HOST}:{PORT}/v1")
    print(f"  调试            ： http://{HOST}:{PORT}/api/search?q=测试")
    print("=" * 62)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
