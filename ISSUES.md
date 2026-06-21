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

### #14 击杀后无反馈 (功能缺失)
- **文件**: `agent/skills/macro/SKILL.md`, `agent/planner/planner.py`, `agent/graph/nodes.py`
- **问题**: `kill` 事件能被检测但无对应 Skill，击杀后无任何反馈 — 这是最关键的反馈窗口
- **修复**: `macro` Skill 新增 `kill` 和 `gold_spike` 事件，加入完整的"击杀后行动决策表"（按游戏时间和位置给出不同建议：推线镀层 / 打龙 / 推塔 / 回城）。`planner.py` 加 kill 基础消息。`nodes.py` RAG 拼接 "after getting kill capitalize advantage" 查询
- **日期**: 2026-06-19

### #15 坐标无区域语义 (功能缺失)
- **文件**: `agent/map_zones.py` (新建), `agent/app.py`, `agent/memory/injector.py`
- **问题**: 只有 `(8954, 5321)` 原始坐标，LLM 不知道玩家在"上路河道"还是"中路一塔"
- **修复**: 创建 `map_zones.py` 区域解析器，将坐标映射到 20 个语义区域（泉水/基地/龙坑/大龙坑/河道/野区/中路等），区分蓝队/红队视角。`app.py` state handler 自动计算玩家区域和敌方可见区域。`injector.py` 记忆注入中附带区域和敌方位置信息
- **日期**: 2026-06-19

---

## 四、Bug 审查修复 (2026-06-20) — Go + Python 全量审查

> 对 Go Collector + Python Agent 进行全面交叉审查，共发现 28 个问题，全部修复。

### 第一批：致命/数据丢失 (12 个, #16-#27)

### #16 致命: ActivePlayer 缺 items 字段 → 运行时崩溃
- **文件**: `agent/models/state.py`, `agent/app.py:284`
- **问题**: `ActivePlayer` 模型无 `items` 字段，但 `app.py` 直接访问 `latest_state.active_player.items`，触发 `AttributeError` 崩溃
- **修复**: `ActivePlayer` 添加 `items: list[Item]` 字段；同时新增 `GameState.sync_active_player()` 方法从 `all_players` 中匹配活跃玩家并复制 items
- **日期**: 2026-06-20

### #17 致命: UserContext 缺 context 字段 → 运行时崩溃
- **文件**: `agent/memory/models.py`, `agent/app.py:328-329`
- **问题**: `UserContext` 模型无 `context` 字段，但 `app.py` 每帧 state 更新时写入导致 `AttributeError`
- **修复**: `UserContext` 添加 `context: dict[str, Any]` 字段
- **日期**: 2026-06-20

### #18 Python Player 模型缺少 6 个字段 → 数据静默丢弃
- **文件**: `agent/models/state.py`
- **问题**: Go Collector 发送的 champion_name/kills/deaths/assists/current_gold/creep_score 被 Pydantic 静默丢弃
- **修复**: `Player` 模型补齐全部 6 个字段
- **日期**: 2026-06-20

### #19 Python GameEvent 缺 dragon_type → 龙种信息丢失
- **文件**: `agent/models/state.py`
- **问题**: Go Collector 的 dragon_type 未被解析
- **修复**: `GameEvent` 添加 `dragon_type: str = ""`
- **日期**: 2026-06-20

### #20 detect_signals 死亡过滤键名错误 → 死亡玩家仍收到事件
- **文件**: `agent/graph/nodes.py:71`
- **问题**: 读血量用 `current_health` 但 model_dump key 是 `health`，hp 永远取默认值 1
- **修复**: 改为 `active.get("health", 1)`
- **日期**: 2026-06-20

### #21 Engine 冷却去重丢弃同一 tick 内同类型事件
- **文件**: `collector/internal/event/engine.go`
- **问题**: 单遍循环中第一个事件过冷却后立即设置时间戳，第二个同名事件被 0ms 冷却拦截
- **修复**: 重写为两遍处理 (Pass 1 收集 → Pass 2 写冷却)
- **日期**: 2026-06-20

