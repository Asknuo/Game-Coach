package event

import "github.com/game-coach/collector/internal/lol"

type Event struct {
	Name string                 `json:"name"`
	Data map[string]interface{} `json:"data"`
}

// Detector compares consecutive GameState snapshots to detect events.
//
// Coverage:
//
//	Go-native:        low_health, dragon_soon, baron_soon, jungle_check, strategy_check
//	Migrated from Py: death, kill, item_purchased, item_sold, item_upgraded,
//	                  gold_spike, enemy_item_purchased, enemy_item_sold
//	Enemy tracking:   enemy_gold_lead, enemy_fed
type Detector struct {
	lastState       *lol.GameState
	dragonWarned    bool
	baronWarned     bool
	lowHealthWarned bool

	// -- migrated from Python EventDetector --
	lastDeaths      int
	lastKills       map[string]int // summonerName → kills
	lastActiveItems map[int]int    // slot → itemID
	lastGold        float64
	enemyItems      map[string]map[int]int // enemyName → slot → itemID

	// -- enemy threat tracking --
	enemyGoldWarned    map[string]bool // enemyName → already warned for gold lead
	enemyFedMilestones map[string]int  // enemyName → last kill milestone warned

	// -- teamfight detection --
	recentKillTimes []float64 // ChampionKill event times in the current window

	initialized bool // first tick after reset: record snapshots, skip events
}

func NewDetector() *Detector {
	return &Detector{}
}

func (d *Detector) Detect(state *lol.GameState) []Event {
	if state == nil || !state.IsInGame() {
		d.reset()
		return nil
	}

	var events []Event

	// ── Go-native events ──

	// dragon_soon
	if state.DragonTimer != nil && state.DragonTimer.SecondsLeft <= 30 && !d.dragonWarned {
		d.dragonWarned = true
		events = append(events, Event{
			Name: "dragon_soon",
			Data: map[string]interface{}{
				"seconds_left": state.DragonTimer.SecondsLeft,
				"game_time":    state.GameTime,
			},
		})
	}

	// baron_soon
	if state.BaronTimer != nil && state.BaronTimer.SecondsLeft <= 30 && !d.baronWarned {
		d.baronWarned = true
		events = append(events, Event{
			Name: "baron_soon",
			Data: map[string]interface{}{
				"seconds_left": state.BaronTimer.SecondsLeft,
				"game_time":    state.GameTime,
			},
		})
	}

	// low_health
	hp := state.ActivePlayerHealthPct()
	if hp > 0 && hp < 30 && !d.lowHealthWarned {
		d.lowHealthWarned = true
		events = append(events, Event{
			Name: "low_health",
			Data: map[string]interface{}{
				"health_pct": hp,
				"game_time":  state.GameTime,
			},
		})
	}
	if hp >= 40 {
		d.lowHealthWarned = false
	}

	// ── Migrated from Python: active-player events ──

	ap := state.ActivePlayerFromAll()
	if ap != nil && d.initialized {
		events = append(events, d.detectDeath(ap, state.GameTime)...)
		events = append(events, d.detectMyItems(ap, state.GameTime)...)
		events = append(events, d.detectKills(ap, state)...)
	}

	if d.initialized {
		events = append(events, d.detectGoldSpike(state)...)
		events = append(events, d.detectEnemyItems(state)...)
		events = append(events, d.detectEnemyGoldLead(state)...)
		events = append(events, d.detectEnemyFed(state)...)
	} else {
		d.firstTickInit(state)
	}

	// ── Periodic checks (need lastState) ──

	if d.lastState != nil {
		events = append(events, d.detectLaning(state)...)
		events = append(events, d.detectMacro(state)...)
	}

	// ── Teamfight detection ──
	events = append(events, d.detectTeamfight(state)...)

	d.lastState = state
	return events
}

// detectDeath emits a "death" event when the active player's death count increases.
func (d *Detector) detectDeath(ap *lol.Player, gameTime float64) []Event {
	if ap.Deaths > d.lastDeaths {
		diff := ap.Deaths - d.lastDeaths
		evts := make([]Event, 0, diff)
		for i := 0; i < diff; i++ {
			evts = append(evts, Event{
				Name: "death",
				Data: map[string]interface{}{
					"total_deaths": ap.Deaths,
					"game_time":    gameTime,
				},
			})
		}
		d.lastDeaths = ap.Deaths
		return evts
	}
	d.lastDeaths = ap.Deaths
	return nil
}

