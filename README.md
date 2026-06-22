# Game Coach — LOL AI Coach Agent

实时读取英雄联盟客户端数据，检测游戏事件，由 AI Agent 结合 RAG 知识库和 LLM 生成精准 coaching 建议，通过 Overlay / Voice / Desktop Pet 多通道输出。

```
LOL Client ─┬─ Live Client Data API (127.0.0.1:2999) ─────┐
            └─ LCU API (lockfile 端口) ────────────────────┤
                                                           ▼
                                                  Collector (Go / Python)
                                                       │
                                         WebSocket state + event 推送
                                                       │
                                                       ▼
                                             Agent (Python FastAPI)
                                             ┌─ LangGraph 8 节点流水线 ─┐
                                             │  ① parse_event          │
                                             │  ② detect_signals       │
                                             │  ③ route_skill ────────┐│
                                             │  ④ retrieve_knowledge ←┤│
                                             │  ⑤ inject_memory       ││
                                             │  ⑥ llm_polish          ││
                                             │  ⑦ validate            ││
                                             │  ⑧ publish             ││
                                             └─────────────────────────┘│
                                             ┌──────────────────────────┘
                                             │  Skill 文件夹系统
                                             │  ├─ SKILL.md (指导方针)
                                             │  ├─ gotchas.md (坑点清单)
                                             │  ├─ references/ (深度参考)
                                             │  └─ scripts/ (执行脚本)
                                             └─ Planner 自动注册表 (YAML frontmatter)
                                                       │
                                            WebSocket tip 推送
                                                       │
                                     ┌─────────────────┼─────────────────┐
                                     ▼                 ▼                  ▼
                               Collector 日志      Overlay 页面      Desktop Pet
                                                 (Web Speech API)   (Edge TTS / PyQt6)
```

## 设计理念

### 从 Anthropic 博客汲取的经验

本项目的 Skill 系统设计参考了 Anthropic 内部几百个 skill 沉淀的工程经验：

| 原则 | 在本项目中的体现 |
|------|----------------|
| **Skill 是文件夹，不是文件** | 每个 skill 含 SKILL.md + references/ + gotchas.md + scripts/ |
| **Description 前 250 字符决定命运** | YAML frontmatter 中的 `description` 字段是 Planner 路由的核心依据 |
| **坑点清单含金量最高** | 每个 skill 的 `gotchas.md` 在 LLM 润色时作为最高信号注入 |
| **别说显而易见的事** | Skill 指导方针只写 Claude 无法从代码推断的游戏知识 |
| **验证类 skill 回报最大** | `review` skill 专门做对局复盘，是 Anthropic 实测效率最高的 skill 类型 |
| **渐进式披露** | 三层加载：description 常驻 → SKILL.md 按需 → references/ 懒加载 |
| **贵精不贵多** | 7 个 skill 各司其职，每个都落在 Anthropic 9 大分类中的某一类 |

### LLM 润色的三层上下文结构

```
┌──────────────────────────────────────┐
│  ① Coaching Guidelines (SKILL.md)    │  ← 该 skill 的指导方针
│  ② CRITICAL Gotchas (gotchas.md)    │  ← 绝对不能给的错误建议
│  ③ Game Knowledge (ChromaDB RAG)     │  ← 英雄攻略 + 游戏机制 + 装备
│  ④ Player Context (Memory)          │  ← 玩家历史和对局记忆
└──────────────────────────────────────┘
                ▼
         LLM (DeepSeek / OpenAI)
                ▼
         润色后的 Coaching Tip
```

---

## 项目结构

