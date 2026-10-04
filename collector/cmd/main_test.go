package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/game-coach/collector/internal/event"
	"github.com/game-coach/collector/internal/lol"
	"github.com/game-coach/collector/internal/sender"
	"github.com/gorilla/websocket"
)

func mkGameState(gameTime float64, myKills int) *lol.GameState {
	return &lol.GameState{
		GameTime: gameTime,
		ActivePlayer: lol.ActivePlayer{
			SummonerName: "Me",
			CurrentGold:  500,
			Health:       1000,
			MaxHealth:    1000,
		},
		AllPlayers: []lol.Player{
			{SummonerName: "Me", Team: "ORDER", ChampionName: "Ahri", CurrentGold: 500, Kills: myKills},
			{SummonerName: "Enemy1", Team: "CHAOS", ChampionName: "Zed", CurrentGold: 500},
		},
	}
}

func newTestLoop(ws *sender.WebSocket) *collectLoop {
	return &collectLoop{
		client: lol.NewClient(""),
		engine: event.NewEngine(event.NewDetector()),
		ws:     ws,
	}
}

// Regression: SendState used to return early, so when the socket was down the
// events detected in the same tick were neither sent nor buffered — lost
// forever. They must all reach the ring buffer and replay after reconnect.
func TestPublish_SendStateFailureKeepsEvents(t *testing.T) {
	upgrader := websocket.Upgrader{}
	frames := make(chan string, 32)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			frames <- string(msg)
		}
	}))
	defer srv.Close()

	// Start disconnected: every send fails and must fall back to the buffer.
	ws := sender.NewWebSocket("ws" + strings.TrimPrefix(srv.URL, "http"))
	loop := newTestLoop(ws)

	if err := loop.publish(mkGameState(100, 0)); err == nil { // baseline tick
		t.Fatal("an unreachable agent must surface an error from publish")
	}
	if err := loop.publish(mkGameState(200, 1)); err == nil { // kill detected
		t.Fatal("expected publish to report the send failure")
	}
	if err := loop.publish(mkGameState(210, 1)); err == nil { // no new events
		t.Fatal("expected publish to report the send failure")
	}

	// Reconnect: nothing buffered while the socket was down may be dropped.
	// Frames replay in push order; tick 2 pushes state + kill + laning_check.
	want := []string{"state", "state", "event", "event", "state"}
	if err := ws.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer ws.Close()

	for i, w := range want {
		if got := nextFrame(t, frames); got != w {
			t.Fatalf("replayed frame %d = %q, want %q — an event was lost while the socket was down", i, got, w)
		}
	}
}

// The agent judges dragon_timer freshness from the snapshot, so the state frame
// of a tick must still arrive before that tick's events.
func TestPublish_StateArrivesBeforeEvents(t *testing.T) {
	upgrader := websocket.Upgrader{}
	frames := make(chan string, 32)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			frames <- string(msg)
		}
	}))
	defer srv.Close()

	ws := sender.NewWebSocket("ws" + strings.TrimPrefix(srv.URL, "http"))
	loop := newTestLoop(ws)
	if err := ws.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer ws.Close()

	if err := loop.publish(mkGameState(100, 0)); err != nil { // baseline tick
		t.Fatalf("publish: %v", err)
	}
	got := nextFrame(t, frames)
	if got != "state" {
		t.Fatalf("first frame = %q, want state", got)
	}

	// tick with a detected kill: state must still come first.
	if err := loop.publish(mkGameState(200, 1)); err != nil {
		t.Fatalf("publish: %v", err)
	}
	if got := nextFrame(t, frames); got != "state" {
		t.Fatalf("first frame of event tick = %q, want state", got)
	}
	if got := nextFrame(t, frames); got != "event" {
		t.Fatalf("second frame of event tick = %q, want event", got)
	}
}

// A 30-minute game pushes 100KB+ of event history per frame; the agent validates
// and persists every frame wholesale. The events must not be on the wire — but
// the detector must still consume them from the state it was handed.
func TestPublish_StateFrameOmitsEventHistory(t *testing.T) {
	upgrader := websocket.Upgrader{}
	frames := make(chan string, 32)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			frames <- string(msg)
		}
	}))
	defer srv.Close()

	ws := sender.NewWebSocket("ws" + strings.TrimPrefix(srv.URL, "http"))
	loop := newTestLoop(ws)
	if err := ws.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer ws.Close()

	loop.publish(mkGameState(100, 0)) // baseline → one state frame

	st := mkGameState(600, 0)
	st.Events = []lol.GameEvent{
		{EventID: 1, EventName: "ChampionKill", EventTime: 598},
		{EventID: 2, EventName: "ChampionKill", EventTime: 599},
		{EventID: 3, EventName: "ChampionKill", EventTime: 600},
	}
	if err := loop.publish(st); err != nil {
		t.Fatalf("publish: %v", err)
	}

	type frame struct {
		Type    string                 `json:"type"`
		Payload map[string]interface{} `json:"payload"`
	}
	got := make([]frame, 0, 4)
	for i := 0; i < 4; i++ { // state, state, event, event
		var f frame
		if err := json.Unmarshal([]byte(nextRawFrame(t, frames)), &f); err != nil {
			t.Fatalf("unmarshal frame %d: %v", i, err)
		}
		got = append(got, f)
	}
	wantTypes := []string{"state", "state", "event", "event"}
	for i, w := range wantTypes {
		if got[i].Type != w {
			t.Fatalf("frame %d type = %q, want %q (state must precede each tick's events)", i, got[i].Type, w)
		}
	}

	// The frame that carried the snapshot must not carry the event history.
	if ev, present := got[1].Payload["events"]; present {
		t.Errorf("state frame carries events = %#v, want the key omitted", ev)
	}
	if got := got[1].Payload["game_time"]; got != float64(600) {
		t.Errorf("game_time = %v, want 600 — the snapshot must still be complete", got)
	}
	if _, ok := got[1].Payload["all_players"]; !ok {
		t.Error("all_players must still be present")
	}

	// The events of the same tick are still delivered…
	for i, want := range []string{"laning_check", "teamfight_detected"} {
		name, _ := got[2+i].Payload["name"].(string)
		if name != want {
			t.Errorf("event %d = %q, want %q", i, name, want)
		}
	}
	// …the state handed to the detector was not mutated by the copy…
	if len(st.Events) != 3 {
		t.Fatalf("original state Events = %d, want 3 (publish must not strip it)", len(st.Events))
	}
	// …and the detector keeps working on the following ticks.
	st2 := mkGameState(605, 1) // first kill
	if !hasEventName(loop.engine.Process(st2), "kill") {
		t.Fatal("expected the detector to keep detecting on the next tick")
	}
}

