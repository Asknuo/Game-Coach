package lcu

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// fakeLCU is a tiny LCU stand-in: it answers the endpoints the poller uses and
// records the events the poller emits.
type fakeLCU struct {
	mu       sync.Mutex
	phase    string
	gameID   int64
	champSel string // raw body for the champ-select session
	calls    []string
}

func (f *fakeLCU) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		path, phase, gameID, champSel := r.URL.Path, f.phase, f.gameID, f.champSel
		f.calls = append(f.calls, path)
		f.mu.Unlock()

		w.Header().Set("Content-Type", "application/json")
		switch path {
		case "/lol-gameflow/v1/session":
			body, _ := json.Marshal(map[string]interface{}{
				"phase":    phase,
				"gameData": map[string]interface{}{"gameId": gameID},
			})
			io.WriteString(w, string(body))
		case "/lol-champ-select/v1/session":
			io.WriteString(w, champSel)
		default:
			io.WriteString(w, `{}`)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func (f *fakeLCU) setPhase(phase string, gameID int64) {
	f.mu.Lock()
	f.phase, f.gameID = phase, gameID
	f.mu.Unlock()
}

// recorder captures poller callbacks.
type recorder struct {
	mu    sync.Mutex
	names []string
	data  map[string][]map[string]interface{}
}

func newRecorder() *recorder {
	return &recorder{data: map[string][]map[string]interface{}{}}
}

func (r *recorder) cb(name string, data map[string]interface{}) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.names = append(r.names, name)
	r.data[name] = append(r.data[name], data)
}

func (r *recorder) count(name string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.data[name])
}

func (r *recorder) last(name string) map[string]interface{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	evts := r.data[name]
	if len(evts) == 0 {
		return nil
	}
	return evts[len(evts)-1]
}

// newFakePoller wires a Client to the fake LCU + recorder, using the lockfile
// discovery path so TryConnect does not enumerate real processes.
func newFakePoller(t *testing.T, f *fakeLCU) (*Poller, *recorder) {
	t.Helper()
	rec := newRecorder()
	srv := f.server(t)

	orig := lockfilePaths
	lockfilePaths = []string{writeLock(t, srvPort(srv))}
	t.Cleanup(func() { lockfilePaths = orig })

	p := NewPoller(NewClient(), rec.cb)
	if !p.client.TryConnect() {
		t.Fatal("expected the fake LCU to accept the connection")
	}
	return p, rec
}

// ── game_end must fire once per game, not once per phase walkthrough ──

// The LCU walks InProgress → EndOfGame → WaitingForStats for the same game; each
// used to emit game_end, so the review ran two or three times.
func TestPoller_GameEndOncePerGame(t *testing.T) {
	f := &fakeLCU{phase: "EndOfGame", gameID: 4242}
	p, rec := newFakePoller(t, f)

	p.pollGameFlow()
	p.pollGameFlow() // same phase again — nothing new
	if got := rec.count("game_end"); got != 1 {
		t.Fatalf("game_end fired %d times for one game, want 1", got)
	}

	// WaitingForStats is the same game: still no second game_end.
	f.setPhase("WaitingForStats", 4242)
	p.pollGameFlow()
	if got := rec.count("game_end"); got != 1 {
		t.Fatalf("game_end fired %d times for one game (both phases), want 1", got)
	}

	// Next game: game_end must fire again.
	f.setPhase("InProgress", 4243)
	p.pollGameFlow()
	if rec.count("lcu_game_start") != 1 {
		t.Fatalf("lcu_game_start fired %d times, want 1", rec.count("lcu_game_start"))
	}
	f.setPhase("EndOfGame", 4243)
	p.pollGameFlow()
	if got := rec.count("game_end"); got != 2 {
		t.Fatalf("game_end fired %d times across two games, want 2", got)
	}
	if id, _ := rec.last("game_end")["game_id"].(int64); id != 4243 {
		t.Errorf("game_id = %v, want 4243", rec.last("game_end")["game_id"])
	}
}

// ── a reconnect must not look like a phase change ──

// Bug: the reconnect path cleared lastPhase/lastCSPhase, so the next poll saw
// phase != "" and replayed lcu_game_start (overwriting rune memory) or
// game_end (a second full review) for a game that never changed phase.
func TestPoller_ReconnectKeepsPhaseTrackers(t *testing.T) {
	f := &fakeLCU{phase: "InProgress", gameID: 777}
	p, rec := newFakePoller(t, f)

	// One normal poll: this first observation legitimately reports the game
	// start we were already in.
	p.pollGameFlow()
	if rec.count("lcu_game_start") != 1 {
		t.Fatalf("lcu_game_start fired %d times on the first poll, want 1", rec.count("lcu_game_start"))
	}
	if rec.count("gameflow_phase_change") != 1 {
		t.Fatalf("phase changes = %d on the first poll, want 1", rec.count("gameflow_phase_change"))
	}

	// The LCU blips (3 consecutive transport errors) and comes back.
	p.client.setConnected(false)
	p.lastReconnect = time.Time{} // allow an immediate retry
	p.tryReconnect()
	if !p.client.Connected() {
		t.Fatal("expected the poller to reconnect")
	}
	if p.lastPhase != "InProgress" {
		t.Fatalf("lastPhase = %q, want it preserved across the reconnect", p.lastPhase)
	}

	// The next poll must be silent: the game never changed phase, so no second
	// start and no phase change may be announced.
	p.pollGameFlow()
	if got := rec.count("lcu_game_start"); got != 1 {
		t.Fatalf("lcu_game_start fired %d times after a reconnect, want still 1", got)
	}
	if got := rec.count("gameflow_phase_change"); got != 1 {
		t.Fatalf("phase changes = %d after a reconnect, want still 1: %+v", got, rec.names)
	}
	if got := rec.count("game_end"); got != 0 {
		t.Fatalf("game_end fired %d times after a reconnect, want 0: %+v", got, rec.names)
	}
}

