package lol

import (
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/shirou/gopsutil/v3/process"
)

const liveClientPort = "2999" // Live Client Data API fixed port

const (
	// processScanBackoff is the minimum gap between full process enumerations,
	// and the base for the exponential backoff after a failed scan. Waiting for
	// a game, the collector used to enumerate every process of the machine on
	// every tick (hundreds of syscalls/second on macOS).
	processScanBackoff    = 5 * time.Second
	processScanBackoffMax = 60 * time.Second
)

// processScanBackoffDuration doubles the base once per consecutive failed scan,
// capped at processScanBackoffMax.
func processScanBackoffDuration(fails int) time.Duration {
	d := processScanBackoff
	for i := 0; i < fails; i++ {
		d *= 2
		if d >= processScanBackoffMax {
			return processScanBackoffMax
		}
	}
	return d
}

type Client struct {
	lockfilePath string
	password     string // guarded by mu — rewritten on every refresh / invalidation
	mu           sync.Mutex
	// apiPort is the Live Client Data API port (2999). A field so tests can
	// point the client at a local TLS server; production always uses 2999.
	apiPort    string
	httpClient *http.Client
	hasCreds   atomic.Bool // password available (from lockfile or process)
	inGame     atomic.Bool // game is actually running (200 from API)
	objectives *ObjectiveTracker

	// processScan is the expensive full-process enumeration; replaced in tests.
	processScan func() (port, token string)
	// lastScan / scanFails (guarded by mu) rate-limit processScan.
	lastScan  time.Time
	scanFails int
}

func NewClient(lockfilePath string) *Client {
	c := &Client{
		lockfilePath: lockfilePath,
		apiPort:      liveClientPort,
		objectives:   NewObjectiveTracker(),
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			},
		},
	}
	c.processScan = func() (string, string) { return c.discoverFromProcess() }
	return c
}

func (c *Client) HasCredentials() bool { return c.hasCreds.Load() }
func (c *Client) IsInGame() bool       { return c.inGame.Load() }

// setPassword stores a fresh password and marks credentials as usable.
func (c *Client) setPassword(pw string) {
	c.mu.Lock()
	c.password = pw
	c.mu.Unlock()
	c.hasCreds.Store(true)
}

// invalidateCredentials drops the cached password so the next
// RefreshCredentials re-reads it. Called on 401/403: the LOL client rewrites
// its lockfile on restart, so a password that used to work stays 401 forever
// unless it is dropped here — every tick would otherwise fail until restart.
// The process-scan throttle is cleared too: recovery from a 401 is worth an
// immediate (expensive) re-scan.
func (c *Client) invalidateCredentials() {
	c.mu.Lock()
	c.password = ""
	c.lastScan = time.Time{}
	c.scanFails = 0
	c.mu.Unlock()
	c.hasCreds.Store(false)
}

// currentPassword returns the password to authenticate with.
func (c *Client) currentPassword() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.password
}

// RefreshCredentials tries to obtain the auth password for the Live Client Data API.
// The port is always 2999; we only need the password.
func (c *Client) RefreshCredentials() error {
	// Method 1: lockfile (only LeagueClient format: LeagueClient:port:password:protocol)
	// Cheap (one stat + one small read), so it is never throttled.
	path, err := c.resolveLockfile()
	if err == nil {
		data, err := os.ReadFile(path)
		if err == nil {
			parts := strings.Split(strings.TrimSpace(string(data)), ":")
			// LeagueClient format: LeagueClient:port:password:protocol
			if len(parts) >= 4 && parts[0] == "LeagueClient" {
				c.setPassword(parts[2])
				return nil
			}
			// Riot Client format: Riot Client:riot_port:lcu_port:password:protocol
			if len(parts) >= 5 && parts[0] == "Riot Client" {
				c.setPassword(parts[3])
				return nil
			}
		}
	}

	// Method 2: process command line via gopsutil (no admin required, like Python psutil)
	// This enumerates every process on the machine, so it is rate-limited: while
	// we are waiting for a game there is nothing to gain from doing it every tick.
	now := time.Now()
	if !c.allowProcessScan(now) {
		c.hasCreds.Store(false)
		return fmt.Errorf("waiting for live client data (not in game)")
	}
	c.markProcessScan(now)

	_, token := c.processScan()
	if token == "" {
		c.noteScanFailure()
		c.hasCreds.Store(false)
		return fmt.Errorf("waiting for live client data (not in game)")
	}
	c.noteScanSuccess()
	c.setPassword(token)
	return nil
}

// allowProcessScan reports whether a fresh process enumeration may run now.
func (c *Client) allowProcessScan(now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.lastScan.IsZero() {
		return true
	}
	return !now.Before(c.lastScan.Add(processScanBackoffDuration(c.scanFails)))
}

// markProcessScan records that a scan just started at now.
func (c *Client) markProcessScan(now time.Time) {
	c.mu.Lock()
	c.lastScan = now
	c.mu.Unlock()
}

