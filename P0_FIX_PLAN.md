# P0 正确性修复方案（2026-09-19）

> 范围：仅 P0 正确性问题（影响每一局输出质量 / 崩溃挂起面 / 密钥泄露面）。
> 已在当前代码逐条复核，file:line 均为最新。

## 一、问题清单与修复设计

### A. Go Collector — 跨局状态泄漏（影响每局输出质量）

#### A1. 不在对局时不重置 detector 状态

- 证据：`collector/cmd/main.go:133-138` — `ErrNotInGame` 分支直接 `return nil, false`，
  不调 `l.engine.Reset()`；对比 `:141` 普通错误分支有调。
- 后果：上一局的 `dragonWarned/baronWarned` 锁存（`detector_objectives.go:10,22`）、
  `enemyItems/lastKills/lastGold` 基线残留 → 第二局起龙/大龙预警失效、敌方装备误报。
- 修复：`fetchState` 的 not-in-game 分支同样调用 `l.engine.Reset()`。
- 被否替代：在 `Detector.Detect` 里靠 `IsInGame()` 自查（`detector.go:53-55` 已有该逻辑）
  ——被否因为 not-in-game 时 `FetchGameState` 返回 error，`Detect` 根本不会被调用。

#### A2. ObjectiveTracker 跨局泄漏 + nil 解引用

- 证据：`collector/internal/lol/objectives.go:75-81` — `ev.EventID <= t.lastEventID → continue`
  挡在新游戏 `GameStart`（新一局事件 ID 从 1 重新开始，必然 ≤ 上一局水位）之前，
  `:87-88` 的 `GameStart → Reset` 永远不可达。
- 证据：`objectives.go:40-41` 在 `:43` 判 `state == nil` **之前**执行 `state.DragonTimer = nil`，
  nil 入参必 panic，判空是死代码。
- 修复：
  1. `Enrich` 开头先判 `state == nil || state.GameTime <= 0 → t.Reset(); return`，再赋值 timer；
  2. `syncEvents` 检测水位回退：`if ev.EventID < t.lastEventID { t.Reset() }`
     （ID 变小 = 新一局事件列表，重置后继续正常消费），与 GameStart 语义双保险。
- 同步修正被错误断言固化的测试：`objectives_test.go:32`（本批只改水位/重置相关断言）。
- 不在本批：「被杀龙种 ≠ 下一条龙种」「Elder 杀后不再刷新」属于数据语义错误（P1），
  需要真实事件流验证，另批处理。

#### A3. teamfight 检测把历史击杀重复计入窗口

- 证据：`collector/internal/event/detector_periodic.go:53-57` — 每 tick 遍历
  `state.Events` **全量历史**追加 `ChampionKill`，无 EventID 水位；同一条击杀
  在 15s 窗口内被多个 tick 重复追加 → 实际 2 杀即可凑满 3 计数误报团战。
- 修复：`Detector` 增加 `lastKillEventID int` 水位字段（`reset()` 清零），
  `detectTeamfight` 只追加 `ev.EventID > d.lastKillEventID` 的击杀并推进水位。
- 被否替代：按 `EventTime >= cutoff` 过滤——被否因为同一事件仍会被多个 tick 重复追加。

#### A4. 冷却键不带 subject，跨玩家互相吞事件

- 证据：`collector/internal/event/engine.go:63,73` — `e.cooldown[ev.Name]` 仅按事件名；
  敌方 A 买装后 30s 内 B 的买装被丢弃；`kill` 30s 冷却吞掉玩家双杀
  （detector 基线本身已保证每杀只报一次，引擎层冷却属二次伤害，`detector_player.go:150`）。
- 修复（已确认）：
  1. 冷却键改为 `name + "|" + subject`，subject 从 `ev.Data` 取
     `enemy_name`（enemy_* 族）/ `summoner_name`（如有），取不到为空串（行为同现状）；
  2. **`kill` 冷却整体移除**（用户拍板）：节奏完全交给 detector 基线与 Agent 侧队列。

### B. Python Agent — 反馈闭环 / 挂起 / 崩溃面

#### B1. Redis 反馈闭环永不消费（当前 2 个红灯测试的根因）

- 证据：`agent/memory/redis_store.py:114` — `via_redis = False` 初始化后，
  成功从 Redis 读到 advice 的路径（`:116-118`）从不置 True；
  `_consume_advice`（`:148-155`）因此永远走进程内 dict 分支，
  **Redis 里的 `last_advice` 键从不删除** → 同一建议在 25s 窗口内被每个
  state 帧重复判定、置信度被反复 ±调整。
