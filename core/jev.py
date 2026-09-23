"""JEV 客户端：一次性并行返回结构化判断（choice / score / noul）。

支持两种接入源：
- typesafe：官方 https://api.typesafe.ai/v1/systemone（用 TYPESAFE_API_KEY）
- openrouter：https://openrouter.ai/api/v1/decisions（用 OPENROUTER_API_KEY，模型 typesafe/jev-1.13）
"""
from __future__ import annotations

import json
import os
import re

import requests

# 官方 TypeSafe 端点；openrouter 端点见 .env 的 OPENROUTER_DECISIONS_URL
DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def default_questions() -> dict:
    """把微信消息的决策点全部塞进一次调用。问题 id 即返回结果的 key。"""
    return {
        "should_reply": {
            "type": "noul",
            "instructions": "结合 state 中 recent_messages_with_older_first（最近几条消息的语境）与这一条最新消息，判断作为被 AI 代替回复的真人，此刻是否应该礼貌地回复？",
            "criteria": {
                "true": "正常对话：问候、提问、闲聊、求助、对方在等回应，或语境需要接话",
                "false": "不需要回复：广告推销、验证码、无意义系统消息、对方自说自话、话题已自然结束",
            },
        },
        "is_ad": {
            "type": "noul",
            "instructions": "这条消息是广告、营销、拉群、诈骗或不明链接吗？",
            "criteria": {
                "true": "广告营销、推广链接、刷单兼职、求关注转发",
                "false": "正常社交消息",
            },
        },
        "needs_human": {
            "type": "noul",
            "instructions": "这件事是否必须由真人本人处理，AI 代答有风险（涉及钱、隐私、承诺、法律、紧急事务）？",
            "criteria": {
                "true": "转账、退款、密码验证码、合同协议、威胁恐吓、突发紧急事件",
                "false": "日常问答、闲聊等 AI 可以代答",
            },
        },
        "intent": {
            "type": "choice",
            "instructions": "对方发这条消息的核心意图是什么？",
            "criteria": {
                "chat": "普通闲聊、问候、唠家常",
                "question": "提问、寻求建议或答案",
                "request": "请求帮忙做某件事",
                "complaint": "投诉、表达不满或指责",
                "gratitude": "表达感谢",
                "greeting_only": "只是打个招呼，没有实质性内容",
                "nonsense": "乱码、纯表情、测试消息、无意义内容",
            },
        },
        "emotion": {
            "type": "score",
            "instructions": "对方当前的【情绪状态】在 1 到 5 分之间打几分？",
            "criteria": [
                "很不满、生气、急躁",
                "有点不满或催促",
                "平静、中性",
                "比较友好、轻松",
                "非常热情、开心、熟络",
            ],
        },
        "my_emotion": {
            "type": "score",
            "instructions": "你是被代替回复的真人本人，读完对方这条消息后你【自己的真实情绪反应】在 1 到 5 分之间打几分？（对方骂你你会烦、对方夸你你会开心、平时中性）",
            "criteria": [
                "很生气、想发作",
                "有点不耐烦、糟心",
                "平静、中性",
                "比较愉悦、轻松",
                "很开心、心情大好",
            ],
        },
        "urgency": {
            "type": "score",
            "instructions": "这件事的【紧急程度】在 1 到 5 分之间打几分？",
            "criteria": [
                "完全不急，闲聊而已",
                "不太急，可以慢慢回",
                "一般，当天回即可",
                "比较急，希望尽快得到答复",
                "非常紧急，等不及",
            ],
        },
    }


class JevClient:
    def __init__(self, api_key: str | None = None, endpoint: str | None = None,
                 model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or _first_env("TYPESAFE_API_KEY", "OPENROUTER_API_KEY") or ""
        self.endpoint = endpoint or _first_env("TYPESAFE_BASE_URL", "OPENROUTER_DECISIONS_URL") or DEFAULT_ENDPOINT
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.session = requests.Session()  # requests 会自动读取环境里的 HTTPS_PROXY

    def decide(self, state: str | dict, questions: dict | None = None) -> dict:
        """返回 answers：{ question_id: {"type":..., "choice"/"score"/"noul":..., ...} }"""
        payload = {
            "model": self.model,
            "state": state if isinstance(state, str) else json.dumps(state, ensure_ascii=False),
            "questions": questions or default_questions(),
        }
        resp = self.session.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["answers"]


class OllamaJevClient:
    """本地/手机部署的迷你 JEV（Ollama 协议 /api/chat），免费、不依赖代理。

    decide() 接口与 JevClient 保持一致，bot 可直接替换使用。
    """

    def __init__(self, base_url: str | None = None, model: str = "minijev", timeout: float = 60.0):
        self.base_url = (base_url or _first_env("OLLAMA_BASE") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model
        self.timeout = timeout

    def decide(self, state: str | dict, questions: dict | None = None) -> dict:
        """迷你 JEV 一次性输出 6 维判定 JSON（该不该回/广告/转人工/意图/情绪/紧急）。

        含容错：JSON 解析失败或字段缺失时回退保守默认值，不会崩。
        """
        if isinstance(state, dict):
            latest = state.get("message", "")
            recent = state.get("recent_messages_with_older_first") or []
        else:
            latest, recent = state, []
        ctx = "；".join(str(x)[:80] for x in recent[-3:])
        sys_prompt = (
            "你是微信回复的决策器。结合最近对话与最新消息，一次输出以下 7 个判断的 JSON，"
            "不要任何其它文字。字段与取值："
            "should_reply(0~1 是否该真人回复)、is_ad(0~1 是否广告/营销/诈骗)、"
            "needs_human(0~1 是否必须真人处理，涉及钱/隐私/承诺/紧急)、"
            "intent(chat|question|request|complaint|gratitude|nonsense，对方真实意图)、"
            "emotion(1~5，1=很生气 5=很开心，对方情绪)、"
            "my_emotion(1~5，1=很生气想发作 5=很开心，被代替回复的真人读完这条后的情绪反应)、"
            "urgency(1~5，1=不急 5=非常急)。"
            '示例：{"should_reply":0.9,"is_ad":0.02,"needs_human":0.1,'
            '"intent":"question","emotion":3,"my_emotion":3,"urgency":2}'
        )
        user_msg = f"最近对话：{ctx or '（无）'}\n最新消息：{latest}"
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
        }
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        content = (resp.json()["message"]["content"] or "").strip()

        # ---- 容错解析：剥代码块 → 取第一个 {…} 块 → 数值清洗 ----
        data: dict = {}
        text = re.sub(r"^```(json)?", "", content, flags=re.M).strip()
        text = re.sub(r"```$", "", text).strip()
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = {}
        if not isinstance(data, dict):
            data = {}

        def _f(key: str, default: float) -> float:
            v = data.get(key, default)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return default
            if key in ("emotion", "my_emotion", "urgency"):
                return max(1.0, min(5.0, v))
            return max(0.0, min(1.0, v))

        intent = str(data.get("intent", "chat") or "chat")
        if intent not in ("chat", "question", "request", "complaint", "gratitude", "nonsense"):
            intent = "chat"

        return {
            "should_reply": {"type": "noul", "noul": _f("should_reply", 0.5)},
            "is_ad": {"type": "noul", "noul": _f("is_ad", 0.0)},
            "needs_human": {"type": "noul", "noul": _f("needs_human", 0.0)},
            "intent": {"type": "choice", "choice": intent},
            "emotion": {"type": "score", "score": _f("emotion", 3.0)},
            "my_emotion": {"type": "score", "score": _f("my_emotion", 3.0)},
            "urgency": {"type": "score", "score": _f("urgency", 2.0)},
        }