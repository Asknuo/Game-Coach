package lol

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func serverPort(t *testing.T, srv *httptest.Server) string {
	t.Helper()
	return strconv.Itoa(srv.Listener.Addr().(*net.TCPAddr).Port)
}

func writeLockfile(t *testing.T, path, password string) {
	t.Helper()
	content := "LeagueClient:2999:" + password + ":https"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func basicAuthHeader(password string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte("riot:"+password))
}

// Regression: the Live Client password goes stale whenever the LOL client
// restarts (it rewrites the lockfile). Without dropping the cached password on
// 401/403 the collector 401-ed on every tick until restarted.
func TestGet_UnauthorizedInvalidatesCredentials(t *testing.T) {
	const stalePass, freshPass = "old-pass", "new-pass"
	var seenAuth atomic.Value

	handler := func(w http.ResponseWriter, r *http.Request) {
		seenAuth.Store(r.Header.Get("Authorization"))
		if r.Header.Get("Authorization") != basicAuthHeader(freshPass) {
			w.WriteHeader(http.StatusUnauthorized)
			io.WriteString(w, "bad token")
			return
		}
		w.Write([]byte(`{"gameData":{"gameTime":10}}`))
	}
	srv := httptest.NewTLSServer(http.HandlerFunc(handler))
	defer srv.Close()

	lock := filepath.Join(t.TempDir(), "lockfile")
	writeLockfile(t, lock, stalePass)

	c := NewClient(lock)
	c.apiPort = serverPort(t, srv)
	if err := c.RefreshCredentials(); err != nil {
		t.Fatalf("RefreshCredentials: %v", err)
	}
	if !c.HasCredentials() {
		t.Fatal("expected credentials after refresh")
	}

	if _, err := c.get(context.Background(), "/liveclientdata/allgamedata"); err == nil {
		t.Fatal("expected an error for the stale password, got nil")
	}
	if c.HasCredentials() {
		t.Fatal("401 must drop cached credentials")
	}
	if pw := c.currentPassword(); pw != "" {
		t.Fatalf("password = %q, want empty after 401", pw)
	}
	// get() must not even attempt a request without credentials.
	if _, err := c.get(context.Background(), "/liveclientdata/allgamedata"); err == nil {
		t.Fatal("expected 'no credentials' error once invalidated")
	}

	// The LOL client restarted and wrote a new password to the lockfile —
	// the next refresh must pick it up and the request must succeed.
	writeLockfile(t, lock, freshPass)
	if err := c.RefreshCredentials(); err != nil {
		t.Fatalf("RefreshCredentials after lockfile rewrite: %v", err)
	}
	if !c.HasCredentials() {
		t.Fatal("expected credentials to be refreshed from the new lockfile")
	}
	if pw := c.currentPassword(); pw != freshPass {
		t.Fatalf("password = %q, want %q", pw, freshPass)
	}
	raw, err := c.get(context.Background(), "/liveclientdata/allgamedata")
	if err != nil {
		t.Fatalf("get after refresh: %v", err)
	}
	if len(raw) == 0 {
		t.Fatal("expected a response body")
	}
	if auth, _ := seenAuth.Load().(string); auth != basicAuthHeader(freshPass) {
		t.Errorf("Authorization = %q, want the refreshed token", auth)
	}
}

// 403 is the same failure mode as 401 (some proxies/LCU variants answer 403).
func TestGet_ForbiddenInvalidatesCredentials(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()

	lock := filepath.Join(t.TempDir(), "lockfile")
	writeLockfile(t, lock, "whatever")

	c := NewClient(lock)
	c.apiPort = serverPort(t, srv)
	if err := c.RefreshCredentials(); err != nil {
		t.Fatalf("RefreshCredentials: %v", err)
	}
	if _, err := c.get(context.Background(), "/liveclientdata/allgamedata"); err == nil {
		t.Fatal("expected an error for 403, got nil")
	}
	if c.HasCredentials() {
		t.Fatal("403 must drop cached credentials")
	}
}

// A 404 means "not in game" — the credentials are still valid and must be kept,
// otherwise the next tick would re-scan processes for no reason.
func TestGet_NotFoundKeepsCredentials(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	lock := filepath.Join(t.TempDir(), "lockfile")
	writeLockfile(t, lock, "still-valid")

	c := NewClient(lock)
	c.apiPort = serverPort(t, srv)
	if err := c.RefreshCredentials(); err != nil {
		t.Fatalf("RefreshCredentials: %v", err)
	}
	_, err := c.get(context.Background(), "/liveclientdata/allgamedata")
	if err != ErrNotInGame {
		t.Fatalf("err = %v, want ErrNotInGame", err)
	}
	if !c.HasCredentials() {
		t.Fatal("404 must keep the credentials")
	}
}

