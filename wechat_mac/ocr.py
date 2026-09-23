"""截图 + Vision OCR 快通道：亚秒检测当前微信聊天框内容变化。

背景：
  AX 辅助功能树每节点约 65ms，一次完整气泡扫描要 3~7 秒。
  本模块换思路：截微信消息区画面 → Vision OCR（Accurate 复用）→ 文本。

性能设计（v2）：
  - 常态轮询走 probe_light()：只截消息区 + dHash 指纹（不做 OCR）≈0.15~0.2s
  - 指纹变化才走 scan()：一次整窗截图 → Pillow 内存裁出标题栏/消息区，
    分别 OCR ≈0.5~0.8s。把昂贵的 OCR 从"每轮"降到"仅在变化时"。

原理：
  screencapture -R 区域截图（需"屏幕录制"授权）+ VNRecognizeTextRequest。
  窗口定位只用 CGWindowList（微信 4.x 主窗可见时可取），不依赖 AX。

可靠性：
  - 无授权/无窗口/截图失败 → 返回 ok=False，调用方降级处理。
  - 微信窗口需在屏幕可见位置（"跟随当前聊天框"的前提）。
"""
from __future__ import annotations

import subprocess
import time

import Quartz
from Foundation import NSURL

# 微信消息区在窗口内的裁剪（逻辑点：左会话列表约 312，顶部标题约 56，底部输入区约 120）
MSG_LEFT = 312
MSG_TOP = 56
MSG_BOTTOM = 120


def have_screen_capture() -> bool:
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def wechat_frontmost() -> bool:
    """微信是否在当前前台应用（读屏只在微信最前时进行，防错读其它窗口）。

    用系统 API NSWorkspace.frontmostApplication 判定（CGWindowList 的返回顺序
    在不同 macOS 上不可靠，会把前台微信误判成"被遮挡"）。
    AppKit 不可用时返回 True 放行（不拦截，保证值守能跑）。
    """
    try:
        from AppKit import NSWorkspace
        name = str(NSWorkspace.sharedWorkspace()
                   .frontmostApplication().localizedName() or "")
        return name in ("微信", "WeChat")
    except Exception:
        return True   # 判不了就放行，避免误伤正常值守


def _region_shot(x: int, y: int, w: int, h: int, path: str) -> bool:
    """截图屏幕区域（points），成功返回 True。"""
    try:
        r = subprocess.run(
            ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path],
            capture_output=True, text=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def _read_image(path: str):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


_VISION_REQ = None   # 复用 VNRecognizeTextRequest（Accurate 首次加载慢，之后 0.2~0.4s）


def _get_vision_request():
    global _VISION_REQ
    if _VISION_REQ is None:
        import Vision
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
        req.setUsesLanguageCorrection_(True)
        _VISION_REQ = req
    return _VISION_REQ


def _ocr(img) -> list[tuple[str, float]]:
    """Vision OCR 返回 [(文本, x中心归一化), ...]，按阅读顺序自上而下。

    x 中心归一化（0~1）：微信里自己的消息气泡靠右（x≈0.6~0.95），
    对方的靠左（x≈0.05~0.4）。engine 用它判断"这条是不是自己刚发的"。
    """
    import Vision

    request = _get_vision_request()
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    ok, _err = handler.performRequests_error_([request], None)
    if not ok:
        return []
    obs = request.results() or []
    items = []
    for ob in obs:
        box = ob.boundingBox()
        cands = ob.topCandidates_(1)
        if not cands or not cands[0]:
            continue
        text = (cands[0].string() or "").strip()
        if text:
            xc = box.origin.x + box.size.width / 2
            items.append((box.origin.y + box.size.height, xc, text))
    items.sort(key=lambda t: -t[0])
    lines, last_y, buf, buf_x, buf_n = [], None, [], 0.0, 0
    for y, xc, text in items:
        if last_y is not None and abs(y - last_y) > 0.06:
            if buf:
                lines.append((" ".join(buf), buf_x / max(buf_n, 1)))
                buf = []
                buf_x = 0.0
                buf_n = 0
        buf.append(text)
        buf_x += xc
        buf_n += 1
        last_y = y
    if buf:
        lines.append((" ".join(buf), buf_x / max(buf_n, 1)))
    return lines


