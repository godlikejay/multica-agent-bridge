package supervisor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigAndRoute(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "connections.json")
	config := `{"connections":[{"id":"work","server_url":"https://WORK.example.invalid/","app_url":"https://app.example.invalid","token_file":"/run/secrets/work","workspace_ids":["ws-1"]},{"id":"cloud","server_url":"https://cloud.example.invalid","token_file":"/run/secrets/cloud","max_active_runs":2}]}`
	put := func(data string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	put(config)
	cfg, err := Load(path, false)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := cfg.Route("work", "https://work.example.invalid", "ws-1", false); err != nil || got.MaxActiveRuns != 4 {
		t.Fatalf("trusted route: %#v / %v", got, err)
	}
	if got, err := cfg.Route("cloud", "https://cloud.example.invalid/", "other-workspace", false); err != nil || got.AppURL != "" {
		t.Fatalf("API-only route: %#v / %v", got, err)
	}
	if cfg.Connections[0].AppURL != "https://app.example.invalid" {
		t.Fatal("lost explicit upstream browser URL")
	}
	for _, broken := range []struct{ id, endpoint, workspace string }{
		{"work", "https://cloud.example.invalid", "ws-1"},
		{"work", "https://work.example.invalid/path", "ws-1"},
		{"work", "https://work.example.invalid", "ws-2"},
		{"work", "https://work.example.invalid", "../ws-1"},
		{"cloud", "https://cloud.example.invalid", strings.Repeat("w", 101)},
		{"bad", "https://work.example.invalid", "ws-1"},
	} {
		if _, err := cfg.Route(broken.id, broken.endpoint, broken.workspace, false); err == nil {
			t.Fatalf("accepted %+v", broken)
		}
	}
	for _, invalid := range []string{
		strings.Replace(config, `"cloud"`, `"work"`, 1),
		strings.Replace(config, `"cloud"`, `"../cloud"`, 1),
		strings.Replace(config, `"ws-1"`, `"ws-1","ws-1"`, 1),
		strings.Replace(config, "ws-1", strings.Repeat("w", 101), 1),
		strings.Replace(config, `"/run/secrets/work"`, `"relative"`, 1),
		strings.Replace(config, `"https://WORK.example.invalid/"`, `"https://user:abc@work.example.invalid?secret=abc"`, 1),
		strings.Replace(config, "https://app.example.invalid", "http://localhost:13100", 1),
		strings.Replace(config, `"max_active_runs":2`, `"max_active_runs":-1`, 1),
		strings.Replace(config, `"max_active_runs":2`, `"key":"abc"`, 1),
		config + "null",
	} {
		put(invalid)
		if _, err := Load(path, false); err == nil || strings.Contains(err.Error(), "abc") {
			t.Fatalf("expected input error without secret: %v", err)
		}
	}
}

func TestTestHTTPIsNarrowAndOptIn(t *testing.T) {
	for _, entry := range []struct {
		target              string
		testHTTP, app, want bool
	}{
		{"http://backend-a:8080", true, false, true},
		{"http://backend-b:8080", true, false, true},
		{"http://backend-a:8080", false, false, false},
		{"http://127.0.0.1:18180", true, false, false},
		{"http://backend-a.example.invalid:8080", true, false, false},
		{"http://backend-a:8080", true, true, false},
		{"http://localhost:13100", true, true, true},
		{"http://frontend-b:3000", true, true, true},
	} {
		if _, err := normalizeURL(entry.target, entry.testHTTP, entry.app); (err == nil) != entry.want {
			t.Fatalf("bad URL result for %+v: %v", entry, err)
		}
	}
}
