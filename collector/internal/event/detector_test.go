package event

import (
	"testing"

	"github.com/game-coach/collector/internal/lol"
)

// mkState returns a minimal in-game state: "Me" (ORDER) vs "Enemy1" (CHAOS).
func mkState(gameTime float64) *lol.GameState {
	return &lol.GameState{
		GameTime: gameTime,
		ActivePlayer: lol.ActivePlayer{
			SummonerName: "Me",
			CurrentGold:  500,
			Health:       1000,
			MaxHealth:    1000,
		},
		AllPlayers: []lol.Player{
			{SummonerName: "Me", Team: "ORDER", ChampionName: "Ahri", CurrentGold: 500},
			{SummonerName: "Enemy1", Team: "CHAOS", ChampionName: "Zed", CurrentGold: 500},
		},
	}
}

func me(s *lol.GameState) *lol.Player {
	return &s.AllPlayers[0]
}

func hasEvent(evs []Event, name string) bool {
	return eventByName(evs, name) != nil
}

func eventByName(evs []Event, name string) *Event {
	for i := range evs {
		if evs[i].Name == name {
			return &evs[i]
		}
	}
	return nil
}

func TestDetect_FirstTickRecordsWithoutEvents(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	st.ActivePlayer.Health = 500 // 50% hp — no low_health
	me(st).Deaths = 1
	me(st).Kills = 1
	me(st).Items = []lol.Item{{ItemID: 1056, Slot: 0}}

	if evs := d.Detect(st); len(evs) != 0 {
		t.Fatalf("first tick must record baseline silently, got %+v", evs)
	}
	if evs := d.Detect(st); len(evs) != 0 {
		t.Fatalf("identical second tick must emit nothing, got %+v", evs)
	}
}

func TestDetect_Death(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	me(st).Deaths = 1
	d.Detect(st) // baseline records deaths=1

	st2 := mkState(200)
	me(st2).Deaths = 3
	evs := d.Detect(st2)
	deaths := 0
	for _, ev := range evs {
		if ev.Name == "death" {
			deaths++
			if ev.Data["total_deaths"] != 3 {
				t.Errorf("total_deaths = %v, want 3", ev.Data["total_deaths"])
			}
		}
	}
	if deaths != 2 {
		t.Errorf("expected 2 death events for deaths jump 1→3, got %d", deaths)
	}
	if hasEvent(d.Detect(st2), "death") {
		t.Error("same deaths must not re-trigger")
	}
}

func TestDetect_ItemPurchased(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	me(st).Items = []lol.Item{{ItemID: 1056, Slot: 0}}
	d.Detect(st) // baseline

	st2 := mkState(150)
	me(st2).Items = []lol.Item{{ItemID: 1056, Slot: 0}, {ItemID: 3020, Slot: 1}}
	ev := eventByName(d.Detect(st2), "item_purchased")
	if ev == nil {
		t.Fatal("expected item_purchased")
	}
	if ev.Data["item_id"] != 3020 {
		t.Errorf("item_id = %v, want 3020", ev.Data["item_id"])
	}
}

func TestDetect_ItemUpgraded(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	me(st).Items = []lol.Item{{ItemID: 1056, Slot: 0}}
	d.Detect(st) // baseline

	st2 := mkState(150)
	me(st2).Items = []lol.Item{{ItemID: 3020, Slot: 0}}
	evs := d.Detect(st2)
	ev := eventByName(evs, "item_upgraded")
	if ev == nil {
		t.Fatalf("expected item_upgraded, got %+v", evs)
	}
	if ev.Data["old_item_id"] != 1056 || ev.Data["new_item_id"] != 3020 || ev.Data["slot"] != 0 {
		t.Errorf("upgrade = %+v", ev.Data)
	}
	if hasEvent(evs, "item_purchased") || hasEvent(evs, "item_sold") {
		t.Errorf("upgrade must not also emit purchased/sold, got %+v", evs)
	}
}

func TestDetect_Kill(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	d.Detect(st) // baseline: Me 0 kills, Enemy1 0 kills

	st2 := mkState(200)
	me(st2).Kills = 2
	st2.AllPlayers[1].Kills = 3
	evs := d.Detect(st2)
	ev := eventByName(evs, "kill")
	if ev == nil {
		t.Fatalf("expected kill event, got %+v", evs)
	}
	if ev.Data["killer"] != "Me" || ev.Data["total_kills"] != 2 {
		t.Errorf("kill data = %+v", ev.Data)
	}
	if kills := countEvents(evs, "kill"); kills != 1 {
		t.Errorf("enemy kills must not trigger kill event, got %d kill events", kills)
	}
}

func countEvents(evs []Event, name string) int {
	n := 0
	for _, ev := range evs {
		if ev.Name == name {
			n++
		}
	}
	return n
}