### #22 death 事件被 60s 冷却 → 连续死亡丢失
- **文件**: `collector/internal/event/engine.go`
- **问题**: death 配了 60s 冷却，60s 内连续阵亡只触发第一次
- **修复**: 从 cooldownDurations 中移除 death
- **日期**: 2026-06-20

### #23 重置后首次 tick 产生虚假事件
- **文件**: `collector/internal/event/detector.go`
- **问题**: 重置后快照全为 nil，已有道具/击杀被识别为"新增"
- **修复**: 新增 initialized 标志 + firstTickInit() 仅记录快照
- **日期**: 2026-06-20

### #24 publish 装备过期校验永远被跳过
- **文件**: `agent/models/state.py`, `agent/app.py`
- **问题**: 装备过期检查依赖 active_player.items 但 Go 不发送此字段
- **修复**: 在 app.py 接收 state 后调用 sync_active_player() 补全 items
- **日期**: 2026-06-20

### #25 WebSocket heartbeat 与 send 并发写竞态
- **文件**: `collector/internal/sender/websocket.go`
- **问题**: heartbeat 直接调 conn.WriteMessage，send 在锁内写，两个 goroutine 并发写同一连接
- **修复**: 拆分为 writeMu (WriteMessage) + readMu (SetReadDeadline)，所有写路径统一加锁
- **日期**: 2026-06-20

### #26 FetchGameState 失败导致 detector 状态跳跃
- **文件**: `collector/cmd/main.go`, `collector/internal/event/engine.go`
- **问题**: 失败时 continue，下次成功时 lastState 滞后多 tick，周期检测误判
- **修复**: 失败时调用 engine.Reset() 重置检测器状态
- **日期**: 2026-06-20

### #27 敌方高经济玩家无特殊检测
- **文件**: `collector/internal/lol/parser.go`, `collector/internal/event/detector.go`, `agent/planner/planner.py`, `agent/graph/nodes.py`
- **问题**: 缺失敌方经济追踪
- **修复**: Go Player 补齐 CurrentGold/Assists/CreepScore；新增 enemy_gold_lead 和 enemy_fed 事件
- **日期**: 2026-06-20

---

### 第二批：低优全面修复 (16 个, #28-#43)

### Go Collector 侧 (7 个)

### #28 WebSocket SetReadDeadline 并发调用 (心跳 vs pong handler)
- **文件**: `collector/internal/sender/websocket.go`
- **问题**: heartbeat 和 pong handler 并发调用 conn.SetReadDeadline，违反 gorilla/websocket 并发约束
- **修复**: 拆分 writeMu / readMu，readMu 保护所有 SetReadDeadline 调用
- **日期**: 2026-06-20

### #29 detectGoldSpike 金币归零后永久失效
- **文件**: `collector/internal/event/detector.go:281`
- **问题**: `d.lastGold > 0` 守卫导致 gold 归零后 lastGold 不更新，gold_spike 永久失效
- **修复**: 移除 lastGold > 0 守卫；始终在函数末尾 `d.lastGold = gold`
- **日期**: 2026-06-20

### #30 client.available 无锁读写 → 数据竞争
- **文件**: `collector/internal/lol/client.go`
- **问题**: `bool` 字段被多 goroutine 读写 (RefreshCredentials + get)
- **修复**: `bool` → `atomic.Bool`，统一用 .Load() / .Store()
- **日期**: 2026-06-20

### #31 FetchGameState 忽略传入 context
- **文件**: `collector/internal/lol/client.go:96`
- **问题**: `_ = ctx` 丢弃 context，关闭时 HTTP 请求无法取消
- **修复**: 传递 ctx 到 get()，用 http.NewRequestWithContext
- **日期**: 2026-06-20

