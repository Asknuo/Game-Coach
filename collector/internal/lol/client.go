package lol

import (
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"time"

	"github.com/shirou/gopsutil/v3/process"
)

const liveClientPort = "2999" // Live Client Data API fixed port

type Client struct {
	lockfilePath string
	password     string
	httpClient   *http.Client
	hasCreds     atomic.Bool // password available (from lockfile or process)
	inGame       atomic.Bool // game is actually running (200 from API)
	objectives   *ObjectiveTracker
}

func NewClient(lockfilePath string) *Client {
	return &Client{
		lockfilePath: lockfilePath,
		objectives:   NewObjectiveTracker(),
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			},
		},
	}
}

func (c *Client) HasCredentials() bool { return c.hasCreds.Load() }
func (c *Client) IsInGame() bool       { return c.inGame.Load() }

// RefreshCredentials tries to obtain the auth password for the Live Client Data API.
// The port is always 2999; we only need the password.
func (c *Client) RefreshCredentials() error {
	// Method 1: lockfile (only LeagueClient format: LeagueClient:port:password:protocol)
	path, err := c.resolveLockfile()
	if err == nil {
		data, err := os.ReadFile(path)
		if err == nil {
			parts := strings.Split(strings.TrimSpace(string(data)), ":")
			// LeagueClient format: LeagueClient:port:password:protocol
			if len(parts) >= 4 && parts[0] == "LeagueClient" {
				c.password = parts[2]
				c.hasCreds.Store(true)
				return nil
			}
			// Riot Client format: Riot Client:riot_port:lcu_port:password:protocol
			if len(parts) >= 5 && parts[0] == "Riot Client" {
				c.password = parts[3]
				c.hasCreds.Store(true)
				return nil
			}
		}
	}

	// Method 2: process command line via gopsutil (no admin required, like Python psutil)
	_, token := c.discoverFromProcess()
	if token != "" {
		c.password = token
		c.hasCreds.Store(true)
		return nil
	}

	c.hasCreds.Store(false)
	return fmt.Errorf("waiting for live client data (not in game)")
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

	url := "https://127.0.0.1:" + liveClientPort + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}

	token := base64.StdEncoding.EncodeToString([]byte("riot:" + c.password))
	req.Header.Set("Authorization", "Basic "+token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrNotInGame
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("status %d: %s", resp.StatusCode, string(body))
	}

	return io.ReadAll(resp.Body)
}

// ErrNotInGame is returned when the game is not running (port 2999 responds 404).
var ErrNotInGame = fmt.Errorf("not in game")

// IsNotInGame checks if the error means the game is not reachable
// (connection refused or HTTP 404 — both mean not in game).
func IsNotInGame(err error) bool {
	if err == nil {
		return false
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
