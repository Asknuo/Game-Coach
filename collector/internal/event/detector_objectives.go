package event

import "github.com/game-coach/collector/internal/lol"

// detectObjectives emits dragon_soon / baron_soon / low_health events.
func (d *Detector) detectObjectives(state *lol.GameState) []Event {
	var events []Event

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

	return events
}
