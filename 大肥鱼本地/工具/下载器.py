#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多线程断点续传下载器
====================
用法：
    python 下载器.py <URL> <保存路径> [线程数]
    python 下载器.py --file 清单.txt          # 清单每行：URL<空格>保存路径

特性：分片多线程、断点续传（中断后重跑同一条命令即可）、失败自动重试。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CHUNK = 1 << 20


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def probe(url: str, timeout: int = 25) -> tuple[int, bool]:
    """返回 (文件大小, 是否支持Range)。"""
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        accept = r.headers.get("Accept-Ranges", "")
        cr = r.headers.get("Content-Range", "")
        if r.status == 206 and "/" in cr:
            return int(cr.split("/")[-1]), True
        return int(r.headers.get("Content-Length") or 0), "bytes" in accept


class Progress:
    def __init__(self, total: int, label: str):
        self.total, self.label = total, label
        self.done = 0
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.tty = sys.stdout.isatty()

    def add(self, n: int) -> None:
        with self.lock:
            self.done += n

    def draw(self, force: bool = False) -> None:
        d = self.done
        el = max(time.time() - self.t0, 0.001)
        sp = d / el
        if self.total:
            pct = d * 100 / self.total
            eta = (self.total - d) / sp if sp > 0 else 0
            bar_n = 28
            fill = int(bar_n * d / self.total)
            bar = "█" * fill + "─" * (bar_n - fill)
            line = (f"  {self.label} [{bar}] {pct:5.1f}%  "
                    f"{human(d)}/{human(self.total)}  {human(sp)}/s  剩{eta/60:.1f}分")
        else:
            line = f"  {self.label} {human(d)}  {human(sp)}/s"
        if self.tty:
            sys.stdout.write("\r" + line + "   ")
        else:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _fetch_range(url: str, part: str, start: int, end: int, prog: Progress,
                 stop: threading.Event) -> None:
    pos = os.path.getsize(part) + start if os.path.exists(part) else start
    while pos <= end and not stop.is_set():
        try:
            req = urllib.request.Request(url, headers={**UA, "Range": f"bytes={pos}-{end}"})
            with urllib.request.urlopen(req, timeout=40) as r, open(part, "ab") as f:
                while pos <= end and not stop.is_set():
                    buf = r.read(min(CHUNK, end - pos + 1))
                    if not buf:
                        break
                    f.write(buf)
                    pos += len(buf)
                    prog.add(len(buf))
            if pos > end:
                return
        except Exception:  # noqa: BLE001
            if stop.is_set():
                return
            time.sleep(2)  # 断线续传


def download(url: str, dest: str, threads: int = 6, label: str | None = None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    label = label or os.path.basename(dest)
    size, ranges = probe(url)
    if size and os.path.exists(dest) and os.path.getsize(dest) == size:
        print(f"  [跳过] {label} 已存在且完整（{human(size)}）", flush=True)
        return dest

    print(f"  [开始] {label}  大小 {human(size) if size else '未知'}  "
          f"{'分片' if ranges else '单线程'}下载 {threads} 线程", flush=True)

    if not ranges or not size:
        prog = Progress(size, label)
        _fetch_range(url, dest, 0, (size or (1 << 62)) - 1, prog,
                     threading.Event())
        return dest

    n = max(1, min(threads, 16))
    step = size // n
    bounds = [(i * step, size - 1 if i == n - 1 else (i + 1) * step - 1) for i in range(n)]
    prog = Progress(size, label)
    for i, (s, e) in enumerate(bounds):  # 已下载的部分计入进度
        p = f"{dest}.part{i}"
        if os.path.exists(p):
            prog.done += min(os.path.getsize(p), e - s + 1)

    stop = threading.Event()
    ts = [threading.Thread(target=_fetch_range,
                           args=(url, f"{dest}.part{i}", s, e, prog, stop), daemon=True)
          for i, (s, e) in enumerate(bounds)]
    for t in ts:
        t.start()
    try:
        while any(t.is_alive() for t in ts):
            prog.draw()
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print("\n  已中断（重跑同一命令可续传）", flush=True)
        raise
    prog.done = sum(min(os.path.getsize(f"{dest}.part{i}"), e - s + 1)
                    for i, (s, e) in enumerate(bounds))
    prog.draw()
    if sys.stdout.isatty():
        sys.stdout.write("\n")

    # 合并分片
    print(f"  合并分片…", flush=True)
    with open(dest, "wb") as out:
        for i in range(n):
            p = f"{dest}.part{i}"
            with open(p, "rb") as f:
                while True:
                    b = f.read(CHUNK * 4)
                    if not b:
                        break
                    out.write(b)
            os.remove(p)
    got = os.path.getsize(dest)
    if size and got != size:
        raise RuntimeError(f"大小不符：期望 {size}，实际 {got}")
    print(f"  ✔ 完成 {label}  {human(got)}", flush=True)
    return dest


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 管道下强制 UTF-8，避免中文/符号报错
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--file":
        with open(args[1], encoding="utf-8") as f:
            jobs = [ln.strip().split(None, 1) for ln in f
                    if ln.strip() and not ln.startswith("#")]
        for url, dest in jobs:
            download(url, dest, int(args[2]) if len(args) > 2 else 6)
    else:
        url, dest = args[0], args[1]
        download(url, dest, int(args[2]) if len(args) > 2 else 6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
