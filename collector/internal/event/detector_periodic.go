package event

import "github.com/game-coach/collector/internal/lol"

// Periodic / window-based checks: laning_check, macro_check, teamfight_detected.

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
