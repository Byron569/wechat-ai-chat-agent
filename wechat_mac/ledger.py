"""消息已处理台账（SQLite）：按会话 + 文本记录"已回复 / 判定不回"，防止同一消息反复消费。

背景：
  catchup（打开对话自动补回）每次切框都会判定一次"最后一条对方消息"。
  JEV 判定"不回"后，最后一条仍是对方 → 反复切回同一会话会重复判定、白烧 token。
  台账记录 (会话名, 文本, 动作, 时间)，窗口内同文本不再重复判定。

边界说明：
  - 台账只记"机器人自己的动作"；真人手动回复后最后一条消息变右侧，
    catchup 的归属判定（_catchup_target）会自然停手，不依赖台账。
  - 窗口期内对方重发同文本（rare）会被当作已处理跳过；窗口可配置。
  - SQLite 为标准库、单文件；项目已有单实例锁保证单写者，无并发担忧。
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

RETENTION_SEC = 24 * 3600   # 记录保留时长（超过即清理，防表无限膨胀）


def _text_same(a: str, b: str) -> bool:
    """忽略空白的近似比对（与 engine._same_text 同思路，OCR 有抖动不能精确相等）。"""
    a = re.sub(r"\s+", "", (a or "")).strip()[:200]
    b = re.sub(r"\s+", "", (b or "")).strip()[:200]
    if not a or not b:
        return False
    if min(len(a), len(b)) <= 8:
        return a in b or b in a
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= 0.8


class ReplyLedger:
    """SQLite 台账。created(fn)/mark(replied|skipped)/recently(window 内是否已处理)。"""

    def __init__(self, db_path: str | Path):
        self.db = Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS replied("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "chat TEXT NOT NULL,"
            "text TEXT NOT NULL,"
            "action TEXT NOT NULL,"          # 'replied' 已发回复 | 'skipped' 判定不回
            "ts REAL NOT NULL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replied_chat ON replied(chat)")
        self._conn.commit()

    def __del__(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def mark(self, chat: str, text: str, action: str = "replied") -> None:
        """记录一条已处理：先清该会话过期旧行，再插入。"""
        now = time.time()
        try:
            self._conn.execute("DELETE FROM replied WHERE chat=? AND ts<?",
                               (chat, now - RETENTION_SEC))
            self._conn.execute(
                "INSERT INTO replied(chat, text, action, ts) VALUES (?,?,?,?)",
                (chat, (text or "")[:200], action, now))
            self._conn.commit()
        except sqlite3.Error:
            pass   # 台账失败不阻断值守主流程

    def recently(self, chat: str, text: str, window_sec: float) -> bool:
        """窗口内该会话是否处理过近似文本。window_sec<=0 视为关闭去重。"""
        if not text or window_sec <= 0:
            return False
        now = time.time()
        try:
            rows = self._conn.execute(
                "SELECT text FROM replied WHERE chat=? AND ts>=?",
                (chat, now - window_sec)).fetchall()
        except sqlite3.Error:
            return False
        return any(_text_same(text, r[0]) for r in rows)