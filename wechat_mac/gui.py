"""悬浮监控面板：置顶小窗，实时显示托管状态、心跳、当前会话、最近收发事件。

设计：
- 独立"空闲心跳"线程：即使未开启托管，也刷新当前会话名 + 心跳时间（AH 存活证明）
- 开启托管后：值守主循环(watch_loop)负责收发，事件经队列回到界面
- 单进程 / 单实例：点"退出程序"会强杀整个进程，不残留
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

from wechat_mac.engine import watch_loop

LOG_COLOR = "#2b2b2b"
LOG_TAG_COLORS = {
    "event": "#1a73e8",   # 收发记录（对方话/机器人回复）→ 蓝
    "error": "#c0392b",   # 异常（发送失败/OCR失效/心跳出错）→ 红
    "status": "#7a7a7a",  # 系统状态流 → 灰
}
# 错误特征词：命中则日志标红 + 顶部亮橙点
ERROR_MARKS = ("失败", "出错", "失效", "异常")

DOT_COLORS = {
    "off": "#9aa0a6",      # 未托管，AI 空闲
    "on": "#34c759",       # 托管中（绿）
    "err": "#ff9500",      # 最近出错（橙）
}


def is_error_text(t: str) -> bool:
    return any(m in (t or "") for m in ERROR_MARKS)


class FloatPanel:
    def __init__(self, auto_start: bool = True):
        self.root = tk.Tk()
        self.root.title("微信托管")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"380x360+{sw - 408}+{sh - 410}")
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        self._q: queue.Queue = queue.Queue()
        self._watch_active = False
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._last_err = ""
        self._last_event = ""
        self._last_mode: str | None = None
        # 缓存 watch 配置（会话角标判定用；值守启动时会用最新配置新建 bot）
        try:
            from core.config import load_config
            self._watch_cfg = load_config()
        except Exception:
            self._watch_cfg = {}

        self._build_ui()

        # 空闲心跳线程（常驻，未托管也跑）
        self._idle = threading.Thread(target=self._idle_loop, daemon=True)
        self._idle.start()
        self._poll_ui()

        # 默认自启托管（单进程即开即跑）；--noauto 可关闭
        if auto_start:
            self.root.after(300, self.toggle)

    # ---------- 界面 ----------
    def _build_ui(self):
        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.dot = tk.Label(top, text="●", fg=DOT_COLORS["off"], font=("Arial", 14))
        self.dot.pack(side="left")
        self.status_lbl = tk.Label(top, text="未托管", font=("Arial", 11, "bold"))
        self.status_lbl.pack(side="left", padx=(4, 12))
        tk.Label(top, text="监测心跳:").pack(side="left")
        self.hb_lbl = tk.Label(top, text="--:--:--", width=8, anchor="w")
        self.hb_lbl.pack(side="left")

        self.chat_lbl = tk.Label(self.root, text="当前会话：读取中…",
                                 fg="#666", anchor="w", font=("Arial", 10))
        self.chat_lbl.pack(fill="x", padx=10, pady=(0, 2))
        self.badge_lbl = tk.Label(self.root, text="", fg="#999", anchor="w",
                                  font=("Arial", 9))
        self.badge_lbl.pack(fill="x", padx=10, pady=(0, 4))

        self.log = scrolledtext.ScrolledText(self.root, height=8, width=48,
                                             fg=LOG_COLOR, font=("Menlo", 9),
                                             state="disabled", wrap="word")
        for tag, color in LOG_TAG_COLORS.items():
            self.log.tag_config(tag, foreground=color)
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

        self.btn = tk.Button(self.root, text="开启托管", command=self.toggle,
                             font=("Arial", 11, "bold"), height=1)
        self.btn.pack(fill="x", padx=8, pady=(2, 4))
        self.quit_btn = tk.Button(self.root, text="退出程序", command=self._quit,
                                  font=("Arial", 10), fg="#c0392b", height=1)
        self.quit_btn.pack(fill="x", padx=8, pady=(0, 8))

    # ---------- 日志 ----------
    def _append_log(self, text: str, tag: str = "status"):
        self.log.config(state="normal")
        self.log.insert("end", text if text.endswith("\n") else text + "\n",
                        (tag if tag in LOG_TAG_COLORS else "status",))
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.config(state="disabled")

    # ---------- 会话角标：当前会话是否在自动回范围内 ----------
    def _chat_badge(self, name: str) -> tuple[str, str]:
        """返回 (文字, 颜色)：'可自动回'(绿)/'名单外·不自动回'(橙)/'补回关闭'(灰)。
        依赖 config.yaml watch 段的 catchup 开关与白/黑名单。
        """
        from wechat_mac.engine import _chat_allowed
        try:
            watch = (self._watch_cfg or {}).get("watch", {})
        except Exception:
            watch = {}
        if not watch.get("catchup_enabled", True):
            return "自动补回：关闭", "#999999"
        wl = list(watch.get("catchup_whitelist", []) or [])
        bl = list(watch.get("catchup_blacklist", []) or [])
        if _chat_allowed(name, wl, bl):
            return "自动回：启用", "#7cb342"
        return "自动回：名单外", "#ff9500"

    def _update_badge(self, name: str):
        try:
            text, color = self._chat_badge(name or "当前会话")
            self.badge_lbl.config(text=text, fg=color)
        except Exception:
            pass

    def _set_dot(self, mode: str):
        if mode != self._last_mode:
            self._last_mode = mode
            self.dot.config(fg=DOT_COLORS.get(mode, DOT_COLORS["off"]))

    # ---------- 空闲心跳（未托管也跑，证明 AI 在监看） ----------
    def _idle_loop(self):
        from wechat_mac.bridge import WeChatBridge
        b = WeChatBridge()
        while True:
            try:
                if self._watch_active:
                    time.sleep(1.2)
                    continue
                hb = b.heartbeat()
                now = time.strftime("%H:%M:%S")
                self._q.put(("hb", now, hb.get("name", "当前会话")))
                if self._last_err:
                    self._last_err = ""
                    self._q.put(("dot", "off"))
            except Exception as e:
                msg = str(e)[:60]
                if msg != self._last_err:
                    self._last_err = msg
                    self._q.put(("log", f"[心跳出错] {msg}"))
                    self._q.put(("dot", "err"))
            time.sleep(4.0)  # 微信 AX 单次扫描约 3~5 秒，心跳按此节奏

    # ---------- 队列 → UI ----------
    def _poll_ui(self):
        try:
            while True:
                item = self._q.get_nowait()
                tag = item[0]
                if tag == "status":
                    self.status_lbl.config(text=item[1])
                    # 错误后恢复正常状态 → 点亮恢复（绿=托管/灰=空闲）
                    if self._last_mode == "err" and not is_error_text(item[1]):
                        self._set_dot("on" if self._watch_active else "off")
                elif tag == "hb":
                    self.hb_lbl.config(text=item[1])
                    self.chat_lbl.config(text=f"当前会话：{item[2]}")
                    self._update_badge(item[2])
                elif tag == "dot":
                    self._set_dot(item[1])
                elif tag == "log":
                    t = item[1] if len(item) > 1 else ""
                    ttag = item[2] if len(item) > 2 else "status"
                    # 错误特征词 → 自动标红 + 顶部亮橙点
                    if ttag == "status" and is_error_text(t):
                        ttag = "error"
                        self._set_dot("err")
                    self._append_log(t, ttag)
                else:
                    self._append_log(item[1])
        except queue.Empty:
            pass
        self.root.after(200, self._poll_ui)

    # ---------- 托管开关 ----------
    def toggle(self):
        if self._watch_active:
            self._stop.set()
            self._thread.join(timeout=3)
            self._watch_active = False
            self.status_lbl.config(text="未托管")
            self.btn.config(text="开启托管")
            self._set_dot("off")
            self._append_log("[停止托管]")
            return

        from core.bot import WeChatBot
        from core.config import load_config

        self._stop = threading.Event()
        bot = WeChatBot(load_config())
        self._watch_active = True
        self._thread = threading.Thread(
            target=watch_loop,
            kwargs={"bot": bot, "interval": 1.0, "stop": self._stop,
                    "on_event": self._on_event, "on_status": self._on_status},
            daemon=True,
        )
        self._thread.start()
        self.status_lbl.config(text="托管中…")
        self.btn.config(text="关闭托管")
        self._set_dot("on")
        self._append_log("[托管已开启：跟随当前聊天框]")

    def _on_status(self, text: str):
        self._q.put(("status", text))       # 顶部状态栏
        self._q.put(("log", text, "status"))  # 日志区（错误词自动转红）

    def _on_event(self, name, msg, reply):
        t = time.strftime("%H:%M:%S")
        self._q.put(("log", f"[{t}] {name} 收到：{msg}", "event"))
        self._q.put(("log", f"[{t}]     回复：{reply}", "event"))

    def _quit(self):
        """退出程序：停止值守线程 + 销毁窗口 + 强制结束进程（保证不残留不卡住）。"""
        if self._thread is not None:
            self._stop.set()
            try:
                self._thread.join(timeout=2)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 强制退出：确保 idle 线程、值守线程、单实例锁等全部随进程清理干净
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main(auto_start: bool = True) -> int:
    FloatPanel(auto_start=auto_start).run()
    return 0