// Waiting for a game, the collector enumerated every process of the machine on
// every tick. The scan must be rate-limited while the lockfile is absent.
func TestRefreshCredentials_ThrottlesProcessScan(t *testing.T) {
	c := NewClient(filepath.Join(t.TempDir(), "no-such-lockfile"))
	var scans atomic.Int32
	c.processScan = func() (string, string) {
		scans.Add(1)
		return "", "" // no League client running
	}

	for i := 0; i < 5; i++ {
		if err := c.RefreshCredentials(); err == nil {
			t.Fatalf("tick %d: expected 'not in game', got nil", i)
		}
		if c.HasCredentials() {
			t.Fatalf("tick %d: no password must have been found", i)
		}
	}
	if got := scans.Load(); got != 1 {
		t.Fatalf("process scans = %d in 5 ticks, want 1 (rate-limited)", got)
	}

	// A 401 means the credentials really are stale, so the next refresh must
	// re-scan immediately instead of waiting out the throttle.
	c.setPassword("stale")
	c.invalidateCredentials()
	if err := c.RefreshCredentials(); err == nil {
		t.Fatal("expected 'not in game'")
	}
	if got := scans.Load(); got != 2 {
		t.Fatalf("process scans after a 401 = %d, want 2 (invalidating clears the throttle)", got)
	}
}

// The lockfile path is one stat + one small read, so it stays unthrottled:
// refreshing must keep working every tick while a game runs.
func TestRefreshCredentials_LockfileNeverThrottled(t *testing.T) {
	lock := filepath.Join(t.TempDir(), "lockfile")
	writeLockfile(t, lock, "pass-1")

	c := NewClient(lock)
	var scans atomic.Int32
	c.processScan = func() (string, string) {
		scans.Add(1)
		return "", ""
	}

	for i := 0; i < 5; i++ {
		writeLockfile(t, lock, "pass-1")
		if err := c.RefreshCredentials(); err != nil {
			t.Fatalf("tick %d: %v", i, err)
		}
	}
	if got := scans.Load(); got != 0 {
		t.Fatalf("process scans = %d, want 0 (the lockfile answered every tick)", got)
	}
	if pw := c.currentPassword(); pw != "pass-1" {
		t.Fatalf("password = %q, want pass-1", pw)
	}
}

// A found token must also clear the failure streak, so the throttle returns to
// its base delay after the client is seen again.
func TestRefreshCredentials_ScanSuccessResetsBackoff(t *testing.T) {
	c := NewClient(filepath.Join(t.TempDir(), "no-such-lockfile"))
	var scans atomic.Int32
	c.processScan = func() (string, string) {
		scans.Add(1)
		return "", "found-token"
	}

	if err := c.RefreshCredentials(); err != nil {
		t.Fatalf("RefreshCredentials: %v", err)
	}
	if pw := c.currentPassword(); pw != "found-token" {
		t.Fatalf("password = %q, want found-token", pw)
	}
	c.mu.Lock()
	fails := c.scanFails
	c.mu.Unlock()
	if fails != 0 {
		t.Fatalf("scanFails = %d, want 0 after a successful scan", fails)
	}
}

// IsNotInGame must recognise the syscall error, not just the English message:
// Windows 中文镜像 reports 积极拒绝 and Linux varies, so a string-only check
// misclassified every "not in game" tick as a fetch error.
func TestIsNotInGame(t *testing.T) {
	if IsNotInGame(nil) {
		t.Error("nil error must not be 'not in game'")
	}
	if IsNotInGame(errors.New("status 500: boom")) {
		t.Error("a server error is not 'not in game'")
	}
	// Go wraps the real syscall error all the way up.
	if !IsNotInGame(&net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}) {
		t.Error("ECONNREFUSED wrapped in net.OpError must be 'not in game'")
	}
	if !IsNotInGame(fmt.Errorf("dial tcp 127.0.0.1:2999: %w", syscall.ECONNREFUSED)) {
		t.Error("ECONNREFUSED wrapped in fmt.Errorf must be 'not in game'")
	}
	// Legacy string matches are kept as a fallback.
	if !IsNotInGame(errors.New("dial tcp: connection refused")) {
		t.Error("the English string match must keep working")
	}
}

// The throttle must back off exponentially on repeated failures and cap out.
func TestProcessScanBackoffDuration(t *testing.T) {
	want := []time.Duration{
		processScanBackoff,
		10 * time.Second,
		20 * time.Second,
		40 * time.Second,
		processScanBackoffMax,
		processScanBackoffMax,
	}
	for i, w := range want {
		if got := processScanBackoffDuration(i); got != w {
			t.Errorf("processScanBackoffDuration(%d) = %v, want %v", i, got, w)
		}
	}
}
