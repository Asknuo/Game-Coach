# P1 修复方案

> 范围来源：`P0_FIX_PLAN.md` §三"下一批 P1 候选"。所有问题均已对照当前代码逐一核实，
> 证据为 file:line。与 P0 一样，"确实不成立"和"看起来有问题但实际成立"分开写。

---

## 一、现状与证据

### A. Collector（Go）

| # | 问题 | 证据 | 现状判定 |
|---|------|------|---------|
| A1 | 下一条龙种 = 被杀龙种 | `objectives.go:97` `t.nextDragonType = ev.DragonType`；`objectives_test.go:32` 还把该行为写进了断言 | **成立**。Live Client API 不提供下一条龙的元素，`DragonKill.DragonType` 是被杀的那条。当前 `DragonTimer.Type` 在首条龙之后全是错的（且会误导 LLM 给出"火龙团"建议）。P0 期间加的 `unknown` 只兜住了空值 |
| A2 | Elder 龙会"复活" | `objectives.go:8` `elderDragonRespawn=360`、`:93-95` Elder kill 后 6 分钟又刷一条 Elder | **成立**。远古龙是全游戏唯一不重生的目标，杀掉后 dragon timer 应彻底消失 |
| A3 | LCU 断连单向 | `client.go:226,258` 传输错误置 `connected=false`，此后再无人置回；`poller.go:37-55` `TryConnect` 只在启动时调一次，失败即永久放弃 | **成立**。客户端重启 / 一次网络抖动后，本局游戏所有 LCU 数据（召唤师名、符文、 Mastery、gameflow 事件）永久丢失 |
| A4 | `exec.Command("python")` 无超时 | `client.go:131` | **成立**。python 不在 PATH 时立刻失败没问题；但若用户机器上 python 是 WindowsApps 存根/被弹窗拦截，`TryConnect` 会卡死在启动路径上 |

### B. Agent（Python）

| # | 问题 | 证据 | 现状判定 |
|---|------|------|---------|
| B1 | 后台 ingest 用第二个 Chroma 客户端 | `lifecycle.py:24` `Ingestor()` → `pipeline.py:31` 自建 `ChromaStore()`（第二个 `PersistentClient` 指向同一目录）；`pipeline.py:536-543` `_rebuild_collection` 删集合后只更新**自己 store** 的属性，主进程 `ctx.retriever.store`（`context.py:68`）仍持已删除集合的句柄 | **成立**。过期重建后，线上 RAG 检索查的是悬空集合（空结果或直接异常），新摄入的数据永远不可见 |
| B2 | 断连后 queue handler 悬挂 | `session.py:32` `set_handler(self.handle_coaching)`，断开路径（`:38-43`）从不清理 | **成立**。旧会话断开→新会话建立前的空档期，queue drain 会回调死会话：send 失败、`tips_published` 虚增 |
| B3 | urgent 直启无并发上限 | `session.py:125-128` 每个 urgent 事件 spawn 一个完整 LLM 流水线任务 | **成立**。血量反复跌破阈值 + 龙团连续报急时可并发拉起任意多个 LLM 请求 |
| B4 | 游戏上下文传 None（"看起来支持但实际不成立"） | `routing.py:90` `planner.plan(event, None)`；`generation.py:64` `apolish(tip, None, ...)`；而 `graph/state.py:22,66` 明明携带了 `game_state` | **成立**。`planner.py` 各 builder 里 `hp_pct`（low_health 的"（xx% HP）"）永远缺失；`_build_polish_prompt` 的双方阵容/时间/经济上下文永远为空——LLM 润色实际只看到 skill 文本 + RAG |
| B5 | `int(0 or DEFAULT)` 吞掉 cooldown:0 | `validation.py:97`；`skills/review/SKILL.md:4` 声明 `cooldown: 0` | **成立**。review 的 0（本意"复盘不去重"）被 `or` 变成 `DEFAULT_TIP_TTL=120`。且即使修好，ttl=0 传给 Redis `setex` 会报错，需配套处理 |
| B6 | 防抖参数三处文档漂移 | 实际运行值 = README:692（15s / 2 条 / 25s），由 `context.py:81` 显式传入；但 `queue.py:31-33` 构造默认值是 30/3/30，`queue.py:3` docstring 又声称"dragon_soon ≤30s 绕过此队列"（阈值逻辑实际在 `services/events.py is_urgent`，`URGENT_EVENTS` 只有 low_health/death 两个无条件项） | **仅默认参数与注释漂移，运行时行为正确**。修法：默认值对齐 context、docstring 改为事实，不动行为 |

### C. 死代码（核实过调用方）

| # | 内容 | 证据 |
|---|------|------|
| C1 | `skills/__init__.py:17` `__all__` 导出不存在的 `load_skill`（`from skills import *` 直接 ImportError） | 全仓 grep 无任何 `load_skill` 引用 |
| C2 | `retriever.py` 未被使用的方法：`search_champion(:37)`、`search_champion_abilities(:62)`、`search_guide_by_time(:126)`、`search_all(:176)`；删掉 `search_all` 后 `search_runes(:144)`、`search_summoner_spells(:150)` 也随之全死 | 唯一外部入口是 `retrieval.py:99 → aggregate_coaching_context`，其内部只用 `search_guide/search_game_info/search_items` |
| C3 | `openai_client.py:215 polish`（同步）+ `:110 _call_with_retry` + 同步 `OpenAI` 客户端 | 全仓（含 tests）无调用方；注释声称"LangGraph 同步节点/离线脚本"使用，实际所有节点均为 async 走 `apolish` |