### #32 POLL_INTERVAL 解析失败静默忽略
- **文件**: `collector/cmd/main.go:29`
- **问题**: `ParseDuration` 失败无日志，配置错误难排查
- **修复**: else 分支加 WARNING 日志
- **日期**: 2026-06-20

### #33 Enrich 中 DragonTimer/BaronTimer 可能残留旧值
- **文件**: `collector/internal/lol/objectives.go:38`
- **问题**: 重用时未置 nil，不满足条件时返回上一次的值
- **修复**: Enrich 入口直接 `state.DragonTimer = nil; state.BaronTimer = nil`
- **日期**: 2026-06-20

### #34 Connect 持锁期间网络 I/O (阻塞可达 5s)
- **文件**: `collector/internal/sender/websocket.go:76`
- **问题**: writeMu 锁覆盖 dialer.DialContext()，持锁期间阻塞所有 send 操作
- **修复**: 锁降级 — 仅在检查/设置 conn 时加锁，拨号在锁外执行
- **日期**: 2026-06-20

### Python Agent 侧 (9 个)

### #35 紧急 task 竞态 — 可能读到未来帧的 game_state
- **文件**: `agent/app.py:344`
- **问题**: `create_task(handle_coaching(...))` 闭包捕获 `nonlocal latest_state`，task 执行时 latest_state 可能已更新为新帧
- **修复**: 创建 task 前 `snapshot = latest_state` + 传递 `_snapshot` 到协调函数
- **日期**: 2026-06-20

### #36 泉水坐标缺 is_position_valid 校验
- **文件**: `agent/graph/nodes.py:86`
- **问题**: 坐标 (0,0) 被当作泉水，坐标无效时错误抑制 low_health 事件
- **修复**: 加 is_position_valid() 检查，无效坐标跳过泉水距离判定
- **日期**: 2026-06-20

### #37 kill_count_before 永远为 0 → low_health 事件误抑制
- **文件**: `agent/graph/nodes.py:92`
- **问题**: `gs.get("kill_count_before", 0)` 始终为 0，导致有击杀的玩家 low_health 永远被跳过
- **修复**: 移除该抑制逻辑（kill 事件由独立 handler 处理）
- **日期**: 2026-06-20

### #38 detect_signals 信号分类缺失 10 种事件
- **文件**: `agent/graph/nodes.py`
- **问题**: kill/death/enemy_fed/enemy_gold_lead/teamfight_detected 等事件无信号分类
- **修复**: 新增 10 个 elif 分支：item_upgraded/gold_spike → power_spike，kill → kill_secured，death → player_died，enemy_fed → danger+enemy_fed，enemy_gold_lead → enemy_power_spike+danger 等
- **日期**: 2026-06-20

### #39 Python ActivePlayer 缺 team 字段
- **文件**: `agent/models/state.py`
- **问题**: map_zones.py 每帧 fallback 到 all_players 中查找 team
- **修复**: ActivePlayer 添加 `team: str = ""`
- **日期**: 2026-06-20

### #40 Go ActivePlayer 缺 items 字段 (Go 侧残留)
- **文件**: `collector/internal/lol/parser.go`
- **问题**: Go ActivePlayer 结构体未解析装备，Python 靠 sync_active_player 补齐
- **修复**: ActivePlayer 加 Items []Item + 解析逻辑，消除 Python workaround 依赖
- **日期**: 2026-06-20

### #41 companion.py 线程传参修改私有属性
- **文件**: `agent/companion.py:539`
- **问题**: `ws_thread._target = _start_ws; ws_thread._args = (pet,)` 修改 Thread 私有属性
- **修复**: `threading.Thread(target=lambda: _start_ws(pet), daemon=True)`
- **日期**: 2026-06-20

### #42 bridge.py game_start 事件 payload 格式不规范
- **文件**: `agent/collector/bridge.py:103`
- **问题**: payload 原样透传 LCU 数据，Agent 侧无固定格式可解析
- **修复**: 规范化 payload 含 summoner_name/champion_name/assigned_position；game_end 补充发送事件含 duration
- **日期**: 2026-06-20

