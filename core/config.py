"""配置加载：.env + config.yaml"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(config_path: str | Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")  # 文件不存在时自动忽略

    path = Path(config_path) if config_path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg or {}