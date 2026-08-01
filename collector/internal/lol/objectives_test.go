package lol

import "testing"

func TestObjectiveTracker_DefaultDragonSpawn(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{GameTime: 290}
	tracker.Enrich(state)
	if state.DragonTimer == nil {
		t.Fatal("expected dragon timer within warn window")
	}
	if state.DragonTimer.SecondsLeft > 11 || state.DragonTimer.SecondsLeft < 9 {
		t.Errorf("seconds_left = %v, want ~10", state.DragonTimer.SecondsLeft)
	}
}

func TestObjectiveTracker_DragonRespawnAfterKill(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{
		GameTime: 600,
		Events:   []GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 600, DragonType: "Infernal"}},
	}
	tracker.Enrich(state)
	if state.DragonTimer != nil {
		t.Fatalf("dragon spawn in 300s must not warn, got %+v", state.DragonTimer)
	}
	state = &GameState{GameTime: 875}
	tracker.Enrich(state)
	if state.DragonTimer == nil {
		t.Fatal("expected dragon timer at ~25s left")
	}
	if state.DragonTimer.Type != "Infernal" || state.DragonTimer.SecondsLeft > 26 || state.DragonTimer.SecondsLeft < 24 {
		t.Errorf("timer = %+v", state.DragonTimer)
	}
}

func TestObjectiveTracker_ElderDragonRespawn(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{
		GameTime: 1500,
		Events:   []GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 1500, DragonType: "Elder"}},
	}
	tracker.Enrich(state)
	state = &GameState{GameTime: 1805}
	tracker.Enrich(state)
	if state.DragonTimer == nil {
		t.Fatal("expected elder dragon timer")
	}
	if state.DragonTimer.SecondsLeft > 56 || state.DragonTimer.SecondsLeft < 54 {
		t.Errorf("seconds_left = %v, want ~55 (elder respawn 6min)", state.DragonTimer.SecondsLeft)
	}
}

func TestObjectiveTracker_BaronRespawn(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{
		GameTime: 1300,
		Events:   []GameEvent{{EventID: 1, EventName: "BaronKill", EventTime: 1300}},
	}
	tracker.Enrich(state)
	state = &GameState{GameTime: 1610}
	tracker.Enrich(state)
	if state.BaronTimer == nil {
		t.Fatal("expected baron timer")
	}
	if state.BaronTimer.SecondsLeft > 51 || state.BaronTimer.SecondsLeft < 49 {
		t.Errorf("seconds_left = %v, want ~50", state.BaronTimer.SecondsLeft)
	}
}

func TestObjectiveTracker_GameStartResets(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{
		GameTime: 600,
		Events:   []GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 600, DragonType: "Infernal"}},
	}
	tracker.Enrich(state)
	state = &GameState{
		GameTime: 700,
		Events:   []GameEvent{{EventID: 10, EventName: "GameStart", EventTime: 700}},
	}
	tracker.Enrich(state)
	if state.DragonTimer != nil {
		t.Fatalf("dragon timer must be reset by GameStart, got %+v", state.DragonTimer)
	}
	state = &GameState{GameTime: 1150}
	tracker.Enrich(state)
	if state.BaronTimer == nil {
		t.Fatal("expected default baron timer at 20:00")
	}
}