func (c *Client) noteScanSuccess() {
	c.mu.Lock()
	c.scanFails = 0
	c.mu.Unlock()
}

func (c *Client) noteScanFailure() {
	c.mu.Lock()
	c.scanFails++
	c.mu.Unlock()
}

func (c *Client) discoverFromProcess() (port, token string) {
	procs, err := process.Processes()
	if err != nil {
		return "", ""
	}

	targets := map[string]bool{
		"LeagueClient.exe":   true,
		"LeagueClientUx.exe": true,
	}

	for _, p := range procs {
		name, err := p.Name()
		if err != nil {
			continue
		}

		if !targets[name] {
			continue
		}

		// Method A: read command line
		cmdline, _ := p.Cmdline()
		if cmdline != "" {
			if idx := strings.Index(cmdline, "--remoting-auth-token="); idx != -1 {
				rest := cmdline[idx+len("--remoting-auth-token="):]
				if end := strings.IndexAny(rest, " \t\r\""); end != -1 {
					token = rest[:end]
				} else {
					token = strings.TrimSpace(rest)
				}
				if token != "" {
					return port, token
				}
			}
		}

		// Method B: derive lockfile from exe path (password = --remoting-auth-token)
		exe, err := p.Exe()
		if err != nil || exe == "" {
			continue
		}
		lockPath := filepath.Join(filepath.Dir(exe), "lockfile")
		data, err := os.ReadFile(lockPath)
		if err != nil {
			continue
		}
		parts := strings.Split(strings.TrimSpace(string(data)), ":")
		// LeagueClient format: LeagueClient:port:password:protocol
		if len(parts) >= 4 && parts[0] == "LeagueClient" {
			return "", parts[2] // password
		}
		// Riot Client format: Riot Client:riot_port:lcu_port:password:protocol
		if len(parts) >= 5 && parts[0] == "Riot Client" {
			return "", parts[3] // password
		}
	}
	return "", ""
}

func (c *Client) resolveLockfile() (string, error) {
	if c.lockfilePath != "" {
		return c.lockfilePath, nil
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}

	candidates := []string{
		filepath.Join(home, "AppData", "Local", "Riot Games", "Riot Client", "Config", "lockfile"),
		`D:\WeGameApps\英雄联盟\Riot Client Data\User Data\Config\lockfile`, // WeGame 国服
	}
	if runtime.GOOS == "darwin" {
		candidates = []string{
			filepath.Join(home, "Library", "Application Support", "Riot Games", "Riot Client", "Config", "lockfile"),
		}
	}

	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p, nil
		}
	}

	return "", fmt.Errorf("lockfile not found")
}

func (c *Client) FetchGameState(ctx context.Context) (*GameState, error) {
	raw, err := c.get(ctx, "/liveclientdata/allgamedata")
	if err != nil {
		c.inGame.Store(false)
		return nil, err
	}

	c.inGame.Store(true)

	state, err := ParseGameState(raw)
	if err != nil {
		return nil, err
	}
	c.objectives.Enrich(state)
	return state, nil
}

func (c *Client) get(ctx context.Context, path string) ([]byte, error) {
	if !c.hasCreds.Load() {
		return nil, fmt.Errorf("no credentials")
	}

	url := "https://127.0.0.1:" + c.apiPort + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}

	token := base64.StdEncoding.EncodeToString([]byte("riot:" + c.currentPassword()))
	req.Header.Set("Authorization", "Basic "+token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrNotInGame
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		// Credentials are stale (LOL client restarted and rewrote its lockfile
		// password). Drop them so the next tick refreshes instead of 401-ing on
		// every request until the collector is restarted.
		c.invalidateCredentials()
		return nil, fmt.Errorf("status %d: %s", resp.StatusCode, readBody(resp))
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d: %s", resp.StatusCode, readBody(resp))
	}

	return io.ReadAll(resp.Body)
}

// readBody drains a bounded prefix of the response body for error messages.
func readBody(resp *http.Response) []byte {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
	return body
}

// ErrNotInGame is returned when the game is not running (port 2999 responds 404).
var ErrNotInGame = fmt.Errorf("not in game")

// IsNotInGame checks if the error means the game is not reachable
// (connection refused or HTTP 404 — both mean not in game).
//
// The string match is only a fallback: Go always wraps the real syscall error,
// and errors.Is works no matter which language the OS localises the message into
// (Windows 中文镜像 reports 积极拒绝, not "actively refused"). Every tick that
// misclassifies this spams an error log and resets the event detector.
func IsNotInGame(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "connection refused") ||
		strings.Contains(msg, "actively refused") ||
		strings.Contains(msg, "No connection could be made")
}

func (c *Client) FetchRaw(path string) (json.RawMessage, error) {
	data, err := c.get(context.Background(), path)
	if err != nil {
		return nil, err
	}
	return json.RawMessage(data), nil
}