func TestDetect_GoldSpike(t *testing.T) {
	d := NewDetector()
	d.Detect(mkState(100)) // baseline gold 500

	st := mkState(200)
	st.ActivePlayer.CurrentGold = 1100
	ev := eventByName(d.Detect(st), "gold_spike")
	if ev == nil {
		t.Fatal("expected gold_spike for +600 gold")
	}
	if ev.Data["delta"] != 600.0 {
		t.Errorf("delta = %v, want 600", ev.Data["delta"])
	}

	st2 := mkState(210)
	st2.ActivePlayer.CurrentGold = 1150
	if hasEvent(d.Detect(st2), "gold_spike") {
		t.Error("+50 gold must not trigger gold_spike")
	}
}

func TestDetect_LowHealth(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	st.ActivePlayer.Health = 250 // 25%
	if eventByName(d.Detect(st), "low_health") == nil {
		t.Fatal("expected low_health at 25% hp")
	}
	if hasEvent(d.Detect(st), "low_health") {
		t.Fatal("low_health must not re-trigger while still low")
	}

	st2 := mkState(200)
	st2.ActivePlayer.Health = 450 // 45% — clears the warned flag
	d.Detect(st2)

	st3 := mkState(210)
	st3.ActivePlayer.Health = 200 // 20%
	if eventByName(d.Detect(st3), "low_health") == nil {
		t.Fatal("expected low_health again after recovery above 40%")
	}
}

// A 25-minute game has 2-4 dragons: each spawn must warn once, and the
// countdown running out under 30s must not re-warn for the same spawn.
func TestDetect_DragonSoonPerSpawn(t *testing.T) {
	d := NewDetector()

	// Dragon #1 spawns at 625 (infernal taken at 325 + 5 min respawn).
	st := mkState(600)
	st.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 625, SecondsLeft: 25}
	if eventByName(d.Detect(st), "dragon_soon") == nil {
		t.Fatal("expected dragon_soon for the first dragon")
	}

	// Same spawn, still inside the 30s window — no duplicate.
	for _, left := range []float64{20, 10, 5} {
		s := mkState(625 - left)
		s.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 625, SecondsLeft: left}
		if hasEvent(d.Detect(s), "dragon_soon") {
			t.Fatalf("dragon_soon must fire once per spawn, re-fired at %vs left", left)
		}
	}

	// Dragon spawns (timer goes away) and is killed; the next spawn appears 5
	// minutes later — that one must warn again.
	gone := mkState(626)
	if hasEvent(d.Detect(gone), "dragon_soon") {
		t.Fatal("no dragon pending → no dragon_soon")
	}
	st2 := mkState(900)
	st2.DragonTimer = &lol.DragonInfo{Type: "Ocean", SpawnTime: 920, SecondsLeft: 20}
	if eventByName(d.Detect(st2), "dragon_soon") == nil {
		t.Fatal("expected dragon_soon for the second dragon of the game")
	}

	// …and the third one.
	d.Detect(mkState(1210)) // timer gone again
	st3 := mkState(1250)
	st3.DragonTimer = &lol.DragonInfo{Type: "Mountain", SpawnTime: 1270, SecondsLeft: 20}
	if eventByName(d.Detect(st3), "dragon_soon") == nil {
		t.Fatal("expected dragon_soon for the third dragon of the game")
	}
}

// Baron uses the same spawn-identity latch as dragon.
func TestDetect_BaronSoonPerSpawn(t *testing.T) {
	d := NewDetector()

	st := mkState(1200)
	st.BaronTimer = &lol.BaronInfo{SpawnTime: 1225, SecondsLeft: 25}
	if eventByName(d.Detect(st), "baron_soon") == nil {
		t.Fatal("expected baron_soon")
	}

	st2 := mkState(1210)
	st2.BaronTimer = &lol.BaronInfo{SpawnTime: 1225, SecondsLeft: 15}
	if hasEvent(d.Detect(st2), "baron_soon") {
		t.Fatal("baron_soon must fire once per spawn")
	}

	st3 := mkState(1226) // baron taken, timer gone
	if hasEvent(d.Detect(st3), "baron_soon") {
		t.Fatal("no baron pending → no baron_soon")
	}

	st4 := mkState(1600)
	st4.BaronTimer = &lol.BaronInfo{SpawnTime: 1620, SecondsLeft: 20}
	if eventByName(d.Detect(st4), "baron_soon") == nil {
		t.Fatal("expected baron_soon for the next baron spawn")
	}
}

// Timer outside the warn window (SecondsLeft > 30) must not fire.
func TestDetect_ObjectiveSoonRespectsWindow(t *testing.T) {
	d := NewDetector()
	st := mkState(600)
	st.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 700, SecondsLeft: 100}
	if hasEvent(d.Detect(st), "dragon_soon") {
		t.Fatal("100s left must not warn")
	}
	st2 := mkState(640)
	st2.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 700, SecondsLeft: 25}
	if eventByName(d.Detect(st2), "dragon_soon") == nil {
		t.Fatal("25s left must warn")
	}
}

