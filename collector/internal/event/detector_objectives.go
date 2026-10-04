package event

import "github.com/game-coach/collector/internal/lol"

// noSpawn is the sentinel stored in lastDragonSpawn/lastBaronSpawn when no
// spawn has been warned yet. Spawn timestamps are always > 0, so any real
// SpawnTime differs from it.
const noSpawn = -1.0

// dragonSoon reports whether the current frame should emit dragon_soon.
// The warning is keyed on the *spawn identity* (SpawnTime), not a boolean
// latch: a 20-minute game has 2-4 dragons and each one must warn again.
func (d *Detector) dragonSoon(state *lol.GameState) bool {
	timer := state.DragonTimer
	if timer == nil {
		// No dragon pending (spawned/killed or game ended) — forget the last
		// spawn so the next dragon is treated as a new one.
		d.lastDragonSpawn = noSpawn
		return false
	}
	if timer.SecondsLeft <= 30 && timer.SpawnTime != d.lastDragonSpawn {
		d.lastDragonSpawn = timer.SpawnTime
		return true
	}
	return false
}

// baronSoon is dragonSoon for Baron.
func (d *Detector) baronSoon(state *lol.GameState) bool {
	timer := state.BaronTimer
	if timer == nil {
		d.lastBaronSpawn = noSpawn
		return false
	}
	if timer.SecondsLeft <= 30 && timer.SpawnTime != d.lastBaronSpawn {
		d.lastBaronSpawn = timer.SpawnTime
		return true
	}
	return false
}

// detectObjectives emits dragon_soon / baron_soon / low_health events.
func (d *Detector) detectObjectives(state *lol.GameState) []Event {
	var events []Event

	// dragon_soon — one warning per dragon spawn, every spawn of the game
	if d.dragonSoon(state) {
		events = append(events, Event{
			Name: "dragon_soon",
			Data: map[string]interface{}{
				"seconds_left": state.DragonTimer.SecondsLeft,
				"game_time":    state.GameTime,
			},
		})
	}

	// baron_soon — one warning per baron spawn, every spawn of the game
	if d.baronSoon(state) {
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

	return events
}