### #43 live_client.py dragon_timer/baron_timer 未填充
- **文件**: `agent/collector/live_client.py:465`
- **问题**: _build_game_state 未导出龙/大龙倒计时字段
- **修复**: LiveGameData 加 dragon_timer/baron_timer Optional 字段；_build_game_state 导出
- **日期**: 2026-06-20

---

## 五、启动适配 Bug 修复 (2026-06-20) — WeGame 国服 + 启动流程

### #44 致命: ringBuffer.drainSince 结构体整体清零导致 mutex 重复解锁 panic
- **文件**: `collector/internal/sender/websocket.go:54`
- **问题**: `*b = ringBuffer{}` 将整个结构体（含 mutex）清零，之后 `defer b.mu.Unlock()` 对零值 mutex 调 Unlock → `sync: unlock of unlocked mutex` panic，Go Collector 启动即崩溃
- **修复**: 移除 `defer b.mu.Unlock()`，改为单独重置 `b.head`/`b.count`/`b.ring` 字段，最后手动 `b.mu.Unlock()`
- **日期**: 2026-06-20

### #45 WeGame 国服 lockfile 路径错误 — LeagueClient\lockfile 为空文件
- **文件**: `agent/collector/lcu_client.py`, `collector/config/config.yaml`, `collector/internal/lol/client.go`
- **问题**: `D:\WeGameApps\英雄联盟\LeagueClient\lockfile` 是 0 字节空文件，被优先匹配导致 lockfile 解析失败。正确的 Riot Client lockfile 在 `D:\WeGameApps\英雄联盟\Riot Client Data\User Data\Config\lockfile`（52 字节）
- **修复**: 
  - Python `_LOCKFILE_PATHS` 移除空文件路径，加入正确的 Riot Client lockfile 路径
  - Go `resolveLockfile()` 加入 WeGame 国服路径
  - YAML 配置 `lockfile_path` 默认指向正确路径
- **日期**: 2026-06-20

### #46 国服 LCU API 凭据不在 lockfile 中，需从进程命令行提取
- **文件**: `agent/collector/lcu_client.py`
- **问题**: Riot Client lockfile 只含 Riot Client 端口/密码（59062），不包含 LCU API 凭据。国服新版架构中 `--remoting-auth-token` 和 `--app-port` 在 `LeagueClientUx.exe` 进程命令行中，传统 lockfile 完全不可用。LCU 监听 59162 端口，需正确 token 访问
- **修复**: 
  - 新增 `_try_connect_from_process()` — 用 `psutil` 扫描 `LeagueClientUx.exe` 进程，正则提取 `--remoting-auth-token=` 和 `--app-port=`
  - 新增 `_connect_lcu(port, password)` — 抽取连接测试逻辑
  - `_try_connect()` 改为两阶段：先 lockfile → 失败则进程命令行回退
  - 依赖新增 `psutil` 和 `re` 导入
- **日期**: 2026-06-20

### #47 国服 summoner displayName 为空，UI 显示 "?"
- **文件**: `agent/collector/bridge.py`
- **问题**: 国服 LCU API 返回的 `displayName` 为空字符串（外服才有值），实际召唤师名称在 `gameName` 字段（如"利物浦浦浦"）
- **修复**: `lcu_connected` 回调中 `displayName or gameName` 兜底
- **日期**: 2026-06-20

### #48 桌宠 WebSocket recv() 超时导致频繁断连
- **文件**: `agent/companion.py`, `agent/app.py`
- **问题**: Agent 的 `overlay_ws` 在 `receive_text()` 阻塞等数据，但桌宠只收不发 → uvicorn 超时主动断开连接（WinError 10054）。同时 `recv()` 默认超时过短
- **修复**: 
  - 桌宠每 15 秒发 `{"type":"ping"}` 心跳，`recv()` 超时改为 5 秒便于心跳调度
  - Agent 忽略 `type: ping` 消息
  - 异常处理分层：内层 catch `WebSocketTimeoutException` 做心跳，真正错误才抛到外层重连
