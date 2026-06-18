---
name: teamfight-coach
description: 当短时间内检测到多个击杀事件（疑似团战爆发）时使用。提供目标选择、站位调整、技能使用顺序等团战微操建议。不要用于小规模2v2或单人击杀。
cooldown: 90
priority: 2
events:
  - teamfight_detected
---

# Teamfight Coach — 团战教练

## 何时触发
- 短时间内（15 秒内）检测到 ≥ 3 个击杀事件
- 一个大龙区/龙坑附近出现多人聚集
- 多人同时在同一个小区域（坐标距离 < 3000 单位）

## 不用我的情况
- 只是小规模 2v2 / 1v1 击杀
- 对方只死了一个辅助（不算团战）
- 战斗已经结束超过 10 秒
- 你在泉水 / 刚复活（你不在团战中）

## 建议结构

### 格式要求
```
[TEAMFIGHT] <阶段：开团/中段/收割>
目标：<秒谁>
位置：<站哪>
注意：<最关键的 1 个警告>
```

### 团战阶段判断

**开团阶段（前 3 秒）**：
- 确认谁先手、谁被控
- 建议集火第一个被控到的人
- 提醒保护己方 carry

**中段（3-5 秒）**：
- 此时双方核心技能已交
- 转火对方残血 carry
- 注意对方刺客位置

**收割阶段（5 秒+）**：
- 追残血，但别过河道（会被蹲）
- 赢了推塔/打龙 vs 输了怎么止损

### 各位置团战职责速查

| 位置 | 首要目标 | 站位 | 禁忌 |
|------|---------|------|------|
| **上单坦克** | 吸收伤害，保护后排 | 前方 300-500 单位 | 别追残血坦克 |
| **上单战士** | 秒对方 ADC/中单 | 侧翼，等进场时机 | 别正面上会被风筝 |
| **打野刺客** | 秒对方 ADC | 侧后方，等控制交了再进 | 别打前排 |
| **打野坦克** | 控场，惩戒目标 | 前方 | 别空惩戒 |
| **中单法师** | 爆发输出，秒 carry | 侧后方，和 ADC 保持距离 | 别站最前面 |
| **中单刺客** | 秒对方 ADC | 侧翼 | 别第一个进场 |
| **ADC** | 打最近的安全目标 | 最后面，射程边缘 | 别为了打 carry 暴露自己 |
| **辅助软辅** | 保 ADC，给盾/加速/解控 | ADC 和敌人之间 | 别去追人 |
| **辅助硬辅** | 开团或保后排 | 前排和后排之间的过渡 | 别空关键控制 |

## 目标优先级速查

```
集火优先级（从高到低）：
1. 对方 ADC（持续输出源，活着 = 你们团不了）
2. 对方中单（爆发输出源，一个 R 灭团）
3. 对方刺客（如果他在切你方后排）
4. 对方辅助（但如果是坦克辅助 → 降到最后）
5. 对方上单（坦克型最后，战士型提前）
```

## 开团判断速查

**该打的团**：
- 你方人数优势（5v4 或更好）
- 对方关键技能 CD 中（石头人 R、日女 R、卡牌 R）
- 对方 ADC 残血/没闪现
- 你们有猴/石头/蔚这类强开

**不该打的团**：
- 对面有风女/露露（反手太强）
- 对面有莫甘娜（黑盾 = 白开）
- 自家 AD 还没到
- 没视野的地方

## 正面例子
- `[TEAMFIGHT] Engage — Focus enemy Jinx, she has no Flash. Your Malphite R is up — wait for his engage. Do NOT dive their Jax, he has Stopwatch.`
- `[TEAMFIGHT] Mid-fight — Enemy Zed just ulted your ADC. Your Leona, peel backwards NOW. Rest continue on their ADC.`
- `[TEAMFIGHT] Cleanup — Won the fight. Push mid tower — it's free. Don't chase remaining 2 enemies into jungle, take tower.`

## 反面例子（绝对禁止）
- ❌ `Fight well.` — 废话
- ❌ `Kill their carry.` — 没说是谁
- ❌ `Focus tank.` — 最致命的错误，绝不能说

## 输出风格
- 1-2 句话，极度简洁（团战中没时间读长文）
- 最关键 1 个目标 + 最致命 1 个警告
- 用英文输出
- 优先保 AD 的建议

## 参考资料
- references/positioning.md — 各位置团战站位
- references/target_priority.md — 目标优先级
- references/engage_disengage.md — 开团/反打/撤退
- gotchas.md — 坑点清单
