"""值守主循环：纯 OCR 读消息（无 AX 辅助功能树）。

流程：
  OCR 全扫描(~1s) → 读 {会话名, 最后气泡, dHash} →
    ① 消息区画面变化且不是自己的回复 → 立即判断并回复
    ② 超 fallback 秒无成功交互 → 补处理最后一条未回消息
    ③ 再无可补/补过 → 自动发保活话术（fallback 秒无人回应则再发）
  所有收发/兜底/报错都写入项目根目录 wechat_replies.log

稳定性要点：
  - dHash 指纹做"画面变化"检测：OCR 文本抖动不影响判断，亚秒感知新气泡
  - 气泡判断用最近一次 OCR 文本（避免乱码噪声进决策链）
  - 会话名取自整窗 OCR 首行（微信标题栏），切框时重置基线
  - 自己刚发的回复用 last_sent 近似匹配过滤，不会自说自话
  - 窗口被遮挡/截图失败时：连续 3 次失败即暂停（说明微信不在前台），每 20 轮重试
"""
from __future__ import annotations

import threading
import time

from core.config import ROOT
from wechat_mac import ax
from wechat_mac.bridge import WeChatBridge
from wechat_mac.ledger import ReplyLedger

LOG_PATH = ROOT / "wechat_replies.log"