- **日期**: 2026-06-20

### #49 LCU 客户端日志过于冗余
- **文件**: `agent/collector/lcu_client.py`
- **问题**: lockfile 解析/API 测试失败时反复输出 WARNING 级别日志，刷屏影响可读性
- **修复**: 将 `_try_connect` 系列方法的失败日志从 `logger.warning` 降级为 `logger.debug`；仅连接成功输出 `logger.info`
- **日期**: 2026-06-20

---

## 六、Go Collector LCU 适配 (2026-06-21) — WeGame 国服

### #50 Go LCU lockfile 解析错误 — Riot Client 格式被误当成 LCU
- **文件**: `collector/internal/lcu/client.go`
- **问题**: `tryLockfile()` 对 `Riot Client:21968:59062:password:https` 格式取 `parts[2]`(21968) 当端口、`parts[3]`(59062) 当密码，连到 Riot Client 而非 LCU API。5 秒超时后才回退到进程发现
- **修复**: 检查 `parts[0] == "LeagueClient"`，只处理标准 LCU 格式 `LeagueClient:port:password:protocol`；Riot Client 格式直接跳过，立即回退到进程发现
- **日期**: 2026-06-21

### #51 Go LCU displayName → gameName 兜底（同 #47）
- **文件**: `collector/internal/lcu/poller.go`
- **问题**: 国服 `displayName` 为空，召唤师名称显示空白；`onGameStart` 发送的 `summoner_name` 也是空
- **修复**: 
  - `TryConnect` 连接日志：`strVal("displayName")` 为空时取 `strVal("gameName")`
  - `fetchSummoner`：`SummonerInfo.DisplayName` 使用新增 `strValOr` 兜底
  - 新增 `strValOr()` 辅助函数
- **日期**: 2026-06-21

### #52 Go LCU 进程发现 + Riot Client lockfile 完整方案（2026-06-21 多轮迭代）
- **文件**: `collector/internal/lcu/client.go`, `collector/internal/lol/client.go`, `collector/internal/lcu/poller.go`
- **根因链**:
  1. WMIC/`Get-CimInstance` 读取进程命令行需要管理员权限 → 返回空结果
  2. 引入 `gopsutil` 替代，但 `Cmdline()` 同样返回空（Windows 权限限制）
  3. 回退：用 gopsutil `Exe()` 获取进程路径 → 推导同目录 lockfile
  4. `D:\WeGameApps\英雄联盟\LeagueClient\lockfile` 存在但为空（WeGame 不写 LCU lockfile）
  5. **最终方案**：解析 Riot Client lockfile `Riot Client:43232:53276:password:https` 获取 LCU 端口和密码
- **子 Bug #52a**: lockfile 格式误解
  - `Riot Client:port1:port2:password:https` 中 "Riot Client" 之间是**空格**不是冒号
  - Split(":") 后 `parts[0] = "Riot Client"`，**5 个 parts**，不是 6 个
  - 原检查 `len(parts) >= 6 && parts[0] == "Riot"` 永远为 false
  - 修复：`len(parts) >= 5 && strings.HasPrefix(content, "Riot Client")` → `parts[2]=lcu_port, parts[3]=password`
- **子 Bug #52b**: `connect()` 自检死锁
  - `connect()` 调用 `Get()` 验证连接，但 `Get()` 第一行 `if !c.connected` 检查 → 直接返回 error
  - 修复：调用 `Get()` 前先设 `c.connected = true`，失败时重置
- **子 Bug #52c**: LCU 认证密码不匹配
  - Riot Client lockfile 中的密码是 Riot Client 的密码，**≠** LCU `--remoting-auth-token`
  - 验证端点 `/lol-summoner/v1/current-summoner` 和 `/lol-gameflow/v1/session` 均返回 404
  - **结论**：普通用户无权限读取进程命令行 → LCU 不可用；需 admin 运行 Collector
