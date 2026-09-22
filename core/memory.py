"""聊天记录记忆：解析微信导出的 txt/jsonl，检索"你的历史回答"作为回复风格参考。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?")
# 手工标注格式："说话人: 内容"（说话人 ≤12 字符）
SPEAKER_RE = re.compile(r"^([^：:\s][^：:]{0,11}?)[：:]\s*(.*)$")
# "我"的几种常见叫法
ME_NAMES = {"我", "自己", "me", "myself", "本人"}


def _bigrams(s: str) -> set:
    s = (s or "").strip().lower()
    if len(s) > 1:
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {s} if s else set()


class ChatMemory:
    """从 txt / jsonl 聊天记录中提取 (对方消息, 我的回复) 对，供 few-shot 参考。

    my_names：哪些身份算"我"（默认 Byron/我/自己…）。败龙是你小号，
    败龙的发言最能代表你的真实语气，加入 my_names 后会被当作"我"回复样本。
    """

    def __init__(self, my_name: str = "老王", max_examples: int = 3,
                 max_other_chars: int = 120, max_self_chars: int = 200):
        self.my_name = my_name
        self.max_examples = max_examples
        self.max_other, self.max_self = max_other_chars, max_self_chars
        self.pairs: list[tuple[str, str]] = []
        # 兜底"我"身份：默认标签 + 显式配置的 my_name
        self.my_names: set[str] = set(ME_NAMES) | {my_name}

    def add_self_identity(self, *names: str) -> None:
        """额外把某身份视为"我"（如败龙=你小号）。"""
        for n in names:
            if n:
                self.my_names.add(n.strip())

    # ---------- 加载 ----------
    def load(self, path_or_dir: str | Path, exclude: set | None = None) -> int:
        """加载一个文件或目录（*.txt / *.jsonl）；返回解析出的对话对数量。

        exclude：指定要跳过的文件名集合（如败龙存档，只存不学时避开）。
        """
        path = Path(path_or_dir)
        files = (sorted(path.glob("*.txt")) + sorted(path.glob("*.jsonl"))) if path.is_dir() else [path]
        exclude = exclude or set()
        total = 0
        for f in files:
            if f.name in exclude:
                continue
            try:
                msgs = self._parse_jsonl(f) if f.suffix == ".jsonl" else self._parse_txt(f)
            except Exception as e:
                logger.warning("解析 %s 失败：%s", f, e)
                continue
            pairs = self._to_pairs(msgs)
            self.pairs.extend(pairs)
            total += len(pairs)
        logger.info("聊天记录已加载：%d 条对话对（%s）", total, path)
        return total

    # ---------- 解析 ----------
    def _parse_txt(self, f: Path) -> list[dict]:
        """三种行格式混用皆可：
        1) 微信导出："时间 名字: 内容"  2) "时间 名字" + 内容换行
        3) 手工粘贴："名字: 内容"（你自己写成 我:，对方名字随便写）
        以 # 或 // 开头的行视为注释忽略。"""
        msgs: list[dict] = []
        cur_sender, cur_text = None, []

        def flush():
            nonlocal cur_sender, cur_text
            if cur_sender is not None and cur_text:
                msgs.append({"sender": cur_sender, "text": "".join(cur_text).strip()})
            cur_sender, cur_text = None, []

        for raw in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            m = TS_RE.match(line)
            if m:
                flush()
                rest = line[len(m.group(0)):].strip()
                if ":" in rest:
                    name, _, content = rest.partition(":")
                    cur_sender = name.strip()
                    cur_text = [content.strip()] if content.strip() else []
                else:
                    cur_sender = rest
                    cur_text = []
                continue
            sp = SPEAKER_RE.match(line)
            if sp and not sp.group(1).lower().startswith(("http", "www", "get", "post", "c:")):
                flush()
                name = sp.group(1).strip()
                cur_sender = self.my_name if name in self.my_names else name
                cur_text = [sp.group(2).strip()]
                continue
            cur_text.append(line)
        flush()
        return msgs

    def _parse_jsonl(self, f: Path) -> list[dict]:
        msgs = []
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("text"):
                msgs.append({"sender": (o.get("sender") or o.get("name") or "对方"),
                             "text": str(o["text"]).strip()})
        return msgs

    def _to_pairs(self, msgs: list[dict]) -> list[tuple[str, str]]:
        """近似配对：一条"对方"消息之后紧随的"我(含败龙)"的消息视为其回复。"""
        pairs = []
        for i, m in enumerate(msgs):
            if m["sender"] not in self.my_names or i == 0:
                continue
            prev = msgs[i - 1]
            if prev["sender"] not in self.my_names and prev["text"]:
                pairs.append((prev["text"][: self.max_other], m["text"][: self.max_self]))
        return pairs

    # ---------- 检索 ----------
    def retrieve(self, text: str, k: int | None = None) -> list[tuple[str, str]]:
        """按字符 bigram 重合度找最相似的 (对方消息, 我的回复)。无重合则不返回，避免注入无关历史。"""
        k = k or self.max_examples
        if not self.pairs:
            return []
        q = _bigrams(text)
        scored = []
        for other, me in self.pairs:
            inter = len(q & _bigrams(other))
            union = len(q | _bigrams(other)) or 1
            if inter:
                scored.append((inter / union, other, me))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(o, m) for _, o, m in scored[:k]]

    def format_examples(self, text: str) -> str:
        hits = self.retrieve(text)
        if not hits:
            return ""
        lines = ["【你过去的类似对话（参考语气口吻，不要照搬）】"]
        for other, me in hits:
            lines.append(f"对方：{other}")
            lines.append(f"你：{me}")
        return "\n".join(lines)