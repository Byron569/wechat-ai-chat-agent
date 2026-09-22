# JEVChat · 微信 AI 自动回复 Agent

模仿<strong>真人说话口吻</strong>的微信自动回复助手。收到消息后，先由 **JEV 决策层**判断「该不该回 / 是不是广告 / 是否需要转人工」，再由 **LLM 生成层**（默认小米 MiMo）按人设风格写出回复，最后按真人习惯<strong>分条发送</strong>。

本项目由两部分组成：

| 部分 | 能力 | 运行方式 |
|------|------|----------|
| `core/` | 决策 + 生成 + 记忆 + 风控的核心逻辑（与微信解耦） | `python main.py`（可当模拟器 / Webhook 服务） |
| `wechat_mac/` | macOS 值守端：截图 + Vision OCR 读微信消息、点选发送 | `python -m wechat_mac gui` |

---

## ✨ 特性

- 🔍 **纯 OCR 检测（无 AX 辅助功能树）**：截图 + macOS Vision（Accurate 中文识别）+ dHash 画面指纹，常态轮询 ≈0.1s、画面变化精读 ≈0.6s/轮
- 🧠 **双层 AI**：JEV（决策该不该回）→ LLM（按人设生成回复），部署在云端或本地均可
- 🎭 **真人语气**：可注入历史聊天记录学说话风格；支持「败龙小号」风格采集存档
- ✍️ **分条发送**：默认去掉逗号、一句一行连发（像真人）；脏话/挑衅会顶回去，最多回 3 句防对骂死循环
- 🛡️ **多重防护**：本地关键词过滤广告/转账、JEV 兜底、风控限频、单实例锁、纯文字去 emoji/AI 腔
- 🔔 **保活**：对方长时间不回时 AI 主动发一句（默认 10 分钟一次）

---

## 🧱 架构

```
        微信消息（OCR 读到文本）
                │
                ▼
  ┌─────────────────────────────────────┐
  │ wechat_mac/engine.py  值守主循环       │
  │   ①截图+dHash 指纹（判断画面变没变）    │
  │   ②画面变 → 整窗 OCR 精读文本          │
  │   ③左右判定（灰框=对方 / 绿框=自己）    │
  │   ④增量提取新增的对方消息              │
  └─────────────────────────────────────┘
                │  cand（merge 多条）
                ▼
  ┌─────────────────────────────────────┐
  │ core/bot.py  handle_message()        │
  │   ① 记入会话记忆 session              │
  │   ② 硬规则 rules（广告/脏话/转账…）    │
  │   ③ JEV 决策：该不该回/是否广告/转人工  │
  │   ④ LLM 生成回复（persona+记忆+上下文）│
  │   ⑤ 洗稿：去emoji/AI腔/逗号→分条       │
  │   ⑥ 风控延迟 → 返回 reply              │
  └─────────────────────────────────────┘
                │
                ▼
        wechat_mac 按行分条发送
```

**两条链路解耦**：`core/` 只依赖消息文本，可单独用模拟器/Webhook 测试；`wechat_mac/` 负责真实微信的读与发。

---

## 🧪 环境要求

| 要求 | 说明 |
|------|------|
| **OS** | 值守端 `wechat_mac/` 仅支持 **macOS**（依赖截图 + Vision OCR）；`core/` 逻辑跨平台 |
| **Python** | 3.10+（pyobjc 12 建议 3.10–3.12） |
| **微信** | macOS 版微信（WeChat 4.x），需登录保持运行 |
| **系统权限** | ① **屏幕录制**（截图读消息必需）② 辅助功能（发送用的点击/粘贴） |
| **网络** | JEV 与 LLM 走云端（MiMo/TypeSafe）或本地 Ollama；国内访问境外服务需代理 |

> 💡 屏幕录制授权：系统设置 → 隐私与安全性 → 屏幕录制 → 勾选运行本程序的终端/PyCharm。

---

## ⚙️ 安装

```bash
# 1. 下载并进入项目
git clone <your-repo-url>
cd ai-chat-agent

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt
# macOS 若 pyobjc 没装全，补：pip install "pyobjc-framework-Vision" "pyobjc-framework-Quartz"

# 4. 配置密钥
cp .env.example .env
#   按注释往 .env 填你的 Key（至少填一个 JEV 入口 + 一个 LLM 入口）
```

---

## 🔧 配置（config.yaml）

所有行为都在 `config.yaml` 集中控制，关键段：

