// Package mailbox implements the single-writer file protocol.
package mailbox

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"unicode/utf8"
)

const MaxFile = 256 * 1024
const MaxBody = 32 * 1024
const maxState = 16 * 1024 * 1024

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$`)

type Message struct {
	Version   int    `json:"version"`
	Kind      string `json:"kind"`
	ID        string `json:"id"`
	IssueID   string `json:"issue_id"`
	RunID     string `json:"run_id"`
	Epoch     uint64 `json:"epoch"`
	Body      string `json:"body,omitempty"`
	IssueKey  string `json:"issue_key,omitempty"`
	SourceID  string `json:"source_id,omitempty"`
	Revision  string `json:"revision,omitempty"`
	ParentID  string `json:"parent_id,omitempty"`
	RequestID string `json:"request_id,omitempty"`
	Status    string `json:"status,omitempty"`
	RemoteID  string `json:"remote_id,omitempty"`
}
type State struct {
	Version    int               `json:"version"`
	IssueID    string            `json:"issue_id"`
	RunID      string            `json:"run_id"`
	Epoch      uint64            `json:"epoch"`
	TaskEvent  *Message          `json:"task_event,omitempty"`
	Completion string            `json:"completion,omitempty"`
	Seen       map[string]string `json:"seen"`
	Actions    map[string]Action `json:"actions"`
}
type Action struct {
	Digest  string  `json:"digest"`
	Receipt Message `json:"receipt"`
}
type Box struct{ Root string }

func ID(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}
func (b Box) Init() error {
	if !filepath.IsAbs(b.Root) {
		return errors.New("BRIDGE_ROOT must be an absolute path")
	}
	for _, path := range []string{b.Root, b.dir("inbox"), b.dir("outbox"), b.dir("state")} {
		if err := os.MkdirAll(path, 0700); err != nil {
			return err
		}
		f, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if !f.IsDir() {
			return errors.New("mailbox paths must be real directories")
		}
	}
	return nil
}
func (b Box) dir(d string) string { return filepath.Join(b.Root, d) }
func (b Box) Lock() (func(), error) {
	f, err := os.OpenFile(filepath.Join(b.dir("state"), "writer.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, errors.New("another Bridge writer is active")
	}
	return func() { f.Close() }, nil
}
func (b Box) statePath(issue string) string {
	return filepath.Join(b.dir("state"), ID("issue", issue)+".json")
}
func (b Box) Load(issue string) (State, error) {
	s := State{Version: 1, IssueID: issue, Seen: map[string]string{}, Actions: map[string]Action{}}
	raw, err := os.ReadFile(b.statePath(issue))
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return s, err
	}
	if len(raw) > maxState {
		return s, errors.New("state exceeds size limit")
	}
	if err = json.Unmarshal(raw, &s); err != nil {
		return s, errors.New("invalid saved state")
	}
	if s.Version != 1 || s.IssueID != issue || s.Seen == nil || s.Actions == nil {
		return s, errors.New("saved state identity or version mismatch")
	}
	return s, nil
}
func (b Box) Save(s State) error {
	raw, err := json.Marshal(s)
	if err != nil {
		return err
	}
	if len(raw)+1 > maxState {
		return errors.New("state exceeds size limit")
	}
	return atomicJSON(b.statePath(s.IssueID), json.RawMessage(raw))
}
func (b Box) Emit(m Message) error {
	if !validID.MatchString(m.ID) {
		return errors.New("invalid event id")
	}
	p := filepath.Join(b.dir("outbox"), m.ID+".json")
	// The same event ID can be re-emitted after a restart, but never rewritten as a different event.
	old, err := os.ReadFile(p)
	if err == nil {
		var previous Message
		if json.Unmarshal(old, &previous) != nil || previous != m {
			return errors.New("conflicting outbox event")
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return atomicJSON(p, m)
}

type Input struct {
	Message Message
	Digest  string
}

func (b Box) Inputs() ([]Input, error) {
	entries, err := os.ReadDir(b.dir("inbox"))
	if err != nil {
		return nil, err
	}
	var results []Input
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".json") || e.IsDir() || !validID.MatchString(strings.TrimSuffix(name, ".json")) {
			continue
		}
		f, err := os.OpenFile(filepath.Join(b.dir("inbox"), name), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if err != nil {
			continue
		}
		info, err := f.Stat()
		if err != nil || !info.Mode().IsRegular() || info.Size() > MaxFile {
			f.Close()
			continue
		}
		raw, err := io.ReadAll(io.LimitReader(f, MaxFile+1))
		f.Close()
		if err != nil || len(raw) > MaxFile || !utf8.Valid(raw) {
			continue
		}
		var m Message
		d := json.NewDecoder(bytes.NewReader(raw))
		d.DisallowUnknownFields()
		if d.Decode(&m) != nil || d.Decode(new(any)) != io.EOF {
			continue
		}
		if m.ID+".json" != name || m.Version != 1 || m.RunID == "" || m.IssueID == "" || m.Epoch == 0 || len(m.Body) > MaxBody || strings.TrimSpace(m.Body) == "" {
			continue
		}
		if m.Kind != "reply" && m.Kind != "complete" {
			continue
		}
		if m.Status != "" || m.RequestID != "" || m.RemoteID != "" || m.SourceID != "" || m.Revision != "" || m.IssueKey != "" {
			continue
		}
		results = append(results, Input{m, ID(string(raw))})
	}
	return results, nil
}

func atomicJSON(path string, v any) error {
	dir := filepath.Dir(path)
	f, err := os.CreateTemp(dir, ".stage-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	e := json.NewEncoder(f)
	e.SetEscapeHTML(false)
	if err = e.Encode(v); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return fmt.Errorf("replace state: %w", err)
	}
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