### D. 工程防线（P0 遗留候选）

- `gofmt -l` 仍有约 6 个未格式化文件（P0 只格式化了当批触碰的 3 个）。
- 无 CI；Python 无 ruff/mypy 门禁。

### E. 明确不做（本批）

- Redis 无密码 / 端口绑全网卡 / WS 零鉴权 —— P0 评审时已定"另议"。
- 部署相关 —— 按你的既定偏好：只提交推送，不碰部署。

---

## 二、核心设计决策（含被否方案）

1. **A1 龙种**：杀掉龙后 `nextDragonType = "unknown"`（首刷同样 unknown）。
   被否：按龙魂局规则推断下一条元素——需要本地维护每条龙的随机序列状态，Live Client 不提供种子，实现不了准确推断；宁可显式 unknown 也不给 LLM 喂假数据。
2. **A2 Elder**：`DragonKill(Elder)` → `dragonScheduled=false`、清空 spawn/type。
   被否：保留 360s 常量改注释——语义仍然错误。
3. **A3 LCU 重连**：`poll()` 里 `!client.Connected()` 时限流重试 `TryConnect`（≥30s 一次），成功后补发 `lcu_connected`。
   被否：独立重连 goroutine——多一处并发状态机，收益相同复杂度更高。
4. **B1 Chroma 单客户端**：`Ingestor.__init__` 接受可选注入 store/embedder，`bg_ingest` 传入 `ctx.retriever.store`（复用同一 client，`_rebuild_collection` 的属性回写自然作用于共享 store）。
   被否：ingest 完成后重启进程/重建 retriever——破坏"后台、不阻塞"的初衷。
5. **B2 handler 清理**：断连收尾时 `ctx.queue.set_handler(None)`；`_drain` 已有 `if not self._handler: return` 兜底，无需加锁。
6. **B3 并发上限**：会话级 `asyncio.Semaphore(2)`，获取不到则丢弃并 log（urgent 事件本来就允许丢弃，overlay 不是消息队列）。
   被否：入队降级——绕过 queue 正是 urgent 的设计意图。
7. **B4 传真实 state**：`routing/generation` 从 `state["game_state"]`（dict）构造 `GameState` 传入；解析失败回退 None（保持现行为）。
8. **B5 cooldown 语义**：`raw = meta.get("cooldown")`，`None` → DEFAULT_TIP_TTL；`int(raw) <= 0` → 不去重（跳过 `mark_tip_sent`）。
9. **C 死代码**：全部删除（含 `polish`/`_call_with_retry`/同步 `OpenAI` 客户端）。理由：`apolish` 已覆盖全部真实调用路径，保留只会让"双通道"注释继续误导评审。
   被否：留着"以后可能用"——retriever 那批检索 API 存在两年无人调用即为答案。

---

## 三、分期改动清单

**第 1 期 — Collector（Go）**
- `objectives.go`：Elder 不重生 + 龙种 unknown（A1/A2）；更新 `objectives_test.go:32` 断言
- `client.go`：`tryPythonPsutil` 改 `exec.CommandContext` + 5s 超时（A4）
- `poller.go`：限流重连 + `lcu_connected` 补发（A3）
- 回归：`go vet ./... && go test ./...`

**第 2 期 — Agent（Python）**
- `knowledge/ingest/pipeline.py` + `services/lifecycle.py`：Ingestor 注入 store（B1）
- `services/session.py`：断连清 handler（B2）+ urgent 信号量（B3）
- `graph/nodes/routing.py`、`graph/nodes/generation.py`：传真实 GameState（B4）
- `graph/nodes/validation.py`：cooldown 0 语义（B5）
- `memory/queue.py`：默认值对齐 15/2/25 + docstring 修正（B6，不改行为）
- 新增/更新 pytest：stale-session 不发布、ttl=0 不去重、low_health 消息含 HP%、Ingestor 共享 store
- 回归：`cd agent && ../venv/Scripts/python -m pytest tests -q`（基线 36 passed）

**第 3 期 — 清理（待你确认是否入批）**
- `skills/__init__.py`（C1）、`retriever.py`（C2）、`openai_client.py`（C3）

**第 4 期 — 防线（待你确认是否入批）**
- `gofmt -w` 全量 + `.github/workflows/ci.yml`（go vet/test + ruff + pytest）；mypy 严格模式不入 CI（存量类型债太重）

---

## 四、红线与已知坑

- `_rebuild_collection` 属性回写依赖共享 store 实例——B1 修完后 ingest 并发保护仍只有"启动时判断过期"一处，勿再引入第二个 `Ingestor()` 构造点。
- B4 后 `llm_polish` 的 prompt 会真正带上阵容/时间，输出分布变化：测试断言不要锁定具体文案，只断言含关键字段。
- A3 重连成功后 `p.lastPhase` 是陈旧值，首次 `pollGameFlow` 可能误报一次 phase change——重连路径上重置 `lastPhase=""`。
- `mark_tip_sent` 跳过后，`was_tip_recently_sent` 对 review 永远 False：这正是 review（每局一次，由 `game_end` 事件本身保证）想要的语义。
- 删除 C3 时注意 `openai_client.py` 顶部注释与 README 的"双通道"描述要同步改。

## 五、阻塞待确认

1. 第 3 期死代码删除、第 4 期 CI/gofmt 是否纳入本批？（第 1、2 期默认执行）
2. A1 龙种统一 "unknown" 后，`dragon_soon` 事件与 overlay 文案将不再显示"火龙/水龙"——接受吗？（另一选项：仅首刷显示已知元素需接 Data Dragon 序列，超出本批）