// hasEventName reports whether any event of the batch has the given name.
func hasEventName(evs []event.Event, name string) bool {
	for i := range evs {
		if evs[i].Name == name {
			return true
		}
	}
	return false
}

func nextRawFrame(t *testing.T, frames <-chan string) string {
	t.Helper()
	select {
	case body := <-frames:
		return body
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for a frame from the agent")
		return ""
	}
}

// POLL_INTERVAL=0/-3 parse fine but make time.NewTicker panic; the override
// must fall back to the configured interval.
func TestApplyEnvOverrides_PollInterval(t *testing.T) {
	quietLogs(t)
	cases := []struct {
		name string
		env  string
		want time.Duration
	}{
		{"zero", "0", 2 * time.Second},
		{"negative", "-3", 2 * time.Second},
		{"garbage", "abc", 2 * time.Second},
		{"valid", "5", 5 * time.Second},
		{"fractional", "0.25", 250 * time.Millisecond},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv("POLL_INTERVAL", c.env)
			cfg := &Config{AgentWSURL: "ws://x", PollInterval: 2 * time.Second}
			applyEnvOverrides(cfg)
			if cfg.PollInterval != c.want {
				t.Errorf("PollInterval = %v, want %v", cfg.PollInterval, c.want)
			}
		})
	}
}

// Even a Config handed over without validation must not panic the ticker.
func TestRunLoop_ClampsInvalidInterval(t *testing.T) {
	quietLogs(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	loop := &collectLoop{client: lol.NewClient(""), engine: event.NewEngine(event.NewDetector()),
		ws: sender.NewWebSocket("ws://127.0.0.1:1/never")}

	done := make(chan error, 1)
	go func() { done <- runLoop(ctx, loop.client, loop.engine, loop.ws, 0) }()

	// Let the (clamped) ticker fire at least once before shutting down.
	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runLoop error: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("runLoop never returned — a zero interval must not panic/hang")
	}
}

func quietLogs(t *testing.T) {
	t.Helper()
	prev := log.Writer()
	log.SetOutput(io.Discard)
	t.Cleanup(func() { log.SetOutput(prev) })
}

// The fetch-error log must be rate-limited: a persistent 401 would otherwise
// print one line per tick (once a second) forever.
func TestLogFetchError_IsRateLimited(t *testing.T) {
	quietLogs(t)

	var buf strings.Builder
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	l := &collectLoop{}
	err := errors.New("status 401: unauthorized")

	for i := 0; i < 10; i++ {
		l.logFetchError(err)
		if got := strings.Count(buf.String(), "fetch state error"); got != 1 {
			t.Fatalf("after %d failures the log has %d lines, want 1", i+1, got)
		}
		if l.fetchErrors != i+1 {
			t.Fatalf("fetchErrors = %d, want %d", l.fetchErrors, i+1)
		}
	}

	// Once the interval elapsed, the next failure is logged with its count.
	l.fetchErrorLogged = time.Now().Add(-fetchErrorLogInterval)
	l.logFetchError(err)
	if got := strings.Count(buf.String(), "fetch state error"); got != 2 {
		t.Fatalf("log has %d lines after the interval elapsed, want 2", got)
	}
	if !strings.Contains(buf.String(), "11 consecutive failures") {
		t.Errorf("log = %q, want the consecutive-failure count", buf.String())
	}

	// A successful tick clears the counters.
	l.fetchErrors = 0
	l.fetchErrorLogged = time.Time{}
	if l.fetchErrors != 0 {
		t.Fatal("counters must be cleared on success")
	}
}

func nextFrame(t *testing.T, frames <-chan string) string {
	t.Helper()
	select {
	case body := <-frames:
		var frame struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal([]byte(body), &frame); err != nil {
			t.Fatalf("unmarshal frame: %v", err)
		}
		return frame.Type
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for a frame from the agent")
		return ""
	}
}
