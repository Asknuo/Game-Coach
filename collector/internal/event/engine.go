package event

import (
	"sync"
	"time"

	"github.com/game-coach/collector/internal/lol"
)

type Engine struct {
	detector *Detector
	mu       sync.Mutex
	cooldown map[string]time.Time
}

func NewEngine(detector *Detector) *Engine {
	return &Engine{
		detector: detector,
		cooldown: make(map[string]time.Time),
	}
}

var cooldownDurations = map[string]time.Duration{
	"dragon_soon":     60 * time.Second,
	"baron_soon":      60 * time.Second,
	"low_health":      45 * time.Second,
	"item_purchased":  30 * time.Second,
	"jungle_check":    120 * time.Second,
	"strategy_check":  300 * time.Second,
}

func (e *Engine) Process(state *lol.GameState) []Event {
	raw := e.detector.Detect(state)

	e.mu.Lock()
	defer e.mu.Unlock()

	var out []Event
	now := time.Now()

	for _, ev := range raw {
		cd := cooldownDurations[ev.Name]
		if cd == 0 {
			cd = 30 * time.Second
		}
		if last, ok := e.cooldown[ev.Name]; ok && now.Sub(last) < cd {
			continue
		}
		e.cooldown[ev.Name] = now
		out = append(out, ev)
	}

	return out
}
