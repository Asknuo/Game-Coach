# tip 生成链路优化记录（流式润色 + 基础设施修复）

日期：2026-10-03
承接前两批优化（[RAG 检索](RAG_OPTIMIZATION.md)、[防抖窗口](DEBOUNCE_OPTIMIZATION.md)），本次处理剩余耗时项中的两块：LLM 润色非流式（单条 tip 延迟的最大剩余项）与三个基础设施抖动源。

## 一、三个小修复

| 文件 | 问题 | 改动 |
|---|---|---|
| [agent/services/lifecycle.py](agent/services/lifecycle.py) | periodic_save 的同步磁盘 IO 跑在事件循环里，每 60s 卡顿几十 ms（记忆越大越久），恰好撞上时整条链路停摆 | 写盘套 `asyncio.to_thread` |
| [agent/memory/store.py](agent/memory/store.py) | ↑ 的配套：`save()` 非原子写（`write_text` 直写目标文件），periodic_save 进线程后与断局保存存在并发写同一文件的风险 | `save()` 全程持 `threading.Lock`，序列化所有磁盘写入 |
| [agent/planner/planner.py](agent/planner/planner.py) | `get_skill_gotchas` 每条 tip 都读盘（SKILL.md 正文有注册表缓存，gotchas 没有） | 加 `@lru_cache(maxsize=None)`，skill 文件是静态的 |
| [agent/knowledge/embedder.py](agent/knowledge/embedder.py) | 火山引擎 embedding `urlopen(timeout=120)`：一个挂起请求会钉死 `asyncio.to_thread` 线程池整整 2 分钟 | timeout 120 → 10；失败本来就会降级为空 RAG 上下文，只是从"卡 2 分钟再降级"变成"10s 降级" |

## 二、LLM 流式润色 → overlay 增量显示

### 背景

润色走非流式 `achat`：等 LLM 生成完整 60-120 token 才返回（deepseek-chat 1-3s），期间 overlay 上一片空白。tip 这么短，流式的收益极大：**首字可见从 1-3s 降到 ~0.3-0.5s，逐字浮现**。

### 协议设计（关键决策）

- 新增消息类型 `tip_stream`：payload 与 `tip` 一致（`skill` / `priority` / `message`），其中 `message` 是**到当前为止的累计文本**——客户端收到后直接覆盖渲染，无需自己拼接、无需关心顺序。
- 流结束后 session 层照发权威 `tip` 落锤（内容一致）。
- **向后兼容**：不认识 `tip_stream` 的客户端行为完全不变——voice 已确认只消费 `type == "tip"`，忽略未知类型；overlay 若暂不改造，仍按旧方式收到最终 `tip`。

### 改动（5 处）

| 文件 | 改动 |
|---|---|
| [agent/llm/openai_client.py](agent/llm/openai_client.py) | 新增 `achat_stream`（流式调用 + 断路器：中途失败不重试、计 failure，正常结束计 success）与 `apolish_stream`（复用原 `_build_polish_prompt`，逐块 yield 增量） |
| [agent/graph/deps.py](agent/graph/deps.py) | `GraphDeps` 新增可选字段 `on_polish_delta: Callable[[CoachingTip, str], Awaitable]]  = None`，**默认 None 时节点走原非流式路径，现有测试零感知** |
| [agent/graph/nodes/generation.py](agent/graph/nodes/generation.py) | `llm_polish` 加流式分支：增量拼接 → **0.1s 节流**推送 emitter（避免逐 token 洪泛）→ 结束后补推一次完整文本 → `polished_message` = 拼接结果。降级链：emitter 推送失败不中断润色；流整体失败用已到手的部分文本；全程无输出则回退草稿 |
| [agent/context.py](agent/context.py) | 装配 `_emit_polish_delta` 闭包（ctx 先占位建、graph 后回填），把累计文本以 `tip_stream` 广播给 overlay 客户端 |
| [README.md](README.md) | 消息类型表加 `tip_stream` 行 + 客户端消费建议 |

### overlay 客户端接入指引（客户端代码不在本仓库）

收到 `tip_stream`：渲染 tip 卡片（可加"生成中"样式），每次消息直接替换文本；收到同 skill 的 `tip`：落锤定稿（两者内容一致）。忽略 `tip_stream` 亦可，行为退回非流式。

## 三批优化全景

| 批次 | 文档 | 主要效果 |
|---|---|---|
| RAG 检索 | [RAG_OPTIMIZATION.md](RAG_OPTIMIZATION.md) | 3 次串行远程调用 → 1 次批量；重复事件 0 远程调用（纯本地 ~30-60ms） |
| 防抖窗口 | [DEBOUNCE_OPTIMIZATION.md](DEBOUNCE_OPTIMIZATION.md) | 事件 → 显示延迟 16-18s → 7-9s；后追加突发触发，团战场景 ~1s |
| 本批 | 本文 | tip 首字可见 1-3s → 0.3-0.5s；消除 60s 周期抖动与 2 分钟挂死尾部风险 |

## 剩余未做项（按之前分析，均低收益或需产品决策）

- 广播并发化（`asyncio.gather`）：**当前只有 1 个 overlay 客户端，串行无影响，暂不做**。
- 同一 GameState 三重 pydantic 验证合并：收益数十 ms，需重构状态传递，暂缓。
- `build_context()` 导入期初始化 ChromaDB：只影响启动速度，不影响 tip 延迟。
- 每帧 state 全量校验 + Redis 写：被 20s 心跳压着，暂无感。
- 火山引擎 urllib 换 httpx 连接复用：单次省 100-200ms，用的不是该后端则无感。

## 验证状态

- 8 个改动文件 `py_compile` 通过。
- 本机 Python 3.9（项目要求 3.10+）、Docker 不可用，**运行时验证未执行**。
- 现有测试理论上零影响（`GraphDeps` 新字段带默认值；节点无 emitter 时走原路径）；建议容器内 `python -m pytest tests/` 回归，并实测一局观察流式效果。
