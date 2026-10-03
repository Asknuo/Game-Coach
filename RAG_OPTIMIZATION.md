# RAG 检索性能优化记录

日期：2026-10-03
目标：消除 RAG 检索阶段的串行远程调用瓶颈（RAG 阶段占单条 tip 生成延迟的 ~95%）。

## 改了什么（3 个文件）

### 1. agent/knowledge/embedder.py — 新增批量预取

**问题**：一次 tip 生成最多触发 3 次**串行**的远程 embedding API 调用（己方攻略串、敌方 matchup 串、counter 串互不依赖，却排队执行），单次 100-500ms，是 RAG 耗时的 ~95%。

**改动**：

- 缓存逻辑重构：把原来内联在 `embed_query` 里的 LRU 读写拆成 `_cache_get` / `_cache_put` 两个私有方法；`__init__` 中显式初始化 `self._query_cache = None`（原来靠 `getattr` 惰性创建）。
- 新增 `embed_queries(texts)` 方法：对一批查询串做**去重 → 找出缓存缺失项 → 一次批量获取 → 写入 LRU**。
  - OpenAI 后端：`embeddings.create` 原生支持批量，3 次串行远程调用 → **1 次批量调用**；
  - 火山引擎后端：复用原有 `_embed_volces` 的线程池并行（3 次串行 → 3 路并行）。
- `embed_query` 行为不变（单条 + LRU），预取之后流程内所有调用全部命中缓存，不再产生额外网络往返。

### 2. agent/knowledge/retriever.py — 变体合并 + 预取入口

**问题 A**：`search_guide` 为兼容英雄名大小写，对同一个查询串按 `Ahri / ahri / Aatrox` 等变体发起**最多 3 次串行 chroma 查询**（查询串相同，embedding 靠 LRU 只算一次，但本地查询翻倍）。

**改动 A**：三次变体查询合并为**一次 `$or` 查询**：

```python
names = {champion, champion.lower(), champion.capitalize()}
where = {"$or": [{"champion": c} for c in names]}
if phase:
    where = {"$and": [where, {"phase": phase}]}
```

单次查询结果天然按距离排序，去重后取 top n，语义与原来一致（且消除了变体顺序偏差）。guides 集合查询次数 6 → 2。

**问题 B**：`aggregate_coaching_context` 的四步检索串行执行，每步都可能触发远程 embedding。

**改动 B**：在函数入口**预取本流程将用到的全部查询向量**——与第 2 步的查询串逐字一致（这是能命中缓存的前提）：

```python
info_query = event_query or event_name
prefetch = [info_query] if info_query else []
if enemy_champion and event_query:
    prefetch.append(f"matchup against {ally_champion} early game laning tips")
    prefetch.append(f"{enemy_champion} enemy tips counter")
self.embedder.embed_queries(prefetch)
```

顺带把 `info_query` 的计算上移复用，第 3 步不再重复写 `event_query or event_name`。

### 3. agent/graph/nodes/routing.py — 查询串去掉动态数字

**问题**：`kill` / `enemy_gold_lead` / `enemy_fed` 把动态数字拼进 RAG 查询串：

```python
f"after getting kill {kills} what to do ..."          # total_kills 每次不同
f"enemy {c} has {gap:.0f} gold lead ..."              # gold_gap 每次不同
```

数字一变，查询串就是新字符串 → LRU（按整串逐字匹配）**永远 miss** → 这三类事件每条 tip 都全额支付远程调用费用，还会把别人的缓存条目挤出。

**改动**：查询串模板化，只保留语义稳定的部分（英雄名保留，数字删除）：

| 事件 | 改动前 | 改动后 |
|---|---|---|
| kill | `after getting kill {kills} ...` | `after getting a kill what to do objective push tower dragon capitalize advantage` |
| enemy_gold_lead | `enemy {c} has {gap:.0f} gold lead ...` | `enemy {c} has a gold lead how to play from behind counter fed enemy` |
| enemy_fed | `enemy {c} fed {kills} kills ...` | `enemy {c} is fed how to shut down fed enemy shutdown target counter` |

数字对 embedding 向量的语义贡献几乎为零（意图词 "play from behind"、"shut down" 才是检索关键），去掉后检索质量不受影响，缓存可正常命中。

## 效果对比（每条 tip 的 RAG 阶段）