- **子 Bug #52d**: LCU 失败后 poller 无限重试
  - 用户要求 "LCU 没连上就不进行后续的重试"
  - 修复：poller `Run()` 只尝试一次 `TryConnect()`，失败直接 log + return
- **涉及文件修改**:
  - `lcu/client.go`: Riot Client lockfile 格式修正（tryLockfile + tryLockfileByPath）、connect 自检修复、tryProcess 精简日志
  - `lol/client.go`: RefreshCredentials + discoverFromProcess 同时支持 Riot Client lockfile 格式
  - `lcu/poller.go`: Run 改为一次尝试，失败跳过
- **日期**: 2026-06-21

---

## 八、运行时 Bug 修复 (2026-06-21)

### #53 LCU poller "websocket: close sent" 错误刷屏
- **文件**: `collector/internal/sender/websocket.go`
- **现象**: 日志反复出现 `[LCU] send event failed: websocket: close sent`，每次断连时重复 5-10 次
- **根因**: LCU poller 作为独立 goroutine 运行，与主循环共享 `ws` 连接。WebSocket 断开后，`runLoop` 返回但在 `ws.Close()` 清零 `w.conn` 之前，LCU poller 回调仍在并发调用 `ws.SendEvent()`，写入已死连接
- **修复**: 在 `send()` 中，`WriteMessage` 失败后立即设置 `w.conn = nil`，后续并发 send 走缓冲路径。事件数据不丢失 — 缓冲在重连后重放。所有 `w.conn` 访问均受 `writeMu` 保护
- **日期**: 2026-06-21

### #54 Go nil 切片序列化导致 Pydantic `all_players` 校验失败 — Agent 频繁断连
- **文件**: `collector/internal/lol/parser.go`, `agent/models/state.py`
- **现象**: agent 日志反复报 `ValidationError: all_players — Input should be a valid list [input_value=None]`，每次报错后 collector 重连，形成断连→重连循环
- **根因**: Live Client API 响应中可能缺少 `allPlayers` 字段（加载画面、游戏初期）。Go `ParseGameState` 创建的 `AllPlayers` 切片为零值 nil，`json.Marshal` 将 nil 切片序列化为 `null`。Python Pydantic `GameState.all_players: list[Player]` 拒绝 `null`
- **修复 (Go 根因)**: `ParseGameState` 初始化 `AllPlayers` 和 `Events` 为空切片 `[]Player{}` / `[]GameEvent{}`
- **修复 (Python 防御层)**: `GameState` 加 `@field_validator("all_players", "events", mode="before")`，将 `None` 自动转为 `[]`
- **日期**: 2026-06-21

---

## 九、优化后的数据流

