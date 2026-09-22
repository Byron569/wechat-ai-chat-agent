"""macOS 辅助功能层：用 System Events（辅助功能）操作微信窗口。

只依赖 osascript / pbcopy / pbpaste 系统命令，无需额外安装 Python 包。
"""
from __future__ import annotations

import json
import subprocess
import time

APP_NAMES = ["微信", "WeChat"]


def _osa(script: str, lang: str = "AppleScript", timeout: int = 90) -> str:
    r = subprocess.run(["osascript", "-l", lang, "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


# ---------- 应用 ----------
def find_app() -> str:
    for name in APP_NAMES:
        try:
            _osa(f'tell application "{name}" to get id')
            return name
        except RuntimeError:
            continue
    raise RuntimeError("找不到微信应用：请确认微信已安装且已登录")


def activate() -> None:
    app = find_app()
    _osa(f'tell application "{app}" to activate')
    time.sleep(0.8)


# ---------- 剪贴板（发中文消息要用"粘贴"，不要用按键逐字打） ----------
def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def get_clipboard() -> str:
    r = subprocess.run(["pbpaste"], capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


# ---------- 点击与按键（System Events，需要"辅助功能"授权） ----------
def click_at(x: int, y: int) -> None:
    _osa(f'tell application "System Events" to click at {{{x}, {y}}}')


def keystroke(text: str, using: str = "") -> None:
    clause = f" using {using}" if using else ""
    _osa(f'tell application "System Events" to keystroke "{text}"{clause}')


def key_code(code: int, using: str = "") -> None:
    clause = f" using {using}" if using else ""
    _osa(f'tell application "System Events" to key code {code}{clause}')


# ---------- 读取窗口/UI ----------
def front_window_bounds() -> dict:
    """返回前窗口的位置尺寸（全局屏幕坐标），找不到异常返回空。"""
    app = find_app()
    js = f'''
function safe(fn, fallback) {{
  try {{ var v = fn(); return v === undefined || v === null ? fallback : v; }}
  catch (e) {{ return fallback; }}
}}
var se = Application("System Events");
var p = se.processes.byName("{app}");
var win = null;
try {{ if (p.windows.length > 0) win = p.windows[0]; }} catch (e) {{}}
if (!win) throw new Error("微信没有可见窗口");
var pos = safe(function() {{ return win.position(); }}, [0, 0]);
var sz  = safe(function() {{ return win.size(); }}, [0, 0]);
var title = safe(function() {{ return String(win.title()); }}, "");
var r = {{}};
r.x = Number(pos[0]); r.y = Number(pos[1]); r.w = Number(sz[0]); r.h = Number(sz[1]); r.title = String(title);
JSON.stringify(r);
'''
    try:
        return json.loads(_osa(js, lang="JavaScript"))
    except Exception:
        return {}


def fast_probe(need_bubble: bool = False) -> dict:
    """快速探针（预算内浅层扫描，约 0.1~0.3s）。

    need_bubble=False（空闲心跳用）：只取当前会话名，扫到输入框即剪枝。
    need_bubble=True（值守用）：额外从"消息区元素"直接下钻取最后一条气泡预览。
    """
    app = find_app()
    js = f'''
function run() {{
  var se = Application("System Events");
  try {{
    var p = se.processes.byName("{app}");
    if (!p.windows || p.windows.length === 0) return JSON.stringify({{ok: false, err: "no window"}});
    var w = p.windows[0];
    var cnt = 0, name = "", nameY = -1, msgEl = null, msgList = null, inXY = null;
    function walk(el, d) {{
      if (cnt++ > 300 || d > 12) return;
      var role = "";
      try {{ role = String(el.role()); }} catch (e) {{ return; }}
      if (!role) return;
      if (role === "AXTextArea") {{
        try {{
          var pos = el.position();
          var y = Number(pos[1]);
          if (y > nameY) {{            // 每次取 y 最大的输入框 = 底部输入区
            nameY = y; name = String(el.title());
            var sz = el.size();
            inXY = [Number(pos[0]) + Number(sz[0]) / 2, y + Number(sz[1]) / 2];
          }}
        }} catch (e) {{}}
      }} else if (role === "AXList" && !msgEl) {{
        try {{
          if (String(el.title()) === "消息") {{
            msgEl = el;
            var pos = el.position(), sz = el.size();
            msgList = {{x: Number(pos[0]), y: Number(pos[1]),
                        w: Number(sz[0]), h: Number(sz[1])}};
          }}
        }} catch (e) {{}}
        return;  // 关键：不下钻任何 AXList，避免扫巨型会话/消息列表
      }}
      var kids = [];
      try {{ kids = el.uiElements(); }} catch (e) {{}}
      for (var i = 0; i < kids.length; i++) walk(kids[i], d + 1);
    }}
    walk(w, 0);
    var bubble = "", done = false, bestYb = -1;
    if (({int(need_bubble)}) && msgEl) {{
      cnt = 0;
      (function walk2(el, d) {{
        if (done || cnt++ > 130 || d > 9) return;
        var role = "";
        try {{ role = String(el.role()); }} catch (e) {{ return; }}
        if (role === "AXStaticText") {{
          try {{
            var pos = el.position();
            var y = Number(pos[1]);
            var t = String(el.title()).trim();
            if (t && y >= bestYb) {{
              bestYb = y; bubble = t;
              if (y >= msgList.y + msgList.h - 30) done = true;
            }}
          }} catch (e) {{}}
        }}
        var kids = [];
        try {{ kids = el.uiElements(); }} catch (e) {{}}
        for (var i = 0; i < kids.length; i++) walk2(kids[i], d + 1);
      }})(msgEl, 0);
    }}
    return JSON.stringify({{ok: true, name: name.trim(), bubble: bubble,
                           x: inXY ? inXY[0] : 0, y: inXY ? inXY[1] : 0,
                           msgL: msgList ? [msgList.x, msgList.y, msgList.w, msgList.h] : null}});
  }} catch (e) {{
    return JSON.stringify({{ok: false, err: String(e.message)}});
  }}
}}
'''
    try:
        out = json.loads(_osa(js, lang="JavaScript"))
        if not out.get("ok"):
            raise RuntimeError(out.get("err", "probe failed"))
        return out
    except json.JSONDecodeError:
        return {"ok": False, "err": "json decode"}
    except RuntimeError:
        # 复跑一次拿错误信息
        try:
            out = json.loads(_osa(js, lang="JavaScript"))
            return out if out.get("ok") else {"ok": False}
        except Exception:
            return {"ok": False}


def snapshot(max_depth: int = 10, max_items: int = 400) -> list[dict]:
    """把微信主窗口的 AX 元素打平成带坐标的 JSON 列表，供 Python 按 title/role 定位。"""
    app = find_app()
    js = f'''
function sc(fn, fb) {{
  try {{ var v = fn(); return v === undefined || v === null ? fb : v; }} catch (e) {{ return fb; }}
}}
function em(el, depth, arr, maxD, maxI) {{
  if (arr.length >= {max_items} || depth > {max_depth}) return;
  var role = sc(function() {{ return String(el.role()); }}, "");
  if (!role) return;
  var title = sc(function() {{ return String(el.title()); }}, "");
  var desc  = sc(function() {{ return String(el.description()); }}, "");
  var val   = sc(function() {{ var v = el.value(); return (typeof v === "object") ? "" : String(v); }}, "");
  var pos   = sc(function() {{ var p = el.position(); return [Number(p[0]), Number(p[1])]; }}, [0, 0]);
  var sz    = sc(function() {{ var s = el.size(); return [Number(s[0]), Number(s[1])]; }}, [0, 0]);
  arr.push({{role: role, title: title, desc: desc, value: val,
             x: pos[0], y: pos[1], w: sz[0], h: sz[1]}});
  var kids = sc(function() {{ return el.uiElements(); }}, []);
  for (var i = 0; i < kids.length; i++) em(kids[i], depth + 1, arr, maxD, maxI);
}}
var se = Application("System Events");
var p = se.processes.byName("{app}");
if (!p.windows || p.windows.length === 0) throw new Error("微信没有可见窗口");
var arr = [];
em(p.windows[0], 0, arr, {max_depth}, {max_items});
JSON.stringify(arr);
'''
    return json.loads(_osa(js, lang="JavaScript"))


def dump_ax(out_path: str, max_depth: int = 8, max_items: int = 500) -> str:
    """把微信主窗口的辅助功能树导出成文本，用于校准选择器。"""
    app = find_app()
    js = f'''
function esc(s) {{
  try {{ return String(s).replace(/\\n/g, "\\\\n").slice(0, 200); }} catch (e) {{ return ""; }}
}}
function get(el, prop) {{
  try {{ var v = el[prop](); if (v === undefined || v === null) return ""; return v; }}
  catch (e) {{ return ""; }}
}}
function walk(el, depth, out) {{
  if (out.count >= {max_depth * 400}) return;
  if (depth > {max_depth}) return;
  var role = get(el, "role");
  if (!role) return;
  var title = esc(get(el, "title"));
  var desc  = esc(get(el, "description"));
  var val   = esc(get(el, "value"));
  out.count++;
  var line = "  ".repeat(depth) + role +
             (title ? "  title=" + title : "") +
             (desc ? "  desc=" + desc : "") +
             (val ? "  value=" + val : "");
  out.lines.push(line);
  try {{
    var kids = el.uiElements();
    for (var i = 0; i < kids.length; i++) walk(kids[i], depth + 1, out);
  }} catch (e) {{}}
}}
var se = Application("System Events");
var p = se.processes.byName("{app}");
if (!p.windows || p.windows.length === 0) throw new Error("微信没有可见窗口");
var out = {{lines: [], count: 0}};
walk(p.windows[0], 0, out);
out.lines.join("\\n");
'''
    text = _osa(js, lang="JavaScript")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def window_title() -> str:
    return front_window_bounds().get("title", "")