```
Game Coach/
├── agent/                          # Python — AI Agent (FastAPI + LangGraph)
│   ├── app.py                      #   主入口：WebSocket、LangGraph 编排、生命周期
│   ├── requirements.txt            #   Agent 依赖（含 pyyaml）
│   ├── Dockerfile                  #   Docker 镜像（基于 python:3.12-slim）
│   │
│   ├── models/                     # 数据模型（Pydantic）
│   │   └── state.py                #   GameState / CoachEvent / CoachingTip / WSMessage / Vec2 / Player 等
│   │
│   ├── graph/                      # LangGraph 图编排（状态机流水线）
│   │   ├── state.py                #   CoachState TypedDict — 20+ 字段在 8 个节点间流转
│   │   ├── nodes.py                #   8 个节点函数：parse_event → detect_signals → route_skill
│   │   │                           #     → retrieve_knowledge → inject_memory → llm_polish → validate → publish
│   │   └── builder.py              #   StateGraph 构建器 + 3 个条件路由分支
│   │
│   ├── planner/                    # 事件 → Skill 路由器
│   │   └── planner.py              #   Planner 类 — 启动时从 SKILL.md frontmatter 自动构建注册表
│   │                               #   提供 get_skill_context() / get_skill_gotchas() 工具函数
│   │
│   ├── skills/                     # ⭐ 7 个文件夹型 Skill（Anthropic 风格）
│   │   ├── survival/               #   [Runbook 类] 血量 < 30% 回城判断
│   │   │   ├── SKILL.md            #     何时触发 + 建议结构 + 输出风格
│   │   │   ├── gotchas.md          #     坑点清单：30% ≠ 必须回城 / 法师 50% 危险 / 炮车线不能回
│   │   │   └── references/         #     深度参考：回城时机 / 危险区域 / 关键眼位
│   │   ├── dragon/                 #   [Runbook 类] 龙/大龙视野和站位
│   │   │   ├── SKILL.md            #     何时触发 + 建议结构 + 输出风格
│   │   │   ├── gotchas.md          #     坑点清单：放龙换先锋 / 大龙逼团不开龙 / 偷龙风险
│   │   │   └── references/         #     深度参考：6 种龙 buff / 龙魂团战 / 大龙视野
│   │   ├── laning/                 #   [业务自动化类] 对线期换血/兵线/等级节点
│   │   │   ├── SKILL.md            #     仅在对线期（<14min）触发，每 3 分钟
│   │   │   ├── gotchas.md          #     坑点清单：不能默认推线 / 法师 50% 就危险 / 先升 2 是无敌窗口
│   │   │   └── references/         #     深度参考：兵线管理 / 换血时机 / 关键等级 / 英雄对位法则
│   │   ├── build/                  #   [库和 API 参考类] 出装克制/顺序/核心装
│   │   │   ├── SKILL.md            #     买装备或金币暴增时触发
│   │   │   ├── gotchas.md          #     坑点清单：不要裸重伤 / 水银 vs 布甲 / 鞋>二级工资装
│   │   │   └── references/         #     深度参考：核心装备 / 克制装备速查 / 出装顺序优先级
│   │   ├── macro/                  #   [业务自动化类] 中期轮转/分带/目标优先级
│   │   │   ├── SKILL.md            #     14 分钟后每 5 分钟触发
│   │   │   ├── gotchas.md          #     坑点清单：别建议"集合打团" / 分带黄金法则 / 先锋换龙是亏的
│   │   │   └── references/         #     深度参考：对线→中期转换 / 边线管理 / 目标优先级 / 打野追踪
│   │   ├── teamfight/              #   [Runbook 类] 团战目标/站位/开团判断
│   │   │   ├── SKILL.md            #     短时间（15s）≥3 击杀时触发
│   │   │   ├── gotchas.md          #     坑点清单：别让刺客打前排 / AD 活着最重要 / 控制别重叠
│   │   │   └── references/         #     深度参考：各位置站位 / 目标优先级 / 开团/反打/撤退
│   │   └── review/                 #   ★ [验证类] 对局复盘分析（ROI 最高）
│   │       ├── SKILL.md            #     游戏结束时触发，输出结构化复盘
│   │       ├── gotchas.md          #     坑点清单：KDA 好看≠打得好 / 70% 的游戏有翻盘机会
│   │       ├── references/         #     深度参考：KPI 基准 / 死亡分类 / 改进计划
│   │       └── scripts/            #     可执行脚本：analyze.py 对局数据分析
│   │
│   ├── llm/                        # LLM 客户端
│   │   └── openai_client.py        #   OpenAIClient — 支持 DeepSeek / OpenAI / 任何兼容 API
│   │
│   ├── prompt/                     # 提示词
│   │   └── coach_prompt.py         #   System prompt — 简洁 LOL 教练角色定义
│   │
│   ├── knowledge/                  # 向量知识库 (ChromaDB + Embedding + RAG)
│   │   ├── chroma_store.py         #   ChromaDB 封装 — 管理 6 个 Collection，cosine 相似度
│   │   ├── embedder.py             #   Embedding — OpenAI 或火山引擎豆包（Doubao）
│   │   ├── retriever.py            #   统一检索接口 + aggregate_coaching_context() 多源聚合
│   │   ├── ingest.py               #   数据摄入脚本 + GuideGenerator 自动生成 172 英雄攻略
│   │   ├── data_fetcher.py         #   Data Dragon API 数据抓取（英雄/装备/符文/召唤师技能）
│   │   └── data/                   #   缓存数据 + 手工 markdown 攻略
│   │
│   ├── memory/                     # 玩家记忆系统（DeerFlow 风格三级记忆）
│   │   ├── models.py               #   PlayerMemory — 用户画像 + 历史记录 + 策略偏好
│   │   ├── store.py                #   MemoryStore — 本地 JSON 持久化
│   │   ├── redis_store.py          #   RedisStore — 会话级状态缓存 + tip 去重
│   │   ├── injector.py             #   MemoryInjector — 格式化记忆为 LLM 可读上下文
│   │   ├── queue.py                #   MemoryQueue — 防抖队列（15s 窗口 / 最多 2 条）
│   │   └── coach_engine.py         #   CoachEngine — 对局断开时生成摘要
│   │
│   ├── collector/                  # Python 版采集器（两个数据源 + 桥接层）
│   │   ├── bridge.py               #   CollectorBridge — 串联 Live + LCU → Agent WebSocket
│   │   ├── live_client.py          #   LiveClientCollector — Live Client Data API（游戏内数据）
│   │   └── lcu_client.py           #   LCUClientCollector — LCU API（大厅数据：召唤师/熟练度/
│   │                               #    符文/GameFlow 阶段追踪/英雄选择）
│   │
│   ├── middleware/                 # 中间件
│   │   └── game_middleware.py       #   GameMiddleware — state/event 消息拦截与预处理
│   │
├── collector/                       # Go — 轻量版采集器 & 事件检测（可选，性能更好）
│   ├── cmd/
│   │   ├── main.go                 #   主入口：轮询 + 事件检测 + WS 发送，信号处理
│   │   └── config.go               #   YAML 配置加载 + 环境变量覆盖
│   ├── config/
│   │   └── config.yaml             #   默认配置（agent_ws_url / poll_interval_sec / lockfile_path）
│   ├── internal/
│   │   ├── lol/
│   │   │   ├── client.go           #   HTTP 客户端 — 自动解析 lockfile 获取端口/密码
│   │   │   ├── parser.go           #   JSON 解析 — 完整的 Go 结构体映射
│   │   │   ├── state.go            #   GameState 工具方法 — IsInGame / EnemyPlayers / ItemCount
│   │   │   └── objectives.go       #   ObjectiveTracker — 龙/大龙刷新追踪 + 事件去重
│   │   ├── event/
│   │   │   ├── detector.go         #   6 种事件检测 — 龙刷新/低血量/买装备/野区/策略 + 定时器
│   │   │   └── engine.go           #   EventEngine — 带冷却的检测封装
│   │   └── sender/
│   │       └── websocket.go        #   WebSocket 发送器 — 双工，接收 Agent tip 日志打印
│   ├── go.mod
│   └── go.sum
│
├── desktop_pet/                     # Python — 桌面小玩偶（独立进程，PyQt6）
│   ├── main.py                      #   入口：QApplication + Win32 全局拖拽
│   ├── pet_controller.py            #   控制器：协调 UI / TTS / WebSocket
│   ├── window.py                    #   无边框置顶窗口（DWM 阴影、右键菜单）
│   ├── pet_widget.py                #   QPainter 手绘角色 + 说话气泡动画
│   ├── ws_client.py                 #   TipClient (QThread) — 订阅 /ws/overlay
│   ├── tts_engine.py                #   Edge TTS（首选）→ pyttsx3（离线回退）
│   ├── live2d_widget.py             #   可选 Live2D 渲染（QWebEngineView）
│   ├── live2d_html/                 #   Live2D / Canvas 回退页面
│   └── requirements.txt             #   桌宠独立依赖
│
├── docker-compose.yml               # Redis 7 Alpine + Agent 容器编排
├── .env.example                     # 环境变量模板（12 个变量）
└── README.md
```

