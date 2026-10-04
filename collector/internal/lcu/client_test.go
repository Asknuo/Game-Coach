package lcu

import (
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
)

// ── keep-alive: response bodies must be drained to reuse the connection ──

type countingListener struct {
	net.Listener
	accepts int32
}

func (l *countingListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err == nil {
		atomic.AddInt32(&l.accepts, 1)
	}
	return c, err
}

// jsonHandler serves a small JSON value followed by ~4KB of trailing whitespace.
// The trailing bytes are what makes the test meaningful: json.Decoder stops
// right after the closing brace, so without draining them the connection cannot
// be reused. (A padded JSON *value* would just make the decoder keep reading.)
func jsonHandler() http.HandlerFunc {
	padding := "\n" + repeat(" ", 4096)
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		body := `[{"pad":"ok"}]`
		if r.URL.Path == "/lol-summoner/v1/current-summoner" || r.URL.Path == "/lol-gameflow/v1/session" {
			body = `{"pad":"ok"}`
		}
		io.WriteString(w, body+padding)
	}
}

// startCountingServer serves padded JSON bodies and counts accepted TCP
// connections (StartTLS wraps the listener, so the counter is returned too).
func startCountingServer(t *testing.T) (*httptest.Server, *countingListener) {
	t.Helper()
	srv := httptest.NewUnstartedServer(jsonHandler())
	cl := &countingListener{Listener: srv.Listener}
	srv.Listener = cl
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv, cl
}

func srvPort(srv *httptest.Server) string {
	return strconv.Itoa(srv.Listener.Addr().(*net.TCPAddr).Port)
}

