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
//
// Implementation is split by event family:
//
//	detector_objectives.go — dragon/baron/low_health
//	detector_player.go     — active-player events (death/items/kills/gold)
//	detector_enemy.go      — enemy tracking (items/gold lead/fed)
//	detector_periodic.go   — timer/window checks (laning/macro/teamfight)
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
	lastKillEventID int       // watermark: state.Events is full game history, consume each event once

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
	events = append(events, d.detectObjectives(state)...)

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
	d.lastKillEventID = 0
	d.initialized = false
}