---

## 快速开始

### 前置条件

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.12+ | Agent 运行环境 |
| Go | 1.22+ | Go 版 Collector（可选，也可用 Python 版 bridge.py） |
| Docker | 最新 | 运行 Redis（也可本地安装） |
| LOL 客户端 | 最新 | 需要对局进行中才能使用 Live Client API |
| Redis | 7.x | 去重和状态缓存（Docker 或本地均可） |

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 填入配置：

```ini
# ── LLM（支持 DeepSeek / OpenAI / 任何 OpenAI 兼容 API）──
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# ── Embedding（OpenAI 或火山引擎豆包）──
EMBEDDING_API_KEY=sk-your-embedding-key
EMBEDDING_MODEL=text-embedding-3-small

# ── Redis ──
REDIS_URL=redis://localhost:6379/0

# ── Collector Agent 通信 ──
AGENT_WS_URL=ws://localhost:8000/ws/collector
POLL_INTERVAL=1.0
PORT=8000
```

### 2. 启动 Redis

```bash
# Docker 方式（推荐）
docker compose up -d redis

# 或本地安装 Redis 后直接运行
redis-server
```

### 3. 安装 Python 依赖

```bash
cd agent
pip install -r requirements.txt
```

依赖清单：

```
fastapi>=0.115.0        # Web 框架
uvicorn[standard]>=0.32.0  # ASGI 服务器
websockets>=13.0        # WebSocket 通信
pydantic>=2.9.0         # 数据验证
redis>=5.2.0            # Redis 客户端
openai>=1.55.0          # LLM API 客户端
python-dotenv>=1.0.0    # 环境变量加载
chromadb>=0.5.0         # 向量数据库
langgraph>=0.2.0        # LangChain 图编排框架
pyyaml>=6.0             # YAML frontmatter 解析
```

