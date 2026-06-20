"""物品 ID → 名称解析器。

加载本地 items.json（dict 或 list 格式），构建 O(1) 的 ID→名称查找表。

用法:
    resolver = ItemResolver()
    name = resolver.get_name("3031")  # → "Infinity Edge"
    names = resolver.get_names(["3031", "6692"])  # → ["Infinity Edge", "Duskblade of Draktharr"]
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 热门装备 ID → 名称硬编码回退表（即使没下载 Data Dragon 也能工作）
FALLBACK_ITEMS: dict[str, dict] = {
    "1001": {"name": "Boots", "cost": 300, "plaintext": "Basic boots"},
    "1004": {"name": "Faerie Charm", "cost": 250, "plaintext": "Mana regen"},
    "1006": {"name": "Rejuvenation Bead", "cost": 300, "plaintext": "HP regen"},
    "1011": {"name": "Giant's Belt", "cost": 900, "plaintext": "Health"},
    "1018": {"name": "Cloak of Agility", "cost": 600, "plaintext": "Crit chance"},
    "1026": {"name": "Blasting Wand", "cost": 850, "plaintext": "Ability power"},
    "1028": {"name": "Ruby Crystal", "cost": 400, "plaintext": "Health"},
    "1029": {"name": "Cloth Armor", "cost": 300, "plaintext": "Armor"},
    "1031": {"name": "Chain Vest", "cost": 800, "plaintext": "Armor"},
    "1033": {"name": "Null-Magic Mantle", "cost": 450, "plaintext": "Magic resist"},
    "1036": {"name": "Long Sword", "cost": 350, "plaintext": "Attack damage"},
    "1037": {"name": "Pickaxe", "cost": 875, "plaintext": "Attack damage"},
    "1038": {"name": "B. F. Sword", "cost": 1300, "plaintext": "Attack damage"},
    "1042": {"name": "Dagger", "cost": 300, "plaintext": "Attack speed"},
    "1043": {"name": "Recurve Bow", "cost": 700, "plaintext": "Attack speed"},
    "1052": {"name": "Amplifying Tome", "cost": 400, "plaintext": "Ability power"},
    "1053": {"name": "Vampiric Scepter", "cost": 900, "plaintext": "Lifesteal"},
    "1054": {"name": "Doran's Shield", "cost": 450, "plaintext": "Sustain starting item"},
    "1055": {"name": "Doran's Blade", "cost": 450, "plaintext": "AD starting item"},
    "1056": {"name": "Doran's Ring", "cost": 400, "plaintext": "AP starting item"},
    "1058": {"name": "Needlessly Large Rod", "cost": 1250, "plaintext": "Ability power"},
    "1082": {"name": "Dark Seal", "cost": 350, "plaintext": "AP stacking item"},
    "1083": {"name": "Cull", "cost": 450, "plaintext": "Farm starting item"},
    "2003": {"name": "Health Potion", "cost": 50, "plaintext": "Heal over time"},
    "2010": {"name": "Total Biscuit of Everlasting Will", "cost": 0, "plaintext": "Run item"},
    "2031": {"name": "Refillable Potion", "cost": 150, "plaintext": "Reusable potion"},
    "2032": {"name": "Hunter's Potion", "cost": 250, "plaintext": "Jungle sustain"},
    "2033": {"name": "Corrupting Potion", "cost": 500, "plaintext": "Lane sustain"},
    "2055": {"name": "Control Ward", "cost": 75, "plaintext": "Vision"},
    "2065": {"name": "Shurelya's Battlesong", "cost": 2200, "plaintext": "Team speed"},
    "2138": {"name": "Elixir of Iron", "cost": 500, "plaintext": "Size/tenacity buff"},
    "2139": {"name": "Elixir of Sorcery", "cost": 500, "plaintext": "True damage buff"},
    "2140": {"name": "Elixir of Wrath", "cost": 500, "plaintext": "AD/lifesteal buff"},
    "2420": {"name": "Seeker's Armguard", "cost": 1600, "plaintext": "AP + armor"},
    "2421": {"name": "Shattered Armguard", "cost": 1600, "plaintext": "AP + armor"},
    "2422": {"name": "Slightly Magical Footwear", "cost": 0, "plaintext": "Rune boots"},
    "2423": {"name": "Stopwatch", "cost": 650, "plaintext": "One-time stasis"},
    "2424": {"name": "Broken Stopwatch", "cost": 0, "plaintext": "Used stasis"},
    "3001": {"name": "Evenshroud", "cost": 2300, "plaintext": "Team damage amp"},
    "3003": {"name": "Archangel's Staff", "cost": 2900, "plaintext": "AP + mana"},
    "3004": {"name": "Manamune", "cost": 2900, "plaintext": "AD + mana"},
    "3006": {"name": "Berserker's Greaves", "cost": 1100, "plaintext": "Attack speed boots"},
    "3009": {"name": "Boots of Swiftness", "cost": 1000, "plaintext": "Slow resist boots"},
    "3011": {"name": "Chemtech Putrifier", "cost": 2300, "plaintext": "Anti-heal support"},
    "3020": {"name": "Sorcerer's Shoes", "cost": 1100, "plaintext": "Magic pen boots"},
    "3024": {"name": "Glacial Buckler", "cost": 900, "plaintext": "Mana + armor"},
    "3026": {"name": "Guardian Angel", "cost": 3200, "plaintext": "Revive on death"},
    "3031": {"name": "Infinity Edge", "cost": 3400, "plaintext": "Crit damage boost"},
    "3033": {"name": "Mortal Reminder", "cost": 3000, "plaintext": "Anti-heal + armor pen"},
    "3035": {"name": "Last Whisper", "cost": 1450, "plaintext": "Armor pen"},
    "3036": {"name": "Lord Dominik's Regards", "cost": 3000, "plaintext": "Tank killer"},
    "3040": {"name": "Seraph's Embrace", "cost": 2900, "plaintext": "AP shield"},
    "3041": {"name": "Mejai's Soulstealer", "cost": 1600, "plaintext": "AP stacking"},
    "3042": {"name": "Muramana", "cost": 2900, "plaintext": "AD on-hit"},
    "3044": {"name": "Phage", "cost": 1100, "plaintext": "HP + move speed"},
    "3046": {"name": "Phantom Dancer", "cost": 2600, "plaintext": "Crit + AS"},
    "3047": {"name": "Plated Steelcaps", "cost": 1100, "plaintext": "Armor boots"},
    "3050": {"name": "Zeke's Convergence", "cost": 2200, "plaintext": "Support aura"},
    "3051": {"name": "Hearthbound Axe", "cost": 1100, "plaintext": "AD + AS"},
    "3053": {"name": "Sterak's Gage", "cost": 3000, "plaintext": "Survival shield"},
    "3057": {"name": "Sheen", "cost": 700, "plaintext": "Spellblade"},
    "3065": {"name": "Spirit Visage", "cost": 2900, "plaintext": "MR + heal boost"},
    "3067": {"name": "Kindlegem", "cost": 800, "plaintext": "HP + AH"},
    "3068": {"name": "Sunfire Aegis", "cost": 2700, "plaintext": "Burn aura"},
    "3070": {"name": "Tear of the Goddess", "cost": 400, "plaintext": "Mana stacking"},
    "3071": {"name": "Black Cleaver", "cost": 3000, "plaintext": "Armor shred"},
    "3072": {"name": "Bloodthirster", "cost": 3400, "plaintext": "Lifesteal + shield"},
    "3074": {"name": "Ravenous Hydra", "cost": 3300, "plaintext": "AoE lifesteal"},
    "3075": {"name": "Thornmail", "cost": 2700, "plaintext": "Reflect damage + anti-heal"},
    "3076": {"name": "Bramble Vest", "cost": 800, "plaintext": "Reflect damage"},
    "3078": {"name": "Trinity Force", "cost": 3333, "plaintext": "All-around stats"},
    "3082": {"name": "Warden's Mail", "cost": 1000, "plaintext": "AS slow aura"},
    "3083": {"name": "Warmog's Armor", "cost": 3000, "plaintext": "HP + regen"},
    "3084": {"name": "Overlord's Bloodmail", "cost": 3100, "plaintext": "HP = AD"},
    "3085": {"name": "Runaan's Hurricane", "cost": 2600, "plaintext": "Multi-target"},
    "3086": {"name": "Zeal", "cost": 1050, "plaintext": "Crit + AS + MS"},
    "3089": {"name": "Rabadon's Deathcap", "cost": 3600, "plaintext": "AP multiplier"},
    "3091": {"name": "Wit's End", "cost": 2800, "plaintext": "MR + AS + on-hit"},
    "3094": {"name": "Rapid Firecannon", "cost": 2600, "plaintext": "Range + AS + Crit"},
    "3095": {"name": "Stormrazor", "cost": 2700, "plaintext": "Crit + slow"},
    "3100": {"name": "Lich Bane", "cost": 3000, "plaintext": "AP spellblade"},
    "3102": {"name": "Banshee's Veil", "cost": 2600, "plaintext": "AP + spell shield"},
    "3107": {"name": "Redemption", "cost": 2300, "plaintext": "Heal + buff"},
    "3108": {"name": "Fiendish Codex", "cost": 900, "plaintext": "AP + AH"},
    "3109": {"name": "Knight's Vow", "cost": 2200, "plaintext": "Redirect damage"},
    "3110": {"name": "Frozen Heart", "cost": 2500, "plaintext": "Armor + mana + AS slow"},
    "3111": {"name": "Mercury's Treads", "cost": 1100, "plaintext": "MR + tenacity boots"},
    "3112": {"name": "Guardian's Orb", "cost": 950, "plaintext": "ARAM AP starter"},
    "3113": {"name": "Aether Wisp", "cost": 850, "plaintext": "AP + MS"},
    "3114": {"name": "Malignance", "cost": 2700, "plaintext": "AP + MR shred ult"},
    "3115": {"name": "Nashor's Tooth", "cost": 3000, "plaintext": "AP + AS"},
    "3116": {"name": "Rylai's Crystal Scepter", "cost": 2600, "plaintext": "AP + slow"},
    "3117": {"name": "Mobility Boots", "cost": 1000, "plaintext": "Fast boots"},
    "3119": {"name": "Winter's Approach/Fimbulwinter", "cost": 2400, "plaintext": "HP + mana + shield"},
    "3121": {"name": "Fimbulwinter", "cost": 2400, "plaintext": "HP shield"},
    "3123": {"name": "Executioner's Calling", "cost": 800, "plaintext": "Anti-heal"},
    "3124": {"name": "Guinsoo's Rageblade", "cost": 3000, "plaintext": "On-hit hybrid"},
    "3133": {"name": "Caulfield's Warhammer", "cost": 1100, "plaintext": "AD + AH"},
    "3134": {"name": "Serrated Dirk", "cost": 1000, "plaintext": "AD + lethality"},
    "3135": {"name": "Void Staff", "cost": 2800, "plaintext": "AP magic pen"},
    "3137": {"name": "Dervish Blade", "cost": 3000, "plaintext": "MR + cleanse"},
    "3139": {"name": "Mercurial Scimitar", "cost": 3300, "plaintext": "MR + cleanse"},
    "3140": {"name": "Quicksilver Sash", "cost": 1300, "plaintext": "Cleanse"},
    "3142": {"name": "Youmuu's Ghostblade", "cost": 2900, "plaintext": "Lethality + speed"},
    "3143": {"name": "Randuin's Omen", "cost": 2700, "plaintext": "Armor + crit resist"},
    "3145": {"name": "Hextech Alternator", "cost": 1100, "plaintext": "AP poke"},
    "3146": {"name": "Hextech Rocketbelt", "cost": 2900, "plaintext": "AP dash + damage"},
    "3152": {"name": "Hextech Protobelt-01", "cost": 2500, "plaintext": "AP dash"},
    "3153": {"name": "Blade of the Ruined King", "cost": 3200, "plaintext": "Tank shred + lifesteal"},
    "3155": {"name": "Hexdrinker", "cost": 1300, "plaintext": "AD + magic shield"},
    "3156": {"name": "Maw of Malmortius", "cost": 2800, "plaintext": "AD + magic shield"},
    "3157": {"name": "Zhonya's Hourglass", "cost": 3000, "plaintext": "AP + stasis"},
    "3158": {"name": "Ionian Boots of Lucidity", "cost": 950, "plaintext": "AH boots"},
    "3161": {"name": "Spear of Shojin", "cost": 3000, "plaintext": "AD + AH"},
    "3165": {"name": "Morellonomicon", "cost": 2200, "plaintext": "AP anti-heal"},
    "3172": {"name": "Athene's Unholy Grail", "cost": 2100, "plaintext": "AP + mana + heal"},
    "3179": {"name": "Umbral Glaive", "cost": 2800, "plaintext": "Lethality + vision"},
    "3181": {"name": "Serylda's Grudge", "cost": 3200, "plaintext": "Lethality + slow"},
    "3184": {"name": "Guardian's Hammer", "cost": 950, "plaintext": "ARAM AD starter"},
    "3187": {"name": "Guardian's Horn", "cost": 950, "plaintext": "ARAM tank starter"},
    "3190": {"name": "Locket of the Iron Solari", "cost": 2200, "plaintext": "Team shield"},
    "3191": {"name": "Dead Man's Plate", "cost": 2700, "plaintext": "Armor + MS"},
    "3193": {"name": "Gargoyle Stoneplate", "cost": 3200, "plaintext": "Resist + big shield"},
    "3211": {"name": "Mikael's Blessing", "cost": 2300, "plaintext": "MR cleanse support"},
    "3222": {"name": "Mikael's Crucible", "cost": 2300, "plaintext": "Cleanse support"},
    "3302": {"name": "Terminus", "cost": 3000, "plaintext": "On-hit resist"},
    "3330": {"name": "Scarecrow Effigy", "cost": 0, "plaintext": "Fiddlesticks trinket"},
    "3340": {"name": "Stealth Ward", "cost": 0, "plaintext": "Trinket ward"},
    "3363": {"name": "Farsight Alteration", "cost": 0, "plaintext": "Long-range trinket"},
    "3364": {"name": "Oracle Lens", "cost": 0, "plaintext": "Sweep trinket"},
    "3504": {"name": "Ardent Censer", "cost": 2300, "plaintext": "Team AS + heal"},
    "3508": {"name": "Essence Reaver", "cost": 2900, "plaintext": "Crit + mana"},
    "3599": {"name": "Kalista's Black Spear", "cost": 0, "plaintext": "Oathsworn"},
    "3600": {"name": "Kalista's Black Spear", "cost": 0, "plaintext": "Oathsworn"},
    "3742": {"name": "Titanic Hydra", "cost": 3300, "plaintext": "HP AD on-hit"},
    "3748": {"name": "Oblivion Orb", "cost": 800, "plaintext": "AP anti-heal"},
    "3814": {"name": "Edge of Night", "cost": 2800, "plaintext": "Lethality + spell shield"},
    "4005": {"name": "Imperial Mandate", "cost": 2500, "plaintext": "Team damage support"},
    "4401": {"name": "Force of Nature", "cost": 2600, "plaintext": "MR + MS stacking"},
    "4628": {"name": "Horizon Focus", "cost": 2700, "plaintext": "AP + reveal"},
    "4629": {"name": "Cosmic Drive", "cost": 3000, "plaintext": "AP + MS"},
    "4630": {"name": "Mask of Abyssal Madness", "cost": 2600, "plaintext": "MR shred MR"},
    "4632": {"name": "Verdant Barrier", "cost": 1000, "plaintext": "AP + MR"},
    "4633": {"name": "Riftmaker", "cost": 3000, "plaintext": "AP + omnivamp"},
    "4635": {"name": "Leeching Leer", "cost": 1100, "plaintext": "AP + omnivamp"},
    "4636": {"name": "Night Harvester", "cost": 2800, "plaintext": "AP + burst"},
    "4637": {"name": "Demonic Embrace", "cost": 3000, "plaintext": "AP + burn"},
    "4638": {"name": "Watchful Wardstone", "cost": 1100, "plaintext": "Vision item"},
    "4641": {"name": "Stirring Wardstone", "cost": 250, "plaintext": "Vision"},
    "4642": {"name": "Bandleglass Mirror", "cost": 900, "plaintext": "AP + mana + AH"},
    "4643": {"name": "Vigilant Wardstone", "cost": 2300, "plaintext": "Upgraded vision"},
    "4644": {"name": "Crown of the Shattered Queen", "cost": 2700, "plaintext": "AP anti-burst"},
    "4645": {"name": "Shadowflame", "cost": 3000, "plaintext": "AP anti-shield"},
    "6029": {"name": "Solstice Sleigh", "cost": 400, "plaintext": "Support sled"},
    "6035": {"name": "Zaz'Zak's Realmspike", "cost": 400, "plaintext": "Burst support"},
    "6333": {"name": "Death's Dance", "cost": 3000, "plaintext": "AD + bleed delay"},
    "6609": {"name": "Celestial Opposition", "cost": 400, "plaintext": "Support shield"},
    "6616": {"name": "Dawncore", "cost": 2700, "plaintext": "Support heal/shield"},
    "6617": {"name": "Moonstone Renewer", "cost": 2200, "plaintext": "Chain heal"},
    "6620": {"name": "Echoes of Helia", "cost": 2200, "plaintext": "AP heal support"},
    "6631": {"name": "Divine Sunderer", "cost": 3300, "plaintext": "Tank buster spellblade"},
    "6632": {"name": "Luden's Companion", "cost": 3000, "plaintext": "AP burst + AoE"},
    "6653": {"name": "Liandry's Torment", "cost": 3000, "plaintext": "AP burn"},
    "6655": {"name": "Liandry's Lament", "cost": 3000, "plaintext": "AP burn"},
    "6656": {"name": "Needlessly Large Wand", "cost": 1250, "plaintext": "AP component"},
    "6660": {"name": "Kraken Slayer", "cost": 3100, "plaintext": "On-hit true damage"},
    "6662": {"name": "Immortal Shieldbow", "cost": 3000, "plaintext": "Crit lifeline"},
    "6664": {"name": "Navori Quickblades", "cost": 2600, "plaintext": "Crit CDR"},
    "6665": {"name": "Recurve Bow", "cost": 700, "plaintext": "AS component"},
    "6666": {"name": "Yun Tal Wildarrows", "cost": 3200, "plaintext": "Crit + AS"},
    "6667": {"name": "Gustwalker Hatchling", "cost": 0, "plaintext": "Speed jungle pet"},
    "6668": {"name": "Scorchclaw Pup", "cost": 0, "plaintext": "Damage jungle pet"},
    "6669": {"name": "Mosstomper Seedling", "cost": 0, "plaintext": "Tank jungle pet"},
    "6670": {"name": "Noonquiver", "cost": 1300, "plaintext": "AD + AS + crit"},
    "6671": {"name": "The Collector", "cost": 3000, "plaintext": "Lethality + crit + execute"},
    "6672": {"name": "Galeforce", "cost": 3000, "plaintext": "Crit dash"},
    "6673": {"name": "Kraken Slayer", "cost": 3100, "plaintext": "On-hit"},
    "6675": {"name": "Scout's Slingshot", "cost": 600, "plaintext": "Crit component"},
    "6676": {"name": "Doran's Lost Sword", "cost": 450, "plaintext": "AD starting"},
    "6677": {"name": "Doran's Lost Ring", "cost": 450, "plaintext": "AP starting"},
    "6691": {"name": "Profane Hydra", "cost": 3300, "plaintext": "Lethality AoE"},
    "6692": {"name": "Voltaic Cyclosword", "cost": 2900, "plaintext": "Lethality + energy burst"},
    "6693": {"name": "Opportunity", "cost": 2700, "plaintext": "Lethality + MS"},
    "6694": {"name": "Axiom Arc", "cost": 3000, "plaintext": "Lethality + ult CDR"},
    "6695": {"name": "Hubris", "cost": 2800, "plaintext": "Lethality + AD stacking"},
    "6696": {"name": "Serpent's Fang", "cost": 2500, "plaintext": "Lethality anti-shield"},
    "6697": {"name": "Cyclosword", "cost": 2900, "plaintext": "Lethality + energy"},
    "6698": {"name": "Sundered Sky", "cost": 3100, "plaintext": "AD + crit heal"},
    "6699": {"name": "Eclipse", "cost": 2800, "plaintext": "AD + lethality + shield"},
    "6700": {"name": "Hollow Radiance", "cost": 2500, "plaintext": "MR + burn aura"},
    "6701": {"name": "Unending Despair", "cost": 2800, "plaintext": "HP + armor + drain"},
    "6702": {"name": "Kaenic Rookern", "cost": 2900, "plaintext": "MR + magic shield"},
    "7000": {"name": "Sandshrike's Claw", "cost": 3100, "plaintext": "AD + spellblade"},
    "7001": {"name": "Sandswimmer's Claw", "cost": 3100, "plaintext": "AD + spellblade"},
    "7002": {"name": "Trailblazer", "cost": 2500, "plaintext": "HP + armor + trail"},
    "7005": {"name": "Stormsurge", "cost": 2900, "plaintext": "AP + burst MS"},
    "7006": {"name": "Hexplate of the B. F. Chain", "cost": 3300, "plaintext": "AS + ult"},
    "7010": {"name": "World Atlas", "cost": 400, "plaintext": "Support item"},
    "7011": {"name": "Runic Compass", "cost": 400, "plaintext": "Support item"},
    "7012": {"name": "Bounty of Worlds", "cost": 400, "plaintext": "Support item"},
    "7020": {"name": "Cryptbloom", "cost": 2800, "plaintext": "AP + MR + heal"},
    "7021": {"name": "Fated Ashes", "cost": 900, "plaintext": "AP burn component"},
    "7022": {"name": "Blackfire Torch", "cost": 2800, "plaintext": "AP burn + mana"},
    "7023": {"name": "Zhonya's Hourglass", "cost": 3000, "plaintext": "AP stasis"},
    "7024": {"name": "Luden's Companion", "cost": 3000, "plaintext": "AP burst"},
    "7025": {"name": "Luden's Companion", "cost": 3000, "plaintext": "AP burst"},
    "7026": {"name": "Rod of Ages", "cost": 2600, "plaintext": "AP + HP + mana growth"},
    "7027": {"name": "Archangel's Staff", "cost": 2900, "plaintext": "AP + mana"},
    "7028": {"name": "Malignance", "cost": 2700, "plaintext": "AP + MR shred"},
    "7029": {"name": "Shadowflame", "cost": 3000, "plaintext": "AP anti-shield"},
    "7030": {"name": "Stormsurge", "cost": 2900, "plaintext": "AP burst"},
    "7031": {"name": "Void Staff", "cost": 2800, "plaintext": "AP pen"},
    "7032": {"name": "Rabadon's Deathcap", "cost": 3600, "plaintext": "AP multiplier"},
    "7033": {"name": "Lich Bane", "cost": 3000, "plaintext": "AP spellblade"},
    "7034": {"name": "Banshee's Veil", "cost": 2600, "plaintext": "AP spell shield"},
    "7035": {"name": "Morellonomicon", "cost": 2200, "plaintext": "AP anti-heal"},
    "7036": {"name": "Horizon Focus", "cost": 2700, "plaintext": "AP reveal"},
    "7037": {"name": "Cosmic Drive", "cost": 3000, "plaintext": "AP + MS"},
    "7038": {"name": "Rylai's Crystal Scepter", "cost": 2600, "plaintext": "AP slow"},
    "7039": {"name": "Riftmaker", "cost": 3000, "plaintext": "AP omnivamp"},
    "7040": {"name": "Liandry's Torment", "cost": 3000, "plaintext": "AP burn"},
    "7041": {"name": "Nashor's Tooth", "cost": 3000, "plaintext": "AP + AS"},
    "7042": {"name": "Hextech Rocketbelt", "cost": 2900, "plaintext": "AP dash"},
    "7043": {"name": "Crown of the Shattered Queen", "cost": 2700, "plaintext": "AP anti-burst"},
    "7044": {"name": "Demonic Embrace", "cost": 3000, "plaintext": "AP + HP burn"},
    "7045": {"name": "Night Harvester", "cost": 2800, "plaintext": "AP burst"},
}


class ItemResolver:
    """物品 ID → 名称解析器。

    优先从本地 items.json 加载，回退到内置硬编码表。
    """

    def __init__(self, data_dir: str | None = None):
        self._items: dict[str, dict] = {}
        self._by_id: dict[str, str] = {}  # id_str → name
        self._by_int_id: dict[int, str] = {}  # int_id → name

        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), "data")

        # 先加载硬编码回退表
        self._items.update(FALLBACK_ITEMS)

        # 尝试加载本地 items.json
        items_path = os.path.join(data_dir, "items.json")
        if os.path.exists(items_path):
            try:
                with open(items_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 兼容 dict 和 list 两种格式
                if isinstance(raw, dict):
                    self._items.update(raw)
                elif isinstance(raw, list):
                    for item in raw:
                        item_id = item.get("id")
                        if item_id:
                            self._items[str(item_id)] = item
            except Exception:
                logger.exception("Failed to load items.json")

        # 构建查找表
        for id_str, item in self._items.items():
            name = item.get("name", "Unknown Item")
            self._by_id[id_str] = name
            try:
                self._by_int_id[int(id_str)] = name
            except (ValueError, TypeError):
                pass

        logger.info("ItemResolver: %d items loaded (%d from local JSON, %d fallback)",
                     len(self._items),
                     len(self._items) - len(FALLBACK_ITEMS),
                     len(FALLBACK_ITEMS))

    def get_name(self, item_id: str | int) -> str:
        """根据物品 ID 返回名称，找不到返回 'Item {id}'."""
        # 尝试字符串 ID
        name = self._by_id.get(str(item_id))
        if name:
            return name
        # 尝试整数 ID
        if isinstance(item_id, int):
            return self._by_int_id.get(item_id, f"Item {item_id}")
        try:
            return self._by_int_id.get(int(item_id), f"Item {item_id}")
        except (ValueError, TypeError):
            return f"Item {item_id}"

    def get_item(self, item_id: str | int) -> dict | None:
        """返回完整的物品数据 dict，找不到返回 None."""
        item = self._items.get(str(item_id))
        if item:
            return item
        if isinstance(item_id, int):
            return self._items.get(str(item_id))
        try:
            return self._items.get(str(int(item_id)))
        except (ValueError, TypeError):
            return None

    def get_names(self, item_ids: list[str | int]) -> list[str]:
        """批量转换 ID → 名称列表."""
        return [self.get_name(iid) for iid in item_ids]

    def describe_item(self, item_id: str | int) -> str:
        """返回物品的完整描述：'名称 (价格g): 简述'."""
        item = self.get_item(item_id)
        if not item:
            return self.get_name(item_id)
        name = item.get("name", "Unknown")
        cost = item.get("gold", {}).get("total", item.get("cost", 0)) if isinstance(item.get("gold"), dict) else item.get("cost", 0)
        plaintext = item.get("plaintext", "")
        if plaintext:
            return f"{name} ({cost}g): {plaintext}"
        return f"{name} ({cost}g)"


# 全局单例
_instance: ItemResolver | None = None


def get_item_resolver() -> ItemResolver:
    global _instance
    if _instance is None:
        _instance = ItemResolver()
    return _instance
