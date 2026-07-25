package event

import "github.com/game-coach/collector/internal/lol"

// Active-player events: death, item_purchased/sold/upgraded, kill, gold_spike.

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
