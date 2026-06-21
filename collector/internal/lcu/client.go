package lcu

import (
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v3/process"
)

// lockfile candidate paths (Windows).
var lockfilePaths = []string{
	`D:\WeGameApps\英雄联盟\Riot Client Data\User Data\Config\lockfile`,
	filepath.Join(os.Getenv("LOCALAPPDATA"), "Riot Games", "Riot Client", "Config", "lockfile"),
	filepath.Join(os.Getenv("LOCALAPPDATA"), "Riot Games", "League of Legends", "lockfile"),
	`C:\Riot Games\League of Legends\lockfile`,
	`C:\Riot Games\Riot Client\lockfile`,
}

// Client communicates with the League Client Update (LCU) HTTPS API.
type Client struct {
	port       string
	password   string
	baseURL    string
	authHeader string
	httpClient *http.Client
	connected  bool
}

func NewClient() *Client {
	return &Client{
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			},
		},
	}
}

func (c *Client) Connected() bool { return c.connected }

// TryConnect attempts to discover and authenticate with the LCU API.
// Returns true on success.
func (c *Client) TryConnect() bool {
	// Method 1: lockfile
	for _, p := range lockfilePaths {
		if c.tryLockfile(p) {
			return true
		}
	}

	// Method 2: process command line (LeagueClientUx.exe)
	return c.tryProcess()
}

func (c *Client) tryLockfile(path string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	content := strings.TrimSpace(string(data))
	if content == "" {
		return false
	}
	parts := strings.Split(content, ":")

	// LeagueClient format: LeagueClient:port:password:protocol
	if len(parts) >= 4 && parts[0] == "LeagueClient" {
		return c.connect(parts[1], parts[2])
	}

	// Riot Client format (WeGame/国服): Riot Client:riot_port:lcu_port:password:protocol
	// Note: "Riot Client" is one token (no colon between "Riot" and "Client"), so 5 parts total.
	// e.g. Riot Client:21968:59062:abc123token:https → parts[2]=port, parts[3]=password
	if len(parts) >= 5 && strings.HasPrefix(content, "Riot Client") {
		return c.connect(parts[2], parts[3])
	}

	return false
}

func (c *Client) tryProcess() bool {
	procs, err := process.Processes()
	if err == nil {
		for _, p := range procs {
			name, err := p.Name()
			if err != nil {
				continue
			}
			if name != "LeagueClient.exe" && name != "LeagueClientUx.exe" {
				continue
			}

			// Method A: gopsutil Cmdline (fast, pure Go)
			cmdline, _ := p.Cmdline()
			if cmdline != "" {
				port, token := parseCommandLine(cmdline)
				if port != "" && token != "" {
					return c.connect(port, token)
				}
			}

			// Method B: lockfile from exe path
			exe, err := p.Exe()
			if err == nil && exe != "" {
				if c.tryLockfileByPath(filepath.Join(filepath.Dir(exe), "lockfile")) {
					return true
				}
			}
		}
	}

	// Method C: Python psutil (most reliable on Windows, no admin required)
	return c.tryPythonPsutil()
}

// tryPythonPsutil calls a tiny Python script that uses psutil to read
// the LeagueClient process command line. psutil on Windows reads the
// PEB via NtQueryInformationProcess, which doesn't need admin.
func (c *Client) tryPythonPsutil() bool {
	script := filepath.Join("scripts", "lcu_cred.py")
	out, err := exec.Command("python", script).Output()
	if err != nil {
		return false
	}
	line := strings.TrimSpace(string(out))
	parts := strings.SplitN(line, "|", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return false
	}
	return c.connect(parts[0], parts[1])
}

// tryLockfileByPath reads a specific lockfile path (handles standard LCU format).
func (c *Client) tryLockfileByPath(path string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	content := strings.TrimSpace(string(data))
	parts := strings.Split(content, ":")
	// LeagueClient format
	if len(parts) >= 4 && parts[0] == "LeagueClient" {
		return c.connect(parts[1], parts[2])
	}
	// Riot Client format
	if len(parts) >= 5 && strings.HasPrefix(content, "Riot Client") {
		return c.connect(parts[2], parts[3])
	}
	return false
}

func parseCommandLine(cmdline string) (port, token string) {
	// Command line looks like: "C:\path\LeagueClient.exe" --app-port=59162 --remoting-auth-token=abc123 ...
	if idx := strings.Index(cmdline, "--app-port="); idx != -1 {
		rest := cmdline[idx+len("--app-port="):]
		if end := strings.IndexAny(rest, " \t\r\""); end != -1 {
			port = rest[:end]
		} else {
			port = strings.TrimSpace(rest)
		}
	}
	if idx := strings.Index(cmdline, "--remoting-auth-token="); idx != -1 {
		rest := cmdline[idx+len("--remoting-auth-token="):]
		if end := strings.IndexAny(rest, " \t\r\""); end != -1 {
			token = rest[:end]
		} else {
			token = strings.TrimSpace(rest)
		}
	}
	return port, token
}

func (c *Client) connect(port, password string) bool {
	c.port = port
	c.password = password
	c.baseURL = "https://127.0.0.1:" + port
	auth := "riot:" + password
	c.authHeader = "Basic " + base64.StdEncoding.EncodeToString([]byte(auth))

	// Temporarily set connected to pass Get()'s guard.
	c.connected = true

	// Test connection.
	resp, err := c.Get("/lol-summoner/v1/current-summoner")
	if err != nil {
		// Fallback: try gameflow endpoint
		resp, err = c.Get("/lol-gameflow/v1/session")
		if err != nil {
			c.connected = false
			return false
		}
	}
	if resp == nil {
		c.connected = false
		return false
	}
	return true
}

// Get makes an authenticated GET request to an LCU API path.
func (c *Client) Get(path string) (map[string]interface{}, error) {
	if !c.connected {
		return nil, fmt.Errorf("not connected")
	}

	url := c.baseURL + path
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", c.authHeader)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.connected = false
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	return result, nil
}

// GetArray makes an authenticated GET request returning a JSON array.
func (c *Client) GetArray(path string) ([]map[string]interface{}, error) {
	if !c.connected {
		return nil, fmt.Errorf("not connected")
	}

	url := c.baseURL + path
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", c.authHeader)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.connected = false
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}

	var result []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	return result, nil
}