// End-to-end for the latch: drives the REAL objective tracker (the live source
// of dragon_timer) through two dragon kills and asserts both spawns warn.
func TestDetect_DragonSoonAcrossRealSpawns(t *testing.T) {
	tracker := lol.NewObjectiveTracker()
	d := NewDetector()

	// Dragon #1 killed at 320 → respawn 620 (5 min).
	tick := func(gameTime float64, events []lol.GameEvent) []Event {
		st := mkState(gameTime)
		st.Events = events
		tracker.Enrich(st)
		return d.Detect(st)
	}

	if hasEvent(tick(560, []lol.GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 320, DragonType: "Infernal"}}), "dragon_soon") {
		t.Fatal("60s left must not warn")
	}
	if !hasEvent(tick(600, []lol.GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 320, DragonType: "Infernal"}}), "dragon_soon") {
		t.Fatal("20s left must warn for dragon #1")
	}
	// Countdown keeps running for the same spawn — still one warning.
	if hasEvent(tick(610, []lol.GameEvent{{EventID: 1, EventName: "DragonKill", EventTime: 320, DragonType: "Infernal"}}), "dragon_soon") {
		t.Fatal("same spawn must not warn twice")
	}

	// Dragon #2 killed at 650 → respawn 950.
	events := []lol.GameEvent{
		{EventID: 1, EventName: "DragonKill", EventTime: 320, DragonType: "Infernal"},
		{EventID: 2, EventName: "DragonKill", EventTime: 650, DragonType: "Cloud"},
	}
	if hasEvent(tick(915, events), "dragon_soon") {
		t.Fatal("35s left is outside the warn threshold — must not warn yet")
	}
	if !hasEvent(tick(925, events), "dragon_soon") {
		t.Fatal("dragon #2 must warn: the latch used to die with the first dragon")
	}
	if hasEvent(tick(940, events), "dragon_soon") {
		t.Fatal("dragon #2 must warn once")
	}
}

// engine.Reset() must clear the per-spawn latch.
func TestDetect_ResetClearsSpawnLatch(t *testing.T) {
	d := NewDetector()
	st := mkState(600)
	st.DragonTimer = &lol.DragonInfo{Type: "Infernal", SpawnTime: 625, SecondsLeft: 25}
	if eventByName(d.Detect(st), "dragon_soon") == nil {
		t.Fatal("expected dragon_soon")
	}
	if hasEvent(d.Detect(st), "dragon_soon") {
		t.Fatal("same spawn must not re-fire")
	}
	d.reset()
	if eventByName(d.Detect(st), "dragon_soon") == nil {
		t.Fatal("reset must allow dragon_soon again")
	}
}

func TestDetect_TeamfightRequiresThreeKills(t *testing.T) {
	d := NewDetector()
	st := mkState(600)
	st.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
	}
	if hasEvent(d.Detect(st), "teamfight_detected") {
		t.Fatal("2 kills must not trigger teamfight")
	}

	st2 := mkState(610)
	st2.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
		{EventID: 3, EventName: "ChampionKill", EventTime: 610},
	}
	if !hasEvent(d.Detect(st2), "teamfight_detected") {
		t.Fatal("3 kills within 15s must trigger teamfight")
	}
}

func TestDetect_TeamfightNoDoubleCount(t *testing.T) {
	// state.Events 是全量历史：同两条击杀跨多个 tick 重复出现时，
	// 无 EventID 水位会被重复计数凑满 3 杀而误报团战
	d := NewDetector()
	st := mkState(600)
	st.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
	}
	for _, t2 := range []float64{600, 601, 602} {
		s := mkState(t2)
		s.Events = st.Events
		if hasEvent(d.Detect(s), "teamfight_detected") {
			t.Fatalf("2 real kills must not trigger teamfight at t=%v", t2)
		}
	}
}

func TestDetect_TeamfightPrunesOutsideWindow(t *testing.T) {
	d := &Detector{recentKillTimes: []float64{100, 110, 120}}
	st := mkState(130)
	if hasEvent(d.Detect(st), "teamfight_detected") {
		t.Fatal("kills older than 15s must be pruned")
	}

	st2 := mkState(133)
	st2.Events = []lol.GameEvent{{EventID: 9, EventName: "ChampionKill", EventTime: 121}}
	if hasEvent(d.Detect(st2), "teamfight_detected") {
		t.Fatal("2 kills in window must not trigger")
	}

	st3 := mkState(134)
	st3.Events = []lol.GameEvent{{EventID: 10, EventName: "ChampionKill", EventTime: 122}}
	if !hasEvent(d.Detect(st3), "teamfight_detected") {
		t.Fatal("3 kills within 15s must trigger")
	}
}

