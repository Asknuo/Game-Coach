package event

import "github.com/game-coach/collector/internal/lol"

// Enemy tracking events: enemy_item_purchased/sold, enemy_gold_lead, enemy_fed.

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