```
游戏客户端 → Live Client API (127.0.0.1:2999)
     │
     ▼
Go Collector (1s 轮询，全部事件检测)
     │  15 种事件: death/kill/item_purchased/item_sold/item_upgraded/
     │  gold_spike/dragon_soon/baron_soon/low_health/
     │  enemy_item_purchased/enemy_item_sold/enemy_gold_lead/enemy_fed/
     │  jungle_check/strategy_check
     │
     │  心跳: 30s 无事件 → 轻量 state
     │  有事件 → state + event 一起发
     │
     ▼
WebSocket → Python Agent
     │
     ├── MapZones: 坐标 → 区域语义
     │   "你在上路河道，对面劫在小龙坑"
     │
     ├── 紧急事件 (low_health/death/dragon_soon/baron_soon/enemy_fed):
     │     绕过队列 → 直接进 LangGraph 流水线 (带 _snapshot 快照)
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

## 七、修改文件清单

| 文件 | 涉及问题 | 改动性质 |
|------|---------|---------|
| `agent/collector/lcu_client.py` | #45 #46 #49 | WeGame lockfile 路径、进程命令行 LCU 凭据提取、日志降噪 |
| `agent/collector/live_client.py` | #1 #2 #7 #8 #43 | 事件检测、心跳、装备增强、dragon/baron 字段补齐 |
| `agent/collector/bridge.py` | #42 #47 | game_start 格式规范化、game_end 事件发送、国服 displayName→gameName 兜底 |
| `agent/app.py` | #4 #10 #12 #13 #15 #16 #24 #35 | 紧急事件绕过队列、Detector 集成、知识库刷新、反馈闭环、区域映射、同步快照防竞态 |
| `agent/detector.py` | #10 | **新建后删除**：独立事件检测模块（全部迁移到 Go） |
| `agent/map_zones.py` | #15 | **新建**：坐标→区域语义解析 |
| `agent/graph/nodes.py` | #5 #6 #9 #14 #20 #36 #37 #38 | 坐标/泉水校验、上下文抑制、时效性、kill RAG、health 键名、信号分类补齐 10 种事件 |
| `agent/models/state.py` | #5 #16 #18 #19 #24 #39 | 坐标工具函数、模型字段补齐（Player 6 字段 + ActivePlayer team/items + GameEvent dragon_type）+ sync_active_player |
| `agent/memory/models.py` | #17 | UserContext 添加 context 字段 |
| `agent/memory/queue.py` | #4 | 紧急事件防御过滤 |
| `agent/memory/redis_store.py` | #13 | 反馈闭环追踪 |
| `agent/memory/injector.py` | #15 | 区域信息注入 |
| `agent/companion.py` | #11 #41 #48 | Edge TTS 三级链路、线程传参 lambda 闭包、WebSocket recv 超时修复 |
| `agent/knowledge/chroma_store.py` | #12 | 知识库新鲜度检查 |
| `agent/knowledge/ingest.py` | #3 #12 | 兼容新旧格式、摄入时间戳 |
| `agent/knowledge/data_fetcher.py` | #3 | items 保存格式 |
| `agent/knowledge/item_resolver.py` | #3 | **新建**：物品名称解析 |
| `agent/planner/planner.py` | #2 #8 #14 #27 | 新事件消息生成 |
| `agent/knowledge/retriever.py` | #8 | 装备事件扩列 |
| `agent/skills/build/SKILL.md` | #2 #8 | 敌方装备检测 |
| `agent/skills/macro/SKILL.md` | #14 | 击杀后决策表 |
| `agent/skills/build/gotchas.md` | #2 | 敌方装备坑点 |
| `collector/cmd/main.go` | #26 #32 | 失败时重置 engine、POLL_INTERVAL 警告日志 |
| `collector/config/config.yaml` | #45 | WeGame 国服 lockfile_path 默认值 |
| `collector/internal/lol/parser.go` | #27 #40 | Player 结构体扩展字段、ActivePlayer 补齐 items 解析 |
| `collector/internal/lol/state.go` | #10 | ActivePlayerFromAll 辅助方法 |
| `collector/internal/lol/client.go` | #30 #31 #45 | available → atomic.Bool、FetchGameState 传递 context、WeGame 国服 lockfile 路径 |
| `collector/internal/lol/objectives.go` | #33 | Enrich 入口置 nil 防止旧值残留 |
| `collector/internal/event/detector.go` | #10 #23 #27 #29 | 事件检测统一、firstTickInit、敌方经济追踪、goldSpike 永久失效 |
| `collector/internal/event/engine.go` | #21 #22 #26 | 两遍冷却去重、death 冷却移除、Reset 方法 |
| `collector/internal/sender/websocket.go` | #25 #28 #34 #44 | writeMu/readMu 拆分、SetReadDeadline 并发保护、Connect 锁降级、ringBuffer drainSince mutex panic 修复 |
| `collector/internal/lcu/client.go` | #50 #52 | lockfile Riot Client 格式跳过、进程发现 WMIC→PowerShell 回退 |
| `collector/internal/lcu/poller.go` | #51 | displayName→gameName 国服兜底、strValOr 辅助函数 |
