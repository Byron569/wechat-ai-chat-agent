"""入口：simulate 模拟收消息 / server 起 Webhook（未来接 wxauto、wechaty）。

用法：
    python main.py simulate            # 交互式模拟
    python main.py simulate --file msgs.jsonl   # 回放消息文件，每行一个 JSON
    python main.py server --host 127.0.0.1 --port 8000   # Webhook 服务
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from core.bot import WeChatBot
from core.config import ROOT, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def build_bot(config_path: str | None = None) -> WeChatBot:
    return WeChatBot(load_config(config_path))


def show_welcome() -> None:
    print("=" * 56)
    print(" JEV + LLM(默认小米 MiMo) 微信自动回复 · 核心逻辑模拟器")
    print(" 直接输入一条消息回车即可看到机器人的处理结果")
    command_hint = "发送 /quit 退出；/stats 查看风控状态"
    print(f" {command_hint}")
    print("=" * 56)


def cmd_simulate(config_path: str | None, dump: str | None) -> int:
    bot = build_bot(config_path)
    show_welcome()

    lines = open(dump, encoding="utf-8") if dump else sys.stdin
    try:
        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/stats":
                print(f"  [风控] 今日已回复 {bot.risk.reply_count} 条")
                continue

            payload = None
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = None
            if not payload:
                payload = {"sender_id": "模拟好友", "nickname": "模拟好友", "text": line}

            print(f"\n  >> {payload.get('text', '')}")
            reply = bot.handle_message(payload)
            print(f"  << {reply}" if reply else "  (不回复)")
    finally:
        if dump:
            lines.close()
    return 0


def cmd_server(config_path: str | None, host: str, port: int) -> int:
    from fastapi import FastAPI
    import uvicorn

    bot = build_bot(config_path)
    app = FastAPI(title="JEV+LLM 微信自动回复核心", version="0.1.0")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/webhook/wechat")
    def webhook(payload: dict):
        """接收一条微信消息，返回 {"reply": ...}；reply 为 null 表示不回复。
        将来 wxauto / wechaty 等真实接入方把收到的消息 POST 到这里即可。"""
        reply = bot.handle_message(payload)
        return {"reply": reply}

    print(f"Webhook 已启动：POST http://{host}:{port}/webhook/wechat")
    uvicorn.run(app, host=host, port=port)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JEV + LLM 微信自动回复核心")
    parser.add_argument("--config", help="config.yaml 路径（默认项目根目录）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sim = sub.add_parser("simulate", help="交互式模拟收消息")
    p_sim.add_argument("--file", help="回放消息文件（每行一个 JSON 消息）")

    p_srv = sub.add_parser("server", help="启动 Webhook 服务")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.command == "simulate":
        return cmd_simulate(args.config, args.file)
    return cmd_server(args.config, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())