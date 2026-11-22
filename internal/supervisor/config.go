// Package supervisor starts one locally configured Multica daemon per connection.
package supervisor

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	TestHTTPEnv = "MULTICA_BRIDGE_TEST_HTTP"
	MailboxRoot = "/bridge"
	RuntimeRoot = "/bridge-runtime"
	maxConfig   = 256 << 10
)

type Config struct {
	Connections []Connection `json:"connections"`
}

type Connection struct {
	ID            string   `json:"id"`
	ServerURL     string   `json:"server_url"`
	WorkspaceIDs  []string `json:"workspace_ids,omitempty"`
	TokenFile     string   `json:"token_file"`
	MaxActiveRuns int      `json:"max_active_runs,omitempty"`
}

var idRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,47}$`)
var workspaceRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$`)

// Load reads operator-owned config, never a request body or task-local file.
func Load(path string, testHTTP bool) (Config, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return Config{}, errors.New("config path must be absolute and clean")
	}
	raw, err := readFile(path, maxConfig)
	if err != nil {
		return Config{}, errors.New("cannot read connection configuration")
	}
	var cfg Config
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil || decoder.Decode(new(any)) != io.EOF {
		return Config{}, errors.New("invalid JSON connection configuration")
	}
	if len(cfg.Connections) == 0 {
		return Config{}, errors.New("configure at least one connection")
	}
	seen := make(map[string]bool)
	for i := range cfg.Connections {
		c := &cfg.Connections[i]
		if !idRE.MatchString(c.ID) || seen[c.ID] {
			return Config{}, fmt.Errorf("connection %d: invalid or duplicate id", i+1)
		}
		seen[c.ID] = true
		if c.ServerURL, err = normalizeURL(c.ServerURL, testHTTP); err != nil {
			return Config{}, fmt.Errorf("connection %s: invalid server_url", c.ID)
		}
		if !filepath.IsAbs(c.TokenFile) || filepath.Clean(c.TokenFile) != c.TokenFile || strings.ContainsAny(c.TokenFile, "\x00\r\n") {
			return Config{}, fmt.Errorf("connection %s: token_file must be an absolute clean path", c.ID)
		}
		if c.MaxActiveRuns == 0 {
			c.MaxActiveRuns = 4
		}
		if c.MaxActiveRuns < 1 {
			return Config{}, fmt.Errorf("connection %s: max_active_runs must be positive", c.ID)
		}
		have := make(map[string]bool)
		for _, id := range c.WorkspaceIDs {
			if !workspaceRE.MatchString(id) || have[id] {
				return Config{}, fmt.Errorf("connection %s: invalid or duplicate workspace id", c.ID)
			}
			have[id] = true
		}
	}
	return cfg, nil
}

func normalizeURL(raw string, testHTTP bool) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || u.RawPath != "" {
		return "", errors.New("invalid endpoint")
	}
	host := strings.ToLower(u.Hostname())
	if u.Scheme != "https" {
		testHost := host == "backend-a" || host == "backend-b"
		if !testHTTP || !testHost || u.Scheme != "http" {
			return "", errors.New("endpoint must use https")
		}
	}
	for _, segment := range strings.Split(u.Path, "/") {
		if segment == "." || segment == ".." {
			return "", errors.New("invalid endpoint path")
		}
	}
	u.Host = strings.ToLower(u.Host)
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

// Route verifies daemon-injected task scope against the local connection mapping.
func (cfg Config) Route(id, endpoint, workspace string, testHTTP bool) (Connection, error) {
	for _, c := range cfg.Connections {
		if c.ID != id {
			continue
		}
		normalized, err := normalizeURL(endpoint, testHTTP)
		if err != nil || normalized != c.ServerURL || !workspaceRE.MatchString(workspace) {
			return Connection{}, errors.New("task does not match local connection")
		}
		if len(c.WorkspaceIDs) == 0 {
			return c, nil
		}
		for _, allowed := range c.WorkspaceIDs {
			if workspace == allowed {
				return c, nil
			}
		}
		return Connection{}, errors.New("task workspace not allowed for local connection")
	}
	return Connection{}, errors.New("unknown local connection")
}

func readFile(path string, max int) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > int64(max) {
		return nil, errors.New("not a bounded regular file")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, int64(max)+1))
	if err == nil && len(data) > max {
		err = errors.New("file exceeds size limit")
	}
	return data, err
}
