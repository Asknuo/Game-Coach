# Game Coach — 问题记录与优化方案

> 本文档记录系统审查中发现的所有 Bug 和可优化的设计问题，以及对应用过的和计划中的修复方案。

---

## 一、已修复的问题

### #1 state 消息每 0.5s 无条件发送
- **文件**: `agent/collector/live_client.py` → `_detect_events()`
- **问题**: 每次轮询都发送完整 state，99% 的消息是重复数据，一局产生 ~1800 条无效消息
- **修复**: 改为"有事件才附带 state"，无事件时完全静默；轮询间隔从 0.5s 放宽到 1.0s
- **日期**: 2026-06-19

### #2 没有对面装备变化的检测
- **文件**: `agent/collector/live_client.py`, `agent/skills/build/SKILL.md`
- **问题**: `item_purchased` 只检测自己的装备，不知道对面出了什么装备
- **修复**: 新增 `_enemy_items_snapshot` 快照 + `_detect_enemy_items()` 方法，对比对面 5 人的装备变化；`build` Skill 新增 `enemy_item_purchased` 事件
- **日期**: 2026-06-19

### #3 itemID 无法翻译为装备名称
- **文件**: `agent/knowledge/data_fetcher.py`, `agent/knowledge/item_resolver.py` (新建)
- **问题**: `data_fetcher.py` 保存 items 为数组时丢弃了 itemID key，且没有 ID→名称的查找表
- **修复**: `fetch_items()` 改为保存 `{itemID: {...}}` dict 格式；新建 `ItemResolver` 类提供 O(1) 的 ID→名称映射，含 200+ 热门装备硬编码回退表
- **日期**: 2026-06-19

---

## 二、当前会话中修复的问题 ✅

### #4 紧急事件反馈延迟过大 (P0)
- **文件**: `agent/app.py` → `collector_ws`, `agent/memory/queue.py`
- **问题**: 所有事件统一进入 15 秒防抖队列，紧急事件（低血量、即将刷龙）的反馈严重滞后。低血量事件发出时玩家已经在 10 秒前回城了，建议完全无用
- **修复**: 紧急事件（`low_health`, `death`, `dragon_soon`/`baron_soon` with `seconds_left <= 30`）绕过队列直接送流水线；非紧急事件保持 15s 窗口机制
- **日期**: 2026-06-19

### #5 坐标无效时仍被使用 (P1)
- **文件**: `agent/graph/nodes.py` → `retrieve_knowledge()`
- **问题**: 迷雾中的敌人可能返回 `(0, 0)` 坐标（API 未返回有效值），但距离计算器仍会将其当作合法坐标，导致 LLM 基于错误位置生成建议
- **修复**: 在 `retrieve_knowledge` 中增加坐标有效性校验，拒绝 `(0, 0)` 或不存在的坐标
- **日期**: 2026-06-19

### #6 低血量事件上下文缺失 (P1)
- **文件**: `agent/graph/nodes.py` → `detect_signals()`
- **问题**: 纯阈值触发（hp < 30%），不考虑你在泉水回血、刚单杀对面、或对面也残血的情况
- **修复**: 在 `detect_signals` 中增加上下文抑制逻辑：
  - 在泉水（距离泉水 < 1500）→ 跳过
  - 刚击杀敌人（< 5s 内）→ 跳过，已经安全
  - 对面也残血 + 你有技能 → 降级为提示"可以尝试击杀"
- **日期**: 2026-06-19

### #7 心跳机制缺失 (P1)
- **文件**: `agent/collector/live_client.py`
- **问题**: 改为"有事件才发 state"后，如果长时间（15 分钟）无事件，Agent 的 `latest_state` 会严重过时，突然来一个事件时用的 state 数据全错
- **修复**: 新增心跳机制：超过 30 秒没有事件触发时，主动发送一次轻量 state 刷新 Agent 快照（不触发事件处理）
- **日期**: 2026-06-19

