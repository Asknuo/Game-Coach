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
	"dragon_soon":          60 * time.Second,
	"baron_soon":           60 * time.Second,
	"low_health":           45 * time.Second,
	"item_purchased":       30 * time.Second,
	"item_sold":            30 * time.Second,
	"item_upgraded":        30 * time.Second,
	"gold_spike":           60 * time.Second,
	"enemy_item_purchased": 30 * time.Second,
	"enemy_item_sold":      30 * time.Second,
	"enemy_gold_lead":      120 * time.Second,
	"enemy_fed":            90 * time.Second,
	"laning_check":         120 * time.Second,
	"macro_check":          300 * time.Second,
	"teamfight_detected":   90 * time.Second,
	"game_end":             0, // no cooldown — one-shot event
	// kill / death: no cooldown — detector 基线已保证每杀/每死只报一次，
	// 引擎层再加冷却会吞掉双杀等连续事件
}

// subjectDataKeys 列出事件 Data 中标识"事件主体"的字段。
// 冷却键按 name+subject 区分，否则敌方 A 的买装会在冷却窗口内吞掉敌方 B 的买装。
var subjectDataKeys = []string{"enemy_name"}

func cooldownKey(ev Event) string {
	for _, k := range subjectDataKeys {
		if v, ok := ev.Data[k].(string); ok && v != "" {
			return ev.Name + "|" + v
		}
	}
	return ev.Name
}

// Process runs the detector and applies cross-tick cooldown deduplication.
// Two-pass design: all events in the same tick batch pass through together,
// then cooldown is applied — preventing same-tick events from blocking each other.
func (e *Engine) Process(state *lol.GameState) []Event {
	raw := e.detector.Detect(state)

	e.mu.Lock()
	defer e.mu.Unlock()

	now := time.Now()

	// Pass 1: collect events that pass cross-tick cooldown
	var out []Event
	for _, ev := range raw {
		key := cooldownKey(ev)
		cd, ok := cooldownDurations[ev.Name]
		if !ok {
			// No cooldown configured → allow through always
			out = append(out, ev)
			continue
		}
		if last, exists := e.cooldown[key]; exists && now.Sub(last) < cd {
			continue
		}
		out = append(out, ev)
	}

	// Pass 2: apply cooldown for all accepted events (after collecting, so same-name
	// events in the same batch don't block each other)
	for _, ev := range out {
		if _, hasCD := cooldownDurations[ev.Name]; hasCD {
			e.cooldown[cooldownKey(ev)] = now
		}
	}

	return out
}

// Reset resets the detector state (e.g. on fetch failure to avoid stale-state jump).
func (e *Engine) Reset() {
	e.detector.reset()
	e.mu.Lock()
	e.cooldown = make(map[string]time.Time)
	e.mu.Unlock()
}
