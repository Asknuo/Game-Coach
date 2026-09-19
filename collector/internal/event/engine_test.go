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

	// 冷却过期后，新一波击杀（EventID 4–6）应再次触发团战检测
	st2 := mkState(605)
	st2.Events = append(st.Events,
		lol.GameEvent{EventID: 4, EventName: "ChampionKill", EventTime: 601},
		lol.GameEvent{EventID: 5, EventName: "ChampionKill", EventTime: 602},
		lol.GameEvent{EventID: 6, EventName: "ChampionKill", EventTime: 603},
	)
	if !hasEvent(e.Process(st2), "teamfight_detected") {
		t.Fatal("expected teamfight_detected after cooldown expiry with fresh kills")
	}
}

func TestEngine_KillNotSwallowed(t *testing.T) {
	e := NewEngine(NewDetector())
	e.Process(mkState(100)) // baseline

	st := mkState(200)
	me(st).Kills = 1
	if !hasEvent(e.Process(st), "kill") {
		t.Fatal("expected first kill event")
	}
	st2 := mkState(210)
	me(st2).Kills = 2
	if !hasEvent(e.Process(st2), "kill") {
		t.Fatal("double kill must not be swallowed by a name-only cooldown")
	}
}

// Two different enemies acting within one cooldown window must not block each other.
func TestEngine_EnemyCooldownIsPerSubject(t *testing.T) {
	e := NewEngine(NewDetector())
	e.Process(mkState(100)) // baseline

	st2 := mkState(150)
	st2.AllPlayers[1].Items = []lol.Item{{ItemID: 3142, Slot: 0}}
	if !hasEvent(e.Process(st2), "enemy_item_purchased") {
		t.Fatal("expected enemy_item_purchased for Enemy1")
	}

	st3 := mkState(155)
	st3.AllPlayers[1].Items = []lol.Item{{ItemID: 3142, Slot: 0}}
	st3.AllPlayers = append(st3.AllPlayers, lol.Player{
		SummonerName: "Enemy2", Team: "CHAOS", ChampionName: "Yasuo",
		CurrentGold: 500, Items: []lol.Item{{ItemID: 3143, Slot: 0}},
	})
	if !hasEvent(e.Process(st3), "enemy_item_purchased") {
		t.Fatal("Enemy2 purchase within Enemy1's cooldown window must not be swallowed")
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
