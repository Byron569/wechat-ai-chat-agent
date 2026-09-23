"""编排核心：接收微信消息 → 硬规则 → JEV 决策 → DeepSeek 生成 → 返回回复。"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path

from core.config import ROOT
from core.jev import JevClient, default_questions
from core.llm import LLMClient
from core.memory import ChatMemory
from core.rules import KeywordRules, RiskControl
from core.session import SessionMemory

logger = logging.getLogger(__name__)

INTENT_LABELS = {
    "chat": "闲聊",
    "question": "提问",
    "request": "请求帮忙",
    "complaint": "投诉/不满",
    "gratitude": "感谢",
    "greeting_only": "打招呼",
    "nonsense": "无意义",
}


# ---------- 回复后处理（生成后兜底洗稿，去掉 AI 味） ----------
# 常见 AI 句式/词：命中直接删掉整段词，不拦后面的内容
AI_PHRASE_RE = re.compile(
    r"(?:首先|其次|总的(?:来说|说来)|总而言之|作为(?:一个)?(?:人工智能|AI(?:助手|机器人)?)|"
    r"非常抱歉|温馨提示|希望对你有帮助|很高兴能?为你服务|还有什么可以帮(?:你|您))"
    r"[,，:：、]?"
)
# markdown 结构符号（行首列表/标题/粗斜体/代码/链接）
MARKDOWN_RE = re.compile(r"(?:^\s*(?:#{1,6}\s+|[*\-+•]\s+|\d+[.、)]\s+)|[*_`~\[\]]|\*\*)", re.MULTILINE)
# emoji / 特殊符号（含变体选择符）
EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]"
    r"[\uFE00-\uFE0F\u200D]?"
)
# 中文/英文/数字混合长度（用于超长截断判断）
LENGTH_CAP = 80

# 针对本地小模型（手机的 blossom 4B 等）的额外人设强化：
# 小模型容易把"发送者/自己"写进回复、复述对方的话、冒客服腔，这里硬性压制
SMALL_MODEL_HINT = """
【针对本机小模型的额外铁律（必须遵守）】
1. 绝对不要复述、照抄对方的话，直接回答对方问的问题。
2. 回复里禁止出现对方名字、自己的名字、@某人和任何"上下文/发送者/辅助判断"字样。
3. 禁止客服腔的"随时待命/为您服务/有什么可以帮您/请随时联系我"等表达。
4. 能一发字绝不十个字，一句讲完不加后缀。
"""


def _remove_markdown(s: str) -> str:
    return MARKDOWN_RE.sub("", s)


def _remove_emojis(s: str) -> str:
    return EMOJI_RE.sub("", s)


def _remove_ai_phrases(s: str) -> str:
    return AI_PHRASE_RE.sub("", s)


def _trim_clean(s: str) -> str:
    """清理：去掉 markdown/emoji/AI 句式和多余标点，压成单行。"""
    s = _remove_markdown(s)
    s = _remove_emojis(s)
    s = _remove_ai_phrases(s)
    # 清理残留的孤立标点（如「，我…」开头逗号、连串标点）
    s = re.sub(r"^[,，。.!！?？、;；:：\s]+", "", s)
    s = re.sub(r"([，。.!！?？、;；:：])\1+", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cap_length(s: str) -> str:
    """超过长度上限时，从最后一个句子标点处截断，避免硬切。"""
    if len(s) <= LENGTH_CAP:
        return s
    cut = 0
    for m in re.finditer(r"[。！？!?；…]", s[:LENGTH_CAP]):
        cut = m.end()
    return (s[:cut] if cut else s[:LENGTH_CAP]).strip()


class WeChatBot:
    def __init__(self, config: dict):
        self.cfg = config
        jev_cfg = config.get("jev", {})
        llm_cfg = config.get("llm", {})
        self.jev = self._build_jev_client(jev_cfg)
        self.llm = LLMClient(
            provider=llm_cfg.get("provider", "mimo"),
            model=llm_cfg.get("model", "mimo-v2.5-pro"),
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 200),
            enable_thinking=llm_cfg.get("enable_thinking"),
            presence_penalty=llm_cfg.get("presence_penalty", 0.0),
            frequency_penalty=llm_cfg.get("frequency_penalty", 0.0),
            effort=llm_cfg.get("effort"),
        )
        self.rules = KeywordRules(config.get("rules", {}))
        self.risk = RiskControl(config.get("risk", {}))
        # 争执抑制：每个联系人的脏话计数与时间戳，防止"对骂死循环"
        self._argue: dict[str, dict] = {}
        self._argue_max = int(config.get("risk", {}).get("argue_max_replies", 3))
        self._argue_timeout = float(config.get("risk", {}).get("argue_timeout", 300))
        # 败龙（你小号）的发言最像你的真实语气：默认只采集存档、不参与学习，
        # 避免把测试期夹杂的旧话题（如"传家宝"）检索进回复参考。
        bailong_cfg = config.get("bailong", {}) or {}
        self._bailong_learn = bool(bailong_cfg.get("learn", False))
        self._bailong_excluded = set()
        if bailong_cfg.get("self_name") and bailong_cfg.get("style_file"):
            self._bailong_excluded = {Path(bailong_cfg["style_file"]).name}
        # memory 构建时排除败龙存档文件（learn=false 不学习它）
        self.memory = self._build_memory(config.get("memory", {}), exclude=self._bailong_excluded)
        self._bailong_style_path = None
        if bailong_cfg.get("self_name"):
            style_p = ROOT / bailong_cfg.get("style_file", "data/bailong_style.jsonl")
            self._bailong_style_path = style_p
            # learn=true 才把败龙当"我"身份并加载样本学习
            if self.memory and self._bailong_learn:
                self.memory.add_self_identity(bailong_cfg["self_name"])
                if style_p.exists():
                    try:
                        self.memory.load(style_p)
                    except Exception as e:
                        logger.warning("加载败龙样本失败：%s", e)
        # 会话级上下文记忆：按联系人记录最近对话，长对话不丢上文
        session_cfg = config.get("session", {})
        self.session = SessionMemory(
            max_history=session_cfg.get("max_history", 12),
            path=(ROOT / session_cfg["path"]) if session_cfg.get("path") else None,
            enabled=session_cfg.get("enabled", True),
        ) if session_cfg.get("enabled", True) else None
        # 生成回复时只取最近 N 条进 prompt（防旧话题漂移）；切框是否用当前屏重建上下文
        self._ctx_size = max(int(session_cfg.get("context_size", 6) or 6), 2)
        self._reset_on_switch = bool(session_cfg.get("reset_on_switch", True))
        # 当前会话的临时提示词（对方身份/话题；仅内存，切框即失效）
        self._temp_prompt = ""
        self._temp_prompt_for: str | None = None
        # 预热 JEV 阈值
        self.jev_thresholds = {
            "should_reply_min": jev_cfg.get("should_reply_min", 0.35),
            "is_ad_max": jev_cfg.get("is_ad_max", 0.75),
            "needs_human_min": jev_cfg.get("needs_human_min", 0.85),
            "urgency_human": jev_cfg.get("urgency_human", 4),
        }
        self.human_templates = config.get("human_templates") or ["这件事需要老王本人确认，他回来后第一时间联系你。"]

    @staticmethod
    def _build_jev_client(jev_cfg: dict) -> JevClient:
        """按 config 的 jev.provider 选择 JEV 接入源：
        typesafe(官方) / openrouter / jevai(中转站) / opencode(OpenCode Zen，含免费档 jev-1.13-free)。"""
        provider = jev_cfg.get("provider", "typesafe")
        if provider == "openrouter":
            return JevClient(
                api_key=os.getenv("OPENROUTER_API_KEY"),
                endpoint=os.getenv("OPENROUTER_DECISIONS_URL") or "https://openrouter.ai/api/v1/decisions",
                model=jev_cfg.get("model", "typesafe/jev-1.13"),
            )
        if provider == "jevai":
            return JevClient(
                api_key=os.getenv("JEVAI_API_KEY"),
                endpoint=os.getenv("JEVAI_BASE_URL") or "https://jev-ai.pro/api/v1/systemone",
                model=jev_cfg.get("model", "jev-latest"),
            )
        if provider == "opencode":
            return JevClient(
                api_key=os.getenv("OPENCODE_API_KEY"),
                endpoint=os.getenv("OPENCODE_JEVA_URL") or "https://opencode.ai/zen/v1/systemone",
                model=jev_cfg.get("model", "jev-1.13"),
            )
        if provider == "ollama":
            from core.jev import OllamaJevClient
            return OllamaJevClient(
                base_url=os.getenv("OLLAMA_BASE") or "http://127.0.0.1:11434",
                model=jev_cfg.get("model", "minijev"),
            )
        return JevClient(model="jev-latest")

    @staticmethod
    def _build_memory(mem_cfg: dict, exclude: set | None = None) -> ChatMemory | None:
        """按 config 的 memory 段加载聊天记录（data 目录下的 *.txt / *.jsonl）。

        exclude：跳过指定文件名（如败龙存档文件，避免被当历史学习）。
        """
        if not mem_cfg.get("enabled", True):
            return None
        mem_dir = ROOT / mem_cfg.get("dir", "data")
        if not mem_dir.exists():
            return None
        memory = ChatMemory(
            my_name=mem_cfg.get("my_name", "老王"),
            max_examples=mem_cfg.get("max_examples", 3),
        )
        memory.load(mem_dir, exclude=exclude)
        return memory

    # ---------- 对外主入口 ----------
    def set_temp_prompt(self, nickname: str, text: str) -> None:
        """设置【当前会话】的临时提示词（对方身份/当前话题），仅内存、不落盘。

        切换会话（clear_temp_prompt）或重启后即失效——目的是短期的"当前对话
        背景设定"，让生成的回复更贴合对方身份与话题。
        """
        self._temp_prompt_for = nickname
        self._temp_prompt = (text or "").strip()

    def clear_temp_prompt(self) -> None:
        """切换到其它对话框时清空临时提示词（临时性）。"""
        self._temp_prompt = ""
        self._temp_prompt_for = None

    def temp_prompt(self) -> tuple[str | None, str]:
        """返回 (绑定的会话名, 提示词文本)（面板显示用）。"""
        return self._temp_prompt_for, self._temp_prompt

    def reset_chat(self, nickname: str, msges: list) -> None:
        """切框时用当前屏幕消息重建该会话上下文（防旧话题漂移，翻旧账）。

        msges: ocr 行 [(文本, x中心), ...]（自上而下）。左<0.45=对方、右>0.55=自己、
        居中行（转账/系统提示）忽略。由 engine 在切框轮调用。
        """
        if not self.session or not self._reset_on_switch:
            return
        from core.session import ROLE_OTHER, ROLE_SELF
        rows: list[tuple[str, str]] = []
        for item in msges or []:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            t, xc = item[0], item[1]
            t = (str(t) or "").strip()
            if not t:
                continue
            try:
                xf = float(xc)
            except (TypeError, ValueError):
                continue
            if xf > 0.55:
                rows.append((ROLE_SELF, t))
            elif xf < 0.45:
                rows.append((ROLE_OTHER, t))
            # 居中带（0.45~0.55）忽略：转账/红包/系统提示不是上下文消息
        if rows:
            self.session.rebuild(nickname, rows)
            self.session.save()

    def handle_message(self, msg: dict) -> str | None:
        """输入消息 dict，返回要发送的回复文本；返回 None 表示不回复。

        消息约定字段：sender_id、nickname、is_group、text（msg_type 可选）。
        """
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        sender_id = msg.get("sender_id") or msg.get("nickname") or "unknown"
        nickname = msg.get("nickname") or msg.get("sender_id") or "unknown"

        # 先把对方这条消息记进会话，后续生成回复时能带上完整上文
        if self.session:
            self.session.push_other(nickname, text)

        forced_reply = False
        rule = self.rules.check(text)
        if rule:
            if rule.action == "skip":
                logger.info("不回复 %s：%s", sender_id, rule.reason)
                return None
            if rule.action == "fixed":
                return self._fixed_reply(rule.fixed_text, nickname)
            if rule.action == "human":
                return self._human_reply(nickname)
            forced_reply = True  # always_reply/aggressive：仍然跑 JEV 但不过滤
            logger.info("强制回复 %s：%s", sender_id, rule.reason)
            # 争执抑制：脏话只允许顶着回 N 句，避免对骂死循环
            if rule.aggressive and not self._argue_gate(nickname):
                logger.info("争执抑制：已回顶 %d 句，本句不再追加（%s）", self._argue_max, sender_id)
                return None
        else:
            # 对方没再挑衅（正常话）→ 视为争执结束，清空该联系人计数
            self._argue.pop(nickname, None)

        # 1. JEV 决策（失败时用保守默认值兜底，保证不哑火）
        answers = self._decide(msg)
        if answers is None:
            answers = {
                "should_reply": {"noul": 0.5},
                "is_ad": {"noul": 0.0},
                "needs_human": {"noul": 0.0},
                "urgency": {"score": 1.0},
                "emotion": {"score": 3.0},
                "my_emotion": {"score": 3.0},
                "intent": {"choice": "chat"},
            }

        if not forced_reply:
            if self._skip_by_jev(answers, text):
                return None
            if self._needs_human(answers):
                return self._human_reply(nickname)

        # 2. 生成回复
        reply = self._generate_reply(msg, answers)
        if not reply:
            return None

        # 3. 风控：发送前等待（模拟真人节奏），判断是否真的发送
        wait = self.risk.before_reply_wait(sender_id)
        if wait is None:
            logger.warning("已达每日回复上限，放弃回复 %s", sender_id)
            return None
        if wait > 0:
            logger.info("风控等待 %.1fs 后回复 %s", wait, sender_id)
            time.sleep(wait)
        self.risk.note_reply(sender_id)
        # 发送的回复也记进会话，形成完整"对方→我"轮次
        self._remember_self(nickname, reply)
        # 按真人习惯排版：平时分条发、偶尔逗号连成一句
        reply = self._style_format(reply)
        return reply

    # ---------- 内部逻辑 ----------
    def _decide(self, msg: dict) -> dict | None:
        state = {
            "sender": msg.get("nickname") or msg.get("sender_id", "unknown"),
            "is_group": bool(msg.get("is_group", False)),
            "message": msg.get("text", ""),
            "message_type": msg.get("msg_type", "text"),
        }
        # 最近几条消息作为上下文，让 JEV 结合语境判断
        context = msg.get("context") or []
        if context:
            state["recent_messages_with_older_first"] = list(context)
        try:
            return self.jev.decide(state)
        except Exception as e:
            logger.warning("JEV 决策失败，走兜底：%s", e)
            return None

    def _skip_by_jev(self, answers: dict, _text: str) -> bool:
        t = self.jev_thresholds
        if answers.get("is_ad", {}).get("noul", 0.0) > t["is_ad_max"]:
            logger.info("JEV 判定为广告，不回复")
            return True
        if answers.get("should_reply", {}).get("noul", 0.5) < t["should_reply_min"]:
            logger.info("JEV 判定无需回复 (should_reply=%.2f)", answers["should_reply"].get("noul", 0.5))
            return True
        return False

    def _needs_human(self, answers: dict) -> bool:
        t = self.jev_thresholds
        noul = answers.get("needs_human", {}).get("noul", 0.0)
        urgency = answers.get("urgency", {}).get("score", 1.0)
        return noul > t["needs_human_min"] or urgency >= t["urgency_human"]

    def _human_reply(self, nickname: str) -> str:
        wait = self.risk.before_reply_wait(nickname)
        if wait is None:
            return None
        if wait > 0:
            time.sleep(wait)
        self.risk.note_reply(nickname)
        text = random.choice(self.human_templates)
        self._remember_self(nickname, text)
        return text

    def _fixed_reply(self, text: str, nickname: str) -> str | None:
        """固定话术同样走风控（防刷屏），再返回。"""
        wait = self.risk.before_reply_wait(nickname)
        if wait is None:
            return None
        if wait > 0:
            time.sleep(wait)
        self.risk.note_reply(nickname)
        self._remember_self(nickname, text)
        return text

    def _remember_self(self, nickname: str, text: str) -> None:
        """把"我方发出的回复"记进会话记忆并落盘（供长对话上下文连续）。"""
        if self.session:
            self.session.push_self(nickname, text)
            self.session.save()

    def _argue_gate(self, nickname: str) -> bool:
        """争执抑制门闩：一次挑衅事件允许回顶最多 argue_max 句。

        返回 True=本句可以回；False=已超上限，本句抑制不追加。
        超过 argue_timeout 秒没有新挑衅，视为新一轮，计数重置。
        """
        now = time.time()
        st = self._argue.get(nickname)
        if not st or now - st["t"] > self._argue_timeout:
            # 新一轮挑衅
            self._argue[nickname] = {"n": 1, "t": now}
            return True
        if st["n"] < self._argue_max:
            st["n"] += 1
            st["t"] = now
            return True
        # 已达上限：抑制本句，但更新时间戳保持压制
        st["t"] = now
        return False

    def _style_format(self, text: str) -> str:
        """按真人习惯排版回复：平时去掉逗号、一句话一行分条连发，极少用逗号连句。

        例：生成"在吗？老师找你 有点事" → 绝大多数 "在吗？\n老师找你有点事"，
            极少数 10% 才保留逗号连成一句。只有一句时去掉逗号单行发。
        """
        text = (text or "").strip()
        if not text:
            return text
        fmt = self.cfg.get("format", {})
        comma_prob = float(fmt.get("comma_prob", 0.10))
        min_parts = int(fmt.get("min_parts", 2))

        # 把原有换行压成空格
        text = re.sub(r"\s*\n\s*", " ", text).strip()

        # 极少情况（comma_prob）：整个保留逗号连成一句
        if random.random() < comma_prob:
            return text.rstrip("，,").strip()

        # 绝大多数：去掉所有逗号。多句按句末标点断成多行；单句去掉逗号后单行发。
        no_c = re.sub(r"[，,、]", "", text).strip()
        if re.search(r"[。！？!?；;]", no_c):
            sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", no_c) if s.strip()]
            joined = re.sub(r"\n+", "\n", "\n".join(sents)).strip("\n")
            return joined or no_c
        # 无句末标点但原本用逗号连接多句（如"睡了没，传家宝还稳着吧"）→ 逗号即断句点，拆行连发
        if re.search(r"[，,]", text):
            segs = [s.strip() for s in re.split(r"[，,]", text) if s.strip()]
            segs = [re.sub(r"[、]", "", s).strip() for s in segs]
            segs = [s for s in segs if s]
            if len(segs) >= min_parts:
                return "\n".join(segs)
        return no_c

    def _generate_reply(self, msg: dict, answers: dict) -> str | None:
        text = (msg.get("text") or "").strip()
        nickname = msg.get("nickname") or msg.get("sender_id") or "unknown"
        intent = answers.get("intent", {}).get("choice", "chat")
        emotion = answers.get("emotion", {}).get("score", 3.0)
        my_emotion = answers.get("my_emotion", {}).get("score", 3.0)
        urgency = answers.get("urgency", {}).get("score", 1.0)
        is_group = bool(msg.get("is_group", False))

        system = self.cfg.get("persona", "").strip()
        if getattr(self.llm, "provider", "mimo") == "ollama":
            system += SMALL_MODEL_HINT
        # 当前会话临时提示词（对方身份/当前话题）注入人设层，让回复更贴合
        if self._temp_prompt and nickname == self._temp_prompt_for:
            system += f"\n\n【本次对话临时背景（对方是谁/在聊什么，回复必须贴合）】\n{self._temp_prompt}\n"
        examples = self.memory.format_examples(text) if self.memory else ""

        # 注入最近对话上下文：优先用会话记忆（带"对方/我"标记，跨轮次不丢上文），
        # 会话为空（如模拟器/首次）时退回调用方传入的 context。
        # 只取最近 _ctx_size 条，防旧话题漂移污染本条回复
        context_lines = ""
        if self.session and self.session.has(nickname):
            hist = self.session.recent(nickname, n=self._ctx_size)
            if hist:
                lines = [f"{i}. {m}" for i, m in enumerate(hist, 1)]
                context_lines = "最近这段对话（按先后顺序，最后一条是对方刚发的）：\n" + "\n".join(lines) + "\n\n"
        if not context_lines:
            context = msg.get("context") or []
            if context:
                lines = [f"{i}. {m}" for i, m in enumerate(context, 1)]
                context_lines = "最近这段对话（按先后顺序，最后一条是对方刚发的）：\n" + "\n".join(lines) + "\n\n"

        user = (
            f"{examples}\n"
            f"{context_lines}"
            f"对方最新发来的微信消息：\n{text}\n\n"
            f"上下文：发送者={msg.get('nickname') or msg.get('sender_id', '未知')}，群聊={'是' if is_group else '否'}\n"
            f"辅助判断：对方意图≈{INTENT_LABELS.get(intent, intent)}，对方情绪分 {emotion:.0f}/5，"
            f"你现在的情绪分 {my_emotion:.0f}/5，紧急度 {urgency:.0f}/5\n\n"
            f"请按人设直接回复这条最新消息，只输出回复内容本身，不要任何解释。"
        )
        try:
            reply = self.llm.chat(system=system, user=user)
            if reply:
                reply = _cap_length(_trim_clean(reply)) or None
            if not reply:  # 空返回或被洗稿清空，重试一次兜底
                logger.warning("LLM 返回空内容，重试一次")
                reply = self.llm.chat(system=system, user=user)
                if reply:
                    reply = _cap_length(_trim_clean(reply)) or None
            return reply or None
        except Exception as e:
            logger.warning("LLM 生成失败：%s", e)
            return None

    def idle_ping(self, nickname: str, context: list[str] | None = None) -> str | None:
        """保活：根据最近对话，让 AI 生成一句自然的主动话术（不固定文案）。"""
        system = self.cfg.get("persona", "").strip()
        if getattr(self.llm, "provider", "mimo") == "ollama":
            system += SMALL_MODEL_HINT
        # 保活话术同样贴合当前会话的临时背景（若有）
        if self._temp_prompt and nickname == self._temp_prompt_for:
            system += f"\n\n【本次对话临时背景（对方是谁/在聊什么，话术必须贴合）】\n{self._temp_prompt}\n"
        # 优先用会话记忆拼最近对话，没有则退回调用方传入的 context
        ctx = "（没有历史消息）"
        if self.session:
            hist = self.session.recent(nickname)
            if hist:
                ctx = "；".join(hist[-6:])
        if ctx == "（没有历史消息）" and (context or [])[-3:]:
            ctx = "、".join((context or [])[-3:])
        user = (
            f"这是和「{nickname}」的最近对话：{ctx}\n\n"
            f"对方有一阵子没回消息了，请像真人一样主动发一句自然的话把话题自然接下去。\n"
            f"要求：\n"
            f"1. 从最近的对话内容里挑一个具体话题切入，禁止用「人呢/在吗/去哪了/掉厕所/丢了」这类催促找人式开场；\n"
            f"2. 如果之前已经发过催促式话术，这次必须换一种说法，绝不重复同一句式；\n"
            f"3. 一两句即可，别太热情、别像客服，只输出这句话本身。"
        )
        try:
            reply = self.llm.chat(system=system, user=user)
            if reply:
                reply = _cap_length(_trim_clean(reply)) or None
            if not reply:
                reply = self.llm.chat(system=system, user=user)
                if reply:
                    reply = _cap_length(_trim_clean(reply)) or None
            return self._style_format(reply) if reply else None
        except Exception as e:
            logger.warning("保活话术生成失败：%s", e)
            return None

    # ---------- 败龙风格采集（值守时由 engine 调用） ----------
    def record_bailong(self, nickname: str, self_text: str,
                      other_text: str | None = None) -> bool:
        """把败龙(你小号)的发言作为"我"的回复样本存进 data/bailong_style.jsonl。

        nickname 是要学的对象（默认配置里的"败龙"）；self_text 是对方对败龙说的话；
        other_text 可选指向前一条消息。这里约定：采集的样本形式为
        {sender: 自己名(other), text: other_text} 后跟 {sender: 败龙, text: self_text}，
        即"别人说 X，败龙回 Y" → 学败龙的回法。
        返回是否写入了新样本。
        """
        if not self._bailong_style_path:
            return False
        me = self.cfg.get("bailong", {}).get("self_name", "败龙")
        if nickname != me or not self_text:
            return False
        # (对方说, 败龙回) → 学败龙的回复口吻
        prev_text = (other_text or "").strip()
        if not prev_text:
            return False
        # 内容清洗：过滤系统噪声、别人名前缀、太短/纯标点，避免污染语气样本
        self_text = self_text.strip()
        self_text = re.sub(r"[\"「」“”]?(败龙|Byron)['\":\s]+", "", self_text).strip()
        self_text = re.sub(r"“?" + re.escape(me) + r"”?\s*(撤回|撒回)了一条消息", "", self_text).strip()
        if not self_text or len(self_text) < 2:
            return False
        if re.fullmatch(r"[\s，。！？!?.,、]+", self_text):
            return False
        # 多行：取首行（撤回等系统提示常占多行）
        self_text = self_text.splitlines()[0].strip()
        if len(self_text) < 2:
            return False
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            json.dumps({"sender": "对方", "text": prev_text[:120], "ts": now},
                       ensure_ascii=False),
            json.dumps({"sender": me, "text": self_text[:200], "ts": now},
                       ensure_ascii=False),
        ]
        try:
            self._bailong_style_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._bailong_style_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            # 只储存存档；learn 决定是否注入内存学习（默认 false，仅存档不学习）
            return True
        except Exception as e:
            logger.warning("记录败龙风格失败：%s", e)
            return False