"""微信桥接层 v3：纯 OCR 读消息，AX 只保留发送所需的点击/剪贴板操作。

读消息全部走 ocr.scan()（CGWindowList 定位窗口 + Vision OCR + dHash），
不碰辅助功能树，避免 AX 每节点 ~65ms 的天花板（原 3~7s/轮 → 现在 ~1s）。

发送（输出的末端操作）仍用系统级点击+剪贴板粘贴（AX 无更优替代，且
用户要求 AI 输出聊天内容不动，发送链路保持不变）。
"""
from __future__ import annotations

import time

from wechat_mac import ax, ocr


class WeChatBridge:
    def __init__(self):
        self._last_input_xy: tuple[int, int] | None = None
        self._win: tuple[int, int, int, int] | None = None

    # ---------- 底层 ----------
    def _layout(self) -> dict:
        if self._win is None:
            self._win = ocr._pick_window()
        if not self._win:
            raise RuntimeError("找不到微信窗口（请确认微信已打开且在前台）")
        return ocr.layout(self._win)

    # ---------- 认知（纯 OCR） ----------
    def scan(self, win: tuple | None = None, scale: float = 1.0) -> dict:
        """一次纯 OCR 全扫描：name（会话名）/ bubble（最新气泡）/ hash / msges。

        scale：消息区放大倍数（>1 提升小字识别，透传给 ocr.scan）。
        """
        w = win or self._win
        s = ocr.scan(w, scale=scale)
        if s.get("ok") and s.get("win"):
            self._win = s["win"]        # 窗口可能被拖动，刷新
            if s["layout"]["input"]:
                self._last_input_xy = tuple(s["layout"]["input"])
        return s

    def probe_light(self, win: tuple | None = None) -> dict:
        """常态快探：截图+dHash 指纹（无 OCR，≈0.12s）。"""
        w = win or self._win
        s = ocr.probe_light(w)
        if s.get("ok") and s.get("win"):
            self._win = s["win"]
        return s

    def heartbeat(self, need_bubble: bool = False) -> dict:
        """兼容旧接口：{name, bubble}。纯 OCR 版无视 need_bubble，统一全量扫描。"""
        s = self.scan()
        if not s.get("ok"):
            return {"name": "当前会话", "bubble": ""}
        return {"name": s.get("name") or "当前会话",
                "bubble": s.get("bubble") or ""}

    def read_previews(self, n: int = 8) -> list[str]:
        """读最近最多 n 条消息文本（OCR 消息区文本行，作为判断上下文）。"""
        s = self.scan()
        if not s.get("ok"):
            return []
        out = [t for t, _ in (s.get("msges") or [])]
        return out[-n:]

    def current_chat_name(self) -> str:
        return self.scan().get("name") or "当前会话"

    # ---------- 发送（AX 末尾操作，保持不变） ----------
    def send(self, text: str) -> None:
        lay = self._layout()
        xy = self._last_input_xy or lay["input"]
        ax.set_clipboard(text)
        time.sleep(0.1)
        ax.click_at(*xy)
        time.sleep(0.4)
        ax.keystroke("v", using="command down")
        time.sleep(0.2)
        ax.key_code(36)  # 回车发送
        time.sleep(0.8)