### #8 装备检测只看新增不看卖出/合成 (P2)
- **文件**: `agent/collector/live_client.py` → `_detect_enemy_items()`, `item_purchased` 检测
- **问题**: 只检测"多了什么装备"，不检测"少了什么装备"（卖出）或"什么合成了"（slot 替换）。敌人卖了多兰剑不被感知；两个散件合成大件被误报为"新增了一件"
- **修复**: 对比完整装备快照，区分三种变化：
  - `gained`: 新增（slot 上出现了新 itemID）
  - `lost`: 消失（slot 上的 itemID 不再存在）
  - `upgraded`: 同 slot 的 itemID 变了 + 总装备数不变 → 合并为合成事件
- **日期**: 2026-06-19

### #9 publish 节点缺少时效性检查 (P2)
- **文件**: `agent/graph/nodes.py` → `publish()`
- **问题**: LLM 生成的建议可能在发出时已经过时（dragon_soon → 龙已被偷；low_health → 已回血）。publish 节点没有任何时效性校验
- **修复**: 在 publish 节点前对比当前 state 和事件发生时的 state，关键条件已变则放弃建议：
  - `low_health`: 当前血量已 > 50% → 取消
  - `dragon_soon`: 龙已被击杀 → 取消
  - `item_purchased`: 装备已不在栏位 → 取消
- **日期**: 2026-06-19

---

## 三、本会话中修复的问题 ✅

### #10 双采集器冗余 (架构)
- **文件**: `agent/detector.py` (新建), `agent/app.py`
- **问题**: Go 和 Python 两套 Collector 做相同的事；Python 采集器有更丰富的事件检测（death/kill/enemy_item等），但只有单独启动 bridge.py 才能用
- **修复**: 创建 `agent/detector.py` EventDetector 类，将 Python 采集器的事件检测逻辑提取为独立模块。Agent 的 `collector_ws` 在每次收到 Go Collector 的 state 后自动运行 Detector，检测高级事件（death/kill/item_upgraded/enemy_item_purchased）。Python 采集器保留为独立工具但不再是必需品
- **日期**: 2026-06-19

### #11 桌面玩偶用本地 TTS，体验差 (UX)
- **文件**: `agent/companion.py`
- **问题**: `pyttsx3` 本地 TTS 声音机械化、中文效果差
- **修复**: 改为三级 TTS 链路：Edge TTS（首选，微软免费 API，中文自然度高）→ pyttsx3（回退）→ 静音。Edge TTS 使用 `zh-CN-XiaoxiaoNeural` 女声 + 15% 语速加速。音频通过系统 Media.SoundPlayer 跨平台播放。环境变量 `EDGE_VOICE` / `EDGE_RATE` 可自定义
- **日期**: 2026-06-19

### #12 ChromaDB 知识库一次性摄入，不更新 (数据)
- **文件**: `agent/knowledge/chroma_store.py`, `agent/knowledge/ingest.py`, `agent/app.py`
- **问题**: 装备改了、英雄调了、meta 变了 → ChromaDB 向量库停在摄入那一刻
- **修复**: `ChromaStore` 新增 `needs_refresh()`（检查 `.last_ingest` 时间戳 + collection 是否为空）和 `mark_ingested()`（写入时间戳）。`ingest.py` 的 `ingest_all()` 完成后自动调用 `mark_ingested()`。`app.py` 的 `lifespan` 启动时自动检查新鲜度，超过 7 天或首次启动 → 自动运行 `Ingestor().ingest_all()`
- **日期**: 2026-06-19

### #13 SKILL.md 是静态手写规则，无自我迭代 (AI)
- **文件**: `agent/memory/redis_store.py`, `agent/app.py`
- **问题**: LLM 的建议被玩家忽略后不会自动降权
- **修复**: `RedisStore` 新增反馈闭环方法：
  - `record_advice_given()` — 发布建议后记录上下文（血量/装备数等）
  - `check_advice_followed()` — 下次 state 到达时对比关键指标（HP 恢复 > 30%? 装备增加?）
  - `adjust_skill_confidence()` — 被采纳 +0.05, 未被采纳 -0.03, 范围 0.5~1.5
  `app.py` 在 publish 后记录建议，state 到达时检查反馈