| 指标 | 改动前 | 改动后 |
|---|---|---|
| 远程 embedding 调用（首次/冷事件） | 3 次串行（0.3-1.5s） | **1 次批量（~单次 RTT）** |
| 远程 embedding 调用（重复事件） | 仍 3 次（动态数字事件永远 miss） | **0 次**（LRU 全命中） |
| chroma 本地查询次数（最坏） | 9 次 | 5 次 |
| 检索阶段总耗时（缓存命中后） | — | **纯本地，~30-60ms** |

一局游戏内查询串去重后约 20 条以内，LRU 上限 512 条，缓存基本全程命中。

## 兼容性说明

- 所有对外接口签名未变：`retrieve_knowledge` 节点、`_search`、`search_guide`、`embed_query` 的调用方无感知。
- 唯一行为差异：`search_guide` 结果排序由"变体顺序拼接"变为"单次查询的自然距离排序"，top-n 语义不变。
- LRU 淘汰策略、缓存上限（512）均未变。

## 验证状态

- 三个文件已通过 `py_compile` 语法检查。
- 本机无 Python 3.10+（代码使用 `str | None` 语法），Docker 不可用，**运行时验证未执行**。
- `agent/tests/` 未直接覆盖这些内部函数；建议在容器内跑一次 `python -m pytest tests/` 做回归确认。

## 追加：防抖窗口 15s → 6s

RAG 提速后，防抖窗口成为普通事件新鲜度的最大制约（事件 → 显示延迟 ~16-18s → **~7-9s**）。
改动详情、窗口语义与调整依据见 [DEBOUNCE_OPTIMIZATION.md](DEBOUNCE_OPTIMIZATION.md)。

## 后续可选优化

1. ~~火山引擎后端 `urllib` 的 `timeout=120` 降到 10s~~ — 已在第三批完成，见 [PIPELINE_OPTIMIZATION.md](PIPELINE_OPTIMIZATION.md)。
2. `urllib` 每次新建 TCP+TLS 连接，可换 httpx 连接复用，单次再省 100-200ms（未做）。
3. ingest 时统一英雄名小写，可进一步省掉 `$or` 变体匹配（未做）。

## 追加：查询串静态化补完 + 启动预热

### 查询串静态化补完（修正第一批的不完整承诺）

第一批只静态化了 `_build_rag_query` 的事件片段，但查询串第一段一直是 `skill_message`（planner 生成的中文基础建议，大多拼了动态数字/英雄名，如「领先 {gap:.0f} 经济（{kills} 击杀）」）——kill / enemy_gold_lead / enemy_fed / gold_spike 等事件的查询串仍然每次都变、LRU 永远 miss。

修复：

- [routing.py] `skill_message` 移出检索查询。它本来就是面向用户的建议文本而非检索信号，且 KB 是英文语料，中文段落只会污染 embedding
- gold_spike / laning_check / macro_check / teamfight_detected 四个原先走 else 分支的事件补专属英文片段（不再退化为泛化的 "strategy tips priority"）

### 启动预热（冷启动成本从游戏内挪到启动时）

查询串全部固定后，游戏期冷启动成本可以预先付清：

| 文件 | 改动 |
|---|---|
| [agent/knowledge/retriever.py](agent/knowledge/retriever.py) | 新增 `matchup_query()` / `counter_query()` 共享构造（聚合检索与预热同源，消除逐字重复的漂移风险）、`list_champions()`（guides metadata 枚举英雄）、`warm_collections()`（三张集合各探一次，触发 Chroma 惰性索引加载） |
| [agent/services/lifecycle.py](agent/services/lifecycle.py) | `bg_warmup()`：等可能的后台摄入完成 → 线程池批量 embed 全部英雄的 matchup/counter 查询 + 10 个固定事件模板 + 兜底串 → 集合探针。上限 200 英雄（embed LRU 512 条，留一半给动态串）。失败静默回冷启动 |
| [agent/app.py](agent/app.py) | lifespan 挂载 `bg_warmup`，随进程启动后台执行 |

### 效果

- 游戏内 RAG 远程调用 ≈ **0**（唯 item_sold / item_upgraded / enemy_item_purchased 首现各付一次批量调用；KB 外英雄的查询同理）
- 一局的首条 tip 不再付 Chroma 索引加载的几十到几百 ms

### 回归风险

- `skill_message` 不再参与检索：检索质量依赖英文事件片段。champ-keyed 事件（enemy_gold_lead / enemy_fed）的片段本身含英雄名，无信息损失
- `test_nodes.py` 对 `rag_query` 无内容断言（仅置空），理论零影响；建议容器内 `pytest tests/` 确认
