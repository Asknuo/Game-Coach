# Game Coach — LOL AI Coach Agent

实时读取英雄联盟客户端数据，检测游戏事件，由 AI Agent 生成 coaching 建议，最终输出到 Overlay / Voice / Desktop Pet。

## 整体架构

```
LOL Client ─┬─ Live Client API (127.0.0.1:2999) ─────┐
            └─ LCU API (lockfile 端口) ───────────────┤
                                                       ▼
                                              Collector (Go / Python)
                                                    │
                                      WebSocket state + event 推送
                                                    │
                                                    ▼
                                          Agent (Python FastAPI)
                                          ┌─ LangGraph 流水线 ─┐
                                          │  parse_event        │
                                          │  → detect_signals   │
                                          │  → route_skill      │
                                          │  → RAG 向量检索      │
                                          │  → Memory 注入      │
                                          │  → LLM 润色         │
                                          │  → validate / publish│
                                          └─────────────────────┘
                                                    │
                                         WebSocket tip 推送
                                                    │
                                  ┌─────────────────┼─────────────────┐
                                  ▼                 ▼                  ▼
                            Collector 日志      Overlay 页面      Desktop Pet
                                              (Web Speech API)   (pyttsx3 TTS)
```

## 项目结构

```
Game Coach/
├── agent/                     # Python — AI Agent (FastAPI + LangGraph)
│   ├── app.py                 #   主入口：WebSocket、LangGraph 编排
│   ├── companion.py           #   桌面小玩偶（独立启动，tkinter + TTS）
│   ├── requirements.txt       #   Python 依赖
│   ├── Dockerfile             #   Docker 镜像
│   ├── models/                #   数据模型
│   │   └── state.py           #     GameState / CoachEvent / CoachingTip / WSMessage
│   ├── graph/                 #   LangGraph 图编排
│   │   ├── state.py           #     CoachState TypedDict（流水线状态）
│   │   ├── nodes.py           #     8 个节点函数（parse_event → publish）
│   │   └── builder.py         #     StateGraph 构建器 + 条件路由
│   ├── planner/               #   事件 → 技能路由器
│   │   └── planner.py         #     EVENT_SKILL_MAP + Planner.plan()
│   ├── skills/                #   5 个技能模板
│   │   ├── dragon.py          #     龙/大龙视野 & 站位
│   │   ├── recall.py          #     回城时机
│   │   ├── build.py           #     针对性出装
│   │   ├── jungle.py          #     野区检查
│   │   └── strategy.py        #     英雄策略（按游戏阶段）
│   ├── llm/                   #   LLM 客户端
│   │   └── openai_client.py   #     OpenAI / DeepSeek 兼容
│   ├── prompt/                #   提示词
│   │   └── coach_prompt.py    #     System prompt
│   ├── knowledge/             #   向量知识库 (ChromaDB + RAG)
│   │   ├── chroma_store.py    #     ChromaDB 封装，管理 6 个 Collection
│   │   ├── embedder.py        #     Embedding（OpenAI / 火山引擎豆包）
│   │   ├── retriever.py       #     统一检索 + 多源聚合
│   │   ├── ingest.py          #     数据摄入脚本 + 自动攻略生成
│   │   ├── data_fetcher.py    #     Data Dragon API 数据抓取
│   │   └── data/              #     缓存数据 + 手工攻略 markdown
│   ├── collector/             #   Python 版采集器
│   │   ├── bridge.py          #     采集器桥梁（串联 Live + LCU → Agent WS）
│   │   ├── live_client.py     #     Live Client Data API 采集器
│   │   └── lcu_client.py      #     LCU API 采集器（大厅数据）
│   ├── middleware/             #   中间件
│   │   └── game_middleware.py  #     游戏事件拦截中间件
│   └── static/                #   静态资源
│       └── overlay.html       #     浏览器 Overlay（内置 Web Speech API）
│
├── collector/                  # Go — 轻量版采集器 & 事件检测
│   ├── cmd/
│   │   ├── main.go            #   主入口：轮询 + 事件检测 + WS 发送
│   │   └── config.go          #   YAML 配置加载
│   ├── config/
│   │   └── config.yaml        #   默认配置
│   ├── internal/
│   │   ├── lol/
│   │   │   ├── client.go      #     HTTP 客户端（Live Client API）
│   │   │   ├── parser.go      #     JSON 解析（GameState）
│   │   │   ├── state.go       #     GameState 工具方法
│   │   │   └── objectives.go  #     龙/大龙刷新追踪器
│   │   ├── event/
│   │   │   ├── detector.go    #     6 种事件检测（含策略/野区定时器）
│   │   │   └── engine.go      #     事件引擎（带冷却去重）
│   │   └── sender/
│   │       └── websocket.go   #     WebSocket 发送器
│   ├── go.mod
│   └── go.sum
│
├── docker-compose.yml          # Redis + Agent 容器编排
├── .env.example                # 环境变量模板
└── README.md
```

