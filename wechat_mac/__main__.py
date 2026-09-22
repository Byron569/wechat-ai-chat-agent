"""CLI：python -m wechat_mac <probe|once|run|gui>

  probe  打印微信窗口/会话名/消息区 OCR 结果（校准/排查用）
  once   读当前聊天框 → 走核心逻辑 → 回复（--dry 只读不发送）
  run    轮询值守：跟随当前聊天框自动回复
  gui    悬浮监控面板（单实例，默认自启托管）
"""
from __future__ import annotations

import sys
import time

from wechat_mac import ax


def cmd_probe() -> int:
    """纯 OCR 诊断：窗口、会话名、消息区文本、输入框坐标。"""
    from wechat_mac import ocr
    print("屏幕录制授权：", ocr.have_screen_capture())
    wx = ocr._pick_window()
    print("微信窗口：", wx)
    if not wx:
        print("找不到微信主窗口（请确认微信已打开且在屏幕可见位置）")
        return 1
    print("布局：", ocr.layout(wx))
    t0 = time.time()
    s = ocr.scan(wx)
    print(f"OCR 扫描 {time.time()-t0:.2f}s | ok={s.get('ok')} {s.get('err', '')}")
    if not s.get("ok"):
        return 1
    print("当前会话：", s["name"])
    print(f"最后气泡：{s['bubble'][:40] or '（空/图片）'}  dHash={s['hash'][:16]}")
    print("\n消息区文本（自上而下，最新在底部）：")
    for t, xc in s["msges"]:
        side = "右(自己)" if xc > 0.6 else "左(对方)" if xc < 0.4 else "中"
        print(f"  [{side}] {t[:60]}")
    return 0


def cmd_once(dry: bool) -> int:
    """跟随模式：不切换会话，直接处理当前打开的聊天框。"""
    from core.bot import WeChatBot
    from core.config import load_config

    bot = WeChatBot(load_config())
    ax.activate()
    from wechat_mac.bridge import WeChatBridge
    b = WeChatBridge()
    s = b.scan()
    name = s.get("name") or "当前会话"
    print(f"当前会话：{name}  [DRY 不发送]" if dry else f"当前会话：{name}")
    previews = [t for t, _ in (s.get("msges") or [])]
    print("读到的最近消息：")
    for ln in previews:
        print("  |", ln[:80])
    reply = None
    if previews:
        reply = bot.handle_message({"sender_id": "wechat", "nickname": name, "text": previews[-1]})
        print("机器人回复：", reply)
    if reply and not dry:
        b.send(reply)
        print("已发送。")
    elif dry and reply:
        print("（DRY 模式，未发送）")
    else:
        print("未发送回复（无需回复或没有新消息）")
    return 0


def cmd_run(interval: float) -> int:
    """跟随模式：单实例后台值守（不推荐与 gui 同开，锁会拦）。"""
    from wechat_mac.lock import ensure_single
    ensure_single("值守")
    from core.bot import WeChatBot
    from core.config import load_config
    from wechat_mac.engine import watch_loop

    print("跟随当前聊天框：纯 OCR 检测（约 1s/轮），发现新消息即回（Ctrl+C 停止）")
    bot = WeChatBot(load_config())

    def _on_event(name, msg, reply):
        print(f"[{time.strftime('%H:%M:%S')}] {name}：{msg}")
        print(f"[{time.strftime('%H:%M:%S')}] 回复：{reply}")

    watch_loop(bot, interval=interval, on_event=_on_event)
    return 0


def cmd_gui(auto_start: bool = True) -> int:
    from wechat_mac.lock import ensure_single
    ensure_single("监控面板")
    from wechat_mac.gui import FloatPanel
    FloatPanel(auto_start=auto_start).run()
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="微信 OCR 自动回复")
    parser.add_argument("command", choices=["probe", "once", "run", "gui"])
    parser.add_argument("--dry", action="store_true", help="once：只读消息和生成回复，不真正发送")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="值守轮询间隔（秒），OCR 扫描本身约 1s 为主导")
    parser.add_argument("--noauto", action="store_true",
                        help="gui 不自动开启托管（默认自启）")
    args = parser.parse_args()
    if args.command == "probe":
        return cmd_probe()
    if args.command == "once":
        return cmd_once(args.dry)
    if args.command == "gui":
        return cmd_gui(not args.noauto)
    return cmd_run(args.interval)


if __name__ == "__main__":
    sys.exit(main())