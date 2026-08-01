package lcu

import (
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

func TestParseCommandLine(t *testing.T) {
	cases := []struct {
		name     string
		cmdline  string
		wantPort string
		wantTok  string
	}{
		{
			name:     "standard LeagueClient",
			cmdline:  `"C:\Riot Games\League of Legends\LeagueClient.exe" --app-port=59162 --remoting-auth-token=abc123xyz --no-proxy`,
			wantPort: "59162",
			wantTok:  "abc123xyz",
		},
		{
			name:     "token at end of line",
			cmdline:  `--app-port=1234 --remoting-auth-token=tok`,
			wantPort: "1234",
			wantTok:  "tok",
		},
		{
			name:     "port only",
			cmdline:  `--app-port=9999`,
			wantPort: "9999",
			wantTok:  "",
		},
		{
			name:    "no arguments",
			cmdline: `LeagueClient.exe`,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			port, token := parseCommandLine(c.cmdline)
			if port != c.wantPort || token != c.wantTok {
				t.Errorf("parseCommandLine(%q) = (%q, %q), want (%q, %q)",
					c.cmdline, port, token, c.wantPort, c.wantTok)
			}
		})
	}
}

// Regression for ISSUES #50/#52: both LeagueClient and Riot Client lockfile
// formats must be parsed and connected; garbage must be rejected.
func TestTryLockfile_Formats(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	port := strconv.Itoa(srv.Listener.Addr().(*net.TCPAddr).Port)
	dir := t.TempDir()

	t.Run("LeagueClient format", func(t *testing.T) {
		path := filepath.Join(dir, "lockfile_lc")
		if err := os.WriteFile(path, []byte("LeagueClient:"+port+":secret:https"), 0o600); err != nil {
			t.Fatal(err)
		}
		c := NewClient()
		if !c.tryLockfile(path) {
			t.Fatal("expected connect success for LeagueClient lockfile")
		}
	})

	t.Run("Riot Client format", func(t *testing.T) {
		path := filepath.Join(dir, "lockfile_rc")
		if err := os.WriteFile(path, []byte("Riot Client:21968:"+port+":secret:https"), 0o600); err != nil {
			t.Fatal(err)
		}
		c := NewClient()
		if !c.tryLockfile(path) {
			t.Fatal("expected connect success for Riot Client lockfile")
		}
		if c.port != port {
			t.Errorf("port = %q, want %q", c.port, port)
		}
	})

	t.Run("empty lockfile rejected", func(t *testing.T) {
		path := filepath.Join(dir, "lockfile_empty")
		if err := os.WriteFile(path, []byte("   "), 0o600); err != nil {
			t.Fatal(err)
		}
		c := NewClient()
		if c.tryLockfile(path) {
			t.Fatal("empty lockfile must be rejected")
		}
	})

	t.Run("garbage format rejected", func(t *testing.T) {
		path := filepath.Join(dir, "lockfile_bad")
		if err := os.WriteFile(path, []byte("garbage:1:2:3"), 0o600); err != nil {
			t.Fatal(err)
		}
		c := NewClient()
		if c.tryLockfile(path) {
			t.Fatal("unknown lockfile format must be rejected")
		}
	})
}
