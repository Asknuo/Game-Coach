# 四层上下文组装优化记录（显式分层 + token 预算）

日期：2026-10-05
背景：`llm_polish` 节点此前把四层上下文（Guidelines / Gotchas / RAG / Memory）
用 `"\n\n".join(parts)` 裸拼接——**“四层”只是叫法**：代码里没有权重、没有预算，
哪一层该保、哪一层可裁没有任何表达。本次让分层在代码里真实生效。

> 说明：本次**不改动任何 skill 内容**（原因见第四节），只改组装机制。

## 一、改了什么（4 改 1 增）

### 1. 新增 [agent/prompt/context_builder.py](agent/prompt/context_builder.py)

统一组装入口 `build_polish_context(...)`，把四层显式排序并加预算：

- **裁剪优先级**（预算不足时从低到高裁）：`guidelines → rag`
- **永不裁剪**：`gotchas`（最高信号约束）、`memory`（决定个性化）
- **裁剪粒度**：只在 markdown `## ` section 边界整节丢弃，**不会截出半句话**
- **展示顺序**与裁剪顺序解耦：仍是 指导方针 → 坑点 → 知识 → 记忆
- 同时导出 `estimate_tokens()`（CJK ≈ 1 token/字，其余 ≈ 4 char/token），
  供预算与 `is_rich` 判断复用
- 每次组装输出分层 token 明细（debug），超预算时告警（warning）

### 2. [agent/graph/nodes/generation.py](agent/graph/nodes/generation.py)

`llm_polish` 里的四段 `parts.append(...)` + `"\n\n".join` 换成一次
`build_polish_context(...)` 调用，逻辑收敛到单一入口。

### 3. [agent/llm/openai_client.py](agent/llm/openai_client.py)

`_build_polish_prompt` 的 `is_rich` 判断由字符数改为 token：

```diff
- is_rich = rag_context and len(rag_context) > 200
+ is_rich = bool(rag_context) and estimate_tokens(rag_context) >= 60
```

原写法按字符数判断“知识是否丰富”。**中文 200 字的信息量约为英文 200 char 的两倍**，
`len()` 会系统性高估中文上下文，导致本应走 1 句话分支的情况误走 2-3 句分支。

### 4. [agent/tests/test_nodes.py](agent/tests/test_nodes.py)

新增 `TestBuildPolishContext`（6 例）：空输入返回 None、展示顺序与裁剪顺序解耦、
预算不足先裁 guidelines 后裁 rag、gotchas/memory 永不裁剪、section 边界整节截断、
CJK/ASCII token 估算。

### 5. [README.md](README.md)

标题“三层上下文结构”（下方却列了 4 条）修正为**四层**，并补上组装器与预算的说明。

## 二、分层与裁剪顺序（关键决策）

| 层 | 来源 | 角色 | 预算不足时 | 现状体量（token） |
|---|---|---|---|---|
| Gotchas | `gotchas.md` | 反直觉约束，最高信号 | **永不裁剪** | 300–614 |
| Memory | `PlayerMemory` | 玩家个性化 | **永不裁剪** | ~200（注入口已限 200） |
| RAG | ChromaDB 聚合 | 与当前事件/对位强相关 | 最后裁 | ~150–300 |
| Guidelines | `SKILL.md` 正文 | 静态、最泛化 | **最先裁** | 889–1207 |

**为什么 Guidelines 最低优先级**：它是每个 skill 固定的静态文本，和具体这一秒的
局势无关；RAG 和 Memory 才是“针对当前对局”的信息。预算紧张时保住针对性的、裁掉泛化的。

预算默认 **6000 tokens**，环境变量 `LLM_CONTEXT_BUDGET_TOKENS` 可调（≤0 或非法值回落默认）。

## 三、效果

| 维度 | 改动前 | 改动后 |
|---|---|---|
| 分层语义 | 仅字符串顺序，无强弱 | 显式裁剪优先级，gotchas/memory 受保护 |
| 长度控制 | 无上限，随 skill 内容增长 | 有预算护栏，section 边界安全裁剪 |
| 可观测性 | 无 | 每条 tip 记录分层 token 明细；超预算告警 |
| 中英混合上下文判断 | `len()` 字符数，系统性偏差 | `estimate_tokens()` CJK 感知 |

**当前实测**（`estimate_tokens` 口径，guidelines + gotchas）：

| skill | guidelines | gotchas | 小计 |
|---|---|---|---|
| build | 1136 | 614 | 1750 |
| macro | 1186 | 381 | 1567 |
| review | 1207 | 321 | 1528 |
| survival | 1049 | 360 | 1409 |
| dragon | 951 | 372 | 1323 |
| teamfight | 1031 | 300 | 1331 |
| laning | 889 | 464 | 1353 |

叠加 RAG + Memory 后，单条 tip 的注入量约 **1.7–2.2k tokens**，**全部低于默认预算
6000**——即默认配置下**不发生裁剪**，预算是兜住未来膨胀的护栏。需要更激进压缩提示词时，
把 `LLM_CONTEXT_BUDGET_TOKENS` 调低（例如 2000）即会让最大的 skill 开始裁 Guidelines。

## 四、为什么没有做“内容去重”

评审时曾提出：SKILL.md 与 gotchas/references 存在内容重叠，属“真臃肿”。逐文件核实后
**未做删除**，理由：

- 重叠**大多不是逐字重复**。SKILL.md 往往更详细（如 survival 的血量阈值比 gotchas 多了
  15–25% / 25–40% 分档），删掉是**有损**的。
- 删改的是**提示词内容**，直接影响建议质量，而本机**无真实对局可验证回归**。
- 用有损的内容删除去换少量 token 不划算；长度问题改由**代码预算**兜底
  （内容不动，超长时按优先级安全裁剪）。

若确需精简内容，建议单独一轮、逐个 skill 对照真实对局输出进行。

## 五、兼容性

- 对外行为不变：`llm_polish` 产出的上下文文本格式与原来一致（四个 `=== ... ===` 段，
  `\n\n` 分隔），只是多了预算保护。
- `build_polish_context` 是纯函数，无副作用，便于单测。
- 新增依赖仅标准库（`re` / `os` / `logging`）。

## 六、验证状态

- `pytest`：**86 项全通过**（含新增 6 项）。
- `ruff check`：改动文件全部通过。
- 未做真实对局验证：默认预算下不触发裁剪，行为与改动前等价，故运行时风险低；
  若后续调低预算，需实测一局观察建议质量。

## 七、后续可调项

1. 若提示词过长成为问题：调低 `LLM_CONTEXT_BUDGET_TOKENS`（6000 → 2000 量级），
   并实测裁剪后建议质量。
2. 可考虑在裁剪发生时把被丢弃的层名一并写入 tip 元数据，便于线上量化“裁剪频率”。
3. 内容层面如需精简：逐 skill 对照真实输出做**无损**合并（保留信息量最大的一份）。