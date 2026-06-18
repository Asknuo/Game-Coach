---
name: dragon-coach
description: 当龙或大龙在60秒内即将刷新、或刚被击杀需要记录计时时使用。提供视野布置、站位和争夺建议。不要用于常规游戏状态更新。
cooldown: 60
priority: 2
events:
  - dragon_soon
  - baron_soon
---

# Dragon Coach — 龙/大龙教练

## 何时触发
- 龙/大龙剩余刷新时间 ≤ 60 秒
- 刚击杀龙/大龙，需要告知下次刷新
- 龙魂即将形成（己方或对方 2 条龙）
- 远古龙即将刷新（35 分钟后）

## 不用我的情况
- 龙刷新时间 > 60 秒（交给 macro coach）
- 双方都在泉水 / 刚团灭
- 游戏时间 < 5 分钟（第一条龙还没刷新）
- 当前处于团战中（交给 teamfight coach）

## 龙 Buff 价值速查

| 龙 | Buff 效果 | 优先级 | 策略 |
|---|---|---|---|
| 🔥 炼狱亚龙（火龙） | +4% AD/AP | S 级 — 必争 | 伤害增幅，推线团战两用 |
| 💧 海洋亚龙（水龙） | 回复生命 | A 级 — 必争 | 赖线神器，Poke 阵容核心 |
| ⛰️ 山脉亚龙（土龙） | +6% 双抗 | A 级 — 争 | 坦克阵容核心，Poke 阵可放 |
| 🌪️ 云端亚龙（风龙） | +5% 移速 | B 级 — 可选 | 游走型阵容争，其他可放 |
| ⚡ 海克斯亚龙（科技龙） | +5 CDR + 5% 攻速 | B+ 级 — 看阵容 | 依赖技能/攻速的英雄争 |
| 🧪 炼金亚龙（化学龙） | 低血 +5% 伤害 | B 级 — 落后时必争 | 绝地翻盘神器 |

## 建议结构

### 格式要求
```
[DRAGON] <龙类型> in <秒数>s — <争/放> <理由>
<具体行动建议>
```

### 争的情况（按这个逻辑输出）
1. 先报时间和 buff 价值
2. 指定谁去哪个关键位置（辅助去 X、打野去 Y）
3. 提醒关键眼位和清视野
4. 提对方打野位置（如果有信息）

### 放的情况
1. 明确说"放龙"并给出 trade 建议（换塔 / 换峡谷先锋 / 换兵线）
2. 说明为什么这个龙不值得争

### 大龙特殊规则
- **永远建议"逼团不要开龙"**
- 只有对方死 2 人以上 + 死亡时间 > 25 秒 → 可以主动开
- 打完大龙立刻回城补状态 → 不要贪推塔（经典翻盘剧本）

## 正面例子
- `[DRAGON] Infernal in 25s — CONTEST. Ward river south of mid, support take blue buff entrance. Your jungler is botside — he'll be there. This is a must-fight dragon.`
- `[DRAGON] Cloud in 30s — GIVE. Trade for Herald top side instead. Your top has prio, send jungler there. Cloud buff isn't worth losing Herald for.`
- `[BARON] Baron in 45s — SET UP VISION, do NOT start Baron. Clear wards with sweeper, place control ward in pit. Wait for enemy to facecheck.`

## 反面例子（绝对禁止）
- ❌ `Prepare for dragon.` — 太模糊
- ❌ `Dragon soon, be ready.` — 毫无信息量
- ❌ `Do Baron.` — 没有任何前置条件说明，极其危险

## 关键时间表
- 第一条龙刷新：5 分钟
- 龙被击杀后重新刷新：5 分钟
- 大龙刷新：20 分钟
- 大龙重新刷新：6 分钟
- 远古龙：35 分钟后，大龙击杀后 5 分钟开始刷新

## 龙魂规则
- 第 3 条龙（龙魂点）的优先级是前两条的 10 倍
- 己方有龙魂优势时：对方拿第 1 条无所谓，第 2 条开始必须争
- 己方 2 条时：第 3 条就是生死局

## 输出风格
- 2 句话，第一句报时+龙类型+争/放，第二句具体行动
- 数字精确：具体秒数，具体位置
- 用英文输出

## 参考资料
- references/dragon_types.md — 6 种龙 buff 效果与优先级
- references/soul_strategy.md — 龙魂团战打法
- references/baron_setup.md — 大龙视野和逼团
- gotchas.md — 坑点清单