- 失败测试：`agent/tests/test_feedback.py:75` 与 `:96`（实测 32 passed / 2 failed）。
- 修复：成功读取后置 `via_redis = True`（一行）。
- 顺带：`save_state/get_state` 降级 dict 用裸键 `"state"`，多 session 会串——
  改为与 `mark_tip_sent` 一致的 `f"tip/state:{session_id}"` 风格键。

#### B2. LLM 调用无超时，一次挂起停摆整条流水线

- 证据：`agent/llm/openai_client.py:78-81,95-98` — `OpenAI`/`AsyncOpenAI` 构造均未传
  `timeout`，SDK 默认 600s；`achat`（`:133-151`）自带 3 次重试 → 最坏 ~30 分钟，
  期间 `memory/queue.py` 的 gather 卡死，所有事件静默停摆。
- 修复：
  1. 两个客户端构造均传 `timeout=float(os.getenv("LLM_TIMEOUT", "20"))`、
     `max_retries=0`（关闭 SDK 内建重试，避免与自研重试/tenacity 叠加放大）；
  2. `.env.example` 增加 `LLM_TIMEOUT=20`。
- 被否替代：调用侧 `asyncio.wait_for` 包裹——被否因为挂起连接仍占 httpx 池，
  客户端级 timeout 是根治。

#### B3. broadcast 遍历 set 时 await，可并发修改崩溃

- 证据：`agent/services/broadcast.py:21-27` — `for client in ctx.overlay_clients`
  循环体内 `await send_text` 让出控制权，期间 `/ws/overlay` 处理器的
  add / `finally: discard` 可修改同一 set → `Set changed size during iteration`；
  慢客户端还会阻塞全体广播（无每客户端超时）。
- 修复：遍历前 `list(ctx.overlay_clients)` 取快照；发送包
  `asyncio.wait_for(..., timeout=5)`，超时同样计入 dead 清理。

### C. 部署与仓库卫生

#### C1. Docker 镜像泄露密钥

- 证据：`agent/Dockerfile:8` `COPY . .`，构建上下文 `./agent` 内含已填真实 key 的
  `agent/.env`，且**无 `.dockerignore`** → 密钥烧进镜像层。
- 修复（已确认）：新建 `agent/.dockerignore`：`.env`、`__pycache__/`、`.pytest_cache/`、`*.pyc`、
  `memory/data/`、`chroma_data/`（用户拍板：排除出镜像，容器内后台 ingest 重建）。

#### C2. voice 模块从未入库

- 证据：`git log -- voice` 为空；`git status` 显示 `?? voice/` 与 `M .env.example`，
  README 已把语音播报当交付功能宣传。
- 修复：与本次 P0 修复合并交付，分三个 commit：
  1. `fix(collector): P0 跨局状态与事件检测正确性`（A1–A4 + Go 回归测试）
  2. `fix(agent): P0 反馈闭环 / LLM 超时 / 广播快照`（B1–B3 + agent/.dockerignore）
  3. `feat(voice): 语音播报客户端入库`（voice/ + .env.example）
- 完成后提交并推送 origin/main，不执行任何部署。

## 二、验证计划

- Go：`go vet ./...` + `go test ./...`；为 A1–A4 各补回归用例：
  两局连续状态序列（GameStart 重置 / 龙预警第二局可再触发 / 击杀水位不残留 /
  A、B 两名敌人买装均不被吞 / kill 冷却不再吞双杀）。
- Python：`pytest agent/tests` 从 32+2红 → 34 全绿；新增 B1「Redis 键确被删除」
  与 B3「广播中途增删客户端不崩」用例。
- 不执行需要真实 LOL 客户端的手工冒烟，修复后由你实测。

## 三、明确不在本批（下一批 P1 候选）

- 龙种语义 / Elder 复活时间错误（需真实事件流佐证）
- `lifecycle.py` 后台 ingest 用第二个 Chroma 客户端删主进程持有的 collection
- `session.py` 断连后 `queue.set_handler` 不清理（旧会话向死 socket 发 tip）
- 冷却语义三处不一致（`memory/queue.py` / `events.py` / SKILL.md frontmatter）
- `validation.py:97` `int(0 or DEFAULT)` 使 review 的 `cooldown: 0` 反向变 120s
- 紧急通道 `_spawn_coaching` 无并发上限（成本面）
- Redis 无密码 + 端口全网卡发布 + WS 零鉴权（局域网信任模型下另议）
- CI / ruff / mypy / gofmt 防线