## 快速开始

### 前置条件

- Python 3.12+（Agent）
- Go 1.22+（Go 版 Collector，可选；也可用 Python 版 bridge.py）
- LOL 客户端（需要对局进行中才能使用 Live Client API）
- Redis（用于去重和状态缓存）

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 填入配置：

```ini
# LLM（支持 DeepSeek / OpenAI / 任何 OpenAI 兼容 API）
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Embedding（OpenAI 或火山引擎豆包）
EMBEDDING_API_KEY=sk-your-embedding-key
EMBEDDING_MODEL=text-embedding-3-small

# Redis
REDIS_URL=redis://localhost:6379/0
```

### 2. 启动 Redis

```bash
docker compose up -d redis
```

### 3. 准备知识库数据

```bash
cd agent
pip install -r requirements.txt

# 从 Data Dragon 下载最新游戏数据
python -m knowledge.data_fetcher

# 向量化摄入到 ChromaDB
python -m knowledge.ingest
```

### 4. 启动 Agent

```bash
cd agent
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

### 5. 启动 Collector

**方式 A：Go 版（轻量，推荐）**

```bash
cd collector
go run ./cmd
```

**方式 B：Python 版（含 LCU 大厅数据）**

```bash
cd agent
python -m collector.bridge
```

### 6. 验证

| 端点 | 说明 |
|------|------|
| `http://localhost:8000/health` | Agent 健康检查（含记忆统计） |
| `http://localhost:8000/tips/latest` | 最新状态和建议 |
| `http://localhost:8000/overlay` | 浏览器 Overlay 页面 |

### 7. （可选）启动桌面小玩偶

```bash
cd agent
pip install pyttsx3 websocket-client
python companion.py
```

### 8. （可选）Docker 完整部署

```bash
docker compose up -d
```

## WebSocket 协议

### 端点

| 端点 | 通信方 | 说明 |
|------|--------|------|
| `/ws/collector` | Collector ↔ Agent | 双工：Collector 发 state/event，Agent 返回 tip |
| `/ws/overlay` | Overlay / Desktop Pet ↔ Agent | Agent 广播 tip，Overlay 可选发心跳 |

### Collector → Agent

```json
{"type": "state", "payload": {"game_time": 300.5, "active_player": {...}, "all_players": [...]}}
{"type": "event", "payload": {"name": "dragon_soon", "data": {"seconds_left": 25}}}
```

### Agent → Collector / Overlay

```json
{"type": "tip", "payload": {"message": "Dragon in 25s — ward river and prepare to contest.", "skill": "dragon", "priority": 2}}
```

## 核心流程：LangGraph 8 节点流水线