桌宠依赖（独立安装，见步骤 8）：

```
PyQt6>=6.6.0            # 无边框桌面窗口
websocket-client>=1.8.0 # WebSocket 客户端
edge-tts>=6.1.0         # 中文 TTS（首选）
pyttsx3>=2.90           # 离线 TTS 回退
```

### 4. 准备知识库

```bash
cd agent

# 步骤 1：从 Riot Data Dragon 下载最新游戏数据
# （英雄属性、装备、符文、召唤师技能 —— 约 172 个英雄 + 200+ 装备）
python -m knowledge.data_fetcher

# 步骤 2：向量化摄入到 ChromaDB
# （自动为没有手工 markdown 的英雄生成攻略，共 6 个 Collection）
python -m knowledge.ingest
```

摄入完成后，`agent/chroma_data/` 目录下会有 6 个 Collection：
- `lol_items` — 装备
- `lol_champions` — 英雄技能
- `lol_champion_guides` — 英雄攻略
- `lol_runes` — 符文
- `lol_summoner_spells` — 召唤师技能
- `lol_game_info` — 游戏机制

### 5. 启动 Agent

```bash
cd agent
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

启动日志会显示：

```
INFO:agent:Game Coach Agent ready — skills=7, nodes=9, rag=on
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 6. 启动 Collector

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

### 7. 验证

| 端点 | 方法 | 说明 |
|------|------|------|
| `http://localhost:8000/health` | GET | Agent 健康检查 — 服务状态 + 记忆统计 |
| `http://localhost:8000/tips/latest` | GET | 最新状态和建议 — 当前英雄/阶段/记忆概况 |

### 8. （可选）启动桌面小玩偶

桌宠是独立进程，通过 WebSocket 订阅 Agent 的 coaching tip，不参与 LangGraph 流水线。

```bash
# 在项目根目录
pip install -r desktop_pet/requirements.txt
python -m desktop_pet.main
```

连接地址默认为 `ws://localhost:8000/ws/overlay`，可通过环境变量 `AGENT_HOST` / `AGENT_PORT` 修改。

### 9. （可选）Docker 完整部署

```bash
# 确保 .env 已配置
docker compose up -d

# 查看日志
docker compose logs -f agent
```

---

## 核心架构详解

### LangGraph 8 节点流水线