| 段 | 作用 |
|----|------|
| `persona` | **人设**：决定生成回复的语气（改这里定制"你是谁"） |
| `jev` | **决策层**：provider（opencode/jevai/ollama…）+ 该不该回的阈值 |
| `llm` | **生成层**：provider（mimo/ollama）+ 模型名 + 温度/惩罚系数 |
| `rules` | 本地硬规则：`reply_map`固定话术 / `always_reply` / `advertisement`广告拦截 / `human_handoff`转账转人工 / `aggressive_reply`脏话强制回 |
| `watch` | 值守：`fallback_interval` 主动发消息间隔（秒，默认 600=10分钟） |
| `format` | **输出风格**：`comma_prob` 逗号连句概率 / 分条发送间隔 |
| `bailong` | 小号风格采集：`learn: false` = 只存档不学习 |
| `risk` | 风控：最小间隔 / 冷却 / 每日上限 / 争执抑制 |

**环境变量在 `.env`**（`core/config.py` 自动加载），优先级见 `.env.example` 注释。

---

## 🚀 运行

### 方式一：模拟器测试核心逻辑（无需微信）

```bash
python main.py simulate
# 直接输入一条消息回车，看机器人怎么回；/quit 退出，/stats 看风控
```

或起一个 Webhook 服务，供微信机器人框架（wxauto/wechaty 等）接入：

```bash
python main.py server --host 127.0.0.1 --port 8000
# POST http://127.0.0.1:8000/webhook/wechat  {"text":"你好"}
```

### 方式二：macOS 值守（真实微信自动收发）

```bash
python -m wechat_mac gui
```

会弹出一个置顶面板，**自动跟随当前微信聊天框**：
- 当前会话有人发消息 → 自动判断并回复
- 左侧灰框=对方（触发），右侧绿框=自己（忽略），不会自说自话
- 对方长时间不回 → AI 主动发一句（`watch.fallback_interval`，默认 10 分钟）

其他命令：`python -m wechat_mac probe` 诊断 OCR、`python -m wechat_mac once` 处理一次、`python -m wechat_mac run` 后台值守。

---

## 🔒 安全与合规

- ⚠️ **本项目的自动回复可能被用于模仿聊天对象**，请仅用于**自己的账号**做技术学习，勿用于冒充他人、骚扰、营销。
- ⚠️ 涉及转账、验证码、隐私等敏感内容，`human_handoff` 会自动转真人话术，不外发。
- 🔑 `.env`、`data/`（聊天语料）、`wechat_replies.log` 均已被 `.gitignore` 排除，**不会提交到 Git**。请勿手动强推这些文件。

---

## 📁 目录结构

```
.
├── main.py              # 入口：simulate 模拟器 / server Webhook
├── config.yaml          # 全部行为配置（人设/模型/规则/风控）
├── .env.example         # 环境变量模板（复制为 .env 填 Key）
├── requirements.txt     # Python 依赖
├── core/                # 核心逻辑（跨平台）
│   ├── bot.py           # 编排：硬规则→JEV→LLM→洗稿
│   ├── jev.py           # JEV 决策客户端（typesafe/opencode/ollama…）
│   ├── llm.py           # LLM 生成客户端（MiMo/DeepSeek/Ollama）
│   ├── rules.py         # 关键词硬规则 + 风控
│   ├── memory.py        # 历史聊天 few-shot 检索
│   ├── session.py       # 会话级上下文记忆
│   └── config.py        # .env + config.yaml 加载
└── wechat_mac/          # macOS 值守端（纯 OCR）
    ├── __main__.py      # CLI：probe/once/run/gui
    ├── engine.py        # 值守主循环（OCR 检测 + 分发）
    ├── ocr.py           # 截图 + Vision OCR + dHash
    ├── bridge.py        # 读消息/发送桥接
    ├── ax.py            # 系统级点击/粘贴（发送用）
    └── gui.py           # 悬浮监控面板
```

---

## 🛠 常见问题

- **提示缺少屏幕录制权限**：系统设置 → 隐私 → 屏幕录制，勾选终端。
- **发消息没反应**：确认微信窗口在最前且当前有聊天框；`python -m wechat_mac probe` 诊断 OCR 能否读到消息。
- **OCR 读到的全是乱码**：窗口未打开聊天详情，或聊天气泡小字识别受限，可放大窗口。
- **想换 LLM**：改 `config.yaml` 的 `llm.provider`（mimo/ollama）+ 对应 `.env` Key。

---

Made with ❤️ for personal AI chatbot experiments.