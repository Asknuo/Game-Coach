package lol

import "strings"

const (
	firstDragonSpawn       = 300.0 // 5:00
	elementalDragonRespawn = 300.0 // 5 min after kill
	elderDragonRespawn     = 360.0 // 6 min after elder kill
	firstBaronSpawn        = 1200.0 // 20:00
	baronRespawn           = 360.0 // 6 min after kill
	objectiveWarnWindow    = 60.0
)

// ObjectiveTracker derives dragon/baron spawn timers from Live Client events.
type ObjectiveTracker struct {
	lastEventID     int
	nextDragonSpawn float64
	nextDragonType  string
	dragonScheduled bool
	nextBaronSpawn  float64
	baronScheduled  bool
}

func NewObjectiveTracker() *ObjectiveTracker {
	return &ObjectiveTracker{}
}

func (t *ObjectiveTracker) Reset() {
	t.lastEventID = 0
	t.nextDragonSpawn = 0
	t.nextDragonType = ""
	t.dragonScheduled = false
	t.nextBaronSpawn = 0
	t.baronScheduled = false
}

// Enrich fills DragonTimer and BaronTimer on state from tracked kill events.
func (t *ObjectiveTracker) Enrich(state *GameState) {
	if state == nil || state.GameTime <= 0 {
		t.Reset()
		return
	}

	t.syncEvents(state.Events)
	t.applyDefaultSpawns(state.GameTime)

	if t.dragonScheduled && t.nextDragonSpawn > state.GameTime {
		left := t.nextDragonSpawn - state.GameTime
		if left <= objectiveWarnWindow {
			state.DragonTimer = &DragonInfo{
				Type:        t.nextDragonType,
				SpawnTime:   t.nextDragonSpawn,
				SecondsLeft: left,
			}
		}
	}

	if t.baronScheduled && t.nextBaronSpawn > state.GameTime {
		left := t.nextBaronSpawn - state.GameTime
		if left <= objectiveWarnWindow {
			state.BaronTimer = &BaronInfo{
				SpawnTime:   t.nextBaronSpawn,
				SecondsLeft: left,
			}
		}
	}
}

func (t *ObjectiveTracker) syncEvents(events []GameEvent) {
	for _, ev := range events {
		if ev.EventID <= t.lastEventID {
			continue
		}
		t.applyEvent(ev)
		if ev.EventID > t.lastEventID {
			t.lastEventID = ev.EventID
		}
	}
}

func (t *ObjectiveTracker) applyEvent(ev GameEvent) {
	switch ev.EventName {
	case "GameStart":
		t.Reset()
	case "DragonKill":
		respawn := elementalDragonRespawn
		if strings.EqualFold(ev.DragonType, "Elder") {
			respawn = elderDragonRespawn
		}
		t.nextDragonSpawn = ev.EventTime + respawn
		t.nextDragonType = ev.DragonType
		if t.nextDragonType == "" {
			t.nextDragonType = "unknown"
		}
		t.dragonScheduled = true
	case "BaronKill":
		t.nextBaronSpawn = ev.EventTime + baronRespawn
		t.baronScheduled = true
	}
}

func (t *ObjectiveTracker) applyDefaultSpawns(gameTime float64) {
	if !t.dragonScheduled && gameTime < firstDragonSpawn {
		t.nextDragonSpawn = firstDragonSpawn
		t.nextDragonType = "unknown"
		t.dragonScheduled = true
	}

	if !t.baronScheduled && gameTime < firstBaronSpawn {
		t.nextBaronSpawn = firstBaronSpawn
		t.baronScheduled = true
	}
}