```
parse_event ─── 无效事件 → END
  │
  ▼
detect_signals ─ 玩家死亡且非龙/大龙 → END
  │
  ▼
route_skill ─── 无匹配技能 → END
  │   │
  │   └─ 加载 SKILL.md 正文 + gotchas.md 坑点清单
  │
  ▼
retrieve_knowledge (ChromaDB RAG 多源聚合)
  │   └─ Champion 攻略 + 对位敌人 + 游戏机制 + 装备推荐
  ▼
inject_memory (PlayerMemory 格式化注入)
  │   └─ 截断至 200 token
  ▼
llm_polish (DeepSeek / OpenAI 润色)
  │   └─ 注入四层上下文：Guidelines → Gotchas → RAG → Memory
  ▼
validate ────── 去重/冷却 → END
  │   └─ Redis 检查：同 skill 最近发送过则跳过
  ▼
publish (标记已发送 + 构建最终 tip)
```

### 各节点详解

| 节点 | 职责 | 关键逻辑 |
|------|------|---------|
| `parse_event` | 提取 event_name / event_data | 初始化所有中间字段，is_valid 取决于是否有 name |
| `detect_signals` | 死亡过滤 + 信号分类 | 死亡时跳过非龙/大龙事件；分类为 low_health / objective_stage / power_spike |
| `route_skill` | 事件 → Skill + 加载上下文 | 调用 Planner.plan()；加载 SKILL.md 正文和 gotchas.md；拼接 RAG 查询 |
| `retrieve_knowledge` | ChromaDB 多源 RAG | 己方英雄攻略 + 对位敌人攻略 + 游戏机制 + 装备建议 → 聚合为一条文本 |
| `inject_memory` | 记忆注入 | PlayerMemory 格式化为 200 token 的上下文 |
| `llm_polish` | LLM 润色 | 注入四层上下文：Coaching Guidelines → Gotchas → RAG → Memory |
| `validate` | Redis 去重 | 同 skill_name + session_id 最近发送过则标记 should_publish=False |
| `publish` | 标记发送 | Redis mark_tip_sent() + 构建 CoachingTip 输出 |

### Skill 文件夹系统详解

每个 skill 是一个文件夹，遵循 Anthropic 推荐的渐进式披露设计：

```
skills/{skill_name}/
├── SKILL.md              ★ 必需 — YAML frontmatter + 指导方针
│   ├── YAML frontmatter  → 启动时解析为 SKILL_REGISTRY
│   │   ├── name          注册名
│   │   ├── description   ★ 触发条件（前 250 字符最关键）
│   │   ├── events        关联的事件名列表
│   │   ├── priority      默认优先级（1-3）
│   │   └── cooldown      冷却时间（秒）
│   └── Markdown body     → 在 llm_polish 阶段作为 "Coaching Guidelines" 注入
│
├── gotchas.md            ★ 坑点清单 — 全系统最高信号内容
│   └── 只写 Claude 无法从代码推断的游戏知识
│
├── references/           可选 — 深度参考材料（懒加载，LLM 真正需要才读取）
│   ├── *.md              领域专题文档
│   └── ...
│
└── scripts/              可选 — 可执行脚本
    └── *.py              工具函数
```

**加载机制**

```
启动时（一次性）
  Planner.__init__ → build_registry()
  ├─ 遍历 skills/ 下每个子目录
  ├─ 解析 SKILL.md YAML frontmatter
  └─ 构建 EVENT_TO_SKILL 反向索引

运行时（每次事件）
  route_skill 节点
  ├─ Planner.plan() → 查 EVENT_TO_SKILL → 返回 CoachingTip
  ├─ get_skill_context() → 加载 SKILL.md 正文
  └─ get_skill_gotchas() → 加载 gotchas.md

  llm_polish 节点
  └─ 注入四层上下文：Guidelines → Gotchas → RAG → Memory
```

### 7 个 Skill 对照 Anthropic 9 分法