func TestDetect_EnemyItemPurchased(t *testing.T) {
	d := NewDetector()
	st := mkState(100)
	st.AllPlayers[1].Items = []lol.Item{{ItemID: 3142, Slot: 0}}
	d.Detect(st) // baseline

	st2 := mkState(150)
	st2.AllPlayers[1].Items = []lol.Item{{ItemID: 3142, Slot: 0}, {ItemID: 3157, Slot: 1}}
	ev := eventByName(d.Detect(st2), "enemy_item_purchased")
	if ev == nil {
		t.Fatal("expected enemy_item_purchased")
	}
	if ev.Data["enemy_name"] != "Enemy1" || ev.Data["enemy_champion"] != "Zed" {
		t.Errorf("enemy data = %+v", ev.Data)
	}
	ids, ok := ev.Data["item_ids"].([]int)
	if !ok || len(ids) != 1 || ids[0] != 3157 {
		t.Errorf("item_ids = %v, want [3157]", ev.Data["item_ids"])
	}
}

func TestDetect_EnemyGoldLeadAndHysteresis(t *testing.T) {
	d := NewDetector()
	d.Detect(mkState(100)) // baseline

	st := mkState(200)
	st.AllPlayers[1].CurrentGold = 3000 // gap 2500
	if eventByName(d.Detect(st), "enemy_gold_lead") == nil {
		t.Fatal("expected enemy_gold_lead for gap > 2000")
	}

	st2 := mkState(210)
	st2.AllPlayers[1].CurrentGold = 3200 // still > 2000 gap
	if hasEvent(d.Detect(st2), "enemy_gold_lead") {
		t.Fatal("duplicate enemy_gold_lead must be suppressed")
	}

	st3 := mkState(220)
	st3.AllPlayers[1].CurrentGold = 1500 // gap 1000 < 1500 → hysteresis reset
	d.Detect(st3)

	st4 := mkState(230)
	st4.AllPlayers[1].CurrentGold = 3500 // gap 3000 → warn again
	if eventByName(d.Detect(st4), "enemy_gold_lead") == nil {
		t.Fatal("expected re-warning after hysteresis reset")
	}
}

func TestDetect_EnemyFedMilestones(t *testing.T) {
	d := NewDetector()
	d.Detect(mkState(100)) // baseline

	st := mkState(200)
	st.AllPlayers[1].Kills = 4
	ev := eventByName(d.Detect(st), "enemy_fed")
	if ev == nil || ev.Data["milestone"] != 3 {
		t.Fatalf("expected milestone 3, got %+v", ev)
	}

	st2 := mkState(210)
	st2.AllPlayers[1].Kills = 4
	if hasEvent(d.Detect(st2), "enemy_fed") {
		t.Fatal("same milestone must not re-trigger")
	}

	st3 := mkState(220)
	st3.AllPlayers[1].Kills = 6
	ev = eventByName(d.Detect(st3), "enemy_fed")
	if ev == nil || ev.Data["milestone"] != 5 {
		t.Fatalf("expected milestone 5, got %+v", ev)
	}
}

func TestDetect_LaningAndMacroChecks(t *testing.T) {
	d := NewDetector()
	d.Detect(mkState(100)) // baseline

	evs := d.Detect(mkState(200)) // 3:20 → first 3-min bucket
	if eventByName(evs, "laning_check") == nil {
		t.Fatal("expected laning_check at 3:20")
	}

	evs = d.Detect(mkState(380)) // 6:20 → second bucket
	if eventByName(evs, "laning_check") == nil {
		t.Fatal("expected laning_check at 6:20")
	}

	evs = d.Detect(mkState(845)) // 14:05 → macro phase
	if eventByName(evs, "macro_check") == nil {
		t.Fatal("expected macro_check after 14 min")
	}
	if hasEvent(evs, "laning_check") {
		t.Fatal("laning_check must stop after 14 min")
	}
}

func TestDetect_NotInGameResets(t *testing.T) {
	d := NewDetector()
	d.Detect(mkState(100))
	if !d.initialized {
		t.Fatal("expected detector to be initialized")
	}

	if evs := d.Detect(nil); len(evs) != 0 {
		t.Fatalf("nil state must yield no events, got %+v", evs)
	}
	if d.initialized {
		t.Fatal("expected reset after nil state")
	}
	if evs := d.Detect(mkState(0)); len(evs) != 0 {
		t.Fatalf("not-in-game state must yield no events, got %+v", evs)
	}
	if evs := d.Detect(mkState(100)); len(evs) != 0 {
		t.Fatalf("expected baseline silence after reset, got %+v", evs)
	}
}
