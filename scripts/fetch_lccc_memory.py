"""从 HF 镜像（hf-mirror.com）流式抽样 LCCC 微博口语对话，转成 memory 需要的 txt。

LCCC 每行是一个多轮对话数组，说话人是隐含交替的（u0=对方, u1=我, u2=对方…，从末尾看谁最后说，近似处理即可）。
仅用于给 few-shot 提供口语风格参考，量小且干净更重要，默认抽 800 对。

用法：python scripts/fetch_lccc_memory.py [对话对数量]
"""
from __future__ import annotations

import gzip
import json
import sys
import re

import requests

URL = "https://hf-mirror.com/datasets/silver/lccc/resolve/main/lccc_base_valid.jsonl.gz"
OUT = "data/lccc_corpus.txt"

# 分词空格：LCCC 是字级别分词，去掉所有空格
SPLIT_RE = re.compile(r"\s+")
# 过滤明显不适合当微信参考的（太长/太短/带邮箱链接）
LEN_MIN, LEN_MAX = 4, 60


def clean(s: str) -> str:
    s = SPLIT_RE.sub("", s)
    s = re.sub(r"[【】]|#.*?#", "", s)
    return s.strip()


def main(n_pairs: int = 800) -> int:
    pairs: list[tuple[str, str]] = []
    with requests.get(URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with gzip.GzipFile(fileobj=r.raw) as f:
            for line in f:
                if len(pairs) >= n_pairs:
                    break
                try:
                    dial = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(dial, list) or len(dial) < 3:
                    continue
                # 取相邻两条组成 (对方, 我)：默认奇数位是回复方
                for i in range(0, len(dial) - 1, 2):
                    other, me = clean(dial[i]), clean(dial[i + 1])
                    if not (LEN_MIN <= len(other) <= LEN_MAX and LEN_MIN <= len(me) <= LEN_MAX):
                        continue
                    pairs.append((other, me))
                    if len(pairs) >= n_pairs:
                        break
    if not pairs:
        print("未抽取到任何对话对")
        return 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# LCCC 微博口语对话抽样（仅风格参考，说话人交替近似配对，非本人原话）\n")
        for other, me in pairs:
            f.write(f"对方: {other}\n")
            f.write(f"我: {me}\n")
    print(f"已写入 {OUT}：{len(pairs)} 对")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 800))