```
parse_event ─── 无效事件 → END
  │
  ▼
detect_signals ─ 玩家死亡且非龙/大龙事件 → END
  │
  ▼
route_skill ─── 无匹配技能 → END
  │
  ▼
retrieve_knowledge (ChromaDB RAG 多源聚合)
  │
  ▼
inject_memory (PlayerMemory 上下文注入)
  │
  ▼
llm_polish (DeepSeek / OpenAI 润色)
  │
  ▼
validate ────── 去重/冷却 → END
  │
  ▼
publish (标记发送 + 构建 tip)
```

### 各节点职责

| 节点 | 职责 |
|------|------|
| `parse_event` | 提取 event_name/event_data，初始化中间字段 |
| `detect_signals` | 死亡过滤、信号分类（low_health / objective_stage / power_spike）、优先级 |
| `route_skill` | 事件名 → Skill 模板生成，拼接 rag_query |
| `retrieve_knowledge` | ChromaDB 多源 RAG：己方攻略 + 对位敌人 + 游戏机制 + 装备 |
| `inject_memory` | PlayerMemory 格式化注入（截断 250 token） |
| `llm_polish` | LLM 润色：知识丰富时 2-3 句，稀疏时 1 句 20 词 |
| `validate` | Redis 去重（同技能最近发送过则跳过） |
| `publish` | 标记 tip 已发送，输出 CoachingTip |

## Skills（5 个教练技能）

| Skill | 触发事件 | 优先级 | 冷却 | 建议内容 |
|-------|---------|--------|------|---------|
| `dragon` | `dragon_soon` / `baron_soon` | 2-3 | 60s | 视野布置、站位、准备争夺 |
| `recall` | `low_health` | 3 | 45s | 回城时机、注意安全 |
| `build` | `item_purchased` | 1 | 30s | 继续出装建议、根据敌方调整 |
| `jungle` | `jungle_check`（每 3 分钟） | 1 | 120s | 野区路线、追踪敌方打野 |
| `strategy` | `strategy_check`（每 5 分钟） | 1 | 300s | 当前阶段英雄策略提示 |

## 知识库 (ChromaDB)

### 6 个 Collection