// detectMyItems detects item_purchased, item_sold, and item_upgraded events
// by comparing the current item snapshot against the previous one.
func (d *Detector) detectMyItems(ap *lol.Player, gameTime float64) []Event {
	var events []Event

	// Build current snapshot: slot → itemID
	currentBySlot := make(map[int]int)
	currentIDs := make(map[int]bool)
	for _, it := range ap.Items {
		if it.ItemID != 0 {
			currentBySlot[it.Slot] = it.ItemID
			currentIDs[it.ItemID] = true
		}
	}

	prevBySlot := d.lastActiveItems
	if prevBySlot == nil {
		prevBySlot = make(map[int]int)
	}

	// Find pure additions (in current but not in previous)
	var newIDs []int
	for id := range currentIDs {
		found := false
		for _, pid := range prevBySlot {
			if pid == id {
				found = true
				break
			}
		}
		if !found {
			newIDs = append(newIDs, id)
		}
	}

	// Find pure removals (in previous but not in current)
	var removedIDs []int
	for _, pid := range prevBySlot {
		if !currentIDs[pid] {
			removedIDs = append(removedIDs, pid)
		}
	}

	// Find upgrades: same slot, different non-zero IDs at both times
	type upgrade struct {
		slot, oldID, newID int
	}
	var upgrades []upgrade
	for slot, newID := range currentBySlot {
		oldID := prevBySlot[slot]
		if oldID != 0 && oldID != newID && newID != 0 {
			upgrades = append(upgrades, upgrade{slot, oldID, newID})
		}
	}

	// Build set of IDs involved in upgrades for filtering
	upgradedNews := make(map[int]bool)
	upgradedOlds := make(map[int]bool)
	for _, u := range upgrades {
		upgradedNews[u.newID] = true
		upgradedOlds[u.oldID] = true
	}

	// Pure purchases (new items that are NOT the result of an upgrade)
	for _, id := range newIDs {
		if upgradedNews[id] {
			continue
		}
		events = append(events, Event{
			Name: "item_purchased",
			Data: map[string]interface{}{
				"item_id":   id,
				"action":    "purchased",
				"game_time": gameTime,
			},
		})
	}

	// Pure sales (removed items that are NOT consumed by an upgrade)
	for _, id := range removedIDs {
		if upgradedOlds[id] {
			continue
		}
		events = append(events, Event{
			Name: "item_sold",
			Data: map[string]interface{}{
				"item_id":   id,
				"action":    "sold_or_consumed",
				"game_time": gameTime,
			},
		})
	}

	// Upgrades
	for _, u := range upgrades {
		events = append(events, Event{
			Name: "item_upgraded",
			Data: map[string]interface{}{
				"slot":        u.slot,
				"old_item_id": u.oldID,
				"new_item_id": u.newID,
				"action":      "upgraded",
				"game_time":   gameTime,
			},
		})
	}

	d.lastActiveItems = currentBySlot
	return events
}

// detectKills emits a "kill" event when the active player's kill count increases.
func (d *Detector) detectKills(ap *lol.Player, state *lol.GameState) []Event {
	var events []Event

	if d.lastKills == nil {
		d.lastKills = make(map[string]int)
	}

	for _, p := range state.AllPlayers {
		prev := d.lastKills[p.SummonerName]
		if p.Kills > prev && p.SummonerName == ap.SummonerName {
			events = append(events, Event{
				Name: "kill",
				Data: map[string]interface{}{
					"killer":      p.SummonerName,
					"total_kills": p.Kills,
					"game_time":   state.GameTime,
				},
			})
		}
		d.lastKills[p.SummonerName] = p.Kills
	}

	return events
}

// detectGoldSpike emits a "gold_spike" event when gold increases by >500.
func (d *Detector) detectGoldSpike(state *lol.GameState) []Event {
	gold := state.ActivePlayer.CurrentGold
	if gold > 0 {
		delta := gold - d.lastGold
		if delta > 500 {
			d.lastGold = gold
			return []Event{{
				Name: "gold_spike",
				Data: map[string]interface{}{
					"current_gold": gold,
					"delta":        delta,
					"game_time":    state.GameTime,
				},
			}}
		}
	}
	d.lastGold = gold // always sync, prevents permanent failure after gold hits 0
	return nil
}