| Skill | Anthropic 分类 | 触发事件 | 优先级 | 冷却 | 核心价值 |
|-------|---------------|---------|--------|------|---------|
| `survival` | Runbook 排障手册 | `low_health`, `death` | 3 | 45s | 血量 < 30% 时判断回城 vs 继续 |
| `dragon` | Runbook 排障手册 | `dragon_soon`, `baron_soon` | 2-3 | 60s | 龙/大龙刷新时提供视野 & 站位 |
| `laning` | 业务流程自动化 | `laning_check`（<14min 每 3min） | 1 | 180s | 对线换血/兵线管理/等级节点 |
| `build` | 库和 API 参考 | `item_purchased`, `gold_spike` | 1 | 30s | 装备选择/克制出装/出装顺序 |
| `macro` | 业务流程自动化 | `macro_check`（>14min 每 5min） | 1 | 300s | 地图轮转/分带/目标优先级 |
| `teamfight` | Runbook 排障手册 | `teamfight_detected` | 2 | 90s | 团战目标选择/站位/开团判断 |
| `review` | ★ 产品验证 | `game_end` | 3 | 0s | 对局复盘 — Anthropic 实测 ROI 最高 |

---

## WebSocket 协议

### 端点

| 端点 | 通信方 | 方向 | 说明 |
|------|--------|------|------|
| `/ws/collector` | Collector ↔ Agent | 双工 | Collector 发送 state + event；Agent 返回 tip |
| `/ws/overlay` | Overlay / Desktop Pet ↔ Agent | 广播 | Agent 广播 tip 到所有 overlay 客户端 |

### 消息格式

所有消息使用统一的 JSON 格式：

```json
{"type": "<消息类型>", "payload": { ... }}
```

**Collector → Agent**

```json
// State 消息 — 每轮轮询发送完整游戏状态
{
  "type": "state",
  "payload": {
    "game_time": 300.5,
    "active_player": {
      "summoner_name": "Ahri",
      "level": 6,
      "current_gold": 1200,
      "current_health": 450,
      "max_health": 800,
      "position": {"x": 7000, "y": 6000}
    },
    "all_players": [ ... ],
    "events": [ ... ],
    "dragon_timer": {"type": "Infernal", "spawn_at": 305.0, "seconds_left": 4.5},
    "baron_timer": null
  }
}

// Event 消息 — 事件检测到后立即发送
{
  "type": "event",
  "payload": {
    "name": "dragon_soon",
    "data": {"dragon_type": "Infernal", "seconds_left": 25}
  }
}
```

**Agent → Collector / Overlay**

```json
{
  "type": "tip",
  "payload": {
    "message": "Infernal Dragon in 25s — ward river south of mid, position near blue buff entrance. Contest if your jungler is nearby.",
    "skill": "dragon",
    "priority": 2
  }
}
```

### 消息类型枚举

| Type | 方向 | 说明 |
|------|------|------|
| `state` | Collector → Agent | 完整游戏状态（1s 轮询） |
| `event` | Collector → Agent | 检测到的事件 |
| `tip` | Agent → Collector / Overlay | 教练建议（LangGraph 流水线输出） |
| `ping` | Overlay / Desktop Pet → Agent | 心跳保活（Agent 忽略，不断开连接） |

---

## 知识库 (ChromaDB)

### 知识摄入流程

```
Data Dragon API (Riot 官方)
       │
       ▼
data_fetcher.py ───────── 下载 JSON 数据
       │                   ├─ champions.json (172 英雄)
       │                   ├─ items.json (200+ 装备)
       │                   ├─ runes.json (5 路径 × 3 基石 × 9 符文)
       │                   └─ summoner.json (11 召唤师技能)
       │
       ▼
ingest.py ─────────────── 向量化摄入
       │                   ├─ ItemFormatter  → 装备 → 自然语言
       │                   ├─ ChampionFormatter → 技能 → 自然语言
       │                   ├─ GuideGenerator → 自动生成 172 英雄攻略
       │                   │   ├─ 6 种角色模板（战士/坦克/刺客/法师/射手/辅助）
       │                   │   ├─ 3 阶段策略（early 0-14 / mid 15-25 / late 25+）
       │                   │   ├─ 技能连招 + 使用技巧 + 对抗技巧
       │                   │   └─ 出装建议（AD/AP 自适应）
       │                   └─ 分 6 个 Collection，50 条一批嵌入
       │
       ▼
ChromaDB (chroma_data/)
       ├─ lol_items           → 装备语义搜索
       ├─ lol_champions        → 英雄技能查询
       ├─ lol_champion_guides  → 攻略按英雄+阶段检索
       ├─ lol_runes            → 符文系统
       ├─ lol_summoner_spells  → 召唤师技能
       └─ lol_game_info        → 游戏机制常识
```

