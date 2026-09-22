"""LLM 生成客户端：默认接小米 MiMo（OpenAI 兼容协议），也兼容 DeepSeek、Ollama 等任意兼容服务。"""
from __future__ import annotations

import os

from openai import OpenAI


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


# 按量付费；若买了 Token Plan 换成 https://token-plan-cn.xiaomimimo.com/v1
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"


class LLMClient:
    """OpenAI 兼容的对话补全客户端。

    配置读取顺序：显式参数 → LLM_* 环境变量 → MIMO_*/DEEPSEEK_* → 默认 MiMo。
    provider="ollama" 时走手机/本机 Ollama（复用 .env 的 OLLAMA_BASE），无需 API key。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = "mimo-v2.5-pro", temperature: float = 0.7,
                 max_tokens: int = 200, enable_thinking: bool | None = None,
                 presence_penalty: float = 0.0, frequency_penalty: float = 0.0,
                 provider: str = "mimo"):
        if provider == "ollama":
            ollama_base = _first_env("OLLAMA_BASE") or "http://127.0.0.1:11434"
            base_url = base_url or f"{ollama_base.rstrip('/')}/v1"
            api_key = api_key or "ollama"
        self.provider = provider
        self.api_key = api_key or _first_env("LLM_API_KEY", "MIMO_API_KEY", "DEEPSEEK_API_KEY") or ""
        self.base_url = base_url or _first_env(
            "LLM_BASE_URL", "MIMO_BASE_URL", "DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        # httpx 会自动读取环境里的 HTTPS_PROXY
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, system: str, user: str) -> str:
        kwargs = dict(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        # 重复惩罚：仅在配置了非 0 值时传，避免个别兼容服务不认这两个参数
        if self.presence_penalty:
            kwargs["presence_penalty"] = self.presence_penalty
        if self.frequency_penalty:
            kwargs["frequency_penalty"] = self.frequency_penalty
        # MiMo 的思考模式开关不是 OpenAI 标准参数，走 extra_body；仅对 mimo 模型生效
        if self.enable_thinking is not None and "mimo" in self.model.lower():
            kwargs["extra_body"] = {"enable_thinking": self.enable_thinking}

        resp = self.client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()