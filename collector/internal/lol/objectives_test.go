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
	// 下一条龙的元素 Live Client 不提供——只能报 unknown，不能被被杀龙的类型污染
	if state.DragonTimer.Type != "unknown" || state.DragonTimer.SecondsLeft > 26 || state.DragonTimer.SecondsLeft < 24 {
		t.Errorf("timer = %+v", state.DragonTimer)
	}
}

func TestObjectiveTracker_ElderDragonNoRespawn(t *testing.T) {
	tracker := NewObjectiveTracker()
	state := &GameState{
		GameTime: 1500,
		Events:   []GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 1500, DragonType: "Elder"}},
	}
	tracker.Enrich(state)
	if state.DragonTimer != nil {
		t.Fatalf("elder kill must clear dragon timer, got %+v", state.DragonTimer)
	}
	// 即使过了旧的 6min"复活"时间点，也不应再出现龙计时器
	state = &GameState{GameTime: 1805}
	tracker.Enrich(state)
	if state.DragonTimer != nil {
		t.Fatalf("elder never respawns, got %+v", state.DragonTimer)
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

// EventIDs restart at 1 in a new game — a lower ID than the watermark must
// reset the tracker, otherwise the previous game's timers leak into the new one.
func TestObjectiveTracker_NewGameIDRewindResets(t *testing.T) {
	tracker := NewObjectiveTracker()
	s1 := &GameState{GameTime: 600, Events: []GameEvent{
		{EventID: 50, EventName: "DragonKill", EventTime: 600, DragonType: "Infernal"},
	}}
	tracker.Enrich(s1)

	s2 := &GameState{GameTime: 10, Events: []GameEvent{{EventID: 1, EventName: "GameStart"}}}
	tracker.Enrich(s2)
	if s2.DragonTimer != nil {
		t.Fatalf("new game must not inherit previous dragon timer, got %+v", s2.DragonTimer)
	}

	// 首龙默认 5:00；若上局 Infernal 记录泄漏，Type 会是 Infernal 而非 unknown
	s3 := &GameState{GameTime: 295}
	tracker.Enrich(s3)
	if s3.DragonTimer == nil {
		t.Fatal("expected default first-dragon timer at 295s")
	}
	if s3.DragonTimer.Type != "unknown" {
		t.Errorf("dragon type = %s, want unknown (previous game leaked)", s3.DragonTimer.Type)
	}
}

func TestObjectiveTracker_NilStateNoPanic(t *testing.T) {
	NewObjectiveTracker().Enrich(nil)
}
