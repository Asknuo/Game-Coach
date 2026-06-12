package event

import "github.com/game-coach/collector/internal/lol"

type Event struct {
	Name string                 `json:"name"`
	Data map[string]interface{} `json:"data"`
}

type Detector struct {
	lastState       *lol.GameState
	dragonWarned    bool
	baronWarned     bool
	lowHealthWarned bool
	lastItemCount   int
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

	hp := state.ActivePlayerHealthPct()
	if hp > 0 && hp < 25 && !d.lowHealthWarned {
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

	for _, p := range state.AllPlayers {
		if p.SummonerName != state.ActivePlayer.SummonerName {
			continue
		}
		count := state.ItemCount(p)
		if d.lastItemCount > 0 && count > d.lastItemCount {
			events = append(events, Event{
				Name: "item_purchased",
				Data: map[string]interface{}{
					"item_count": count,
					"game_time":  state.GameTime,
				},
			})
		}
		d.lastItemCount = count
		break
	}

	if d.lastState != nil {
		events = append(events, d.detectJungle(state)...)
	}

	d.lastState = state
	return events
}

func (d *Detector) detectJungle(state *lol.GameState) []Event {
	// MVP placeholder: emit jungle_check every 3 minutes after 2:00
	if int(state.GameTime)/180 > int(d.lastState.GameTime)/180 && state.GameTime >= 120 {
		return []Event{{
			Name: "jungle_check",
			Data: map[string]interface{}{
				"game_time": state.GameTime,
			},
		}}
	}
	return nil
}

func (d *Detector) reset() {
	d.lastState = nil
	d.dragonWarned = false
	d.baronWarned = false
	d.lowHealthWarned = false
	d.lastItemCount = 0
}