// detectEnemyItems detects enemy_item_purchased and enemy_item_sold events.
func (d *Detector) detectEnemyItems(state *lol.GameState) []Event {
	var events []Event

	activeTeam := state.ActivePlayerTeam()
	if activeTeam == "" {
		return nil
	}

	if d.enemyItems == nil {
		d.enemyItems = make(map[string]map[int]int)
	}

	for _, p := range state.AllPlayers {
		if p.Team == activeTeam {
			continue // skip allies
		}
		if p.SummonerName == "" {
			continue
		}

		// Current enemy items: slot → itemID
		current := make(map[int]int)
		for _, it := range p.Items {
			if it.ItemID != 0 {
				current[it.Slot] = it.ItemID
			}
		}

		prev := d.enemyItems[p.SummonerName]
		if prev == nil {
			prev = make(map[int]int)
		}

		// Build ID sets
		currentIDs := make(map[int]bool)
		for _, id := range current {
			currentIDs[id] = true
		}
		var prevIDs []int
		for _, id := range prev {
			prevIDs = append(prevIDs, id)
		}

		// Find added / removed
		var added []int
		for id := range currentIDs {
			found := false
			for _, pid := range prevIDs {
				if pid == id {
					found = true
					break
				}
			}
			if !found {
				added = append(added, id)
			}
		}

		var removed []int
		for _, pid := range prevIDs {
			if !currentIDs[pid] {
				removed = append(removed, pid)
			}
		}

		if len(added) > 0 {
			events = append(events, Event{
				Name: "enemy_item_purchased",
				Data: map[string]interface{}{
					"enemy_name":     p.SummonerName,
					"enemy_champion": p.ChampionName,
					"item_ids":       added,
					"game_time":      state.GameTime,
				},
			})
		}
		if len(removed) > 0 {
			events = append(events, Event{
				Name: "enemy_item_sold",
				Data: map[string]interface{}{
					"enemy_name": p.SummonerName,
					"item_ids":   removed,
					"game_time":  state.GameTime,
				},
			})
		}

		d.enemyItems[p.SummonerName] = current
	}

	return events
}

// detectEnemyGoldLead emits "enemy_gold_lead" when any enemy's gold exceeds ours by 2000+.
func (d *Detector) detectEnemyGoldLead(state *lol.GameState) []Event {
	var events []Event

	myGold := state.ActivePlayer.CurrentGold
	if myGold <= 0 {
		return nil
	}

	activeTeam := state.ActivePlayerTeam()
	if activeTeam == "" {
		return nil
	}

	if d.enemyGoldWarned == nil {
		d.enemyGoldWarned = make(map[string]bool)
	}

	for _, p := range state.AllPlayers {
		if p.Team == activeTeam {
			continue
		}
		if p.SummonerName == "" {
			continue
		}

		gap := p.CurrentGold - myGold
		if gap > 2000 && !d.enemyGoldWarned[p.SummonerName] {
			d.enemyGoldWarned[p.SummonerName] = true
			events = append(events, Event{
				Name: "enemy_gold_lead",
				Data: map[string]interface{}{
					"enemy_name":     p.SummonerName,
					"enemy_champion": p.ChampionName,
					"enemy_gold":     p.CurrentGold,
					"my_gold":        myGold,
					"gold_gap":       gap,
					"enemy_kills":    p.Kills,
					"game_time":      state.GameTime,
				},
			})
		}

		// Reset warning if lead drops below 1500 (hysteresis)
		if gap < 1500 {
			d.enemyGoldWarned[p.SummonerName] = false
		}
	}

	return events
}

// detectEnemyFed emits "enemy_fed" when an enemy reaches a kill milestone (3/5/7/10).
func (d *Detector) detectEnemyFed(state *lol.GameState) []Event {
	var events []Event

	activeTeam := state.ActivePlayerTeam()
	if activeTeam == "" {
		return nil
	}

	if d.enemyFedMilestones == nil {
		d.enemyFedMilestones = make(map[string]int)
	}

	milestones := []int{3, 5, 7, 10}

	for _, p := range state.AllPlayers {
		if p.Team == activeTeam {
			continue
		}
		if p.SummonerName == "" {
			continue
		}

		lastMilestone := d.enemyFedMilestones[p.SummonerName]
		for _, m := range milestones {
			if p.Kills >= m && m > lastMilestone {
				d.enemyFedMilestones[p.SummonerName] = m
				events = append(events, Event{
					Name: "enemy_fed",
					Data: map[string]interface{}{
						"enemy_name":     p.SummonerName,
						"enemy_champion": p.ChampionName,
						"kills":          p.Kills,
						"deaths":         p.Deaths,
						"assists":        p.Assists,
						"current_gold":   p.CurrentGold,
						"creep_score":    p.CreepScore,
						"milestone":      m,
						"game_time":      state.GameTime,
					},
				})
				break // only one milestone per tick
			}
		}
	}

	return events
}

