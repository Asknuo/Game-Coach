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
	"time"
)

const liveClientBase = "https://127.0.0.1"

type Client struct {
	lockfilePath string
	port         string
	password     string
	httpClient   *http.Client
	available    bool
}

func NewClient(lockfilePath string) *Client {
	return &Client{
		lockfilePath: lockfilePath,
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			},
		},
	}
}

func (c *Client) IsAvailable() bool {
	return c.available
}

func (c *Client) RefreshCredentials() error {
	path, err := c.resolveLockfile()
	if err != nil {
		c.available = false
		return err
	}

	data, err := os.ReadFile(path)
	if err != nil {
		c.available = false
		return fmt.Errorf("read lockfile: %w", err)
	}

	parts := strings.Split(strings.TrimSpace(string(data)), ":")
	if len(parts) < 5 {
		c.available = false
		return fmt.Errorf("invalid lockfile format")
	}

	c.port = parts[2]
	c.password = parts[3]
	c.available = true
	return nil
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
	_ = ctx
	raw, err := c.get("/liveclientdata/allgamedata")
	if err != nil {
		return nil, err
	}

	return ParseGameState(raw)
}

func (c *Client) get(path string) ([]byte, error) {
	if !c.available {
		return nil, fmt.Errorf("client not available")
	}

	url := fmt.Sprintf("%s:%s%s", liveClientBase, c.port, path)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}

	token := base64.StdEncoding.EncodeToString([]byte("riot:" + c.password))
	req.Header.Set("Authorization", "Basic "+token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.available = false
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("not in game")
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("status %d: %s", resp.StatusCode, string(body))
	}

	return io.ReadAll(resp.Body)
}

func (c *Client) FetchRaw(path string) (json.RawMessage, error) {
	data, err := c.get(path)
	if err != nil {
		return nil, err
	}
	return json.RawMessage(data), nil
}
