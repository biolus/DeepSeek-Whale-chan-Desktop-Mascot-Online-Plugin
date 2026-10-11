#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大肥鱼本地 · 控制台
===================
一个入口管两个模型：选模型 → 起服务 → 开网页对话。
不启动桌宠也能用，就是个标准的本地大模型部署。

文件名保持 ASCII（control.py）：Windows 下中文路径经命令行传参会乱码。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(ROOT, "models")
RUNTIME = os.path.join(ROOT, "运行时", "llama-server.exe")
CLI = os.path.join(ROOT, "运行时", "llama-cli.exe")
LOG_DIR = os.path.join(ROOT, "日志")
STATE_FILE = os.path.join(ROOT, "日志", "上次选择.json")
PET_BAT = os.path.join(ROOT, "鲸鱼娘", "启动鲸鱼娘.bat")
NET_PY = os.path.join(ROOT, "联网", "proxy.py")
NET_PORT = 8081            # 联网插件
PET_PORT = 8095            # 鲸鱼娘后端（server.py）

HOST, PORT = "127.0.0.1", 8080
BASE = f"http://{HOST}:{PORT}"
CTX = 16384                     # 上下文长度
NPAR = 1                        # 并发槽数。默认 4 会按 4 份分配 KV 缓存，8GB 显存扛不住
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 分卷模型只认第一卷
SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)

# 按文件名给模型配一句人话说明（从上往下匹配，第一个命中为准）
HINTS = [
    (r"abliterated", "推理模型 · 去审查版",
     "社区做的去除拒答版本；仍是推理模型，每句要等 15~60 秒"),
    (r"qwen.*instruct", "纯对话模型", "秒回，适合日常聊天 ★推荐"),
    (r"deepseek.*r1", "推理模型", "会先想再答，每句要等 15~60 秒"),
    (r"qwen.*coder", "代码模型", "写代码专用"),
    (r".*-vl-|vision", "视觉模型", "能看图"),
]


def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f}{u}"
        n /= 1024
    return f"{n:.2f}TB"


# ----------------------------------------------------------------------
#  模型扫描
# ----------------------------------------------------------------------
def scan_models() -> list[dict]:
    if not os.path.isdir(MODEL_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(MODEL_DIR)):
        if not fn.lower().endswith(".gguf"):
            continue
        m = SHARD_RE.search(fn)
        if m and m.group(1) != "00001":
            continue                      # 分卷的第 2 卷起，不单独列出
        path = os.path.join(MODEL_DIR, fn)
        size = os.path.getsize(path)
        total = size
        if m:
            for k in range(2, int(m.group(2)) + 1):
                other = SHARD_RE.sub(f"-{k:05d}-of-{m.group(2)}.gguf", fn)
                op = os.path.join(MODEL_DIR, other)
                if os.path.exists(op):
                    total += os.path.getsize(op)
        kind, desc = "模型", ""
        for pat, k, d in HINTS:
            if re.search(pat, fn, re.I):
                kind, desc = k, d
                break
        out.append({
            "file": fn, "path": path, "rel": os.path.join("models", fn),
            "size": total, "kind": kind, "desc": desc,
            "shards": int(m.group(2)) if m else 1,
        })
    return out


def pretty(name: str) -> str:
    n = re.sub(r"\.gguf$", "", name, flags=re.I)
    n = SHARD_RE.sub("", n)
    return n


