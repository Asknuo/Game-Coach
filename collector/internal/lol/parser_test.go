package lol

import (
	"encoding/json"
	"os"
	"testing"
)

// Regression for ISSUES #54: missing allPlayers/events must marshal as [] not null.
func TestParseGameState_MissingSlicesSerializeAsEmptyArray(t *testing.T) {
	raw := []byte("{\"gameData\":{\"gameTime\":123.4},\"activePlayer\":{\"summonerName\":\"Ahri\"}}")
	state, err := ParseGameState(raw)
	if err != nil {
		t.Fatalf("ParseGameState: %v", err)
	}
	out, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	var m map[string]interface{}
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatalf("json.Unmarshal: %v", err)
	}
	players, ok := m["all_players"].([]interface{})
	if !ok || len(players) != 0 {
		t.Errorf("all_players = %#v, want empty array", m["all_players"])
	}
	events, ok := m["events"].([]interface{})
	if !ok || len(events) != 0 {
		t.Errorf("events = %#v, want empty array", m["events"])
	}
}

func TestParseGameState_FullSnapshot(t *testing.T) {
	raw, err := os.ReadFile("testdata/snapshot.json")
	if err != nil {
		t.Fatalf("read testdata: %v", err)
	}
	state, err := ParseGameState(raw)
	if err != nil {
		t.Fatalf("ParseGameState: %v", err)
	}
	if state.GameTime != 645 {
		t.Errorf("GameTime = %v, want 645", state.GameTime)
	}
	if state.ActivePlayer.SummonerName != "Ahri" || state.ActivePlayer.Level != 9 {
		t.Errorf("ActivePlayer = %+v", state.ActivePlayer)
	}
	if state.ActivePlayer.Health != 500 || state.ActivePlayer.MaxHealth != 1000 {
		t.Errorf("ActivePlayer health = %v/%v", state.ActivePlayer.Health, state.ActivePlayer.MaxHealth)
	}
	if len(state.ActivePlayer.Items) != 2 || state.ActivePlayer.Items[1].ItemID != 3020 {
		t.Errorf("ActivePlayer items = %+v", state.ActivePlayer.Items)
	}
	if len(state.AllPlayers) != 2 {
		t.Fatalf("AllPlayers len = %d, want 2", len(state.AllPlayers))
	}
	if state.AllPlayers[1].Team != "CHAOS" || state.AllPlayers[1].Kills != 2 || state.AllPlayers[1].CreepScore != 140 {
		t.Errorf("Zed = %+v", state.AllPlayers[1])
	}
	if len(state.Events) != 2 || state.Events[1].EventName != "DragonKill" || state.Events[1].DragonType != "Infernal" {
		t.Errorf("Events = %+v", state.Events)
	}
}

func TestGameState_Helpers(t *testing.T) {
	state := &GameState{
		GameTime: 645,
		ActivePlayer: ActivePlayer{
			SummonerName: "Ahri",
			Health:       500,
			MaxHealth:    1000,
		},
		AllPlayers: []Player{
			{SummonerName: "Ahri", Team: "ORDER", ChampionName: "Ahri", Position: Vec2{X: 7000, Y: 6000}},
			{SummonerName: "Zed", Team: "CHAOS", ChampionName: "Zed", Items: []Item{{ItemID: 3142, Slot: 0}}},
		},
	}
	if !state.IsInGame() {
		t.Error("IsInGame should be true")
	}
	if pct := state.ActivePlayerHealthPct(); pct != 50 {
		t.Errorf("ActivePlayerHealthPct = %v, want 50", pct)
	}
	if team := state.ActivePlayerTeam(); team != "ORDER" {
		t.Errorf("ActivePlayerTeam = %q, want ORDER", team)
	}
	enemies := state.EnemyPlayers()
	if len(enemies) != 1 || enemies[0].SummonerName != "Zed" {
		t.Errorf("EnemyPlayers = %+v", enemies)
	}
	if n := state.ItemCount(state.AllPlayers[1]); n != 1 {
		t.Errorf("ItemCount = %d, want 1", n)
	}
	ap := state.ActivePlayerFromAll()
	if ap == nil || ap.Team != "ORDER" {
		t.Errorf("ActivePlayerFromAll = %+v", ap)
	}
	state.MergeActivePlayer()
	if state.ActivePlayer.Team != "ORDER" || state.ActivePlayer.ChampionName != "Ahri" || state.ActivePlayer.Position.X != 7000 {
		t.Errorf("MergeActivePlayer result = %+v", state.ActivePlayer)
	}
}

func TestGameState_EdgeCases(t *testing.T) {
	offline := &GameState{ActivePlayer: ActivePlayer{MaxHealth: 1000, Health: 800}}
	if offline.IsInGame() {
		t.Error("GameTime 0 must not be in game")
	}
	unknown := &GameState{
		GameTime:     100,
		ActivePlayer: ActivePlayer{SummonerName: "Ghost"},
		AllPlayers:   []Player{{SummonerName: "Ahri", Team: "ORDER"}},
	}
	if team := unknown.ActivePlayerTeam(); team != "" {
		t.Errorf("ActivePlayerTeam = %q, want empty", team)
	}
	if unknown.ActivePlayerFromAll() != nil {
		t.Error("ActivePlayerFromAll should be nil for unknown summoner")
	}
	if unknown.EnemyPlayers() != nil {
		t.Errorf("EnemyPlayers = %+v, want nil", unknown.EnemyPlayers())
	}
	zeroHealth := &GameState{ActivePlayer: ActivePlayer{Health: 0, MaxHealth: 0}}
	if pct := zeroHealth.ActivePlayerHealthPct(); pct != 100 {
		t.Errorf("ActivePlayerHealthPct with maxHealth 0 = %v, want 100", pct)
	}
}
