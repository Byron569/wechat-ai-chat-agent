"""硬规则与风控：不依赖外部 API，避免该回的不回、不该回的乱回、回太频被封。"""
from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class RuleResult:
    action: str  # "reply" 必定回复 | "skip" 不回复 | "human" 转人工 | "fixed" 固定话术
    reason: str = ""
    fixed_text: str = ""
    aggressive: bool = False   # 命中"挑衅/脏话"规则（用于争执抑制）


class KeywordRules:
    """关键词硬规则，命中即短路，跳过 JEV 判断（always_reply 除外仍需生成）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def check(self, text: str) -> RuleResult | None:
        c = self.cfg
        # 固定话术优先级最高：命中直接回这句
        for kw, reply in c.get("reply_map", {}).items():
            if kw.lower() in text.lower():
                return RuleResult("fixed", f"命中 reply_map: {kw}", fixed_text=reply)
        for kw in c.get("always_reply", []):
            if kw.lower() in text.lower():
                return RuleResult("reply", f"命中 always_reply: {kw}")
        for kw in c.get("aggressive_reply", []):
            if kw.lower() in text.lower():
                return RuleResult("reply", f"命中脏话/挑衅 aggressive_reply: {kw}", aggressive=True)
        for kw in c.get("never_reply", []):
            if kw.lower() in text.lower():
                return RuleResult("skip", f"命中 never_reply: {kw}")
        for kw in c.get("advertisement", []):
            if kw.lower() in text.lower():
                return RuleResult("skip", f"命中广告黑名单: {kw}")
        for kw in c.get("human_handoff", []):
            if kw.lower() in text.lower():
                return RuleResult("human", f"命中 human_handoff: {kw}")
        for p in c.get("ignore_startswith", []):
            if text.startswith(p):
                return RuleResult("skip", f"前缀忽略: {p}")
        for s in c.get("ignore_contains", []):
            if s in text:
                return RuleResult("skip", f"包含忽略: {s}")
        return None


class RiskControl:
    """回复频率风控：全局间隔 + 单联系人冷却 + 随机延迟 + 每日上限。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.last_global = 0.0
        self.last_by_contact: dict[str, float] = defaultdict(float)
        self.reply_count = 0
        self.date = time.strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.reply_count = 0

    def before_reply_wait(self, contact_id: str) -> float | None:
        """返回发送前需要等待的秒数（含模拟真人的随机抖动）。
        返回 None 表示已超出每日上限，本轮放弃回复。"""
        self._roll_day()
        if self.reply_count >= self.cfg.get("max_daily_replies", 200):
            return None

        now = time.time()
        wait = 0.0
        wait = max(wait, self.cfg.get("min_interval", 0.0) - (now - self.last_global))
        wait = max(wait, self.cfg.get("per_contact_cooldown", 0.0) - (now - self.last_by_contact[contact_id]))
        wait += random.uniform(0, self.cfg.get("reply_jitter_max", 0.0))
        return wait

    def note_reply(self, contact_id: str) -> None:
        now = time.time()
        self.last_global = now
        self.last_by_contact[contact_id] = now
        self.reply_count += 1