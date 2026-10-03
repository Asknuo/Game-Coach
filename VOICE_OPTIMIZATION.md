# 语音播报节奏审计与修复

日期：2026-10-03
背景：防抖突发触发（见 [DEBOUNCE_OPTIMIZATION.md](DEBOUNCE_OPTIMIZATION.md)）把团战 tip 的产出延迟压到 ~1s，但 voice 客户端的 TTS 是串行阻塞的——tip 产出更快更多之后，播报反而成了最后一公里瓶颈。本次审计 [voice/voice_broadcast.py](voice/voice_broadcast.py)，发现 4 个节奏问题 + 1 个潜伏 COM 线程问题，全部修复。

## 审计发现

### 1. 紧急 tip 无法打断当前播报（核心问题）

`_tts_loop` 同步调 `speaker.speak()`，一条中文 tip 要播 5-10s。紧急 tip 插队到队首后，仍要等当前条目播完才轮到它——**突发触发抢来的 1s 送达，被 TTS 尾巴吃掉 5-10s**，团战建议到手就过时。

### 2. 静默丢弃 + 指标失真

产出上限与播报能力不匹配：

| | 速率 |
|---|---|
| tip 产出上限 | 6s 窗口 × 2 条 + 突发 3 条/1s ≈ **最多 ~20 条/min** |
| TTS 播报能力 | 单线程串行，每条 5-10s ≈ **6-12 条/min** |

队列（`QUEUE_MAX=5`）会真实打满。满员丢最旧时**无日志、无计数**；且 `spoken` 在入队时递增——被丢弃的 tip 也算"已播"，指标名不副实，用户完全无感知漏播。

### 3. 多个 urgent 之间 LIFO 乱序

原实现 urgent `appendleft` 插到队首：连续两条团战 tip u1、u2，后到的 u2 反而先播（`[u2, u1, ...]`）。

### 4. stop 要播完积压才退出

`__EXIT__` 哨兵走队尾入队，队列非空时 TTS 线程要把剩余 tip 全部播完才退出。

### 5. （潜伏）COM 对象跨线程调用

`SapiSpeaker.__init__` 在**主线程** `Dispatch("SAPI.SpVoice")`，`speak()` 却在 **TTS 线程**调用。STA COM 对象未 marshal 跨线程使用、且 TTS 线程未 `CoInitialize`，是未引爆的雷。

## 修复

全部在 [voice/voice_broadcast.py](voice/voice_broadcast.py)：

### 紧急打断链路（问题 1）

```
handle_tip(urgent) → queue.put(插队) + interrupt.set()
                         ↓
TTS 线程（正在播非紧急条目，每 50ms 轮询）
    → 发现 interrupt → SAPI Skip("Sentence", 1e6) 跳过剩余句子
    → 立即取插队的紧急条目播报
```

- `SapiSpeaker._speak_sapi`：`Speak(text, SVSFlagsAsync)` 异步发起 + 轮询 `Status.RunningState`（`SPRS_DONE`）等完成；打断时 `Skip` 掉剩余句子并返回 False。
- **紧急条目播报期间不接受打断**（`interrupt_event=None`）：连续团战 tip 若互相打断，会播成一串半截碎片——半截碎片比多等几秒更糟。紧急之间靠 urgent-FIFO 排队。
- PowerShell 回退路径不支持打断（`Speak` 同步阻塞），维持原样。

### urgent-FIFO（问题 3）

urgent 插队位置从"队首"改为"队首连续紧急块的尾部"——紧急之间保持到达顺序。

### 溢出可见化（问题 2）

- `TipQueue.put()` 返回被丢弃的文本列表；丢弃时打 warning 日志。
- `handle_tip` 计入 `skipped_overflow`。
- `spoken` 语义改为**完整播出**（在 `_tts_loop` 播完后递增），入队单独计 `queued`，被打断计 `interrupted`。

### 退出即弃播（问题 4）

`TipQueue.close()`：清空积压 + 放入 EXIT 哨兵，`stop()` / `run()` 收尾均改用。

### COM 线程安全（问题 5）

`Dispatch` 从 `__init__`（主线程）移到 `_ensure_sapi()` 惰性初始化——首次 `speak()` 在 TTS 线程内执行 `CoInitialize` + `Dispatch`，彻底消除跨线程 STA 调用。

## 指标语义（改动后）

| 指标 | 含义 |
|---|---|
| `received` | 收到 tip 消息数 |
| `queued` | 通过过滤/去重成功入队数 |
| `spoken` | **完整播出**数 |
| `interrupted` | 被紧急 tip 打断让路的条数 |
| `skipped_overflow` | 队列满员被丢弃数（新增，可观测漏播） |
| `skipped_priority` / `skipped_dedup` | 不变 |

## 测试

[voice/tests/test_voice_broadcast.py](voice/tests/test_voice_broadcast.py)：7 → 13 个。

新增：urgent-FIFO 顺序 / put 返回丢弃列表 / close 清积压 / 溢出计数 / **打断驱动集成测试**（`InterruptibleSpeaker` 模拟播报中，断言紧急条打断低优条且被断条不重播）/ **紧急条不被打断**（断言连续两条紧急都完整播出）/ stop 清积压不消化。

调整：3 处旧断言 `spoken` → `queued`（入队语义），`test_listen_receives_and_speaks` 的 spoken 轮询语义升级为"真实播出"。

## 效果与验证

- 团战紧急 tip：从"送达后还要等当前播报 5-10s"变为 **~50ms 内打断插队**。
- 漏播可观测：`skipped_overflow` + warning 日志。
- 13 voice 测试 + 55 agent 测试全绿（macOS 本地 venv，SAPI 路径无法本机验证，需实测一局确认打断听感）。

## 关联

- [RAG_OPTIMIZATION.md](RAG_OPTIMIZATION.md) / [DEBOUNCE_OPTIMIZATION.md](DEBOUNCE_OPTIMIZATION.md) / [PIPELINE_OPTIMIZATION.md](PIPELINE_OPTIMIZATION.md) — 前三批优化档案
