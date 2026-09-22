"""单实例锁：防止同时跑多个值守/面板，根治双进程互相干扰。

用文件锁(fcntl.flock)：谁先抢到锁谁能启动，后到的直接退出并提示。
"""
from __future__ import annotations

import fcntl
import sys
from pathlib import Path

from core.config import ROOT

LOCK_FILE = ROOT / ".wx_bot.lock"


def ensure_single(app_name: str) -> None:
    """占用单实例锁；若已被占用则提示退出。返回后调用者需持有该文件引用。"""
    f = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{app_name}] 已有另一个实例在运行（单实例限制）。")
        print("请先关闭正在运行的监控面板/值守窗口，再启动。")
        sys.exit(2)
    # 保持文件句柄引用，锁才能一直持有；引用存到全局以免被回收
    global _HOLD
    _HOLD = f


_HOLD: Path | None = None