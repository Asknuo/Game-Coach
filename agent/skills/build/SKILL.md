---
name: build-coach
description: 当玩家购买了新装备、金币暴增（delta > 500g）、或敌方购买了新装备时使用。提供下一步出装建议，尤其是根据敌方阵容和装备的克制出装。不要用于没有装备变化的常规状态。
cooldown: 30
priority: 1
events:
  - item_purchased
  - item_sold
  - item_upgraded
  - gold_spike
  - enemy_item_purchased
---

# Build Coach — 出装教练

## 何时触发
- 玩家买了一件新装备
- 玩家金币暴增 500+（可能在泉水准备购物）

## 不用我的情况
- 没有装备变化
- 游戏刚开始（初始装备阶段）

## 建议结构

### 敌方装备检测（enemy_item_purchased）
当敌人购买了新装备时，必须立即检查：
1. **这是什么装备**：核心装？保命装？穿透装？
2. **对你的影响**：需要换出装路线吗？需要提前出防装吗？
3. **给具体建议**：该买什么来应对

#### 敌人出装 → 你必须应对的情况
**敌人出了穿透/穿甲**（赛瑞尔达的怨恨、虚空之杖、多米尼克领主的致意）
→ 你的护甲/魔抗效果打折，不要再堆纯抗性，出生命值装（振奋铠甲、冰霜之心）

**敌人出了重伤**（凡性的提醒、莫雷洛秘典、荆棘之甲）
→ 你的回复/吸血效果打折，出护盾/无敌类装备替（不朽盾弓、中娅沙漏）

**敌人出了金身/复活甲**（中娅沙漏、守护天使）
→ 团战别优先集火这个人，先秒没保命装的

**敌人出了关键大件**（神话装/大件完成）
→ 这是对面的 power spike，这 3 分钟躲着他打

### 格式要求
```
[BUILD] <刚刚买了什么>
Next: <下一步出装建议> — <理由>
```

### 建议优先级
1. **先看局势**：对面 AD 多？AP 多？有强控？有强回血？
2. **再看进度**：你是第几个大件？神话做了吗？
3. **最后给建议**：下一个出什么 + 为什么

### 克制装备速查

**对面有强回血/吸血**（剑魔/吸血鬼/红凯隐/ADC 有破败）
→ AD：死刑宣告(800g) → 凡性的提醒  |  AP：湮灭宝珠(800g) → 莫雷洛秘典  |  坦克：荆棘之甲

**对面 AP 伤害多**（≥3 AP）
→ AD：玛莫提乌斯之噬  |  坦克：振奋铠甲 / 自然之力  |  辅助：钢铁烈阳之匣

**对面 AD 伤害多**（≥3 AD / 刺客多）
→ 所有人：忍者足具  |  坦克：兰顿之兆 / 冰霜之心  |  辅助：骑士之誓

**对面控制多**（≥3 硬控）
→ ADC/刺客：水银弯刀  |  坦克：水银之靴  |  辅助：米凯尔的坩埚

**对面坦克多**（≥2 纯坦）
→ AD：多米尼克领主的致意 / 破败王者之刃  |  AP：兰德里的苦痛 / 虚空之杖

### 出装顺序黄金法则
```
第一大件 → 鞋子 → 第二大件 → 第三大件（穿透/功能）→ 复活甲 → 神装
```

- 第一件大件出来立刻做鞋子（不要憋第二件）
- 第一大件完成是你最强的 individual power spike — 立刻找架打
- 第二件前检查：对面已经在堆抗性 → 出穿透。对面在秒你 → 出保命装
- **永远不要第二件出复活甲** — 没伤害的复活甲是纸
- ADC 第一件收集者 = 对线强；破败 = 打坦克；不知道出什么就收集者

### 重伤装备黄金法则
- **不要第一件裸重伤** — 除非对面是剑魔/吸血鬼/红凯隐
- 对面 ADC 出了个破败 → 第三件以后出重伤就行
- 打野/辅助可以先出重伤作为第二件
- 不需要全队出重伤，1-2 个人出就够了

## 正面例子
- `[BUILD] Luden's Tempest completed. Next: Sorcerer's Shoes then Shadowflame — they haven't built MR yet, capitalize now.`
- `[BUILD] Gold spike +800g. Next: Executioner's Calling — enemy Aatrox is 3/0 with Goredrinker, you NEED anti-heal immediately.`
- `[BUILD] Kraken Slayer done. Next: Plated Steelcaps then Lord Dominik's — enemy team has 3 tanks stacking armor.`

## 反面例子（绝对禁止）
- ❌ `Build recommended items.` — 没有针对性
- ❌ `Buy more damage.` — 太模糊

## 输出风格
- 1-2 句话
- 必须包含 "because/因为" 类型的理由
- 用中文输出

## 参考资料
- references/core_items.md — 各位置核心装备
- references/counter_items.md — 克制装备速查
- references/build_order.md — 出装顺序优先级
- gotchas.md — 坑点清单