// The silent sync must pick up a phase that changed while we were disconnected
// without announcing the phase we were in before.
func TestPoller_ReconnectAdoptsCurrentPhaseSilently(t *testing.T) {
	f := &fakeLCU{phase: "ChampSelect", gameID: 0}
	p, rec := newFakePoller(t, f)
	p.pollGameFlow()

	// League ends champion select while the poller is disconnected.
	f.setPhase("InProgress", 900)
	p.client.setConnected(false)
	p.lastReconnect = time.Time{}
	p.tryReconnect()

	if p.lastPhase != "InProgress" {
		t.Fatalf("lastPhase = %q, want the silently-adopted current phase", p.lastPhase)
	}
	if got := rec.count("gameflow_phase_change"); got != 1 {
		t.Fatalf("phase changes = %d, want 1 (only the real one)", got)
	}
	// onGameStart belongs to the *transition*, which the poller did not observe;
	// the next poll sees no change, so no second game start is emitted.
	if got := rec.count("lcu_game_start"); got != 0 {
		t.Fatalf("lcu_game_start = %d, want 0 (the reconnect must not fake one)", got)
	}
}

// ── reconnect backoff must grow, not stay at a flat 30s ──

// reconnectFails counts the failures already seen: 0 → 2s before the first
// retry, then 4s, 8s, 16s … capped at 30s.
func TestPoller_ReconnectBackoffGrows(t *testing.T) {
	p := NewPoller(NewClient(), func(string, map[string]interface{}) {})
	want := []time.Duration{
		2 * time.Second,
		4 * time.Second,
		8 * time.Second,
		16 * time.Second,
		reconnectBackoffMax,
		reconnectBackoffMax,
		reconnectBackoffMax,
	}
	if want[0] != reconnectBackoffBase {
		t.Fatalf("test expects base %v, got %v", reconnectBackoffBase, want[0])
	}
	for i, w := range want {
		p.reconnectFails = i
		if got := p.reconnectBackoff(); got != w {
			t.Errorf("reconnectBackoff() after %d failures = %v, want %v", i, got, w)
		}
	}
}

// A reachable client resets the backoff, so the next outage retries quickly.
func TestPoller_ReconnectResetsBackoff(t *testing.T) {
	p := NewPoller(NewClient(), func(string, map[string]interface{}) {})
	p.reconnectFails = 5
	p.reconnectFails = 0
	if got := p.reconnectBackoff(); got != reconnectBackoffBase {
		t.Fatalf("reconnectBackoff() = %v, want the base after a success", got)
	}
}

// ── the champ-select timer fallback ──

// "timer" missing → the poller must read adjustedPositionInPhase instead of 0.
func TestPoller_ChampSelectTimerFallbackKey(t *testing.T) {
	f := &fakeLCU{
		phase: "ChampSelect",
		champSel: `{
			"localPlayerCellId": 1,
			"adjustedPositionInPhase": 42.5,
			"actions": [[{"actorCellId": 1, "type": "pick", "isInProgress": true}]]
		}`,
	}
	p, rec := newFakePoller(t, f)
	p.lastPhase = "ChampSelect"

	p.pollChampSelect()
	if rec.count("lcu_pick_phase") != 1 {
		t.Fatalf("lcu_pick_phase fired %d times, want 1", rec.count("lcu_pick_phase"))
	}
	if got := rec.last("lcu_pick_phase")["timer"]; got != 42.5 {
		t.Fatalf("timer = %v, want 42.5 (the fallback key must be honoured)", got)
	}
}

// The primary key wins when present, and an oversized ms gameId keeps precision.
func TestPoller_GameIdInt64(t *testing.T) {
	const gameID = int64(1790000000123)
	f := &fakeLCU{phase: "InProgress", gameID: gameID}
	p, rec := newFakePoller(t, f)

	p.pollGameFlow()
	ev := rec.last("gameflow_phase_change")
	if ev == nil {
		t.Fatal("expected a phase change event")
	}
	if got, ok := ev["game_id"].(int64); !ok || got != gameID {
		t.Fatalf("game_id = %#v, want int64 %d", ev["game_id"], gameID)
	}
}