# ----------------------------------------------------------------------
#  服务状态
# ----------------------------------------------------------------------
def http_json(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def server_model() -> str | None:
    """返回正在跑的模型文件名；没跑则 None"""
    try:
        d = http_json(BASE + "/v1/models")
        mid = (d.get("data") or [{}])[0].get("id") or ""
        return os.path.basename(mid) if mid else "（未知）"
    except Exception:  # noqa: BLE001
        return None


def proc_running(name: str) -> bool:
    try:
        r = subprocess.run(["tasklist", "/fi", f"imagename eq {name}"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        return name.lower() in r.stdout.lower()
    except Exception:  # noqa: BLE001
        return False


def vram() -> str:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        return r.stdout.strip() or "（读不到）"
    except Exception:  # noqa: BLE001
        return "（读不到）"


def pet_state() -> str:
    # 后端在 8095 上活着 = 桌宠可用（外壳可能开着也可能没开）
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PET_PORT}/api/config", timeout=2) as r:
            back = r.status == 200
    except Exception:  # noqa: BLE001
        back = False
    shell = proc_running("electron.exe")
    if back and shell:
        return "运行中（后端+桌面外壳）"
    if back:
        return "后端已就绪（外壳未开）"
    if not os.path.exists(PET_BAT):
        return "未安装（文件夹不在原位）"
    return "未运行"


def net_state() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{NET_PORT}/api/state", timeout=2) as r:
            if r.status == 200:
                return "运行中"
    except Exception:  # noqa: BLE001
        pass
    return "未运行" if os.path.exists(NET_PY) else "未安装"


def start_net() -> bool:
    """启动联网代理：任何模型、任何客户端都能用"""
    if not os.path.exists(NET_PY):
        print(f"  ✘ 找不到 {NET_PY}")
        return False
    if net_state() == "运行中":
        print("  联网插件已经在跑了。")
        return True
    print("  正在启动联网插件…")
    try:
        subprocess.Popen(["python", NET_PY], cwd=os.path.dirname(NET_PY),
                         creationflags=CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        print(f"  ✘ 启动失败：{e}")
        return False
    for _ in range(20):
        time.sleep(0.5)
        if net_state() == "运行中":
            return True
    return False

def load_last() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8-sig") as f:
            return json.load(f).get("model")
    except Exception:  # noqa: BLE001
        return None


def save_last(model_file: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"model": model_file, "t": time.time()}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
#  启停
# ----------------------------------------------------------------------
def stop_server(quiet: bool = False) -> bool:
    if not proc_running("llama-server.exe"):
        if not quiet:
            print("  大脑本来就没在跑。")
        return False
    subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    for _ in range(20):
        if not proc_running("llama-server.exe"):
            break
        time.sleep(0.3)
    if not quiet:
        print("  已停止，显存已释放。")
    return True


def start_server(model: dict, wait: int = 240) -> bool:
    if not os.path.exists(RUNTIME):
        print(f"  ✘ 找不到运行时：{RUNTIME}")
        return False
    os.makedirs(LOG_DIR, exist_ok=True)
    logpath = os.path.join(LOG_DIR, "大脑.log")
    print(f"  正在启动 {pretty(model['file'])} …（日志：日志\\大脑.log）")
    try:
        logf = open(logpath, "ab", buffering=0)
        subprocess.Popen(
            [RUNTIME, "-m", model["rel"], "-ngl", "99", "-c", str(CTX),
             "--parallel", str(NPAR),     # 默认是 4 个并发槽，每个槽都占一份 KV 缓存，
                                          # 8GB 显存下会吃掉近 1.9GB。个人用单槽足够。
             "--host", HOST, "--port", str(PORT), "-t", "8", "--jinja"],
            cwd=ROOT,                      # 必须在根目录 + 相对路径，
            stdout=logf, stderr=logf,      # 否则中文绝对路径会让 llama.cpp 打不开模型
            stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except Exception as e:  # noqa: BLE001
        print(f"  ✘ 启动失败：{e}")
        return False

    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            if http_json(BASE + "/health", 2).get("status") == "ok":
                print(f"  ✔ 就绪，用时 {time.time()-t0:.1f} 秒")
                save_last(model["file"])
                return True
        except Exception:  # noqa: BLE001
            pass
        sys.stdout.write(f"\r  加载中… {int(time.time()-t0)}s ")
        sys.stdout.flush()
        time.sleep(2)
    print("\n  ✘ 加载超时，看看 日志\\大脑.log")
    return False


def ensure_model(models: list[dict], want: dict) -> bool:
    """确保 8080 上跑的就是 want；跑着别的就先问再换"""
    cur = server_model()
    if cur is None:
        return start_server(want)
    if cur == want["file"]:
        print(f"  ✔ 大脑已经在跑这个模型了，直接复用。")
        return True
    print(f"  当前跑的是：{pretty(cur)}")
    if proc_running("pythonw.exe"):
        print("  ⚠ 鲸鱼娘桌宠正在用它，换模型会把她的脑子换掉（她会重新加载）。")
    if input(f"  要换成 {pretty(want['file'])} 吗？[y/N] ").strip().lower() not in ("y", "yes", "是"):
        print("  保持现状。")
        return True
    stop_server(quiet=True)
    return start_server(want)


# ----------------------------------------------------------------------
#  菜单
# ----------------------------------------------------------------------
def pick_model(models: list[dict], title: str) -> dict | None:
    last = load_last()
    print(f"\n  {title}")
    print("  " + "-" * 62)
    for i, m in enumerate(models, 1):
        star = " ★上次用的" if m["file"] == last else ""
        shard = f" · {m['shards']}卷" if m["shards"] > 1 else ""
        print(f"   [{i}] {pretty(m['file'])}{star}")
        print(f"       {m['kind']} · {human(m['size'])}{shard} · {m['desc'] or '—'}")
    print("   [0] 返回")
    print("  " + "-" * 62)
    try:
        s = input("  请选择：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not s.isdigit() or not (1 <= int(s) <= len(models)):
        return None
    return models[int(s) - 1]


def show_status():
    print("\n  " + "=" * 62)
    print("   当前状态")
    print("  " + "=" * 62)
    cur = server_model()
    print(f"   大脑服务 : {'✔ ' + pretty(cur) if cur else '✘ 未启动'}")
    print(f"   地址     : {BASE}" + ("" if cur else "（未启动）"))
    print(f"   鲸鱼娘   : {pet_state()}")
    print(f"   联网插件 : {net_state()}" + (f"   http://127.0.0.1:{NET_PORT}/v1" if net_state()=="运行中" else ""))
    print(f"   显存     : {vram()}")
    models = scan_models()
    print(f"   可用模型 : {len(models)} 个")
    for m in models:
        print(f"              · {pretty(m['file'])}  {human(m['size'])}")
    print("  " + "=" * 62)
    print("   接第三方客户端（Cherry Studio / ChatBox / 沉浸式翻译…）：")
    print(f"     直连（不联网） {BASE}/v1")
    print(f"     联网版         http://127.0.0.1:{NET_PORT}/v1   ← 想让她能上网查资料就填这个")
    print(f"     API Key 随便填      模型名填上面列出的任意一个")
    print("  " + "=" * 62)


def chat(models: list[dict]):
    m = pick_model(models, "想和哪个模型聊天？")
    if not m:
        return
    if not ensure_model(models, m):
        return
    print(f"\n  打开网页对话界面：{BASE}")
    print("  （就是 llama.cpp 自带的那套聊天界面，支持中文、多轮、改参数）")
    try:
        webbrowser.open(BASE)
    except Exception:  # noqa: BLE001
        print("  浏览器没自动打开的话，手动访问上面的地址。")
    print("\n  提示：这个窗口可以关掉了，模型服务会继续在后台跑。")
    print("        想停就在菜单里选「停止所有服务」。")


def cli_chat(models: list[dict]):
    m = pick_model(models, "用哪个模型开命令行对话？")
    if not m:
        return
    if server_model():
        print("  ⚠ 大脑服务正在运行。两个程序各加载一份模型，8GB 显存放不下。")
        if input("  先停掉服务再开命令行？[y/N] ").strip().lower() not in ("y", "yes", "是"):
            return
        stop_server(quiet=True)
    if not os.path.exists(CLI):
        print(f"  ✘ 找不到 {CLI}")
        return
    print(f"\n  进入命令行对话（{pretty(m['file'])}）")
    print("  输入问题回车即可；输入 /exit 或按 Ctrl+C 退出。\n")
    try:
        # 相对路径 + cwd，避开中文绝对路径问题
        subprocess.run([os.path.join("运行时", "llama-cli.exe"),
                        "-m", m["rel"], "-ngl", "99", "-c", "8192", "-t", "8",
                        "-cnv", "--jinja",
                        "-sys", "你是一个乐于助人的中文AI助手，回答简洁准确。"],
                       cwd=ROOT)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"  ✘ 启动失败：{e}")


def pet_electron_pids() -> list:
    """桌宠外壳自己的 electron 进程号。

    ⚠️ 必须按**可执行文件路径**认，绝不能用 `taskkill /f /im electron.exe` ——
    那会把主人机器上别的 Electron 应用（VS Code、Discord、Slack…）一起杀掉。
    外壳的运行副本在 %LOCALAPPDATA%\\DSWhalePet\\（启动脚本拷过去再跑的），
    从源码目录直接跑的则落在 鲸鱼娘\\外壳\\electron\\ 下面。
    """
    ps = (r"Get-Process electron -ErrorAction SilentlyContinue | "
          r"Where-Object { $_.Path -like '*DSWhalePet*' -or "
          r"$_.Path -like '*外壳*' } | "
          r"ForEach-Object { $_.Id }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=20)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for tok in (r.stdout or "").split():
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


_CTL_PATH = os.path.abspath(__file__)
try:
    _CTL_MTIME = os.path.getmtime(_CTL_PATH)
except Exception:  # noqa: BLE001
    _CTL_MTIME = 0.0


def reload_if_changed() -> None:
    """control.py 在磁盘上被改过，就用新代码重新加载控制台。

    控制台是个常驻菜单：代码在**启动那一刻**就装进内存了，之后磁盘上怎么改
    都不会生效。主人正是踩了这个坑 —— 磁盘上已经修好 [2]/[6]，可他开着的
    那个控制台还在跑旧代码，按 [2] 依然弹出文件夹窗口。
    """
    global _CTL_MTIME
    try:
        m = os.path.getmtime(_CTL_PATH)
    except Exception:  # noqa: BLE001
        return
    if not _CTL_MTIME:
        _CTL_MTIME = m
        return
    if m <= _CTL_MTIME:
        return
    if time.time() - m < 2:      # 文件可能正写到一半，等两秒再说
        return
    _CTL_MTIME = m
    print("\n  ⚠ 检测到 control.py 已被更新，正在用新代码重新加载控制台…\n")
    # ⚠️ execv 不会帮我们 flush：不加这两行，上面那句提示会卡在缓冲区里被丢掉
    #    （stdout 接管道时是块缓冲，实测就是这么丢的）
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.6)
    try:
        os.execv(sys.executable, [sys.executable, _CTL_PATH] + sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"  （自动重载失败：{e} —— 请手动关掉这个窗口重新双击 大肥鱼.bat）")


def port_listening(port: int) -> bool:
    """端口有人在听吗？用 netstat 自己解析 —— 比 Get-NetTCPConnection 快很多，
    而且不会踩"本机连不通的端口要等 2 秒"那个坑。"""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if (len(parts) >= 4 and parts[0].upper() == "TCP"
                and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING"):
            return True
    return False


def pid_on_port(port: int):
    """找出监听这个端口的进程号。
    用来精确关掉桌宠后端，而不是把主人的其它 python 程序一起杀了。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=25)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit() and int(line) > 4:
                return int(line)
    except Exception:  # noqa: BLE001
        pass
    return None


def stop_pet(quiet: bool = False) -> list:
    """关掉桌宠的外壳和后端（菜单 [6]「停止所有服务」会调它）。

    老版本的毛病：这里只 taskkill 了 pythonw.exe —— 可启动脚本起后端用的是
    `python server.py`（进程名 python.exe），外壳又是 electron.exe，
    所以点完之后两个都还活着。

    现在是「先礼后兵」：
      ① 请她体面退出：POST /api/quit → 后端广播 control 事件 → 页面叫
         Electron app.quit()，顺手把模型从显存卸掉，后端随后自己退出。
         直接强杀会在系统托盘留一个点不掉的幽灵图标，所以先走这条路。
      ② 赖着不走的，按 PID 精确杀。**绝不** taskkill /im electron.exe ——
         那会把机器上别的 Electron 应用（VS Code、Discord…）一起送走。
    """
    killed = []
    # 动手之前先看清楚现在有什么。这一步必须在最前面：走过「体面退出」之后
    # 那些进程自己就没了，再查什么都查不到，最后报账会变成"本来就没开"。
    back_pid = pid_on_port(PET_PORT)
    shells = pet_electron_pids()
    net_pid = pid_on_port(NET_PORT)
    if back_pid is None and not shells and net_pid is None:
        if not quiet:
            print("  桌宠： 本来就没开")
        return killed

    # ① 请她体面退出（旧版后端没有这个接口，请求失败也无所谓）
    if back_pid:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{PET_PORT}/api/quit", data=b"{}",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            for _ in range(10):                  # 最多等她 2.5 秒自己走
                if pid_on_port(PET_PORT) is None:
                    break
                time.sleep(0.25)
        except Exception:  # noqa: BLE001
            pass

    # ② 外壳：按可执行文件路径认人
    for sp in shells:
        subprocess.run(["taskkill", "/f", "/pid", str(sp)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    if shells:
        killed.append("桌面外壳（%d 个进程）" % len(shells))
    elif proc_running("electron.exe") and not quiet:
        # 认不出来就别猜：按名字全杀会误伤别的 Electron 应用，宁可不关也不乱杀
        print("  ⚠ 有 electron.exe 在跑，但认不出是不是她的（路径不是默认的")
        print(r"     %LOCALAPPDATA%\DSWhalePet\），不敢乱杀。请到任务管理器里手动结束。")

    # ③ 后端：按端口找，**不**按进程名杀 python
    left = pid_on_port(PET_PORT)
    if left:
        subprocess.run(["taskkill", "/f", "/pid", str(left)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        killed.append("桌宠后端")
    elif back_pid:
        killed.append("桌宠后端（已自己退出）")

    # ④ 联网插件（8081）
    if net_pid:
        subprocess.run(["taskkill", "/f", "/pid", str(net_pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        killed.append("联网插件")

    if not quiet:
        print("  桌宠： " + "、".join(killed) + " 已关闭")
    return killed

def start_pet():
    """启动桌宠 = 双击 鲸鱼娘\启动鲸鱼娘.bat（起后端 + 外壳）。"""
    if not os.path.exists(PET_BAT):
        print("\n  ✘ 没找到 鲸鱼娘\\启动鲸鱼娘.bat")
        print(f"     请确认 {os.path.join(ROOT, '鲸鱼娘')} 目录完整。")
        return

    # 已经在跑就别再开一只。注意：她正常跑着时 pet_state() 返回的是
    # "运行中（后端+桌面外壳）"，而老代码只认 "后端已就绪" ——
    # 结果按 [2] 会再拉起一份，白白多出一个窗口。
    st = pet_state()
    if st.startswith("运行中") or st.startswith("后端已就绪"):
        if pet_electron_pids():
            print("\n  她已经在跑了。")
            return

    print("\n  启动鲸鱼娘桌宠（Live2D 桌面版）…")
    try:
        # ⚠️ 这里必须**只**给一个空串当窗口标题。
        #    原来写的是 ["cmd","/c","start","","",PET_BAT] —— 多出来那个空参数
        #    会被 start 当成"要打开的命令"，于是弹出一个文件夹窗口，桌宠根本没起。
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/D", os.path.dirname(PET_BAT), PET_BAT],
            cwd=ROOT, creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        try:
            os.startfile(PET_BAT)          # 兜底：等于双击那个 bat
        except Exception as e:  # noqa: BLE001
            print(f"  ✘ 启动失败：{e}")
            print(f"     也可以自己双击：{PET_BAT}")
            return

    # 发完指令别撒手 —— 等一会儿看她到底起没起来，省得主人在那儿猜
    print("  已发出启动指令，等她起来（最多 30 秒）…")
    t0 = time.time()
    while time.time() - t0 < 30:
        if pet_electron_pids() and port_listening(PET_PORT):
            print(f"  ✔ 她起来了（后端 + 桌面外壳都在，用时 {time.time() - t0:.1f} 秒）。")
            return
        time.sleep(0.8)
    if port_listening(PET_PORT):
        print("  ⚠ 后端起来了，但没看到桌面外壳 —— 看看那个黑窗口里 bat 的提示；")
        print("     实在起不来就双击 鲸鱼娘\\浏览器模式.bat（功能一样，只是有窗口边框）。")
    else:
        print("  ⚠ 30 秒还没看到她起来。请看看弹出的那个黑窗口里 bat 说了什么；")
        print("     也可以自己双击：", PET_BAT)

MENU = """
  ============================================================
    大肥鱼本地 · 控制台
  ============================================================
    当前大脑： {brain}
    鲸鱼娘  ： {pet}
  ------------------------------------------------------------
    [1] 和模型聊天         选模型 → 起服务 → 开网页对话界面
    [2] 启动鲸鱼娘桌宠
    [3] 命令行对话         黑窗口里直接聊
    [4] 查看状态 / 接口信息
    [5] 联网插件           让本地模型能上网查资料（所有模型通用）
    [6] 停止所有服务
    [0] 退出
  ============================================================
"""


def main() -> int:
    setup_console()
    os.makedirs(LOG_DIR, exist_ok=True)
    while True:
        reload_if_changed()          # 控制台常驻：磁盘上的改动要靠它才生效
        models = scan_models()
        cur = server_model()
        brain = pretty(cur) if cur else "未启动"
        print(MENU.format(brain=brain, pet=pet_state()))
        if not models:
            print(f"  ⚠ {MODEL_DIR} 里没有 .gguf 模型文件。")
        try:
            c = input("  请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  再见。")
            return 0

        if c == "1":
            if models:
                chat(models)
            else:
                print("  没有模型可用。")
        elif c == "2":
            start_pet()
        elif c == "3":
            if models:
                cli_chat(models)
            else:
                print("  没有模型可用。")
        elif c == "4":
            show_status()
        elif c == "6":
            print()
            stop_pet()
            stop_server(quiet=True)
            print("  大脑： llama-server 已停止（如果本来在跑）")
            print("  已清理。")
        elif c in ("0", "q", "exit"):
            print("  再见。")
            return 0
        else:
            print("  没这个选项。")
        print()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已中断。")
        sys.exit(130)
