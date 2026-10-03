# 防抖队列调整记录（window 15s → 6s）

日期：2026-10-03
背景：RAG 检索提速后（见 [RAG_OPTIMIZATION.md](RAG_OPTIMIZATION.md)），单条 tip 的生成延迟只剩 LLM 润色一项主要开销，防抖窗口取代 RAG 成为普通事件**新鲜度**的最大制约。

## 防抖队列是干什么的

位置：[agent/memory/queue.py](agent/memory/queue.py)（运行时注入见 [agent/context.py](agent/context.py)）。

它和 RAG 优化解决的是两个不同层面的问题：

| | 防抖队列 | RAG 检索 |
|---|---|---|
| 管的是 | **一条 tip 该不该、何时生成** | **一条 tip 生成得多快** |
| 层面 | 事件进入流程**之前**（入口攒批） | 事件**已经进入**流程之后 |

队列存在的三个理由，均未被 RAG 优化消解：

1. **成本控制** — 每条 tip 除了 RAG 还要一次 LLM 润色调用（按 token 计费）。一场团战 10 秒内可能触发 5-8 个事件（kill / gold_lead / tower…），没有队列就是 5-8 次 LLM 调用；攒批后只跑一次流程。
2. **语义合并** — 窗口内的连续击杀、同 skill 事件被去重合并成一条有价值的 tip，而不是 3 条互相打架的碎片。
3. **UX 节流** — 玩家 10 秒内读不完 5 条 overlay 提示。

一句话：**队列是"要不要攒"（保留），窗口是"攒多久"（本次调整）**。

## 窗口语义

- **固定窗口**：从窗口内首个事件入队起计时，到期批量消费；窗口期间后续事件只入队、**不延长**窗口。
- **批量消费**：到期后按 priority 降序取前 `max_per_window` 条并发处理。
- **紧急通道**：`low_health` / `death`、以及 ≤30s 的 `dragon_soon` / `baron_soon` 在会话层直接绕过本队列，不受窗口影响。

| 参数 | 值 | 含义 |
|---|---|---|
| `window` | **6.0**（原 15.0） | 防抖窗口秒数 |
| `max_per_window` | 2（未动） | 每窗口最多推送条数 |
| `skill_cooldown` | 25s（未动） | 同 skill（按 EVENT_TO_SKILL 映射粒度）最小间隔 |
| `burst_flush_at` | 3（追加） | 窗口内攒满该条数立即提前消费（团战爆发）；0 = 关闭 |

## 追加：自适应突发触发

固定窗口在突发场景有盲区：团战开打后 1-2 秒内 kill / enemy_gold_lead / tower 已连续入队，tip 却要干等 6s 窗口到期——恰恰是建议最值钱的时刻最慢。

改动（[agent/memory/queue.py](agent/memory/queue.py)）：

- 窗口等待从 `sleep(window)` 改为**可中断等待**（`asyncio.Event` + `wait_for`）：窗口内事件攒满 `burst_flush_at`（默认 3）条 → 立即消费。
- 平静期（单事件、双事件）行为不变，仍等满 6s 攒上下文。
- 顺带修复一个潜伏缺陷：消费期间（LLM 润色 1-3s）新入队的事件没有对应计时器，会滞留到下一条事件才被带走；现在消费结束后若有滞留事件会补启 drain。

效果：**团战爆发时 tip 延迟从 ~6s 降到 ~1s 内**；普通事件延迟不变。

测试：`test_queue.py` 新增 `test_burst_flushes_before_window_expiry`（10s 窗口 + 3 事件，0.5s 内完成消费即证明提前触发）。

## 本次改动

| 文件 | 改动 |
|---|---|
| [agent/memory/queue.py](agent/memory/queue.py) | 默认 `window` 15.0 → 6.0 |
| [agent/context.py](agent/context.py) | 运行时注入值同步（**真正生效的是这处**） |
| [agent/tests/test_queue.py](agent/tests/test_queue.py) | `test_defaults_match_documented_debounce` 断言同步为 6.0 |
| [README.md](README.md) | 目录树注释 + 三层防抖表两处 15s → 6s |

**为什么是 6s 而不是更短**：团战的关联事件（kill → gold_lead → tower）会在几秒内连续到达，6s 刚好收拢这一波；再短（如 3s）会把一波拆成两批，LLM 调用翻倍且两条 tip 互相打架。6s ≈ 一波团战从开打到结束的典型节奏。

## 效果

普通事件 → tip 显示延迟（窗口等待 + 流程执行）：

| | 改动前 | 改动后 |
|---|---|---|
| 窗口等待（最坏） | 15s | 6s |
| 流程执行（RAG + LLM） | ~1-3s | ~1-3s |
| **合计** | **~16-18s** | **~7-9s** |

频率侧无退化：`max_per_window=2` 与 `skill_cooldown=25s` 保持不变——窗口只影响"攒多久"，不产生额外 tip。

## 容易混淆的「15s」（均未改动）

| 位置 | 语义 | 与本队列关系 |
|---|---|---|
| [collector/internal/event/detector_periodic.go](collector/internal/event/detector_periodic.go) | 15s 内 ≥3 击杀 → 判定 teamfight | Go 侧击杀爆发判定，另一套机制 |
| [voice/voice_broadcast.py](voice/voice_broadcast.py) | `PING_INTERVAL = 15.0` | WebSocket 心跳，无关 |
| ~~agent 防抖窗口~~ | ~~15s~~ | 已改为 6s（本次） |

## 后续可调项

- 窗口只有 [context.py](agent/context.py) 一处注入，跑一局观察体感后可继续微调（4-8s 之间）。
- 若仍嫌慢：考虑扩大紧急通道覆盖（哪些事件值得绕过队列直接出 tip），而非继续压窗口。
- 若同 skill 提示太稀疏：降 `skill_cooldown`（25s → 15s），与窗口无关。