def _dhash_file(path: str, size: int = 16) -> str:
    """dHash 感知哈希：从 PNG 文件直接读（Pillow），避免 CGContext 内存访问问题。

    微信消息区每来一条新消息，气泡位置/文字重排 → hash 变化。
    识别不出文字内容（表情/图片）也能感知变化，完美匹配"只做探测"定位。
    """
    try:
        from PIL import Image
        img = Image.open(path).convert("L")          # 灰度
        img = img.resize((size + 1, size), Image.LANCZOS)
        px = list(img.getdata())
        bits = []
        for yy in range(size):
            row0 = px[yy * (size + 1): (yy + 1) * (size + 1)]
            for xx in range(size):
                bits.append("1" if row0[xx] >= row0[xx + 1] else "0")
        return "%x" % int("".join(bits), 2)
    except Exception:
        return ""


def probe_region(x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> dict:
    """按给定屏幕矩形(box)截图 OCR——消息区 bbox 已由 AX 给出时用这个，误差最小。"""
    if not have_screen_capture():
        return {"ok": False, "err": "no screen recording permission"}
    if w <= 0 or h <= 0:
        return {"ok": False, "err": "empty region"}
    t0 = time.time()
    tmp = "/tmp/wx_ocr_snap.png"
    if not _region_shot(int(x), int(y), int(w), int(h), tmp):
        return {"ok": False, "err": "capture failed"}
    hsh = _dhash_file(tmp)
    img = _read_image(tmp)
    if img is None:
        return {"ok": False, "err": "decode failed"}
    lines = _ocr(img)
    return {"ok": True, "lines": lines, "hash": hsh, "ts": time.time(),
            "ms": int((time.time() - t0) * 1000)}


def probe(x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> dict:
    """一次 OCR 快探：{ok, lines, ts, err}。

    x/y/w/h 为微信主窗口 bounds（points）；缺省时先试 CGWindowList，再试 AX。
    lines = 消息区自上而下的 OCR 文本（最后一行≈最新气泡）。
    """
    import Quartz  # noqa: F401  (重复导入无害)

    if not have_screen_capture():
        return {"ok": False, "err": "no screen recording permission"}

    if not (w and h):
        win = _pick_window()
        if not win:
            return {"ok": False, "err": "no wechat window"}
        x, y, w, h = win

    # 消息区 = 窗口切掉左会话栏 / 顶栏 / 底部输入区
    rx, ry = int(x) + MSG_LEFT, int(y) + MSG_TOP
    rw, rh = int(w) - MSG_LEFT - 8, int(h) - MSG_TOP - MSG_BOTTOM
    if rw < 50 or rh < 50:
        return {"ok": False, "err": "window too small"}
    t0 = time.time()
    tmp = "/tmp/wx_ocr_snap.png"
    if not _region_shot(rx, ry, rw, rh, tmp):
        return {"ok": False, "err": "capture failed"}
    img = _read_image(tmp)
    if img is None:
        return {"ok": False, "err": "decode failed"}
    lines = _ocr(img)
    return {"ok": True, "lines": lines, "ts": time.time(),
            "ms": int((time.time() - t0) * 1000)}


def _pick_window() -> tuple[int, int, int, int] | None:
    """定位微信主窗口（仅 CGWindowList，不依赖 AX）：返回 (x, y, w, h) 屏幕坐标。"""
    try:
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        best = None
        for info in infos or []:
            owner = info.get(Quartz.kCGWindowOwnerName, "")
            if owner not in ("微信", "WeChat"):
                continue
            b = info.get(Quartz.kCGWindowBounds) or {}
            w, h = float(b.get("Width", 0)), float(b.get("Height", 0))
            if w >= 400 and h >= 400 and (best is None or w * h > best[2]):
                best = (int(b.get("X", 0)), int(b.get("Y", 0)), int(w), int(h), w * h)
        if best:
            return best[0], best[1], best[2], best[3]
    except Exception:
        return None
    return None


# ---------- 高层纯 OCR 接口（检测 + 读取，完全不碰 AX） ----------
# 微信 4.x 布局常量（逻辑点）：左侧导航 ≈60，会话列表 ≈280 → 聊天区从 340 起
CHAT_LEFT = 340          # 聊天区左边界（窗口内偏移）
TITLEBAR_H = 56          # 聊天区顶部标题栏高度（含会话名）
INPUT_H = 150            # 底部输入区高度
SESSION_NAME_W = 260     # 标题栏会话名占宽（居中位置用）


def layout(wx: tuple[int, int, int, int]) -> dict:
    """由窗口 bounds 推导各子区域坐标（无 AX）：

    返回：msg=(x,y,w,h) 消息区 / input=(x,y) 输入框中心 / title=(x,y,w,h) 聊天区标题栏
    """
    x, y, w, h = wx
    cx = x + CHAT_LEFT
    msg = (cx + 8, y + TITLEBAR_H + 4, w - CHAT_LEFT - 90, h - TITLEBAR_H - INPUT_H)
    input_xy = (cx + int((w - CHAT_LEFT) / 2), y + h - int(INPUT_H / 2) - 6)
    title = (cx + 40, y + 16, w - CHAT_LEFT - 60, 34)   # 聊天区标题栏中央
    return {"msg": msg, "input": input_xy, "title": title}


def probe_light(wx: tuple[int, int, int, int] | None = None) -> dict:
    """常态快探：只截消息区 + dHash，不做 OCR（≈0.15~0.2s）。

    返回 {ok, hash, win, msg}：hash 变化 = 画面有变，再叫 scan() 精读。
    这是值守主循环的默认节奏——把浪费的 OCR 挪到"变化时"。
    """
    if not have_screen_capture():
        return {"ok": False, "err": "no screen recording permission"}
    # 微信被其它窗口遮挡/切到别的 App 时不截（截了也是别的窗口内容，会错读错发）
    if not wechat_frontmost():
        return {"ok": False, "err": "wechat not frontmost"}
    if wx is None:
        wx = _pick_window()
    if not wx:
        return {"ok": False, "err": "no wechat window"}
    lay = layout(wx)
    if lay["msg"][2] < 50 or lay["msg"][3] < 50:
        return {"ok": False, "err": "window too small"}
    t0 = time.time()
    tmp = "/tmp/wx_light.png"
    if not _region_shot(*lay["msg"], tmp):
        return {"ok": False, "err": "capture failed"}
    return {"ok": True, "hash": _dhash_file(tmp), "win": wx,
            "msg": lay["msg"], "ms": int((time.time() - t0) * 1000)}


def scan(wx: tuple[int, int, int, int] | None = None,
         scale: float = 1.0) -> dict:
    """一次纯 OCR 全扫描：{ok, name, bubble, hash, msges:[(文本,x)] , win, layout}

    - win      主窗口 bounds（CGWindowList）
    - name     当前会话名（整窗 OCR 首行）
    - bubble   消息区最后一行文本（≈最新气泡；图片/表情可能为空）
    - hash     消息区 dHash（变化探测）
    - msges    消息区全部文本行（上下文用）
    - scale    消息区放大倍数（>1 时先放大再 OCR，小字识别更准；归一化 x 不变）

    只做 1 次整窗截图 + 1 次整窗 OCR：会话名取首行，消息区文本按
    x 坐标在聊天区左界内过滤（同一张图，不额外 OCR）。
    任何一步失败 → ok=False，err 说明。
    """
    if wx is None:
        wx = _pick_window()
    if not wx:
        return {"ok": False, "err": "no wechat window"}
    if not have_screen_capture():
        return {"ok": False, "err": "no screen recording permission"}
    t0 = time.time()
    if not _region_shot(*wx, "/tmp/wx_full.png"):
        return {"ok": False, "err": "capture failed"}
    import ctypes
    import io
    from PIL import Image
    raw = open("/tmp/wx_full.png", "rb").read()
    cdata = ctypes.create_string_buffer(raw, len(raw))
    cfdata = Quartz.CFDataCreate(None, cdata, len(raw))
    if cfdata is None:
        return {"ok": False, "err": "decode failed"}
    src = Quartz.CGImageSourceCreateWithData(cfdata, None)
    if src is None:
        return {"ok": False, "err": "decode failed"}
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        return {"ok": False, "err": "decode failed"}
    lines = _ocr(cg)   # 整窗自上而下（x 归一化相对整窗）
    lay = layout(wx)
    # 会话名：读"聊天区标题栏"（当前打开会话的名称，而非列表首项）。
    # 从整窗裁出右侧顶部一条，放大后 OCR，比全窗首行稳。
    name = ""
    try:
        import io as _io
        from PIL import Image as _Im
        tx, ty, tw, th = lay["title"]   # 标题条（含聊天区顶部区域）
        pxs = _Im.open("/tmp/wx_full.png")
        s2x = pxs.width / float(wx[2]); s2y = pxs.height / float(wx[3])
        px = max(int((tx - wx[0]) * s2x), 0); py = max(int((ty - wx[1]) * s2y), 0)
        pw = max(int(tw * s2x), 1); ph = max(int(th * s2y), 1)
        cimg = pxs.crop((px, py, min(px + pw, pxs.width), min(py + ph, pxs.height)))
        cimg = cimg.resize((pw * 3, ph * 3), _Im.LANCZOS).convert("RGB")
        _buf = _io.BytesIO(); cimg.save(_buf, format="PNG")
        _raw = _buf.getvalue()
        _cd = ctypes.create_string_buffer(_raw, len(_raw))
        _cf = Quartz.CFDataCreate(None, _cd, len(_raw))
        if _cf is not None:
            _src = Quartz.CGImageSourceCreateWithData(_cf, None)
            if _src is not None:
                _cg = Quartz.CGImageSourceCreateImageAtIndex(_src, 0, None)
                if _cg is not None:
                    name = _extract_chat_name(_ocr(_cg))
    except Exception:
        name = ""
    if not name:
        name = _extract_chat_name(lines)   # 兜底：整窗首行
    # 消息区：从整窗裁剪出聊天区，单独 OCR（保证中文小字识别质量）
    imf = Image.open("/tmp/wx_full.png")
    sx = imf.width / float(wx[2]); sy = imf.height / float(wx[3])
    msg_img = imf.crop((
        int((lay["msg"][0] - wx[0]) * sx), int((lay["msg"][1] - wx[1]) * sy),
        int((lay["msg"][0] - wx[0] + lay["msg"][2]) * sx),
        int((lay["msg"][1] - wx[1] + lay["msg"][3]) * sy)))
    # 放大消息区再 OCR：小字识别更准、气泡左右位置判定更稳（归一化 x 不受放大影响）
    try:
        if float(scale) > 1:
            wpx, hpx = msg_img.size
            msg_img = msg_img.resize(
                (max(int(wpx * float(scale)), 1), max(int(hpx * float(scale)), 1)),
                Image.LANCZOS)
    except Exception:
        pass
    buf = io.BytesIO(); msg_img.save(buf, format="PNG")
    mraw = buf.getvalue()
    mcdata = ctypes.create_string_buffer(mraw, len(mraw))
    mcfdata = Quartz.CFDataCreate(None, mcdata, len(mraw))
    msges = []
    if mcfdata is not None:
        msrc = Quartz.CGImageSourceCreateWithData(mcfdata, None)
        if msrc is not None:
            mimg = Quartz.CGImageSourceCreateImageAtIndex(msrc, 0, None)
            if mimg is not None:
                msges = _ocr(mimg)
    bubble = msges[-1][0] if msges else ""
    # 消息区 dHash 从整窗裁（内存版，保证与 probe_light 同源）
    hsh = _dhash_in_mem(imf, lay["msg"], wx, sx, sy)
    return {
        "ok": True, "name": name, "bubble": bubble,
        "hash": hsh, "msges": msges,
        "win": wx, "layout": lay,
        "ms": int((time.time() - t0) * 1000),
    }


def _dhash_in_mem(img, reg: tuple[int, int, int, int], wx, sx, sy,
                  size: int = 16) -> str:
    """Pillow 图内部区域 dHash（与 probe_light 文件版一致）。"""
    try:
        from PIL import Image
        rx, ry, rw, rh = reg
        px = int((rx - wx[0]) * sx); py = int((ry - wx[1]) * sy)
        pw = max(int(rw * sx), 1); ph = max(int(rh * sy), 1)
        sub = img.crop((px, py, px + pw, py + ph)).convert("L")
        sub = sub.resize((size + 1, size), Image.LANCZOS)
        vals = list(sub.getdata())
        bits = []
        for yy in range(size):
            row0 = vals[yy * (size + 1): (yy + 1) * (size + 1)]
            for xx in range(size):
                bits.append("1" if row0[xx] >= row0[xx + 1] else "0")
        return "%x" % int("".join(bits), 2)
    except Exception:
        return ""


def _scan_region(reg: tuple[int, int, int, int], save: str) -> dict:
    """截区域并对图片做 OCR+dHash（复用 Accurate 模型）。"""
    tmp = save
    if not _region_shot(int(reg[0]), int(reg[1]), int(reg[2]), int(reg[3]), tmp):
        return {"ok": False, "err": "capture failed"}
    img = _read_image(tmp)
    if img is None:
        return {"ok": False, "err": "decode failed"}
    lines = _ocr(img)
    hsh = _dhash_file(tmp)
    return {"ok": True, "lines": lines, "hash": hsh}


def _extract_chat_name(lines: list[tuple[str, float]]) -> str:
    """从整窗 OCR 行中提取当前会话名。

    会话名出现在窗口顶部区域（聊天标题栏/列表首项），过筛条件：
    - 行位置偏上（xc 不严格约束，但要短）
    - 首段像"人名/群名"：长度 2~12，不含常见噪声
    """
    import re
    # 会话名只可能出现在整窗 OCR 的顶部几行（标题栏/列表首项）
    for t, xc in lines[:3]:
        if not t:
            continue
        # 取行内第一段英文/中文串（会话名通常在开头）
        m = re.search(r"([A-Za-z0-9·\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9·_ -]{1,12})", t)
        if not m:
            continue
        cand = m.group(1).strip().strip("·_ -")
        # 去掉行尾常见的"搜索"占位（标题栏右侧按钮）
        cand = re.split(r"\s*(搜索|Search|く|⋯|更多|，|,)", cand)[0].strip()
        cand = cand.strip("·_ -")
        # 去掉末尾黏连的数字（标题栏右上时间/角标被 OCR 黏进来）
        cand = re.sub(r"\d+$", "", cand).strip("·_ -")
        # 剔除明显不是会话名的（搜索占位/按钮/消息文本）
        if not (2 <= len(cand) <= 12):
            continue
        if cand.startswith(("搜索", "微信", "公众号")) and cand != "文件传输助手":
            continue
        if re.fullmatch(r"[0-9.:\s]+", cand):
            continue
        if cand in ("设计", "消息", "联系人", "收藏", "朋友圈"):
            continue
        if re.search(r"(的|了|是|在|我|你|他|吗|呢|好|不)", cand) and re.search(r"\d", cand):
            continue
        return cand
    return ""