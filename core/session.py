"""会话级上下文记忆：按联系人滚动保存最近对话，供回复生成时携带"谁说了什么"的长上下文。

与 ChatMemory 的区别：
  - ChatMemory：静态加载 data/ 下的历史记录，检索相似(对方,我)对做 few-shot 风格参考
  - SessionMemory：运行期内动态累积当前每个联系人最近 N 条消息，跨轮次记忆话题，
    并在重启后从磁盘恢复（不回退到上次中断的电报）

存储：data/session_history.json，按 sender 分组，每人保留最近 max_history 条。
注意：文件名刻意用 .json 而非 .jsonl，避免被 ChatMemory.load 当历史记录扫描进去。
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

ROLE_OTHER = "对方"
ROLE_SELF = "我"


class SessionMemory:
    def __init__(self, max_history: int = 12, path: str | Path | None = None,
                 enabled: bool = True):
        self.max_history = max(int(max_history or 0), 2)
        self.enabled = enabled
        self.path = Path(path) if path else None
        # sender -> deque(("对方"|"我", text))，旧在前新在后
        self._sessions: dict[str, deque] = {}
        if self.enabled:
            self._load()

    # ---------- 数据落盘 ----------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for sender, rows in raw.items():
                if not isinstance(rows, list):
                    continue
                dq = deque(maxlen=self.max_history)
                for r in rows:
                    if isinstance(r, list) and len(r) == 2 and r[0] in (ROLE_OTHER, ROLE_SELF):
                        dq.append((r[0], str(r[1])))
                if dq:
                    self._sessions[sender] = dq
            logger.info("会话记忆已恢复：%d 个联系人", len(self._sessions))
        except Exception as e:
            logger.warning("恢复会话记忆失败：%s", e)

    def save(self) -> None:
        if not self.path or not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {s: list(dq) for s, dq in self._sessions.items() if dq}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.warning("保存会话记忆失败：%s", e)

    # ---------- 写入 ----------
    def push(self, sender: str, role: str, text: str) -> None:
        if not self.enabled:
            return
        text = (text or "").strip()
        if not text:
            return
        dq = self._sessions.setdefault(sender, deque(maxlen=self.max_history))
        dq.append((role, text))

    def push_other(self, sender: str, text: str) -> None:
        self.push(sender, ROLE_OTHER, text)

    def push_self(self, sender: str, text: str) -> None:
        self.push(sender, ROLE_SELF, text)

    # ---------- 读取 ----------
    def recent(self, sender: str, n: int | None = None) -> list[str]:
        """返回最近 n 条（默认全部）"角色: 内容" 文本，旧在前新在后。"""
        dq = self._sessions.get(sender)
        if not dq:
            return []
        n = min(n or len(dq), len(dq))
        return [f"{r}: {t}" for r, t in list(dq)[-n:]]

    def has(self, sender: str) -> bool:
        return bool(self._sessions.get(sender))

    def clear_sender(self, sender: str) -> None:
        self._sessions.pop(sender, None)

    def reset(self) -> None:
        self._sessions.clear()
        self.save()