### 多源聚合 RAG

`aggregate_coaching_context()` 是检索的核心方法，整合四层知识：

```
输入：英雄名 + 对位敌人 + 游戏时间 + 事件类型
  │
  ├─ ① 己方攻略检索
  │    └─ search_guide_by_time(champion, game_time)
  │        自动推断阶段（early < 14min < mid < 25min < late）
  │
  ├─ ② 对位敌人检索
  │    └─ search_guide(enemy_champion, phase)
  │        + 筛选含 "counter" / "对抗" 的段落
  │
  ├─ ③ 通用游戏知识
  │    └─ search_game_info(event_query)
  │        龙机制 / 大龙 / 地图 / 野怪
  │
  └─ ④ 装备推荐（仅物品事件）
       └─ search_items(event_query)
       去重 + 截断 ≤ 500 字符 → 返回聚合文本
```

---

## 数据采集

### 两套 Collector

| 特性 | Go 版 (collector/) | Python 版 (agent/collector/) |
|------|-------------------|------------------------------|
| 语言 | Go 1.22+ | Python 3.12+ |
| 数据源 | Live Client API | Live Client API + LCU API |
| 事件检测 | Go 原生（内置冷却 Engine） | Python（回调通知回调） |
| 大厅数据 | 无 | 召唤师信息 / 英雄熟练度 / 符文 / GameFlow 阶段 |
| 性能 | 轻量，低资源占用 | 功能丰富，依赖较多 |
| 推荐 | ✅ 生产环境 | 开发 / 需要大厅数据时 |

### 检测的事件

| 事件名 | 触发条件 | 来源 |
|--------|---------|------|
| `low_health` | HP < 30%（从 ≥30% 穿越下降） | Live Client API |
| `death` | deaths 计数增加 | Live Client API |
| `item_purchased` | 装备数量增加，逐个找出新装备 | Live Client API |
| `gold_spike` | 金币 delta > 500 | Live Client API |
| `kill` | 当前玩家 kills 增加 | Live Client API |
| `dragon_soon` | 龙被击杀 → 下一条刷新时间（5 分钟间隔） | Live Client API event |
| `baron_soon` | 大龙被击杀 → 下一条刷新时间（6 分钟间隔） | Live Client API event |
| `laning_check` | < 14 分钟，每 3 分钟（Go detector） | 定时器 |
| `macro_check` | > 14 分钟，每 5 分钟（Go detector） | 定时器 |
| `teamfight_detected` | 15 秒内 ≥ 3 个击杀事件 | 待实现 |
| `game_end` | GameFlow → EndOfGame / Collector 断开 | LCU API / WS 断开 |

### LCU 阶段追踪（仅 Python 版）

```
None → Lobby → Matchmaking → ReadyCheck → ChampSelect → InProgress → EndOfGame
                                           │
                                           └─ lcu_champion_picked (英雄选择完成)
                                           └─ lcu_game_start (游戏开始，推送召唤师+符文+熟练度)
```

---

## 输出通道

### 1. Agent 日志

Collector 连接后，Agent 日志实时输出每条 coaching tip：

```
[INFO] [dragon] Infernal Dragon in 25s — ward river south of mid, position near blue buff entrance.
[INFO] [survival] Low HP (22%) — recall immediately and buy Sorcerer's Shoes.
[INFO] [build] Item purchased — next go Void Staff, they already stacked MR.
[INFO] [review] Game ended — CS 187 @ 28min (6.7/min), needs improvement. 3 deaths to ganks.
```

### 2. Desktop Pet 桌面小玩偶（`desktop_pet/`）

独立 PyQt6 客户端，通过 `/ws/overlay` 接收 tip 并语音播报。

```
Agent publish 节点
       │ 广播 {"type":"tip", "payload":{skill, message}}
       ▼
TipClient (QThread)          ← ws://localhost:8000/ws/overlay
       │ pyqtSignal
       ▼
PetController
  ├─ PetWidget      QPainter 手绘角色 + 气泡（~30fps 动画）
  ├─ TTSEngine      Edge TTS → pyttsx3 → 静音
  └─ FramelessPetWindow  无边框置顶、Win32 系统拖拽、右键菜单
```