def _log(text: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def _same_text(a: str, b: str, ratio: float = 0.8) -> bool:
    """忽略空白后近似比较（OCR 识别有抖动，原文精确比较会误判为两条新消息）。"""
    import re
    a, b = re.sub(r"\s+", "", a or ""), re.sub(r"\s+", "", b or "")
    if not a and not b:
        return True
    if not a or not b:
        return False
    # 短文本直接用包含关系，长文本用结构相似度
    if min(len(a), len(b)) <= 8:
        return a in b or b in a
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= ratio


def _diff_bottom(cur: list, prev: list | None) -> list:
    """提取 cur(本帧 msges 带坐标行) 相对 prev 新增的行。

    返回 (text, xc) 列表。微信消息区满屏会【自动滚动】：
    - 未滚动：新消息在底部追加，顶部公共前缀保留 → 取前缀之后的部分
    - 已滚动：顶部旧消息滚出、屏幕整体换了一批，本地位置不再可靠。
      此时退回"内容增量"：cur 里所有在 prev 中找不到的行，都算新增
      （微信新消息永远是新文本，靠 _same_text 逐条排除旧行）。
    prev 为 None（首帧/切框）返回全量（此时置基态不触发）。
    """
    if not cur:
        return []
    if not prev:
        return list(cur)
    cur_text = [t for t, _ in cur]
    prev_text = [t for t, _ in prev]

    # 情况A：顶部公共前缀（未滚动，新消息在底部追加）
    k = 0
    n = min(len(cur_text), len(prev_text))
    while k < n and _same_text(cur_text[k], prev_text[k], 0.75):
        k += 1
    if k > 0:
        # 只取底部新增，且跳过空/时间戳
        return [_ for _ in cur[k:] if _[0].strip() and not _looks_timestamp(_[0])]

    # 情况B：已滚动，顶部对不上。按内容增量：cur 中 prev 里没有的行 = 新增。
    # 把 prev 的全部行作为"已见集合"，逐条判断 cur 是否见过。
    added = []
    seen = list(prev_text)
    for t, xc in cur:
        # 跳过纯时间戳（OCR 常把时间当文本读出）
        if _looks_timestamp(t):
            continue
        if any(_same_text(t, s, 0.75) for s in seen):
            continue
        seen.append(t)          # 防同一帧内重复行互刷
        added.append((t, xc))
    return added


def _looks_timestamp(t: str) -> bool:
    """时间戳/纯数字短行（如 OCR 读出的 00:42）——不是消息，跳过。"""
    import re
    if not t or len(t) < 4:
        return False
    return bool(re.fullmatch(r"\d{1,2}[:：]\d{2}", t.strip()))


OTHER_X_MAX = 0.45   # 行中心 x < 此值 = 对方(左侧灰框) → 触发回复
SELF_X_MIN = 0.55    # 行中心 x > 此值 = 自己(右侧绿框) → 不触发，仅移基线
# 中间带（0.45~0.55：转账/红包/系统提示等居中消息）→ 忽略不触发


def _own_lines(lines: list) -> list:
    """过滤出自己(右侧)的行——这些是 AI 或你自己输入的内容，不触发回复。"""
    return [t for t, xc in lines if xc > SELF_X_MIN]


def _other_lines(lines: list) -> list:
    """过滤出对方(左侧)的行——这些才是要回复的消息（居中带不算对方，忽略）。"""
    return [t for t, xc in lines if xc < OTHER_X_MAX]


def _catchup_target(msges: list) -> str | None:
    """打开对话时，若消息区最后一条非空消息是"对方(左侧)"→ 取它作为补回目标。

    只回最后一条：连发多条时前几条进 SessionMemory 当上下文，不回每一条。
    最后一条是自己(右侧)/居中(转账红包系统提示)/时间戳 → 返回 None 不补回
    （最后是自己=轮到自己等对方，不该回旧消息）。
    """
    if not msges:
        return None
    for t, xc in reversed(msges):
        t = (t or "").strip()
        if not t or _looks_timestamp(t):
            continue            # 跳过时间戳/空行，继续往前看
        return t if xc < OTHER_X_MAX else None   # 只看最后一条"有内容"消息的归属
    return None


def _chat_allowed(name: str, whitelist: list, blacklist: list) -> bool:
    """catchup 白名单/黑名单（支持前缀匹配）：
    黑名单命中 → 否；白名单非空且不命中 → 否；其它 → 是。
    """
    def _hit(pat: str) -> bool:
        p = str(pat).strip()
        return bool(p and (name == p or name.startswith(p)))
    if any(_hit(p) for p in (blacklist or [])):
        return False
    if (whitelist or []) and not any(_hit(p) for p in whitelist):
        return False
    return True


def watch_loop(bot, interval: float = 1.0, stop: threading.Event | None = None,
               on_event=None, on_status=None) -> None:
    """on_event(name, msg, reply) / on_status(text)：可选回调，供 GUI/日志用。"""
    watch_cfg = getattr(bot, "cfg", {}) or {}
    watch_cfg = watch_cfg.get("watch", {})
    fallback_s = float(watch_cfg.get("fallback_interval", 120))
    ping_enabled = bool(watch_cfg.get("ping_enabled", True))
    # 打开对话自动补回最后一条对方消息（catchup）相关配置
    catchup_enabled = bool(watch_cfg.get("catchup_enabled", True))
    catchup_whitelist = list(watch_cfg.get("catchup_whitelist", []) or [])
    catchup_blacklist = list(watch_cfg.get("catchup_blacklist", []) or [])
    # 消息区 OCR 放大倍数（>1 提升小字识别与左右位置精度）
    try:
        ocr_scale = max(1.0, float(watch_cfg.get("ocr_scale", 1.0) or 1.0))
    except (TypeError, ValueError):
        ocr_scale = 1.0
    # 消息已处理台账（防"同一消息反复判定"）；初始化失败则降级为不去重，不影响值守
    try:
        ledger = ReplyLedger(ROOT / "data" / "replied_messages.db")
    except Exception as e:
        ledger = None
        _log(f"台账初始化失败（关闭去重）：{e}")
    try:
        dedup_window = float(watch_cfg.get("catchup_dedup_window", 300))
    except (TypeError, ValueError):
        dedup_window = 300.0
    # 分条发送间隔（模拟真人 enter 连发，随机到上限）
    _fmt = (getattr(bot, "cfg", {}) or {}).get("format", {})
    _line_swing = float(_fmt.get("line_interval", 0.8))
    import random as _random

    b = WeChatBridge()
    last_name = None       # 当前值守的会话（切框时重置基线）
    last_signal = None     # 最后一条气泡预览（OCR 文本，用于近似比对）
    last_sent = None       # 自己最后发出去的回复/保活
    last_handled = None    # 最后一条已处理的消息（无论回没回）
    last_ok = time.time()  # 最后一次成功交互（发送/判定无需回）的时间
    last_ping = 0.0        # 最后一次发保活的时间
    ping_done = False      # 本轮静默期是否已经保活过（防止对空会话反复轰炸）
    _win = None            # 窗口缓存（probe_light/scan 共用，省一次 CGWindow 枚举）
    last_msges = None      # 上次见到的消息区行集（用于提取本次新增的多条）

    def _read3_from(msges: list) -> list[str]:
        """从 scan 的 msges 取最近 3 条，作为 JEV 判断与回复的上下文（同帧复用，不二次扫描）。"""
        return [t for t, _ in (msges or [])][-3:]

    def _read3_extra(b: WeChatBridge) -> list[str]:
        """兜底专用：本帧没 OCR 到文本时补一次精读拿上下文（仅在补处理/保活要走）。"""
        try:
            s = b.scan(_win, scale=ocr_scale)
            if s.get("ok"):
                return [t for t, _ in (s.get("msges") or [])][-3:]
        except Exception:
            pass
        return []

    def _send(text: str) -> str:
        """分条发送，返回实际发出的首行（供 last_sent 对齐 OCR 自气泡）。"""
        ax.activate()
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        if len(lines) == 1:
            b.send(lines[0])
            return lines[0]
        # 分条发送（模拟真人 enter 换行连发）
        for i, ln in enumerate(lines):
            b.send(ln)
            if i < len(lines) - 1:
                time.sleep(0.3 + _random.random() * _line_swing)
        return lines[0]

    def _status(text: str) -> None:
        if on_status:
            on_status(text)

    def _act(name: str, cand: str, context: list[str] | None = None) -> None:
        """处理一条消息：JEV 结合上下文判断 + 判定发送；无论回没回都标记已处理。"""
        nonlocal last_sent, last_ok, last_handled, ping_done
        ping_done = False   # 对方来消息 = 对话活了，允许本轮之后可再保活
        last_handled = cand
        if not cand or _same_text(cand, last_sent, 0.7):
            last_ok = time.time()
            return
        if not context or not any(_same_text(cand, c, 0.7) for c in context):
            context = (context or []) + [cand]
        # 败龙(你小号)的发言当作"我"真实语气样本；前一条对方话当上下文。
        # 只有当值守会话名就是败龙时才采集（其它人的话不学。）
        try:
            prev = next((t for t in reversed(context[:-1]) if not _same_text(t, cand, 0.7)), None)
            if bot.record_bailong(name, cand, prev):
                _log(f"采集败龙风格：{prev[:22]} → 败龙：{cand[:22]}")
        except Exception:
            pass
        _status(f"→ 读到消息（{name}）：「{cand[:18]}…」")
        _status("→ AI 判断中…")
        reply = bot.handle_message({"sender_id": "wechat", "nickname": name,
                                    "text": cand, "context": context})
        if reply:
            try:
                _status("→ 生成完成，准备发送…")
                sent_first = _send(reply)
                last_sent = sent_first or reply
                _log(f"{name} 收到：{cand} → 回复：{reply}")
                _status("→ 已发送回复 ✅")
                if ledger:
                    ledger.mark(name, cand, "replied")
                if on_event:
                    on_event(name, cand, reply)
                elif on_status:
                    on_status(f"已回复 {name}")
            except Exception as e:
                _log(f"发送失败[{name}]：{e}")
                _status(f"→ 发送失败：{e}")
                if on_status:
                    on_status(f"发送失败：{e}")   # 不记账，留给兜底重试
        else:
            _log(f"{name} 判定无需回复：「{cand}」")
            _status("→ 判定无需回复，跳过")
            if ledger:
                ledger.mark(name, cand, "skipped")
            if on_status:
                on_status("（判定无需回复）")
        last_ok = time.time()   # 无论回没回都推进计时，避免兜底刷屏

    if on_status:
        on_status("心跳启动")
    _log(f"值守启动（OCR快探+变化精读，fallback={fallback_s}s, ping={ping_enabled}）")
    # 确保微信前置：OCR 截的是"所见即所得"，前置后才能截到微信本身
    try:
        ax.activate()
    except Exception:
        pass
    fail_cnt = 0
    cycle = 0
    last_hash = ""
    while not (stop and stop.is_set()):
        try:
            # ---------- ① 常态：截图+dHash（≈0.12s），画面没变就歇着 ----------
            pl = b.probe_light(_win)
            if not pl.get("ok"):
                fail_cnt += 1
                _win = None
                if fail_cnt >= 3:
                    _log(f"OCR 通道持续失败（微信未在前台？）：{pl.get('err', '')}")
                    if on_status:
                        on_status(f"OCR 失效：{pl.get('err', '')}")
                    time.sleep(3.0)
                cycle += 1
                time.sleep(interval)
                continue
            fail_cnt = 0
            _win = pl.get("win") or _win
            new_hash = pl.get("hash") or ""
            changed = bool(new_hash) and new_hash != last_hash
            sig = ""
            msges = []
            new_lines = []
            merged = ""
            cycle += 1
            # ---------- ② 画面变了 or 定期校准 → 整窗精读（OCR 一次） ----------
            if changed or cycle % 15 == 14:
                s = b.scan(_win, scale=ocr_scale)
                if not s.get("ok"):
                    _win = None
                    cycle += 1
                    time.sleep(interval)
                    continue
                _win = s["win"]
                name = s.get("name") or "当前会话"
                sig = s.get("bubble") or ""
                msges = s.get("msges") or []
                last_hash = new_hash
            else:
                name = last_name or "当前会话"

            # ---------- 切框感知（仅在精读轮） ----------
            if name != last_name:
                if last_name is not None:
                    _log(f"已切换 → {name}（先适应，不回复历史）")
                    if on_status:
                        on_status(f"已切换 → {name}")
                last_name, last_signal = name, sig
                last_sent = None    # 切框清空：防上一窗口自己回复文案与新框撞车
                last_handled = None
                last_msges = msges or []   # 以本帧为基线（切框轮必有 msges）
                last_hash = new_hash
                ping_done = False   # 新窗口允许保活
                # 打开对话自动补回：最后一条是对方(左侧) → 直接回（白名单/黑名单过滤）
                if catchup_enabled and _chat_allowed(name, catchup_whitelist, catchup_blacklist):
                    catch_text = _catchup_target(msges)
                    if (catch_text and not _same_text(catch_text, last_sent, 0.7)
                            and not catch_text.startswith("你撤回")):
                        # 台账：同一会话同文本在窗口内已处理过（含判定不回）→ 跳过不重复判定
                        if (ledger and ledger.recently(name, catch_text, dedup_window)):
                            _log(f"打开对话补回跳过（{name}）：该消息窗口内已处理过")
                        else:
                            last_signal = catch_text
                            _log(f"打开对话补回（{name}）：「{catch_text[:30]}」")
                            if on_status:
                                on_status(f"→ 打开对话补回（{name}）")
                            _act(name, catch_text, _read3_from(msges))
                _log(f"已切换 → {name}（已清空上一窗口状态/记录）")
                cycle += 1
                time.sleep(interval)
                continue

            # 首帧兜底（正常不走到：切框轮已建基线）；仅当本帧无 msges 又无切框时才建基线
            if last_msges is None:
                last_msges = msges or last_msges
                last_signal = last_signal or "\n".join(t for t, _ in (msges or []))
                cycle += 1
                time.sleep(interval)
                continue

            now = time.time()
            # 正常触发：画面变了 → 找出相对上次新增的行，仅"对方(左侧)"触发回复
            new_lines = _diff_bottom(msges, last_msges)
            other_new = _other_lines(new_lines)   # 对方灰框发来的
            own_new = _own_lines(new_lines)       # 自己绿框（AI/你输入）
            if changed and new_lines:
                # 自己的输出也移动了基线，但绝不触发回复
                last_msges = msges
                if other_new:
                    merged = "\n".join(other_new)
                    if not _same_text(merged, last_sent, 0.7) and not merged.startswith("你撤回"):
                        if not _same_text(merged, last_signal, 0.85):
                            last_signal = merged
                            last_msges = msges
                            _act(name, merged, _read3_from(msges))
            # 变化了但没识别出新文本（纯图片/表情）或仅重排 → 更新基线防反复
            if changed and not new_lines:
                last_msges = msges

            # 兜底：超过 fallback 秒没有成功交互
            if now - last_ok >= fallback_s:
                ctx = []
                # ③-① 还有一条对方发来、没处理过的消息 → 补处理（合并左侧多条）
                fb_lines = _other_lines(_diff_bottom(msges, last_msges))
                fb_merged = "\n".join(fb_lines)
                if (fb_merged and not _same_text(fb_merged, last_sent, 0.7)
                        and not _same_text(fb_merged, last_handled, 0.7)
                        and not fb_merged.startswith("你撤回")):
                    _log(f"兜底：{fallback_s:.0f}s 无动静，补处理「{fb_merged[:20]}」")
                    last_msges = msges
                    _act(name, fb_merged, _read3_from(msges) or _read3_extra(b))
                # ③-② 没得补/补过了（含整框空白 sig 为空）→ MiMo 生成一句话主动发
                # 但同一静默期最多保活 1 次，防对空会话死循环轰炸
                elif ping_enabled and not ping_done and now - last_ping >= fallback_s:
                    last_ping = now
                    last_ok = now
                    ping_done = True     # 本轮静默期已保活过
                    ctx = _read3_from(msges) or _read3_extra(b)
                    try:
                        _status("→ 保活生成中…")
                        ping_reply = bot.idle_ping(name, ctx)
                        if ping_reply:
                            sent_first = _send(ping_reply)
                            last_sent = sent_first or ping_reply
                            _log(f"保活(AI生成)：向 {name} 发送「{ping_reply}」")
                            _status("→ 保活已发送 ✅")
                            if on_status:
                                on_status(f"保活：{ping_reply}")
                        else:
                            _log("保活：AI 生成失败，跳过本次保活")
                            _status("→ 保活生成失败，跳过")
                    except Exception as e:
                        _log(f"保活发送失败：{e}")
                        _status(f"→ 保活发送失败：{e}")
            elif sig and _same_text(sig, last_sent, 0.7):
                last_signal = sig  # 自己刚发的回复/保活，标记为已见
        except Exception as e:
            _log(f"心跳出错：{e}")
            if on_status:
                on_status(f"出错：{e}")
        cycle += 1
        time.sleep(interval)