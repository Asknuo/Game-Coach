package event

import (
	"testing"
	"time"

	"github.com/game-coach/collector/internal/lol"
)

func TestEngine_TwoPassSameTick(t *testing.T) {
	e := NewEngine(NewDetector())
	st := mkState(600)
	st.ActivePlayer.Health = 250 // 25% → low_health
	st.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 620, SecondsLeft: 20}
	evs := e.Process(st)
	if !hasEvent(evs, "dragon_soon") || !hasEvent(evs, "low_health") {
		t.Fatalf("same-tick events must all pass, got %+v", evs)
	}
}

func TestEngine_TeamfightCooldown(t *testing.T) {
	e := NewEngine(NewDetector())
	st := mkState(600)
	st.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
		{EventID: 3, EventName: "ChampionKill", EventTime: 600},
	}
	if !hasEvent(e.Process(st), "teamfight_detected") {
		t.Fatal("expected teamfight_detected on first batch")
	}
	if hasEvent(e.Process(st), "teamfight_detected") {
		t.Fatal("teamfight_detected must respect the 90s cooldown")
	}
}

func TestEngine_TeamfightCooldownExpires(t *testing.T) {
	e := NewEngine(NewDetector())
	st := mkState(600)
	st.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
		{EventID: 3, EventName: "ChampionKill", EventTime: 600},
	}
	e.Process(st)

	e.mu.Lock()
	e.cooldown["teamfight_detected"] = time.Now().Add(-91 * time.Second)
	e.mu.Unlock()

	if !hasEvent(e.Process(st), "teamfight_detected") {
		t.Fatal("expected teamfight_detected after cooldown expiry")
	}
}

func TestEngine_DeathHasNoCooldown(t *testing.T) {
	e := NewEngine(NewDetector())
	e.Process(mkState(100)) // baseline

	st := mkState(200)
	me(st).Deaths = 1
	if !hasEvent(e.Process(st), "death") {
		t.Fatal("expected death event")
	}
	st2 := mkState(300)
	me(st2).Deaths = 2
	if !hasEvent(e.Process(st2), "death") {
		t.Fatal("death must not be blocked by cooldown")
	}
}

func TestEngine_Reset(t *testing.T) {
	e := NewEngine(NewDetector())
	st := mkState(600)
	st.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 625, SecondsLeft: 25}
	if !hasEvent(e.Process(st), "dragon_soon") {
		t.Fatal("expected dragon_soon")
	}
	if hasEvent(e.Process(st), "dragon_soon") {
		t.Fatal("expected suppression before reset")
	}
	e.Reset()
	if !hasEvent(e.Process(st), "dragon_soon") {
		t.Fatal("expected dragon_soon again after reset")
	}
}
