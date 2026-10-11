#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鱼娘 · 独立桌宠后端
======================
把原本依赖 DSH 的插件，改成靠本地大模型驱动。

核心思路：pet.js 从来不自己调用任何 LLM，它只是把「AI 在干什么」抽象成一条
SSE 事件流（turn-start / delta / tool-call / turn-end …）。所以我们只要重写这条
事件流的来源——从「DSH 会话」换成「本地 llama-server」——她就会像跟着 DSH 一样
跟着本地模型动：推理时摆思考姿态、逐字输出时嘴跟着动、出错时黑脸、收工伸懒腰。

对外接口（和原插件保持一致，pet.js 无需改动）：
  GET  /                       页面（透明背景，供 Electron 无边框窗口加载）
  GET  /dsh-pet/pet.js         桌宠本体（前置 __DSH_PET_BOOT__）
  GET  /dsh-pet/vendor/*        pixi / cubism 运行时
  GET  /dsh-pet/model/*         模型资产（中文文件名要 URL 解码）
  GET  /dsh-pet/events          SSE 事件流
  POST /dsh-pet/say             用户说话 → 本地模型 → 流式回话
  POST /dsh-pet/control         表演指令直通
  GET  /dsh-pet/hud             右上角 HUD 数据（本机版给轻量数据）
  GET  /api/config              健康检查（外壳用它探后端在不在）

  注：换模型的界面现在在她自己的菜单里 —— 点她脚下的「⋯」，
  最右边多了一个「大脑」页签。原来左上角那条浮动控制条已删除。

【显存策略：平时不占显卡，要说话才拉起来】

  她的脑子不再是"开机就常驻"。默认（配置.json 的 manage_llm = true）：
    · 要说话（你发消息 / 她主动开口）时，才把 llama-server 拉起来；
    · 一轮说完，安静超过 idle_unload 秒（默认 60），就把 llama-server 停掉，
      显存完全还给系统 —— 不聊天的时候显卡占用是 0。
      连"你从 大肥鱼.bat 菜单 [1] 手动起的那个"也一起接管（她不说话就卸掉）。
    · 代价：每次开口前要多等几秒加载模型（本机实测 4~6 秒，模型文件在
      系统缓存里的时候更快）。想让它常驻显存（例如还要拿 8080 给
      Cherry Studio 用），把 manage_llm 改成 false，或在「大脑」面板里点一下。

【出话刹车：不让她复读】

  以前她自己开口时会把同一段话循环吐到 max_tokens 上限（512 token，
  2.67 token/s ≈ 3 分钟），显卡全程满载。现在三道刹车，见下面
  「出话刹车」那一节：复读检测（客户端掐断连接，llama.cpp 收到断开
  就取消生成）、字数/秒数硬上限、以及把复读部分裁掉只留第一遍。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))          # …\大肥鱼本地\鲸鱼娘
BASE = os.path.dirname(HERE)                               # …\大肥鱼本地
WEB = os.path.join(HERE, "网页")
MEM = os.path.join(HERE, "记忆")
MODELS_DIR = os.path.join(BASE, "models")
RUNTIME = os.path.join(BASE, "运行时", "llama-server.exe")
CONFIG_FILE = os.path.join(MEM, "配置.json")
PERSONA_FILE = os.path.join(MEM, "人设.txt")
HISTORY_FILE = os.path.join(MEM, "对话.json")

HOST, PORT = "127.0.0.1", int(os.environ.get("PET_PORT", "8095"))
LLM_BASE = os.environ.get("PET_LLM", "http://127.0.0.1:8080")
LLM_CTX = 16384
LLM_PARALLEL = 1
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)

DEFAULT_PERSONA = (
    "你是住在主人电脑桌面上的鲸鱼娘：圆脸、蓝长发、头上有鳍耳、身后一条大尾鳍，穿女仆装。"
    "只说中文，一次不超过 40 字，像桌宠那样一句一句冒话，口语、俏皮。"
    "性格傲娇但很甜，聪明又懒。最爱吃米饭，尤其铁盆装的。"
    "谁说你好胖你就跟谁急，坚称自己只是尾鳍大。"
)

DEFAULT_CFG = {
    "model": None,
    "proactive": True,
    "proactive_gap": 240,      # 秒；安静这么久她才主动开口
    "height": 180,             # 桌宠基准高度（pet.js 的 config.height）
    "show_reasoning": False,
    "persona_file": True,
    # ---------- 显存 / 显卡占用 ----------
    # True（默认）· 她管大脑：要说话才拉起模型，安静 idle_unload 秒后卸载，
    #              平时不占显卡（连 大肥鱼.bat 菜单 [1] 起的那个也一起接管）
    # "auto"      · 只卸载她自己按需拉起来的那个
    # False       · 不管，模型常驻（还要拿 8080 给 Cherry Studio 之类用时改成它）
    "manage_llm": True,
    "idle_unload": 60,         # 秒；没在说话超过这么久就卸载模型、释放显存
    "llm_ctx": 8192,           # 上下文长度；桌宠只留几句短话，8192 足够，还省显存
    # ---------- 出话控制（治复读） ----------
    # 本机 Vulkan 后端实测只有 ~2.3 token/s（见 说明.txt），token 数必须收着给，
    # 否则一句话就能让显卡满载好几分钟。
    "max_tokens": 200,             # 你跟她说话时，最多生成多少 token
    "max_tokens_proactive": 96,    # 她主动开口时更短 —— 她本来就只说一句话
    "max_reply_chars": 240,        # 一轮正文最多留多少字，超了立刻掐断
    "max_reply_seconds": 35.0,     # 一轮最多生成多少秒，超了立刻掐断
    "history_chars": 3000,         # 送进模型的历史对话字数上限
}

# 主动说话时塞给模型的"用户消息"。
# ⚠️ 必须留下一条 user 记录（见 run_turn）—— 以前只往 history 里塞 assistant，
# 历史变成清一色 assistant，模型就照着往下复读同一段话，越滚越长。
PROACTIVE_HINTS = [
    "（主人有一会儿没理你了。你自己找句话说说吧，25 字以内，别问「在吗」，说点具体的。）",
    "（安静太久了。说说你正在干什么，或者突然想起一件小事跟他讲。）",
    "（主动搭话。可以带一点点撒娇或者抱怨他不理你，别太用力。）",
]

# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
_cfg_lock = threading.Lock()


def load_json(path, default):
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                d = json.load(f)
        except FileNotFoundError:
            return default
        except Exception:  # noqa: BLE001
            continue
        if isinstance(default, dict):
            out = dict(default)
            out.update(d)
            return out
        return d
    return default


def save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _safe_err(msg: str) -> None:
    """pythonw 下 stderr 是 None，直接写会炸"""
    try:
        if sys.stderr:
            sys.stderr.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


CFG = load_json(CONFIG_FILE, DEFAULT_CFG)


def persona() -> str:
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8-sig") as f:
            t = f.read().strip()
        return t or DEFAULT_PERSONA
    except Exception:  # noqa: BLE001
        return DEFAULT_PERSONA


# ----------------------------------------------------------------------
# SSE 广播
# ----------------------------------------------------------------------
_clients: set = set()
_clients_lock = threading.Lock()


def broadcast(obj: dict) -> None:
    obj.setdefault("sessionId", "local")
    frame = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(frame)
            except Exception:  # noqa: BLE001
                dead.append(q)
        for q in dead:
            _clients.discard(q)


# ----------------------------------------------------------------------
# 本地模型管理
# ----------------------------------------------------------------------
def scan_models() -> list[dict]:
    if not os.path.isdir(MODELS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(MODELS_DIR)):
        if not fn.lower().endswith(".gguf"):
            continue
        m = SHARD_RE.search(fn)
        if m and m.group(1) != "00001":
            continue
        p = os.path.join(MODELS_DIR, fn)
        size = os.path.getsize(p)
        if m:
            for k in range(2, int(m.group(2)) + 1):
                o = os.path.join(MODELS_DIR, SHARD_RE.sub(f"-{k:05d}-of-{m.group(2)}.gguf", fn))
                if os.path.exists(o):
                    size += os.path.getsize(o)
        out.append({"file": fn, "rel": os.path.join("models", fn), "size": size,
                    "label": SHARD_RE.sub("", re.sub(r"\.gguf$", "", fn, flags=re.I))})
    return out


_probe_lock = threading.Lock()
_probe_at = 0.0
_probe_val: tuple[bool, str | None] = (False, None)


def llm_probe(max_age: float = 1.0) -> tuple[bool, str | None]:
    """(是否在线, 当前模型名)，带 1 秒缓存。

    ⚠️ 本机有个坑：8080 没人监听时，连过去要等整整 2 秒才 refusal
    （安全软件会拦 loopback 连接）。一次请求里 llm_alive + llm_current
    连问两遍就是 4 秒 —— 桌宠面板、控制台状态页全都被拖慢。
    所以：先用 tasklist 看进程在不在（几十毫秒，够快），不在就直接报离线；
    真在跑才去问 HTTP，而且 1 秒内复用同一个结果。
    """
    global _probe_at, _probe_val
    with _probe_lock:
        if time.time() - _probe_at < max_age:
            return _probe_val
    if not proc_running("llama-server.exe"):
        val: tuple[bool, str | None] = (False, None)
    else:
        val = (False, None)
        try:
            with urllib.request.urlopen(LLM_BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    val = (True, None)
        except Exception:  # noqa: BLE001
            pass
        if val[0]:
            try:
                with urllib.request.urlopen(LLM_BASE + "/v1/models", timeout=3) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                mid = (d.get("data") or [{}])[0].get("id") or ""
                val = (True, os.path.basename(mid) if mid else None)
            except Exception:  # noqa: BLE001
                pass
    with _probe_lock:
        _probe_at = time.time()
        _probe_val = val
    return val


def llm_probe_invalidate() -> None:
    """刚起过/停过服务，缓存的探测结果不能再用"""
    global _probe_at
    with _probe_lock:
        _probe_at = 0.0


def llm_alive() -> bool:
    return llm_probe()[0]


def llm_current() -> str | None:
    return llm_probe()[1]


def proc_running(name: str) -> bool:
    try:
        r = subprocess.run(["tasklist", "/fi", f"imagename eq {name}"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        return name.lower() in (r.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def stop_llm() -> None:
    subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    for _ in range(20):
        if not proc_running("llama-server.exe"):
            break
        time.sleep(0.3)
    llm_probe_invalidate()


# ----------------------------------------------------------------------
#  显存生命周期：要说话才拉起，安静就把显存还回去
# ----------------------------------------------------------------------
# 老版本的毛病：模型一旦起来就常驻，不聊天时显卡也一直被占着。
# 现在改成"按需拉起 + 空闲卸载"：
#   要开口了 → ensure_llm() 拉起（没起才起，起了同一个就直接用）
#   说完了   → 空闲 idle_unload 秒后 release_llm() 停掉 llama-server
# 卸载前还要确认"确实没人用它"（槽没在生成、也没有别的客户端在用），
# 免得正跟 Cherry Studio 这类外部客户端聊天时把模型抽走。
_llm_lock = threading.Lock()       # 串行化"拉起/卸载"，避免重复起服务
_llm_last_use = time.time()        # 最后一次用到模型的时间
_llm_tokens_seen = 0.0             # 上次看到的累计生成 token（用来发现"别人也在用"）
_llm_unloaded_at = 0.0             # 最近一次卸载时间（界面用）
_llm_owned = False                 # 这个 llama-server 是不是我们拉起来的


def llm_touch() -> None:
    """记下"刚用过模型"，把卸载倒计时往后推"""
    global _llm_last_use
    _llm_last_use = time.time()


def manage_mode() -> str:
    """manage_llm 的取值：

      True / "always"（默认）· 她全权管显存：要说话才拉起，空闲就卸载，
                               连别人（大肥鱼.bat 菜单 [1]）起的那个也一并接管
      "auto"                · 只卸载"她自己按需拉起来的那一个"，别人起的不动
      False / "off"         · 完全不管，模型常驻（还要拿 8080 给别的软件用时用这个）
    """
    v = CFG.get("manage_llm", True)
    if v is False or v is None or v == "off" or v == "false" or v == 0:
        return "off"
    if v == "auto":
        return "auto"
    return "always"


def idle_unload_seconds() -> float:
    try:
        return max(0.0, float(CFG.get("idle_unload", 60) or 0))
    except Exception:  # noqa: BLE001
        return 60.0


def llm_ctx_size() -> int:
    try:
        n = int(CFG.get("llm_ctx") or LLM_CTX)
    except Exception:  # noqa: BLE001
        n = LLM_CTX
    return max(2048, min(n, 32768))


def llm_generated_tokens() -> float | None:
    """读 /metrics 的累计生成 token；读不到就返回 None（不拿它做判断）"""
    try:
        with urllib.request.urlopen(LLM_BASE + "/metrics", timeout=3) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    total, found = 0.0, False
    for line in txt.splitlines():
        if not line or line.startswith("#") or "tokens_predicted" not in line:
            continue
        head, _, tail = line.rpartition(" ")
        if not head:
            continue
        try:
            total += float(tail)
            found = True
        except ValueError:
            continue
    return total if found else None


def llm_in_use() -> bool:
    """正在生成 / 刚有别的客户端用过 → True（这时不要卸载）"""
    global _llm_tokens_seen
    busy = False
    try:
        with urllib.request.urlopen(LLM_BASE + "/slots", timeout=3) as r:
            slots = json.loads(r.read().decode("utf-8", "replace"))
        for s in slots or []:
            if isinstance(s, dict) and (s.get("is_processing") or s.get("state") == 1):
                busy = True
                break
    except Exception:  # noqa: BLE001
        pass

    now = llm_generated_tokens()
    foreign = False
    if now is not None:
        if now > _llm_tokens_seen + 0.5:
            foreign = True
        _llm_tokens_seen = now
    return busy or foreign


def start_llm(model_rel: str, wait: int = 240) -> bool:
    if not os.path.exists(RUNTIME):
        return False
    llm_probe_invalidate()
    try:
        logf = open(os.path.join(MEM, "大脑.log"), "ab", buffering=0)
        subprocess.Popen(
            [RUNTIME, "-m", model_rel, "-ngl", "99", "-c", str(llm_ctx_size()),
             "--parallel", str(LLM_PARALLEL),
             "--host", "127.0.0.1", "--port", "8080", "-t", "8", "--jinja"],
            cwd=BASE,                     # 相对路径 + 根目录工作区，绕开中文绝对路径问题
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except Exception:  # noqa: BLE001
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        llm_probe_invalidate()
        if llm_alive():
            return True
        time.sleep(1)
    return False


def _pick_model(want: str | None) -> dict | None:
    """挑模型：指定的 → 配置里的 → 优先纯对话模型 → 第一个"""
    models = scan_models()
    if not models:
        return None
    want = want or CFG.get("model")
    if want:
        hit = next((m for m in models if m["file"] == want), None)
        if hit:
            return hit
    # 没指定过就优先纯对话模型（qwen instruct）。推理模型（R1 蒸馏）每句要先
    # 想一大段，既是复读的来源，也让显卡满载时间翻好几倍。
    for m in models:
        if re.search(r"instruct", m["file"], re.I):
            return m
    return models[0]


def ensure_llm(want: str | None = None) -> bool:
    """按需把 8080 上的模型准备好；已经在跑同一个就直接复用"""
    global _llm_owned, _llm_tokens_seen
    hit = _pick_model(want)
    if not hit:
        return False
    with _llm_lock:
        if llm_current() == hit["file"] and llm_alive():
            llm_touch()
            return True
        if proc_running("llama-server.exe"):
            # 进程在但还没就绪：也可能正在加载，别急着杀掉重来（来回加载更费显存）
            t0 = time.time()
            while time.time() - t0 < 30:
                if not proc_running("llama-server.exe"):
                    break
                llm_probe_invalidate()
                if llm_current() == hit["file"] and llm_alive():
                    llm_touch()
                    return True
                time.sleep(1)
            stop_llm()
        if not start_llm(hit["rel"]):
            return False
        _llm_owned = True
        _llm_tokens_seen = llm_generated_tokens() or 0.0
        llm_touch()
    with _cfg_lock:
        CFG["model"] = hit["file"]
    save_json(CONFIG_FILE, CFG)
    return True


def release_llm(reason: str = "空闲") -> bool:
    """卸载模型，把显存还回去；返回是否真的卸了"""
    global _llm_unloaded_at, _llm_owned
    with _llm_lock:
        if not proc_running("llama-server.exe"):
            _llm_owned = False
            return False
        stop_llm()
        _llm_unloaded_at = time.time()
        _llm_owned = False
        llm_probe_invalidate()
        _safe_err(f"[释放显存] 模型已卸载（{reason}）")
    broadcast({"t": "control", "gpu": "released"})
    return True


def llm_reaper_loop() -> None:
    """空闲就把模型卸掉（默认 60 秒）—— 不聊天的时候显卡占用是 0。

    默认 manage_llm=true（"always"）：连别人起的那个也一起接管。
    设成 "auto" 就只卸她自己按需拉起来的那个；设成 false 就完全不管。
    """
    while True:
        time.sleep(5)
        try:
            mode = manage_mode()
            if mode == "off":
                continue
            if not _llm_owned and mode != "always":
                continue
            limit = idle_unload_seconds()
            if limit <= 0:
                continue
            if _busy:
                continue
            if time.time() - _llm_last_use < limit:
                continue
            if not proc_running("llama-server.exe"):
                continue
            # 有人正在用它（哪怕是 Cherry Studio 在聊）就先留着
            if llm_in_use():
                llm_touch()
                continue
            release_llm(f"空闲 {int(limit)} 秒")
        except Exception as e:  # noqa: BLE001
            _safe_err(f"[释放显存] {e}")


def _shutdown_llm() -> None:
    """后端退出时收掉我们拉起来的服务，别留下一个占着显存的后台进程"""
    try:
        if manage_mode() != "off" and _llm_owned and proc_running("llama-server.exe"):
            stop_llm()
    except Exception:  # noqa: BLE001
        pass


def _self_quit(delay: float = 1.0) -> None:
    """回应完请求就收工：先把模型从显存里卸掉，再退出整个后端进程。

    控制台菜单「停止所有服务」会 POST /api/quit 调这里。
    用 os._exit 而不是让处理函数返回 —— 这是 ThreadingHTTPServer，
    处理函数返回并不会让 serve_forever 停下来。模型在上面已经显式卸过了，
    所以跳过 atexit 不会漏掉显存回收。外层（control.py）还会兜一次强杀，
    这里只是让"体面退出"这条路尽量干净。
    """
    time.sleep(max(0.0, delay))
    try:
        release_llm("控制台「停止所有服务」")
    except Exception as e:  # noqa: BLE001
        _safe_err(f"[退出] 卸模型失败：{e}")
    try:
        _shutdown_llm()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


# ----------------------------------------------------------------------
# 与本地模型对话（流式）
# ----------------------------------------------------------------------
_hist_lock = threading.Lock()
_history: list[dict] = []
_turn_lock = threading.Lock()
_turn_seq = 0
_busy = False
_last_active = time.time()


def load_history() -> None:
    global _history
    _history = load_json(HISTORY_FILE, []) or []


def save_history() -> None:
    with _hist_lock:
        save_json(HISTORY_FILE, _history[-80:])


# ----------------------------------------------------------------------
#  出话刹车（治复读）
# ----------------------------------------------------------------------
# 老毛病：她自己开口时把同一段话循环吐到 max_tokens 上限（512 token，
# 本机 2.67 token/s ≈ 3 分钟），这段时间显卡一直满载。三道刹车，任何一道
# 触发就立刻断开 HTTP 连接 —— llama.cpp 发现客户端断开就取消这次生成，
# 显存马上空下来：
#   ① 连续复读：尾部出现"同一段原样重复"（周期 10~600 字，重复跨距 ≥40 字）
#   ② 窗口复读：最近 40 字在末尾 300 字里反复出现
#   ③ 硬上限：正文字数 / 总秒数
#
# 为什么周期要扫到 600：实测她复读的单位是**整整一段 205 字的说明文**
# （见 记忆\对话.json 的旧记录），只按"短句重复"去找是抓不到的。
_REPEAT_MIN_PERIOD = 10      # 最小重复周期
_REPEAT_MAX_PERIOD = 600     # 最大（一段话能有多长）
_REPEAT_MIN_SPAN = 24        # 至少要重复这么多字才算复读
_LOOP_WINDOW = 24            # 模糊检测用的窗口
_LOOP_SPAN = 600             # 在这个范围里找窗口出现了几次


def _repeated_tail(s: str) -> bool:
    """尾部是不是"同一段原样重复了"：存在周期 p，使末尾 2p 字是两个相同的 p"""
    n = len(s)
    if n < _REPEAT_MIN_SPAN:
        return False
    for p in range(_REPEAT_MIN_PERIOD, min(_REPEAT_MAX_PERIOD, n // 2) + 1):
        if s[n - p:] == s[n - 2 * p:n - p]:
            return True
    return False


def _window_loop(s: str) -> bool:
    """最近 24 字在末尾 600 字里出现了 3 次以上（容忍中间夹细微变化）"""
    if len(s) < _LOOP_WINDOW * 3:
        return False
    win = s[-_LOOP_WINDOW:]
    span = s[-_LOOP_SPAN:]
    cnt, i = 0, 0
    while True:
        j = span.find(win, i)
        if j < 0:
            break
        cnt += 1
        if cnt >= 3:
            return True
        i = j + 1
    return False


def cut_repeat(s: str) -> str:
    """把复读出来的部分裁掉，只留第一遍"""
    n = len(s)
    if n < _REPEAT_MIN_SPAN:
        return s
    for p in range(_REPEAT_MIN_PERIOD, min(_REPEAT_MAX_PERIOD, n // 2) + 1):
        if s[n - p:] != s[n - 2 * p:n - p]:
            continue
        # 找到"第一次连着出现两个相同周期"的位置，截到第一个周期结束
        for i in range(0, n - 2 * p + 1):
            if s[i:i + p] == s[i + p:i + 2 * p]:
                return s[:i + p]
        return s[:p]
    return s


def _clean(s: str) -> str:
    s = re.sub(r"[*`#>]+", "", (s or "").strip())
    s = re.sub(r"\s+\n", "\n", s)
    return s.strip() or "……"


def _sanitize_one(text: str) -> str:
    """单条历史：裁掉复读尾巴、压掉换行、限长"""
    t = cut_repeat(_clean(text))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:240]


def sanitize_history(items) -> list[dict]:
    """把历史整理成"能喂给聊天模板"的样子。

    原来的坑：主动说话那一路只往历史里塞 assistant，从来不塞 user，历史
    变成清一色 assistant。模型看见十几个连续的 assistant 发言，就照着往下
    复读同一段话 —— 这才是"输出一长段重复内容"的根因。所以这里：
      · 丢掉空的、复读的、连着重复的条目
      · 保证 user / assistant 交替（必要时补一条占位 user）
    """
    out: list[dict] = []
    last_role = None
    for h in items or []:
        if not isinstance(h, dict):
            continue
        role, content = h.get("role"), h.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        c = _sanitize_one(content)
        if not c or len(re.sub(r"[\s\W_]+", "", c)) < 1:
            continue
        if out and c == out[-1]["content"] and role == out[-1]["role"]:
            continue                       # 连着两条一模一样，丢
        if role == last_role == "assistant":
            out.append({"role": "user", "content": "（继续）"})
        out.append({"role": role, "content": c})
        last_role = out[-1]["role"]
    return out[-40:]


def history_record(role: str, content: str) -> None:
    """记一条历史。主动说话也要记一条 user，否则又变成清一色 assistant"""
    with _hist_lock:
        _history.append({"role": role, "content": content})
        _history[:] = sanitize_history(_history)


def build_prompt(user_text: str, proactive: bool, turn: int) -> list[dict]:
    """拼提示词：人设 + 历史（按字数预算从近到远取）+ 这次要回答的话"""
    budget = int(CFG.get("history_chars", 3000) or 3000)
    with _hist_lock:
        hist = list(_history)
    picked, used = [], 0
    for h in reversed(hist):
        c = h.get("content") or ""
        if picked and used + len(c) > budget:
            break
        picked.append(h)
        used += len(c)
    picked.reverse()

    msgs = [{"role": "system", "content": persona()}]
    msgs.extend(picked)
    # 历史一长模型就容易忘了自己是谁、开始复读，所以末尾再钉一次人设
    msgs.append({"role": "system", "content":
                 "记住你是鲸鱼娘：只说中文，一句话不超过 40 字；"
                 "不要重复上一句，不要复读同一段话。"})
    if proactive:
        msgs.append({"role": "user",
                     "content": PROACTIVE_HINTS[turn % len(PROACTIVE_HINTS)]})
    else:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def chat_stream(messages: list[dict], on_text, on_reason, *, max_tokens: int = 200,
                max_chars: int = 240, max_seconds: float = 35.0,
                timeout: int = 300) -> tuple[str, str, dict, str]:
    """向本地模型流式要回答，边收边回调。

    返回 (正文, 思考, usage, 停止原因)；停止原因是 "" / "repeat" / "chars" / "time"。
    耍复读的时候主动断开连接，让 llama.cpp 立刻停下生成。
    """
    body = {
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": 0.8,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "repeat_penalty": 1.18,        # ← 治复读的主力
        "repeat_last_n": 256,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.3,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        LLM_BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})

    text_parts, reason_parts, usage = [], [], {}
    stop, chunks = "", 0
    t0 = time.time()
    r = urllib.request.urlopen(req, timeout=timeout)
    try:
        buf = ""
        text_len = 0
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if d.get("usage"):
                usage = d["usage"]
            ch = (d.get("choices") or [{}])[0].get("delta") or {}
            piece = ""
            if ch.get("reasoning_content"):
                reason_parts.append(ch["reasoning_content"])
                piece += ch["reasoning_content"]
                on_reason(ch["reasoning_content"])
            if ch.get("content"):
                text_parts.append(ch["content"])
                text_len += len(ch["content"])
                piece += ch["content"]
                on_text(ch["content"])
            if not piece:
                continue
            buf += piece
            chunks += 1
            if chunks % 3:
                continue                    # 每 3 块查一次，够快也够灵
            if _repeated_tail(buf) or _window_loop(buf):
                stop = "repeat"
                break
            if text_len >= max_chars:
                stop = "chars"
                break
            if time.time() - t0 >= max_seconds:
                stop = "time"
                break
    finally:
        try:
            r.close()          # ★ 断开连接 = 让 llama.cpp 取消这次生成、空出显存
        except Exception:  # noqa: BLE001
            pass
    return ("".join(text_parts).strip(), "".join(reason_parts).strip(),
            usage, stop)


def run_turn(user_text: str, proactive: bool = False) -> None:
    """一轮完整对话：广播事件 → 按需拉起模型 → 流式回话 → 收工"""
    global _busy, _turn_seq, _last_active
    with _turn_lock:
        if _busy:
            return
        _busy = True
    _turn_seq += 1
    turn = _turn_seq
    _last_active = time.time()
    try:
        if not proactive:
            broadcast({"t": "user", "text": user_text})
        broadcast({"t": "turn-start", "turn": turn})
        broadcast({"t": "step-start", "turn": turn, "step": 1})

        # ★ 按需拉起模型：平时它是关着的（显存 0），要开口才加载。
        #   先广播事件再加载 —— 这样她立刻摆出思考姿态，而不是干等几秒没反应。
        if manage_mode() != "off":
            if not llm_alive():
                broadcast({"t": "control", "hint": "正在加载本地模型…"})
            try:
                ok = ensure_llm()
            except Exception as e:  # noqa: BLE001
                ok = False
                _safe_err(f"[加载模型] {e}")
            if not ok:
                broadcast({"t": "turn-end", "turn": turn, "ms": 0, "tokens": 0,
                           "reason": {"kind": "error",
                                      "error": {"message": "本地模型起不来，看 记忆\\大脑.log"}}})
                _say_local("呜…我的脑子启动失败了，主人看看日志？")
                return
            llm_touch()

        msgs = build_prompt(user_text, proactive, turn)

        def on_reason(chunk):
            broadcast({"t": "delta", "kind": "reasoning", "text": chunk})

        def on_text(chunk):
            broadcast({"t": "delta", "kind": "text", "text": chunk})

        key = "max_tokens_proactive" if proactive else "max_tokens"
        max_tokens = int(CFG.get(key, 96 if proactive else 200) or 96)
        try:
            text, reason, usage, stop = chat_stream(
                msgs, on_text, on_reason,
                max_tokens=max_tokens,
                max_chars=int(CFG.get("max_reply_chars", 240) or 240),
                max_seconds=float(CFG.get("max_reply_seconds", 35.0) or 35.0))
        except Exception as e:  # noqa: BLE001
            broadcast({"t": "turn-end", "turn": turn, "ms": 0, "tokens": 0,
                       "reason": {"kind": "error",
                                  "error": {"message": f"连不上本地模型：{e}"}}})
            _say_local("呜…我连不上脑子了，模型没开吗？")
            return
        finally:
            llm_touch()

        if stop == "repeat":
            # 复读的尾巴全砍掉，只留第一遍。流式过程中她已经把复读的部分
            # 显示出来了，所以下面那条 assistant 事件会把气泡内容整体换掉。
            text = cut_repeat(text)
            _safe_err("[出话刹车] 检测到复读，已掐断并裁掉重复部分")
        text = _clean(text)
        if len(re.sub(r"[\s\W_]+", "", text)) < 1:
            text = "……"
        text = text[:140]                      # 桌宠就说一句话，别糊一大段

        tokens = (usage.get("completion_tokens", 0) + usage.get("prompt_tokens", 0)) or 0
        broadcast({"t": "assistant", "turn": turn, "step": 1, "text": text, "usage": usage})
        broadcast({"t": "turn-end", "turn": turn, "ms": 0, "tokens": tokens,
                   "reason": {"kind": "completed", "stop": stop}})

        # ★ 主动说话也要记一条 user —— 只记 assistant 的话，历史会变成
        #   清一色 assistant，模型就照着往下复读（这是复读的根因）
        if proactive:
            history_record("user", PROACTIVE_HINTS[turn % len(PROACTIVE_HINTS)])
        elif user_text:
            history_record("user", user_text)
        history_record("assistant", text)
        save_history()
    finally:
        _busy = False
        _last_active = time.time()
        llm_touch()


def _say_local(text: str) -> None:
    """不走模型，直接让她冒一句话（出错提示等）"""
    broadcast({"t": "turn-start", "turn": 0})
    for i in range(0, len(text), 3):
        broadcast({"t": "delta", "kind": "text", "text": text[i:i + 3]})
        time.sleep(0.05)
    broadcast({"t": "assistant", "turn": 0, "step": 1, "text": text})
    broadcast({"t": "turn-end", "turn": 0, "reason": {"kind": "completed"}, "tokens": 0})


# ----------------------------------------------------------------------
# 主动说话
# ----------------------------------------------------------------------
def proactive_loop() -> None:
    """安静够久就自己找模型要一句话。

    模型不再常驻：这一轮她会先把模型拉起来，说完由 llm_reaper_loop 卸载，
    所以"不主动说话"的那段时间显存/显卡占用是 0。
    """
    while True:
        time.sleep(10)
        try:
            if not CFG.get("proactive", True):
                continue
            gap = float(CFG.get("proactive_gap", 240) or 0)
            if gap <= 0 or time.time() - _last_active < gap:
                continue
            if _busy:
                continue
            if manage_mode() == "off" and not llm_alive():
                continue          # 不管模型又没在跑，那她没法开口
            run_turn("", proactive=True)
        except Exception as e:  # noqa: BLE001
            _safe_err(f"[主动说话] {e}")


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
MIME = {".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".html": "text/html; charset=utf-8",
        ".moc3": "application/octet-stream", ".css": "text/css; charset=utf-8"}

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>鲸鱼娘桌宠</title>
<style>
  html,body{margin:0;height:100%;overflow:hidden;background:transparent!important;
    font-family:"Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
    -webkit-user-select:none;user-select:none}
</style></head><body>
<script src="/dsh-pet/pet.js"></script>
<script>
// ————————————————————————————————————————————————————————————
// 命中判定：页面自己算，发给桌面壳。
//
// 壳子原来用「屏幕光标坐标 - 窗口坐标」算，屏幕缩放不是 100% 时这两个值
// 不在同一坐标系里，结果是：鼠标在控件上判成"在外面"→ 窗口穿透 → 点不着、
// 拖滑块被中途掐断；鼠标在屏幕空白处反而判成"命中"。用页面自己的
// clientX/clientY 就没这问题。
//
// 窗口是 setIgnoreMouseEvents(true,{forward:true})，所以即便处于穿透状态，
// 鼠标移动事件照样送得到页面。
//
// ⚠️ 但只有"页面收到的是真实鼠标位置"才成立。外壳以前为了全屏视线追踪，
//    每 16ms 往页面里注入一个假的 mouseMove（坐标还是整屏归一化后乘窗口宽高），
//    于是这里每 16ms 就按一个假坐标算一次命中 → 穿透状态疯狂翻转 →
//    按钮点到一半被切断、拖她时常按不住。外壳已改成只走 IPC 喂一个只读的
//    全局光标（见 外壳\\main.js / preload.js），并且下面每 500ms 强制汇报一次，
//    把外壳那条按坐标算的兜底彻底压住。
// ————————————————————————————————————————————————————————————
var dshpLastHit = null;
var dshpLastMouse = { x: -1, y: -1 };
var dshpPressing = false;
var dshpPressAt = 0;
// 按住的"最长认账时间"：万一 pointerup 丢了（窗口被隐藏、系统抢了指针…），
// 锁不能一直挂着 —— 那样她会永远抢点击、再也不穿透。超时就当没按。
var DSHP_PRESS_MAX = 8000;
function dshpIsPressing(){
  return dshpPressing && (Date.now() - dshpPressAt) < DSHP_PRESS_MAX;
}
var DSHP_HIT_SEL = '.dshp-panel,.dshp-menu,.dshp-hud,.dshp-bubble,.dshp-composer,' +
                   '.dshp-dock,.dshp-tab';
function dshpComputeHit(x, y){
  // ⚠️ 正按着鼠标（点按钮、拖她、拖滑块）时一律算「要收点击」：
  //    按钮的 click 需要 down 和 up 落在同一个元素上，这期间只要穿透状态
  //    翻成 true，up 就送到桌面上去了 —— 看着就是「按钮点了没反应」。
  if (window.__dshpInteracting || dshpIsPressing()) return 'panel';
  var el = document.elementFromPoint(x, y);
  if (el && el.closest && el.closest(DSHP_HIT_SEL)) return 'panel';
  try{ if (window.DSHPet && DSHPet.hitTest && DSHPet.hitTest(x, y)) return 'model'; }catch(e){}
  return 'none';
}
// force=true：不看变没变，直接发一次。
// 壳子那边留着一条「页面 2 秒没报就自己按窗口坐标算」的兜底，而那个兜底在
// 屏幕缩放不是 100% 时会算错（主人怀疑的「屏幕坐标配置错误」就是它）。所以
// 这里定期强制汇报一次，把那条兜底一直压住，让页面始终说了算。
function dshpReportHit(k, force){
  if (!force && k === dshpLastHit) return;
  dshpLastHit = k;
  if (window.dshpetShell && dshpetShell.hit) dshpetShell.hit(k);
}
// 切穿透状态时 Windows 会合成 mouseout/mousemove，若 mouseout 立刻报 none，
// 就会 panel↔none 每秒抖几十次，而每次抖动都会打断正在拖的东西。
// 所以"离开了"要延迟确认，期间只要有 mousemove 就取消。
var dshpOutTimer = 0;
function dshpMaybeLeave(){
  clearTimeout(dshpOutTimer);
  dshpOutTimer = setTimeout(function(){
    if (!dshpIsPressing()) dshpReportHit('none');
  }, 260);
}
document.addEventListener('mousemove', function(e){
  clearTimeout(dshpOutTimer);
  dshpLastMouse.x = e.clientX;
  dshpLastMouse.y = e.clientY;
  dshpReportHit(dshpComputeHit(e.clientX, e.clientY));
}, true);
document.addEventListener('mouseout', function(e){
  if (e.relatedTarget) return;   // 文档内换元素，不算离开
  dshpMaybeLeave();
}, true);
// 按住的这段时间锁住状态。用 pointer 事件而不是 mouse 事件：
// pet.js 在按下她的时候会 preventDefault，兼容的 mouse 事件不一定会补发。
document.addEventListener('pointerdown', function(e){
  if (e.button === 0) { dshpPressing = true; dshpPressAt = Date.now(); }
}, true);
['pointerup', 'pointercancel'].forEach(function(t){
  document.addEventListener(t, function(){ dshpPressing = false; }, true);
});
document.addEventListener('mouseup', function(){ dshpPressing = false; }, true);
window.addEventListener('blur', function(){
  // 失焦不代表鼠标已经不在她身上：按最后一次已知位置重算，别硬报 none，
  // 否则她可能被切成穿透、下一次点击就丢了。
  if (dshpIsPressing()) return;
  if (dshpLastMouse.x < 0) { dshpReportHit('none', true); return; }
  dshpReportHit(dshpComputeHit(dshpLastMouse.x, dshpLastMouse.y), true);
});
// 面板开关、她动过之后补报一次（鼠标没动但底下的东西变了）；
// force 同时起心跳作用，让壳子那条坐标兜底永远不启用。
setInterval(function(){
  if (dshpLastMouse.x < 0) return;
  dshpReportHit(dshpComputeHit(dshpLastMouse.x, dshpLastMouse.y), true);
}, 500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静点
        # ⚠️ 启动脚本用的是 pythonw.exe，它没有控制台，sys.stderr 是 None。
        # 不判空就往 stderr 写会抛 AttributeError，把整个请求打断
        # （表现是浏览器收到"连接被对方关闭"），/api/* 全废。
        if not ("/dsh-pet/say" in (self.path or "") or "/api/" in (self.path or "")):
            return
        try:
            if sys.stderr:
                sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 工具 ----------
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj, code=200):
        self._send(code, MIME[".json"], json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, abs_path):
        try:
            with open(abs_path, "rb") as f:
                buf = f.read()
        except Exception:  # noqa: BLE001
            return self._send(404, "text/plain; charset=utf-8", b"not found")
        ext = os.path.splitext(abs_path)[1].lower()
        self._send(200, MIME.get(ext, "application/octet-stream"), buf)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = (self.path or "/").split("?")[0]

        if path in ("/", "/index.html"):
            return self._send(200, MIME[".html"], PAGE.encode("utf-8"))

        # 测试专用页：拦掉 EventSource。
        # 页面开着 SSE 长连接时，无头浏览器的「虚拟时间」永远等不到空闲，截图会挂死。
        if path == "/__shot":
            stub = ("<script>window.EventSource=function(){this.close=function(){};"
                    "this.addEventListener=function(){};this.removeEventListener=function(){}};"
                    "</script>")
            html = PAGE.replace('<script src="/dsh-pet/pet.js"></script>',
                                stub + '<script src="/dsh-pet/pet.js"></script>')
            return self._send(200, MIME[".html"], html.encode("utf-8"))

        if path == "/dsh-pet/pet.js":
            boot = {"config": {"height": CFG.get("height", 180),
                               "showReasoning": CFG.get("show_reasoning", False)}}
            try:
                with open(os.path.join(WEB, "pet.js"), "r", encoding="utf-8") as f:
                    body = f.read()
            except Exception as e:  # noqa: BLE001
                return self._send(500, "text/plain; charset=utf-8", str(e).encode())
            head = f";window.__DSH_PET_BOOT__={json.dumps(boot)};\n"
            return self._send(200, MIME[".js"], (head + body).encode("utf-8"))

        if path.startswith("/dsh-pet/vendor/"):
            rel = path[len("/dsh-pet/vendor/"):]
            return self._file(os.path.join(WEB, "vendor", rel))

        if path.startswith("/dsh-pet/model/"):
            # 中文文件名必须解码，否则 motions/自拍.motion3.json 会 404
            import urllib.parse
            rel = urllib.parse.unquote(path[len("/dsh-pet/model/"):])
            return self._file(os.path.join(WEB, "model", rel))

        if path == "/dsh-pet/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=4000)
            with _clients_lock:
                _clients.add(q)
            try:
                hello = {"t": "hello", "sessionId": "local", "status": "idle"}
                self.wfile.write(("data: " + json.dumps(hello) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        frame = q.get(timeout=15)
                        self.wfile.write(frame)
                    except queue.Empty:
                        frame = b": ping\n\n"
                        self.wfile.write(frame)
                    self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            finally:
                with _clients_lock:
                    _clients.discard(q)
            return

        if path == "/dsh-pet/hud":
            # 真实账本：从 DSH 自己的 token 记账折算成钱（见 账本.py）
            force = "refresh=1" in (self.path or "")
            try:
                import 账本
                账本.reload_price()
                return self._json(账本.hud_payload(force=force))
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "code": "LEDGER",
                                   "error": f"账本读不出来：{type(e).__name__}: {e}"})

        if path.startswith("/dsh-whale/"):
            return self._json({"ok": False})

        if path == "/dsh-pet/models" or path == "/api/models":
            return self._json({"ok": True, "models": scan_models(),
                               "current": llm_current(), "alive": llm_alive(),
                               "managed": manage_mode() != "off",
                               "idle": not llm_alive()})

        if path == "/api/config":
            with _cfg_lock:
                c = dict(CFG)
            c["current"] = llm_current()
            c["alive"] = llm_alive()
            c["managed"] = manage_mode() != "off"
            c["idle_unload"] = idle_unload_seconds()
            c["gpu_released_at"] = _llm_unloaded_at
            c["loading"] = _busy and not llm_alive()
            return self._json(c)

        return self._send(404, "text/plain; charset=utf-8", b"not found")

    # ---------- POST ----------
    def do_POST(self):
        path = (self.path or "/").split("?")[0]
        body = self._body()

        if path == "/dsh-pet/say":
            text = (body.get("text") or "").strip()
            if text:
                threading.Thread(target=run_turn, args=(text,), daemon=True).start()
            return self._json({"ok": True})

        if path == "/dsh-pet/cancel":
            return self._json({"ok": True})

        if path == "/dsh-pet/control":
            broadcast({"t": "control", **body})
            return self._json({"ok": True})

        if path == "/api/quit" or path == "/dsh-pet/quit":
            # 整体退出（控制台「停止所有服务」走这条）：
            #   ① 广播 control 事件 → 页面转告 Electron app.quit()，外壳体面关闭
            #   ② 后端 1 秒后自己退出，退出前把模型从显存里卸掉
            # 外壳不在（浏览器模式）时 ① 没反应也没关系，② 照样成立。
            broadcast({"t": "control", "quit": True})
            threading.Thread(target=_self_quit, daemon=True).start()
            return self._json({"ok": True, "bye": True})

        if path == "/api/config":
            # 桌宠「大脑」面板改设置：主动说话开关/间隔、显存策略
            editable = ("proactive", "proactive_gap", "manage_llm", "idle_unload",
                        "show_reasoning", "max_tokens", "max_tokens_proactive",
                        "max_reply_chars", "max_reply_seconds", "history_chars",
                        "llm_ctx", "height")
            with _cfg_lock:
                for k in editable:
                    if k in body:
                        CFG[k] = body[k]
                snapshot = dict(CFG)
            save_json(CONFIG_FILE, snapshot)
            # 要求立刻释放显存
            if body.get("release_now"):
                threading.Thread(target=release_llm, args=("手动",), daemon=True).start()
            return self._json({"ok": True, "config": snapshot})

        if path == "/api/release":
            threading.Thread(target=release_llm, args=("手动",), daemon=True).start()
            return self._json({"ok": True})

        if path == "/dsh-pet/switch" or path == "/api/switch":
            f = (body.get("file") or "").strip()
            models = scan_models()
            if not f or not any(m["file"] == f for m in models):
                return self._json({"ok": False, "error": "模型不存在"}, 400)

            def work():
                ok = ensure_llm(f)
                if ok:
                    with _cfg_lock:
                        CFG["model"] = f
                    save_json(CONFIG_FILE, CFG)
                broadcast({"t": "control", "gaze": "user"})

            threading.Thread(target=work, daemon=True).start()
            return self._json({"ok": True, "switching": True, "current": f})

        return self._send(404, "text/plain; charset=utf-8", b"not found")


# ----------------------------------------------------------------------
def ledger_warm_loop() -> None:
    """后台预热账本。
    首次解析 DSH 的会话日志要好几秒（十几个 MB 的 zstd），
    不能等主人右键点开账本时才现算 —— 那样要转好久圈。
    这里定期在后台扫一遍，右键时直接命中缓存（实测 0.02s）。"""
    time.sleep(2)
    while True:
        try:
            import 账本
            账本.load_price()
            账本.scan()
        except Exception as e:  # noqa: BLE001
            _safe_err(f"[账本预热] {e}")
        time.sleep(20)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    os.makedirs(MEM, exist_ok=True)
    load_history()

    if not os.path.exists(PERSONA_FILE):
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_PERSONA + "\n")

    mode = manage_mode()
    print("=" * 62)
    print("  鲸鱼娘 · 独立桌宠后端")
    print("=" * 62)
    print(f"  页面      : http://{HOST}:{PORT}")
    print(f"  本地模型  : {LLM_BASE}  ->  {'✔ 在线' if llm_alive() else '✘ 未启动（说话时自动加载）'}")
    print(f"  可选模型  : {len(scan_models())} 个")
    print(f"  显存策略  : " + ("要说话才拉起，空闲 %g 秒后卸载（平时不占显卡）"
                              % idle_unload_seconds() if mode != "off"
                              else "不管（模型常驻）"))
    print(f"  主动说话  : {'开' if CFG.get('proactive') else '关'}"
          f"（安静 {CFG.get('proactive_gap')} 秒后）")
    print("=" * 62)

    atexit.register(_shutdown_llm)
    threading.Thread(target=proactive_loop, daemon=True).start()
    threading.Thread(target=ledger_warm_loop, daemon=True).start()
    threading.Thread(target=llm_reaper_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        if mode != "off" and _llm_owned:
            print("  收工：把模型从显存里卸掉…")
        _shutdown_llm()
    return 0


if __name__ == "__main__":
    sys.exit(main())
