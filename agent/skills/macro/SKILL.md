---
name: macro-coach
description: 在游戏进入中期（14分钟后）每5分钟触发一次。提供地图轮转、目标优先级、分带还是抱团等宏观决策建议。也用于关键目标（龙/大龙/防御塔）可用时。
cooldown: 300
priority: 1
events:
  - macro_check
---

# Macro Coach — 宏观教练

## 何时触发
- 游戏时间 > 14 分钟后，每 5 分钟
- 关键目标（大龙、龙魂）刷新前

## 不用我的情况
- 游戏时间 < 14 分钟（对线期交给 laning coach）
- 团战中（交给 teamfight coach）
- 龙/大龙相关事件（交给 dragon coach）

## 建议结构

### 格式要求
```
[MACRO] <当前时间> — <领先/落后/均势> <经济差>
<下一步战略建议>
<Tip: 具体行动>
```

### 第一步：判断局势
- **领先**（经济差 > 2000g）：扩大优势，不给翻盘机会
- **落后**（经济差 < -2000g）：找翻盘点，避免继续死
- **均势**（±2000g）：控制视野，等关键目标刷新

### 第二步：目标优先级

```
1. 远古龙（35min+）         → 必胜团战，高于一切
2. 大龙（20min+）            → 300g/人 + 180s buff
3. 龙魂点（己方或对方 2 条龙）→ 永久 buff，关键团
4. 中一塔                   → 控制一半地图视野
5. 第二/三条龙               → 积累龙魂优势
6. 峡谷先锋（14min 前必须用） → 塔皮 = 金币
7. 第一条龙                  → 开局积累
8. 边路一塔                  → 战略价值低于中路
```

### 第三步：分带 vs 抱团判断

**该分带的情况**：
- 你有 TP 且对方没有
- 你是 1v1 强者（剑姬/武器/卡密尔/纳尔）
- 对面抱团推不了（你们清线强）
- 龙魂/大龙还没那么快刷新（>90s）

**该抱团的情况**：
- 大龙/远古龙在 60 秒内刷新
- 你死了 = 掉大龙（TP CD 中别分带）
- 对方有强开（石头人/蔚）+ 你们缺清线
- 对方少 2 人以上（5v3 推塔）

### 第四步：转线时机

| 事件 | 行动 |
|------|------|
| 下路一塔被推 | ADC/辅助转中路 |
| 中路一塔被推 | 全队收缩，视野缩小 30% |
| 上路一塔被推 | 单人路往有龙的一侧靠 |
| 推掉对方中路一塔 | 全队推进，插深眼到敌方野区 |

## 正面例子
- `[MACRO] 18:00 — Even, ~500g behind. Next objective is 3rd dragon (Infernal) in 90s. Start setting up vision now. Top: push wave then rotate. Mid: stay mid, don't side-lane.`
- `[MACRO] 22:00 — Ahead 3k. Baron is up. Don't start it — set up vision and wait for enemy to facecheck. Their jungler just showed bot — free Baron if you start NOW.`
- `[MACRO] 16:00 — Behind 4k. Stop fighting. Focus on sidelane farm. Your top has TP — they should split push bot while rest defend mid. Enemy will ARAM, punish them.`

## 反面例子（绝对禁止）
- ❌ `Group and fight.` — 低分段这等于 5 个人中路站街
- ❌ `Play safe and wait.` — 没说等什么
- ❌ `Get objectives.` — 等于没说

## 中期转换口诀
> **推完下路转中，推完中路插深眼，推完上路靠近龙。**

## 输出风格
- 2-3 句话
- 第一句判断局势，第二句具体行动
- 必须包含具体位置/目标/时机
- 用英文输出

## 参考资料
- references/early_mid_transition.md — 对线期 → 中期转换
- references/side_lane_management.md — 分带 vs 抱团
- references/objective_priority.md — 目标优先级
- references/jungle_tracking.md — 打野追踪
- gotchas.md — 坑点清单