特性：
- 320×420 无边框置顶窗口，默认屏幕右下角
- QPainter 手绘角色（眨眼、上下浮动动画）+ 8 秒自动消失的气泡
- Edge TTS 中文朗读（`zh-CN-XiaoxiaoNeural`，可通过 `EDGE_VOICE` / `EDGE_RATE` 配置）
- 每 15 秒向 Agent 发送 `{"type":"ping"}` 心跳保活
- 右键菜单：静音 / 测试语音 / 退出；双击退出；Escape 键退出
- 可选 `live2d_widget.py`：QWebEngineView + Live2D（需 Cubism SDK，默认未启用）

---

## 环境变量完整列表

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `LLM_API_KEY` | LLM API Key | - | ✅ |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.deepseek.com/v1` | - |
| `LLM_MODEL` | LLM 模型名称 | `deepseek-chat` | - |
| `EMBEDDING_API_KEY` | Embedding API Key | 同 LLM_API_KEY | ✅ |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | - | - |
| `EMBEDDING_MODEL` | Embedding 模型 | `text-embedding-3-small` | - |
| `OPENAI_API_KEY` | 兼容旧变量（LLM fallback） | - | - |
| `OPENAI_MODEL` | 兼容旧变量（LLM fallback） | `gpt-4o-mini` | - |
| `REDIS_URL` | Redis 连接字符串 | `redis://localhost:6379/0` | ✅ |
| `AGENT_WS_URL` | Collector → Agent 的 WebSocket 地址 | `ws://localhost:8000/ws/collector` | - |
| `POLL_INTERVAL` | 采集轮询间隔（秒） | `1.0` | - |
| `PORT` | Agent HTTP/WS 服务端口 | `8000` | - |
| `AGENT_HOST` | 桌宠连接 Agent 的主机 | `localhost` | - |
| `AGENT_PORT` | 桌宠连接 Agent 的端口 | `8000` | - |
| `EDGE_VOICE` | Edge TTS 音色 | `zh-CN-XiaoxiaoNeural` | - |
| `EDGE_RATE` | Edge TTS 语速 | `+15%` | - |

---

## 防抖 & 去重 (三层机制)

| 层次 | 组件 | 位置 | 参数 |
|------|------|------|------|
| **第一层** | MemoryQueue | Agent 入口（LangGraph 前置） | 窗口 15s / 最多 2 条 / 同技能冷却 25s |
| **第二层** | Event Engine | Go Collector（事件检测层） | 龙 60s / 低血量 45s / 买装备 30s / 野区 120s / 策略 300s |
| **第三层** | Redis 去重 | LangGraph validate 节点 | 同 session + skill_name 最近发送过则跳过 |

---

## 开发说明

### 添加新 Skill

1. 创建文件夹 `agent/skills/{skill_name}/`
2. 编写 `SKILL.md`（必须包含 YAML frontmatter + 指导方针）
3. 编写 `gotchas.md`（坑点清单，只写 Claude 推断不出来的）
4. 编写 `references/*.md`（深度参考，可选）
5. 编写 `scripts/*.py`（执行脚本，可选）

Agent 启动时自动扫描并注册，无需修改任何代码。

### Skill 编写最佳实践

- **YAML frontmatter 的 `description` 前 250 字符最关键** — 它是 Planner 判断何时触发 skill 的唯一依据
- **正文只写 Claude 推断不出来的** — 删掉它本来就会的（比如"注意安全"、"好好补刀"）
- **坑点清单持续攒** — 每次发现模型给出不当建议，就补一条进 gotchas.md
- **一个 skill 只做一件事** — 横跨多个游戏阶段的 skill 会让 agent 困惑

### 注意事项

- Collector 通过 LoL lockfile 自动发现端口与认证信息
- 无对局时 Collector 会等待并重试
- LLM 调用失败时 Agent fallback 到 skill 模板文本
- PlayerMemory 在 Agent 关闭时自动持久化到 `agent/memory_data/`
- 对局断开（游戏时间 ≥ 120s）时自动生成对局摘要
- `pyyaml>=6.0` 是 yarn frontmatter 解析的必要依赖