func writeLock(t *testing.T, port string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "lockfile")
	if err := os.WriteFile(path, []byte("LeagueClient:"+port+":secret:https"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// Regression: json.Decoder stops mid-body, so the connection was closed on
// every request. Champion-select polling hits 2-3 endpoints every 2s — that is
// ~100 TCP+TLS handshakes a minute.
func TestClient_ReusesConnection(t *testing.T) {
	srv, counter := startCountingServer(t)
	c := NewClient()
	if !c.tryLockfileByPath(writeLock(t, srvPort(srv))) {
		t.Fatal("expected a successful connect")
	}

	for i := 0; i < 5; i++ {
		if _, err := c.Get("/lol-summoner/v1/current-summoner"); err != nil {
			t.Fatalf("Get %d: %v", i, err)
		}
	}
	if got := atomic.LoadInt32(&counter.accepts); got != 1 {
		t.Fatalf("TCP connections = %d, want 1 — the body was not drained, keep-alive is broken", got)
	}
}

// GetArray must drain too (mastery payloads are large: 20 champions).
func TestClientGetArray_ReusesConnection(t *testing.T) {
	srv, counter := startCountingServer(t)
	c := NewClient()
	if !c.tryLockfileByPath(writeLock(t, srvPort(srv))) {
		t.Fatal("expected a successful connect")
	}
	for i := 0; i < 4; i++ {
		if _, err := c.GetArray("/lol-champion-mastery/v1/local-player/champion-mastery"); err != nil {
			t.Fatalf("GetArray %d: %v", i, err)
		}
	}
	if got := atomic.LoadInt32(&counter.accepts); got != 1 {
		t.Fatalf("TCP connections = %d, want 1 — the body was not drained, keep-alive is broken", got)
	}
}

// ── one transport blip must not disconnect the LCU ──

// Regression: a single 5s timeout flipped connected=false while the reconnect
// path was throttled by 30s, so one blip cost 30s of LCU data.
func TestClient_TransportFailuresNeedThreeInARow(t *testing.T) {
	srv, _ := startCountingServer(t)
	c := NewClient()
	if !c.tryLockfileByPath(writeLock(t, srvPort(srv))) {
		t.Fatal("expected a successful connect")
	}
	if !c.Connected() {
		t.Fatal("client must start connected")
	}

	srv.Close() // the League client vanishes

	for i := 1; i <= maxTransportFailures; i++ {
		if _, err := c.Get("/lol-gameflow/v1/session"); err == nil {
			t.Fatalf("failure %d: expected a transport error", i)
		}
		wantConnected := i < maxTransportFailures
		if c.Connected() != wantConnected {
			t.Fatalf("after %d consecutive failures Connected() = %v, want %v",
				i, c.Connected(), wantConnected)
		}
	}
}

// Any completed request (even a 404 during loading) proves the transport is
// alive, so the failure streak must be cleared by it.
func TestClient_SuccessClearsFailureStreak(t *testing.T) {
	fail := make(chan struct{})
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-fail:
			http.Error(w, "gone", 599) // status error, transport fine
		default:
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"pad":"`+repeat("z", 2048)+`"}`)
	}))
	defer srv.Close()

	c := NewClient()
	if !c.tryLockfileByPath(writeLock(t, srvPort(srv))) {
		t.Fatal("expected a successful connect")
	}
	close(fail) // from now on the server answers with a 599

	for i := 0; i < maxTransportFailures-1; i++ {
		if _, err := c.Get("/x"); err == nil {
			t.Fatal("expected a status error")
		}
	}
	if !c.Connected() {
		t.Fatal("status errors must not disconnect the client")
	}
	c.mu.Lock()
	streak := c.failStreak
	c.mu.Unlock()
	if streak != 0 {
		t.Fatalf("failStreak = %d, want 0 (a completed request clears it)", streak)
	}
}

// ── JSON helpers ──

// Regression: floatVal walked its keys as a nested path, so when "timer" was
// missing the "adjustedPositionInPhase" fallback never applied.
func TestFloatValOr_FallbackKeys(t *testing.T) {
	cases := []struct {
		name string
		in   map[string]interface{}
		want float64
	}{
		{"primary key", map[string]interface{}{"timer": 12.5, "adjustedPositionInPhase": 99.0}, 12.5},
		{"fallback key", map[string]interface{}{"adjustedPositionInPhase": 42.0}, 42.0},
		{"neither present", map[string]interface{}{"other": 1.0}, 0},
		{"primary wrong type", map[string]interface{}{"timer": "soon", "adjustedPositionInPhase": 7.0}, 7.0},
		{"nested path is not a path", map[string]interface{}{"timer": map[string]interface{}{"adjustedPositionInPhase": 3.0}}, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := floatValOr(c.in, "timer", "adjustedPositionInPhase"); got != c.want {
				t.Errorf("floatValOr = %v, want %v", got, c.want)
			}
		})
	}
}

// Regression: gameId / lastPlayTime are ms-precision epoch values; routing them
// through float64→int overflows on 32-bit builds.
func TestInt64Val_KeepsMillisecondPrecision(t *testing.T) {
	const ms = int64(1790000000123) // 2026 in ms
	m := map[string]interface{}{"gameId": float64(ms), "lastPlayTime": float64(ms)}
	if got := int64Val(m, "gameId"); got != ms {
		t.Errorf("gameId = %d, want %d", got, ms)
	}
	if got := int64Val(m, "lastPlayTime"); got != ms {
		t.Errorf("lastPlayTime = %d, want %d", got, ms)
	}
	// int and int64 payloads (already decoded elsewhere) must also work.
	if got := int64Val(map[string]interface{}{"k": int64(ms)}, "k"); got != ms {
		t.Errorf("int64 payload = %d, want %d", got, ms)
	}
	if got := int64Val(map[string]interface{}{"k": 7}, "k"); got != 7 {
		t.Errorf("int payload = %d, want 7", got)
	}
	if got := int64Val(map[string]interface{}{}, "missing"); got != 0 {
		t.Errorf("missing key = %d, want 0", got)
	}
}

func repeat(s string, n int) string {
	out := make([]byte, 0, len(s)*n)
	for i := 0; i < n; i++ {
		out = append(out, s...)
	}
	return string(out)
}