| Collection | 数据来源 | 内容 |
|------------|---------|------|
| `lol_items` | Data Dragon items.json | 装备名称、属性、价格、合成路径 |
| `lol_champions` | Data Dragon champions.json | 英雄属性 + Passive/QWER 技能 |
| `lol_champion_guides` | data/champions/*.md + 自动生成 | 攻略（手动 + 172 英雄自动生成） |
| `lol_runes` | Data Dragon runes.json | 符文路径 + 基石/普通符文 |
| `lol_summoner_spells` | Data Dragon summoner.json | 召唤师技能 |
| `lol_game_info` | data/game_info.json | 游戏机制、野怪、地图、龙效果 |

### 自动攻略生成

对于没有手工 markdown 攻略的英雄，系统基于 Data Dragon 数据 + 6 种角色模板自动生成攻略：

- 英雄概览（角色、难度、被动、技能名）
- Early Game Strategy（0-14 分钟）
- Mid Game Strategy（15-25 分钟）
- Late Game Strategy（25+ 分钟）
- Skill Combos（技能连招）
- How to Play / How to Counter（Data Dragon allytips/enemytips）
- Build Recommendations（AD/AP 出装建议）
- Key Stats（基础属性）

### 多源聚合 RAG

`aggregate_coaching_context()` 在检索节点中整合四层知识：

1. **己方英雄** — 按游戏阶段（early/mid/late）检索攻略
2. **对位敌人** — 敌方英雄攻略 + anti-counter 技巧
3. **游戏机制** — 龙、大龙、地图、野怪等通用知识
4. **装备推荐** — 物品类事件附加 build 建议

## 数据采集

### Go 版 Collector（collector/）

轻量级实现，依赖少，性能好。

- 轮询 Live Client API（`https://127.0.0.1:{port}/liveclientdata/allgamedata`）
- 通过 lockfile 自动发现端口和认证
- JSON 解析 + ObjectiveTracker 追踪龙/大龙刷新
- Event Detector 检测 6 种事件 + Event Engine 冷却去重
- WebSocket 发送 state + event 到 Agent

### Python 版 Collector（agent/collector/）

功能更丰富，含 LCU 大厅数据。

| 文件 | 职责 |
|------|------|
| `bridge.py` | 串联 Live + LCU 采集器，WS 发送到 Agent |
| `live_client.py` | 游戏内实时数据：血量、金币、装备、KDA、坐标、事件 |
| `lcu_client.py` | 大厅数据：召唤师、熟练度、符文、GameFlow、英雄选择 |

**检测的事件：**

| 事件 | 触发条件 |
|------|---------|
| `low_health` | HP < 30%（下降穿越阈值） |
| `death` | deaths 数增加 |
| `item_purchased` | 装备数增加 |
| `gold_spike` | 金币 delta > 500 |
| `kill` | 当前玩家 kills 增加 |
| `dragon_soon` | DragonKill 事件（附 5 分钟刷新时间） |
| `baron_soon` | BaronKill 事件（附 7 分钟刷新时间） |

**LCU 阶段追踪：**

- `gameflow_phase_change` — None → Lobby → Matchmaking → ReadyCheck → ChampSelect → InProgress → EndOfGame
- `lcu_champion_picked` — 英雄选择完成
- `lcu_game_start` — 游戏开始时推送召唤师+符文+熟练度

## 输出通道

### 1. Agent 日志

Collector 连接后，Agent 日志会输出每条 tip：

```
[INFO] [dragon] Dragon in 25s — ward river and prepare to contest.
```

### 2. Overlay 页面

浏览器打开 `http://localhost:8000/overlay`：
- 底部居中卡片，毛玻璃效果，淡入动画
- 内置 Web Speech API 语音播报（优先中文女声 Xiaoxiao）
- 右上角半透明历史记录（悬停可见）
- 支持静音、切换语音、测试语音
- 自动重连

### 3. Desktop Pet（桌面小玩偶）

`python companion.py`：
- 无边框置顶窗口（140×180），默认右下角
- 蓝色卡通角色（身体、眼睛、腮红、微笑、耳机、小手、短脚）
- 气泡显示教练建议（8 秒自动消失）
- pyttsx3 本地 TTS 朗读
- 可拖拽、右键菜单（静音/测试/退出）

## 环境变量完整列表

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API Key | - |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | LLM 模型名称 | `deepseek-chat` |
| `EMBEDDING_API_KEY` | Embedding API Key | 同 LLM_API_KEY fallback |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | - |
| `EMBEDDING_MODEL` | Embedding 模型 | `text-embedding-3-small` |
| `OPENAI_API_KEY` | 兼容旧变量（fallback） | - |
| `OPENAI_MODEL` | 兼容旧变量（fallback） | `gpt-4o-mini` |
| `REDIS_URL` | Redis 连接 | `redis://localhost:6379/0` |
| `AGENT_WS_URL` | Collector WS 地址 | `ws://localhost:8000/ws/collector` |
| `POLL_INTERVAL` | 采集轮询间隔（秒） | `1.0` |
| `PORT` | Agent 端口 | `8000` |

## 防抖 & 去重

| 机制 | 参数 | 说明 |
|------|------|------|
| MemoryQueue | 窗口 15s / 最多 2 条 / 同技能冷却 25s | LangGraph 前置过滤 |
| Event Engine (Go) | 龙 60s / 低血量 45s / 买装备 30s / 野区 120s / 策略 300s | 事件冷却 |
| Redis 去重 | validate 节点 | 同 skill_name 最近发过则跳过 |

## 开发说明

- Collector 通过 LoL lockfile 自动发现端口与认证信息
- 无对局时 Collector 会等待并重试
- LLM 调用失败时 Agent fallback 到 skill 模板文本
- PlayerMemory 在 Agent 关闭时自动持久化到本地文件
- 对局断开（>120s 游戏时间）自动生成对局摘要
