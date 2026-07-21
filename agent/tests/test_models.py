"""models/state.py 数据模型与工具函数测试."""

from models.state import (
    ActivePlayer,
    GameState,
    Item,
    Player,
    get_position_distance,
    is_position_valid,
)


class TestGameStateValidation:
    def test_none_coerced_to_empty_list(self):
        """Go nil 切片序列化为 null 时，Pydantic 应将其转为 []（issue #54 回归）."""
        gs = GameState.model_validate({
            "game_time": 10.0,
            "all_players": None,
            "events": None,
        })
        assert gs.all_players == []
        assert gs.events == []

    def test_go_item_id_key_accepted(self):
        """Go collector 序列化的 item_id 键应能填充 Item 模型."""
        item = Item.model_validate({"item_id": 3157, "slot": 0})
        assert item.item_id == 3157

    def test_live_client_itemID_alias_accepted(self):
        """Live Client API 原始的 itemID 键也应兼容."""
        item = Item.model_validate({"itemID": 3157, "slot": 0})
        assert item.item_id == 3157


class TestSyncActivePlayer:
    def test_sync_fills_items_champion_kda(self):
        """sync_active_player 应从 all_players 补全装备、英雄名与 KDA."""
        gs = GameState(
            active_player=ActivePlayer(summoner_name="PlayerOne"),
            all_players=[
                Player(
                    summoner_name="PlayerOne",
                    champion_name="Ahri",
                    kills=3,
                    deaths=1,
                    assists=5,
                    items=[Item(item_id=3157, slot=0)],
                ),
            ],
        )
        gs.sync_active_player()
        ap = gs.active_player
        assert ap.champion_name == "Ahri"
        assert ap.kills == 3
        assert ap.deaths == 1
        assert ap.assists == 5
        assert len(ap.items) == 1
        assert ap.items[0].item_id == 3157

    def test_sync_no_match_is_noop(self):
        gs = GameState(
            active_player=ActivePlayer(summoner_name="Ghost"),
            all_players=[Player(summoner_name="SomeoneElse", champion_name="Zed")],
        )
        gs.sync_active_player()
        assert gs.active_player.champion_name == ""


class TestPositionUtils:
    def test_is_position_valid(self):
        assert not is_position_valid({"x": 0, "y": 0})
        assert is_position_valid({"x": 100, "y": 0})
        assert is_position_valid({"x": 0, "y": 50})

    def test_get_position_distance(self):
        assert get_position_distance({"x": 0, "y": 0}, {"x": 3, "y": 4}) == 5.0

    def test_health_pct(self):
        gs = GameState(active_player=ActivePlayer(health=250, max_health=1000))
        assert gs.active_player_health_pct() == 25.0
        # max_health 为 0（加载画面）时默认满血，避免误判死亡
        gs2 = GameState(active_player=ActivePlayer(health=0, max_health=0))
        assert gs2.active_player_health_pct() == 100.0