// ── Periodic checks ──

// detectLaning emits "laning_check" every 3 minutes during the laning phase (game_time < 14 min).
func (d *Detector) detectLaning(state *lol.GameState) []Event {
	if state.GameTime >= 14*60 || state.GameTime < 120 {
		return nil
	}
	if int(state.GameTime)/180 > int(d.lastState.GameTime)/180 {
		return []Event{{
			Name: "laning_check",
			Data: map[string]interface{}{
				"game_time": state.GameTime,
			},
		}}
	}
	return nil
}

// detectMacro emits "macro_check" every 5 minutes during mid/late game (game_time >= 14 min).
func (d *Detector) detectMacro(state *lol.GameState) []Event {
	if state.GameTime < 14*60 || state.GameTime < 180 {
		return nil
	}
	if int(state.GameTime)/300 > int(d.lastState.GameTime)/300 {
		return []Event{{
			Name: "macro_check",
			Data: map[string]interface{}{
				"game_time": state.GameTime,
			},
		}}
	}
	return nil
}

// detectTeamfight emits "teamfight_detected" when 3+ ChampionKill events
// occur within a 15-second window (game time).
func (d *Detector) detectTeamfight(state *lol.GameState) []Event {
	// Prune old kill times outside the 15s window.
	cutoff := state.GameTime - 15
	kept := d.recentKillTimes[:0]
	for _, t := range d.recentKillTimes {
		if t >= cutoff {
			kept = append(kept, t)
		}
	}
	d.recentKillTimes = kept

	// Collect new ChampionKill events from this tick.
	for _, ev := range state.Events {
		if ev.EventName == "ChampionKill" {
			d.recentKillTimes = append(d.recentKillTimes, ev.EventTime)
		}
	}

	// 3+ kills in 15s window → teamfight.
	if len(d.recentKillTimes) >= 3 {
		d.recentKillTimes = nil // reset to avoid immediate re-trigger
		return []Event{{
			Name: "teamfight_detected",
			Data: map[string]interface{}{
				"game_time": state.GameTime,
			},
		}}
	}
	return nil
}

// firstTickInit records current snapshots without emitting events,
// preventing false item_purchased/kill/death events on collector startup or reconnection.
func (d *Detector) firstTickInit(state *lol.GameState) {
	ap := state.ActivePlayerFromAll()
	if ap != nil {
		d.lastDeaths = ap.Deaths

		currentBySlot := make(map[int]int)
		for _, it := range ap.Items {
			if it.ItemID != 0 {
				currentBySlot[it.Slot] = it.ItemID
			}
		}
		d.lastActiveItems = currentBySlot
	}

	d.lastGold = state.ActivePlayer.CurrentGold

	d.lastKills = make(map[string]int)
	for _, p := range state.AllPlayers {
		d.lastKills[p.SummonerName] = p.Kills
	}

	activeTeam := state.ActivePlayerTeam()
	d.enemyItems = make(map[string]map[int]int)
	for _, p := range state.AllPlayers {
		if p.Team == activeTeam {
			continue
		}
		current := make(map[int]int)
		for _, it := range p.Items {
			if it.ItemID != 0 {
				current[it.Slot] = it.ItemID
			}
		}
		d.enemyItems[p.SummonerName] = current
	}

	d.enemyGoldWarned = make(map[string]bool)
	d.enemyFedMilestones = make(map[string]int)

	d.initialized = true
}

func (d *Detector) reset() {
	d.lastState = nil
	d.dragonWarned = false
	d.baronWarned = false
	d.lowHealthWarned = false
	d.lastActiveItems = nil
	d.lastDeaths = 0
	d.lastKills = nil
	d.lastGold = 0
	d.enemyItems = nil
	d.enemyGoldWarned = nil
	d.enemyFedMilestones = nil
	d.recentKillTimes = nil
	d.initialized = false
}
