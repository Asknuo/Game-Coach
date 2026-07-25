"""攻略生成器 — 基于英雄数据和角色模板，自动生成结构化攻略.

使用 Data Dragon 的：
- tags (角色分类) → 选择角色模板
- allytips → "使用技巧"
- enemytips → "对抗技巧"
- spells → "技能连招"
- info → 难度和属性雷达
"""


class GuideGenerator:
    """基于英雄数据和角色模板，自动生成结构化攻略。"""

    # ── 角色分类映射 ──
    TAG_ROLE_MAP = {
        "Assassin": "刺客",
        "Fighter": "战士",
        "Mage": "法师",
        "Marksman": "射手",
        "Support": "辅助",
        "Tank": "坦克",
    }

    # ── 各角色通用策略模板 ──
    ROLE_STRATEGY: dict[str, dict[str, str]] = {
        "战士": {
            "early": (
                "对线期利用技能消耗和回复优势压制对手。战士通常有不错的续航和持续伤害，"
                "可以积极换血后利用技能回复。注意控线——推线过深容易被 gank。"
                "三级是全盛期，多数战士拥有基础连招。利用草丛取消小兵仇恨来偷A。"
                "6级后单杀能力大幅提升，找角度打击对手是关键。"
            ),
            "mid": (
                "中期是战士最强时期。参与小龙团和先锋团，战士在2-3件套时又肉又有输出。"
                "注意进场时机——别第一个冲，等敌方关键控制交出后从侧翼切入。"
                "分带有优势时给边线压力，让对手疲于防守。利用传送支援远距离团战。"
                "保持经济领先，战士装备贵但滚雪球效果显著。"
            ),
            "late": (
                "后期团战需要精准判断——你是前排但非纯坦克。优先切敌方 ADC/法师，"
                "但不要脱离队友太远。保护己方后排时，利用控制技能拦截敌方突进。"
                "装备成型后，分带可以牵制多人，为队友创造以多打少机会。"
                "关键时刻 CD 很宝贵，等关键技能冷却完毕再进场。"
            ),
        },
        "坦克": {
            "early": (
                "坦克前期伤害不高但坦度足。专注补刀发育，必要时用技能清兵保塔。"
                "利用控制技能配合打野 gank，你的强控是击杀的关键。"
                "做好视野防 gank，坦度优势不代表可以浪。被动叠加双抗/护盾时再换血。"
                "TP 用来支援下路或者回线，不要浪费在对线消耗上。"
            ),
            "mid": (
                "中期是坦克发挥作用的核心时期。配合队友抓单，你的控制链是开启战斗的信号。"
                "小龙和先锋团你是前排核心——站在队伍最前吸收伤害。"
                "保护我方核心输出，用技能打断敌方突进。好的坦克能决定团战节奏。"
                "必要时牺牲自己掩护队友撤退，坦克的死值得。"
            ),
            "late": (
                "后期团战非常依靠你的先手。找到机会打出关键开团后，不要追击——回来保护队友。"
                "你的技能CD在后期很短，持续给出控制。盯紧敌方核心输出位置。"
                "装备成型后你是最硬的盾。正面站住，让队友在安全范围内输出。"
                "注意兵线——作为坦克也可以推线给对手压力。"
            ),
        },
        "刺客": {
            "early": (
                "前期尽量补刀发育，刺客在基础技能全之前伤害不足。用技能消耗耗血，"
                "找机会打出血线压制。注意你的技能冷却——刺客靠一套爆发。"
                "对方血量低于60%时开始寻找击杀机会。利用草丛和视野盲区施压。"
                "6级后是质变点——大部分刺客有单杀或强游走能力。推线后伺机游走。"
            ),
            "mid": (
                "中期是刺客的黄金期。推完线后频繁游走边路，蹲草抓人是你的强项。"
                "捕捉敌方走位失误——脆皮落单就是你的猎物。先手一套秒杀立即脱离。"
                "团战前尝试在敌方视野外找角度切入。盯着对面最脆的人。"
                "经济落后时不要硬打，找落单目标收人头补发育。"
            ),
            "late": (
                "后期团战难度增大。敌方抱团后单切风险很高，需要更好的时机判断。"
                "保持侧翼或后方站位，等队友先开团再用一套带走关键目标。"
                "分带也是一种策略——对手少人开团会犹豫，你来带线施压。"
                "注意金身/复活甲等保命装备的存在，算好敌方关键技能CD。"
                "活着才有输出，不要换一条命——刺客的死通常不值得。"
            ),
        },
        "法师": {
            "early": (
                "法师前期重视补刀和蓝量管理。利用技能消耗同时保持安全的距离。"
                "大多数法师前期较弱，避免无意义换血。利用平A补刀节省蓝量。"
                "注意对方打野位置——法师缺乏逃生能力。六级前后是重要分水岭。"
                "带传送或净化根据对局选择，保命优先。"
            ),
            "mid": (
                "中期法师拥有可观的清线和消耗能力。快速清兵后支援边路。"
                "团战站位靠后——你是输出核心但极脆。利用技能射程在安全线外输出。"
                "注意控制技能的释放——关键控制可以决定团战走向。"
                "关注小龙和先锋团时间，提前用技能在龙坑消耗对手。"
            ),
            "late": (
                "后期法师伤害爆炸。团战前用技能消耗对手血量，但不要贪——保留位移或控制自保。"
                "站后面输出，让你的前排给你创造空间。注意敌方刺客/突进位置。"
                "金身是你的救命装备，关键时刻用。任何控制打到你可能就GG。"
                "你的AOE输出是团战胜负关键，找准角度放技能。"
            ),
        },
        "射手": {
            "early": (
                "ADC 前期以发育为核心。认真补好每一刀，保持兵线在安全位置。"
                "辅助负责消耗，你负责输出——但别贸然跟辅助上头。"
                "注意小地图信息，敌方打野和中路消失时立即后撤。"
                "第一个大件前伤害不显，不要主动求战。经济是ADC的生命线。"
            ),
            "mid": (
                "中期 ADC 开始发力。拿到一两件装备后伤害可观。配合队友推进外塔和拿龙。"
                "团战站位是核心——永远在辅助和坦克身后输出。活着就有输出。"
                "注意对方切入路线，保留闪现/位移技能自保。你的死可能是团灭开端。"
                "推完塔后转线继续施压，不要独自深入敌区。"
            ),
            "late": (
                "后期 ADC 是团队最强输出点。每一步走位都关键——失误即死。"
                "团战优先打离你最近的目标，不要冲到前线去打后排。"
                "出水银饰带/复活甲增加容错。注意对方关键技能是否已交。"
                "优势时跟团推进，劣势时守塔清兵拖延。你的持续输出无人能及。"
            ),
        },
        "辅助": {
            "early": (
                "前期辅助负责视野控制和消耗。帮 ADC 创造安全的补刀环境。"
                "在河道关键位置插眼，掌握敌方打野动向。利用技能消耗对手 AD。"
                "控制好兵线——辅助不要乱 A 兵，让 ADC 控线。"
                "注意小地图给队友打信号，你是团队的眼睛。"
            ),
            "mid": (
                "中期辅助开始游走。帮打野控制野区视野，游走中路施压。"
                "小龙团和先锋团提前做视野，掌控关键区域。你的控制技能是团战发动机。"
                "保护 ADC 和 AP 是你的首要任务。牺牲自己成全队友。"
                "出团队装备（骑士之誓/救赎）增加团队价值。"
            ),
            "late": (
                "后期辅助的视野决定团战走向。提前在关键路口布控——谁有视野谁赢。"
                "团战时紧盯我方核心输出，用技能保护他们。你的价值在于让队友活下来。"
                "注意自己的站位——辅助也怕被秒。关键时刻用自己换 ADC 的命。"
                "购买控制守卫（真眼）保持视野压制，清掉敌方视野。"
            ),
        },
    }

    # ── 伤害类型 → 出装建议 ──
    DAMAGE_BUILD_TIPS: dict[str, dict[str, str]] = {
        "AD": {
            "Fighter": "黑切、血手、死亡之舞是战士核心装。对线 AD 先出布甲鞋，对线 AP 出水银鞋。贪欲九头蛇提供清线和续航。",
            "Assassin": "幽梦、幕刃、夜之锋刃是刺客核心。暗行者之爪提供额外突进。赛瑞尔达的怨恨破甲。",
            "Marksman": "海妖杀手、无尽之刃、多米尼克领主的致意是射手核心。绿叉/饮血提供保命。",
        },
        "AP": {
            "Mage": "卢登的伙伴/兰德里的苦楚看对方阵容。影焰、灭世者的死亡之帽、虚空之杖是法师核心。中娅沙漏提供团战保命。",
            "Assassin": "暗夜收割者提供爆发。巫妖之祸提供普攻伤害。中娅沙漏提供进场容错。",
        },
    }

    def generate(self, champ: dict) -> list[dict]:
        """为单个英雄生成攻略段列表。"""
        name = champ.get("name", "Unknown")
        tags = champ.get("tags", [])
        partype = champ.get("partype", "")
        info = champ.get("info", {})
        lore = champ.get("lore", "")
        blurb = champ.get("blurb", "")
        allytips = champ.get("allytips", [])
        enemytips = champ.get("enemytips", [])
        spells = champ.get("spells", [])
        passive = champ.get("passive", {})
        stats = champ.get("stats", {})

        role = self._classify_role(tags)
        sections = []

        # 1. 英雄概览
        sections.append(self._overview(name, tags, info, lore, blurb, stats, passive, spells, role))

        # 2-4. 早中晚期策略
        strategy = self.ROLE_STRATEGY.get(role, self.ROLE_STRATEGY["战士"])
        sections.append({"heading": "Early Game Strategy (0-14 min)", "body": strategy["early"],
                         "slug": "early_game_strategy", "phase": "early"})
        sections.append({"heading": "Mid Game Strategy (15-25 min)", "body": strategy["mid"],
                         "slug": "mid_game_strategy", "phase": "mid"})
        sections.append({"heading": "Late Game Strategy (25+ min)", "body": strategy["late"],
                         "slug": "late_game_strategy", "phase": "late"})

        # 5. 技能连招
        sections.append(self._skill_combos(name, passive, spells, role))

        # 6. 使用技巧（来自 Data Dragon allytips）
        if allytips:
            sections.append({"heading": "How to Play (Tips)", "body": " ".join(allytips),
                             "slug": "how_to_play", "phase": "meta"})

        # 7. 对抗技巧（来自 Data Dragon enemytips）
        if enemytips:
            sections.append({"heading": "How to Counter (Enemy Tips)", "body": " ".join(enemytips),
                             "slug": "how_to_counter", "phase": "meta"})

        # 8. 出装建议
        sections.append(self._build_recommendations(tags, role, spells, partype))

        # 9. 关键数据
        sections.append(self._key_stats(name, stats))

        return sections

    def _classify_role(self, tags: list[str]) -> str:
        for tag in tags:
            if tag in self.TAG_ROLE_MAP:
                return self.TAG_ROLE_MAP[tag]
        return "战士"

    def _overview(self, name: str, tags: list[str], info: dict, lore: str,
                  blurb: str, stats: dict, passive: dict, spells: list[dict],
                  role: str) -> dict:
        parts = [f"{name} 是一个{role}英雄"]
        if tags:
            parts.append(f"定位：{'/'.join(tags)}")
        if blurb:
            parts.append(blurb)
        if info:
            parts.append(
                f"难度{info.get('difficulty',0)}/10，"
                f"攻击{info.get('attack',0)}/10，"
                f"防御{info.get('defense',0)}/10，"
                f"法术{info.get('magic',0)}/10"
            )
        # 被动技能
        if passive and passive.get("name"):
            parts.append(f"被动：{passive.get('name', '')}")
        # 技能名列表
        spell_names = [s.get("name", "") for s in spells if s.get("name")]
        if spell_names:
            parts.append(f"技能：Q-{spell_names[0] if len(spell_names)>0 else ''} "
                         f"W-{spell_names[1] if len(spell_names)>1 else ''} "
                         f"E-{spell_names[2] if len(spell_names)>2 else ''} "
                         f"R-{spell_names[3] if len(spell_names)>3 else ''}")
        return {
            "heading": "Overview",
            "body": "；".join(parts),
            "slug": "auto_overview",
            "phase": "meta",
        }

    def _skill_combos(self, name: str, passive: dict, spells: list[dict], role: str) -> dict:
        spell_names = [s.get("name", "") for s in spells if s.get("name")]
        passive_name = passive.get("name", "") if passive else ""

        combos = []
        if role in ("刺客", "法师"):
            combos.append("常规消耗连招：利用基础技能进行 poke，压低血线后找机会")
            if len(spell_names) >= 4:
                combos.append(f"爆发连招：{spell_names[0]} → {spell_names[1]} → {spell_names[2]} → {spell_names[3]} 一套带走")
            if passive_name:
                combos.append(f"注意触发被动 {passive_name} 来最大化伤害")
        elif role in ("战士", "坦克"):
            if len(spell_names) >= 4:
                combos.append(f"标准连招：{spell_names[0]} → {spell_names[1]} → {spell_names[2]}插入普攻 → {spell_names[3]}")
            combos.append("利用技能间隙穿插普攻以最大化输出")
            if passive_name:
                combos.append(f"保持被动 {passive_name} 层数以获得增益效果")
        elif role == "射手":
            combos.append("走A是基本功——每发普攻之间移动来保持安全距离")
            if passive_name:
                combos.append(f"留意被动 {passive_name} 的触发条件，最大化攻速/伤害加成")
        elif role == "辅助":
            combos.append("注意技能释放时机，配合 ADC 打出关键控制链")
            if passive_name:
                combos.append(f"利用被动 {passive_name} 给队友提供额外增益")

        if not combos:
            combos.append("熟练掌握基础技能连招，注意技能释放顺序和时机")

        return {
            "heading": "Skill Combos",
            "body": " ".join(combos),
            "slug": "auto_skill_combos",
            "phase": "meta",
        }

    def _build_recommendations(self, tags: list[str], role: str,
                                spells: list[dict], partype: str) -> dict:
        tips = []
        is_ad = self._is_ad_champ(tags, spells, partype)

        if is_ad:
            for sub_role in tags:
                if sub_role in self.DAMAGE_BUILD_TIPS.get("AD", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AD"][sub_role])
                    break
            else:
                if role in self.DAMAGE_BUILD_TIPS.get("AD", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AD"].get(role, "根据对局情况选择核心输出装备"))
        else:
            for sub_role in tags:
                if sub_role in self.DAMAGE_BUILD_TIPS.get("AP", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AP"][sub_role])
                    break
            else:
                if role in self.DAMAGE_BUILD_TIPS.get("AP", {}):
                    tips.append(self.DAMAGE_BUILD_TIPS["AP"].get(role, "选择法术强度或法术穿透装备"))

        if role == "坦克":
            tips.append("出装建议：日炎圣盾、荆棘之甲、自然之力是坦克核心。根据对方主要输出类型选择护甲或魔抗。石像鬼石板甲提供团战无敌。")
        elif role == "辅助":
            tips.append("辅助出装：升级辅助装 → 骑士之誓/救赎 → 警觉眼石。根据对方阵容选择鸟盾/坩埚。控制守卫（真眼）常备2个。")

        return {
            "heading": "Build Recommendations",
            "body": " ".join(tips) if tips else "根据对局和阵容灵活选择出装。",
            "slug": "auto_build",
            "phase": "meta",
        }

    def _is_ad_champ(self, tags: list[str], spells: list[dict], partype: str) -> bool:
        """粗略判断 AD/AP。"""
        if "Marksman" in tags:
            return True
        if "Fighter" in tags:
            return True
        if "Tank" in tags:
            return True
        if "Assassin" in tags:
            # 有些刺客是 AP (如阿卡丽、艾克)，但大部分是 AD
            return True
        return False

    def _key_stats(self, name: str, stats: dict) -> dict:
        stat_info = []
        key_stats_map = {
            "hp": "基础生命值", "hpperlevel": "生命成长", "mp": "基础法力值", "mpperlevel": "法力成长",
            "movespeed": "移速", "attackrange": "攻击距离",
            "armor": "基础护甲", "armorperlevel": "护甲成长",
            "spellblock": "基础魔抗", "spellblockperlevel": "魔抗成长",
            "attackdamage": "基础攻击力", "attackdamageperlevel": "攻击成长",
            "attackspeed": "攻速", "attackspeedperlevel": "攻速成长",
        }
        for k, label in key_stats_map.items():
            if k in stats:
                stat_info.append(f"{label}: {stats[k]}")
        return {
            "heading": "Key Stats",
            "body": " ".join(stat_info),
            "slug": "auto_key_stats",
            "phase": "meta",
        }