- **日期**: 2026-06-19

---

## 四、待处理问题

> 当前无待处理问题。所有已知问题均已修复。

---

## 五、优化后的数据流

```
游戏客户端 → Live Client API (127.0.0.1:2999)
     │
     ▼
Go Collector (1s 轮询，事件检测)
     │
     │  心跳: 30s 无事件 → 轻量 state
     │  有事件 → state + event 一起发
     │
     ▼
WebSocket → Python Agent
     │
     ├── EventDetector: 检测 Go 未覆盖的高级事件
     │   (death/kill/enemy_item/item_sold/item_upgraded/gold_spike)
     │
     ├── MapZones: 坐标 → 区域语义
     │   "你在上路河道，对面劫在小龙坑"
     │
     ├── 紧急事件 (low_health/death/dragon_soon):
     │     绕过队列 → 直接进 LangGraph 流水线
     │
     ├── 非紧急事件:
     │     入队 → 15s 窗口 → 按优先级排序 → 流水线
     │
     ▼
LangGraph 8节点流水线:
  1. parse_event       — 解析
  2. detect_signals     — 信号检测 + 上下文抑制
  3. route_skill        — Skill 路由 + RAG 查询拼接
  4. retrieve_knowledge — ChromaDB 检索 + 坐标校验
  5. inject_memory      — 玩家记忆 + 区域信息注入
  6. llm_polish         — LLM 润色
  7. validate           — Redis 去重
  8. publish            — 时效性检查 + 输出
     │
     ├── record_advice_given(建议记录) → 反馈闭环
     │
     ▼
3 条输出通道: 浏览器 Overlay + 桌面玩偶(Edge TTS) + 控制台
```

---

## 六、修改文件清单

| 文件 | 涉及问题 | 改动性质 |
|------|---------|---------|
| `agent/collector/live_client.py` | #1 #2 #7 #8 | 事件检测、心跳机制、装备增强 |
| `agent/app.py` | #4 #10 #12 #13 #15 | 紧急事件绕过队列、Detector 集成、知识库刷新、反馈闭环、区域映射 |
| `agent/detector.py` | #10 | **新建**：独立事件检测模块 |
| `agent/map_zones.py` | #15 | **新建**：坐标→区域语义解析 |
| `agent/graph/nodes.py` | #5 #6 #9 #14 | 坐标校验、上下文抑制、时效性检查、kill RAG |
| `agent/models/state.py` | #5 | 坐标工具函数 |
| `agent/memory/queue.py` | #4 | 紧急事件防御过滤 |
| `agent/memory/redis_store.py` | #13 | 反馈闭环追踪 |
| `agent/memory/injector.py` | #15 | 区域信息注入 |
| `agent/companion.py` | #11 | Edge TTS 三级链路 |
| `agent/knowledge/chroma_store.py` | #12 | 知识库新鲜度检查 |
| `agent/knowledge/ingest.py` | #3 #12 | 兼容新旧格式、摄入时间戳 |
| `agent/knowledge/data_fetcher.py` | #3 | items 保存格式 |
| `agent/knowledge/item_resolver.py` | #3 | **新建**：物品名称解析 |
| `agent/planner/planner.py` | #2 #8 #14 | 新事件消息生成 |
| `agent/knowledge/retriever.py` | #8 | 装备事件扩列 |
| `agent/skills/build/SKILL.md` | #2 #8 | 敌方装备检测 |
| `agent/skills/macro/SKILL.md` | #14 | 击杀后决策表 |
| `agent/skills/build/gotchas.md` | #2 | 敌方装备坑点 |
