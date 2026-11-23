// Package mailbox implements the per-Issue single-writer file protocol.
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
	"reflect"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"unicode/utf8"
)

const MaxFile = 256 * 1024
const MaxBody = 32 * 1024
const maxState = 16 * 1024 * 1024

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$`)

type Scope struct {
	ConnectionID string `json:"connection_id,omitempty"`
	WorkspaceID  string `json:"workspace_id,omitempty"`
}

type Message struct {
	Scope
	Version      int       `json:"version"`
	Kind         string    `json:"kind"`
	ID           string    `json:"id"`
	IssueID      string    `json:"issue_id"`
	RunID        string    `json:"run_id"`
	Epoch        uint64    `json:"epoch"`
	Body         string    `json:"body,omitempty"`
	IssueKey     string    `json:"issue_key,omitempty"`
	SourceID     string    `json:"source_id,omitempty"`
	Revision     string    `json:"revision,omitempty"`
	RunInput     string    `json:"run_input,omitempty"`
	ParentID     string    `json:"parent_id,omitempty"`
	RequestID    string    `json:"request_id,omitempty"`
	Status       string    `json:"status,omitempty"`
	RemoteID     string    `json:"remote_id,omitempty"`
	ObservedAt   string    `json:"observed_at,omitempty"`
	LastSyncedAt string    `json:"last_synced_at,omitempty"`
	InputIDs     *[]string `json:"input_ids,omitempty"`
}
type State struct {
	Scope
	Version      int               `json:"version"`
	IssueID      string            `json:"issue_id"`
	RunID        string            `json:"run_id"`
	Epoch        uint64            `json:"epoch"`
	TaskEvent    *Message          `json:"task_event,omitempty"`
	Completion   string            `json:"completion,omitempty"`
	Seen         map[string]string `json:"seen"`
	Actions      map[string]Action `json:"actions"`
	RetiredRuns  map[string]bool   `json:"retired_runs,omitempty"`
	Sync         *Message          `json:"sync,omitempty"`
	SyncSequence uint64            `json:"sync_sequence,omitempty"`
	LastSyncedAt string            `json:"last_synced_at,omitempty"`
}
type Action struct {
	Digest         string      `json:"digest"`
	Receipt        Message     `json:"receipt"`
	Intent         *SendIntent `json:"intent,omitempty"`
	Reconciliation *Message    `json:"reconciliation,omitempty"`
}
type SendIntent struct {
	AgentID       string `json:"agent_id"`
	ParentID      string `json:"parent_id,omitempty"`
	Marker        string `json:"marker"`
	PayloadDigest string `json:"payload_digest"`
}
type Box struct {
	Root, Connection, Workspace string
}

func (b Box) scope() Scope {
	return Scope{ConnectionID: b.Connection, WorkspaceID: b.Workspace}
}

func ID(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])
}
func (b Box) Init() error {
	if !filepath.IsAbs(b.Root) {
		return errors.New("BRIDGE_ROOT must be an absolute path")
	}
	if b.scope() != (Scope{}) && (!validID.MatchString(b.Connection) || !validID.MatchString(b.Workspace)) {
		return errors.New("invalid connection or workspace mailbox scope")
	}
	if err := os.MkdirAll(b.Root, 0700); err != nil {
		return err
	}
	if err := realDir(b.Root); err != nil {
		return err
	}
	for _, kind := range []string{"inbox", "outbox", "state"} {
		base := filepath.Join(b.Root, kind)
		if kind == "inbox" && b.Connection != "" {
			// The operator creates Inbox scopes when publishing requests.
			if err := realDir(base); err != nil {
				return err
			}
			continue
		}
		if err := makeDir(base); err != nil {
			return err
		}
		if b.Connection != "" {
			if err := makeDir(filepath.Join(base, b.Connection)); err != nil {
				return err
			}
			if err := makeDir(b.dir(kind)); err != nil {
				return err
			}
		}
	}
	return nil
}

func realDir(path string) error {
	f, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !f.IsDir() {
		return errors.New("mailbox paths must be real directories")
	}
	return nil
}

func makeDir(path string) error {
	if err := os.Mkdir(path, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	return realDir(path)
}

func (b Box) dir(kind string) string {
	return filepath.Join(b.Root, kind, b.Connection, b.Workspace)
}

func (b Box) Lock(issueID string) (func(), error) {
	if !validID.MatchString(issueID) {
		return nil, errors.New("invalid inbox issue id")
	}
	f, err := os.OpenFile(filepath.Join(b.dir("state"), ID("issue", issueID)+".lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return nil, err
	}
	if st, err := f.Stat(); err != nil || !st.Mode().IsRegular() {
		f.Close()
		return nil, errors.New("issue writer lock must be a regular file")
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, errors.New("another Bridge writer is active for this issue")
	}
	return func() { f.Close() }, nil
}
func (b Box) statePath(issue string) string {
	return filepath.Join(b.dir("state"), ID("issue", issue)+".json")
}
func (b Box) Load(issue string) (State, error) {
	s := State{Scope: b.scope(), Version: 1, IssueID: issue, Seen: map[string]string{}, Actions: map[string]Action{}}
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
	// Decode into an empty value so missing scope cannot inherit the expected scope.
	var saved State
	if err = json.Unmarshal(raw, &saved); err != nil {
		return s, errors.New("invalid saved state")
	}
	s = saved
	if s.Scope != b.scope() || s.Version != 1 || s.IssueID != issue || s.Seen == nil || s.Actions == nil {
		return s, errors.New("saved state identity or version mismatch")
	}
	return s, nil
}
func (b Box) Save(s State) error {
	if s.Scope != b.scope() {
		return errors.New("invalid saved state scope")
	}
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
	if m.Scope != (Scope{}) && m.Scope != b.scope() {
		return errors.New("invalid outbox scope")
	}
	m.Scope = b.scope()
	if !validID.MatchString(m.ID) {
		return errors.New("invalid event id")
	}
	p := filepath.Join(b.dir("outbox"), m.ID+".json")
	// The same event ID can be re-emitted after a restart, but never rewritten as a different event.
	if err := writeJSON(p, m, false); !errors.Is(err, os.ErrExist) {
		return err
	}
	old, err := os.ReadFile(p)
	if err != nil {
		return err
	}
	var previous Message
	if json.Unmarshal(old, &previous) != nil || !reflect.DeepEqual(previous, m) {
		return errors.New("conflicting outbox event")
	}
	return syncDir(b.dir("outbox"))
}

type Input struct {
	Message Message
	Digest  string
}

func (b Box) Inputs(issueID string) ([]Input, error) {
	if !validID.MatchString(issueID) {
		return nil, errors.New("invalid inbox issue id")
	}
	// The mailbox root and inbox container belong to a trusted local operator
	// and must not be renamed/replaced while the bridge is active. Open the
	// shelf and each request without following leaf symlinks. Check that the
	// shelf is unchanged around each request open, failing closed on a swap.
	if b.Connection != "" {
		for _, p := range []string{filepath.Join(b.Root, "inbox", b.Connection), b.dir("inbox")} {
			if err := realDir(p); errors.Is(err, os.ErrNotExist) {
				return nil, nil
			} else if err != nil {
				return nil, err
			}
		}
	}
	shelfPath := filepath.Join(b.dir("inbox"), issueID)
	shelf, err := os.OpenFile(shelfPath, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer shelf.Close()
	shelfInfo, err := shelf.Stat()
	if err != nil {
		return nil, err
	}
	unchanged := func() bool {
		current, err := os.Lstat(shelfPath)
		return err == nil && current.IsDir() && os.SameFile(shelfInfo, current)
	}
	entries, err := shelf.ReadDir(-1)
	if err != nil {
		return nil, err
	}
	if !unchanged() {
		return nil, errors.New("inbox issue shelf changed during scan")
	}
	// Preserve the order formerly supplied by os.ReadDir.
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	var results []Input
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".json") || e.IsDir() || !validID.MatchString(strings.TrimSuffix(name, ".json")) {
			continue
		}
		f, err := os.OpenFile(filepath.Join(shelfPath, name), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if !unchanged() {
			if err == nil {
				f.Close()
			}
			return nil, errors.New("inbox issue shelf changed during scan")
		}
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
		if m.Scope != b.scope() || m.ID+".json" != name || m.Version != 1 || m.RunID == "" || m.IssueID != issueID || m.Epoch == 0 || len(m.Body) > MaxBody || strings.TrimSpace(m.Body) == "" {
			continue
		}
		if m.Kind != "reply" && m.Kind != "complete" {
			continue
		}
		if m.Status != "" || m.RequestID != "" || m.RemoteID != "" || m.SourceID != "" || m.Revision != "" || m.RunInput != "" || m.IssueKey != "" || m.ObservedAt != "" || m.LastSyncedAt != "" || m.InputIDs != nil {
			continue
		}
		results = append(results, Input{m, ID(string(raw))})
	}
	return results, nil
}

// RemoveDelivered removes only the bytes the bridge actually accepted. The
// Inbox directory lock is shared with the host submitter, not held during I/O
// to Multica. An absent or replaced request is not an instruction to delete.
func (b Box) RemoveDelivered(issueID, requestID, digest string) error {
	if !validID.MatchString(issueID) || !validID.MatchString(requestID) || digest == "" {
		return errors.New("invalid delivered request identity")
	}
	dir := filepath.Join(b.dir("inbox"), issueID)
	shelf, err := os.OpenFile(dir, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer shelf.Close()
	if err := syscall.Flock(int(shelf.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return err
	}
	defer root.Close()
	current, err := root.Stat(".")
	if err != nil {
		return err
	}
	locked, err := shelf.Stat()
	if err != nil || !os.SameFile(current, locked) {
		return errors.New("inbox issue shelf changed before cleanup")
	}
	name := requestID + ".json"
	info, err := root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > MaxFile {
		return nil
	}
	f, err := os.OpenFile(filepath.Join(dir, name), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil {
		return err
	}
	if !os.SameFile(info, opened) {
		return nil
	}
	raw, err := io.ReadAll(io.LimitReader(f, MaxFile+1))
	if err != nil {
		return err
	}
	if len(raw) > MaxFile || ID(string(raw)) != digest {
		return nil
	}
	current, err = root.Lstat(name)
	if err != nil || !os.SameFile(info, current) {
		return err
	}
	if err := root.Remove(name); err != nil {
		return err
	}
	return shelf.Sync()
}

// PruneAcknowledgedComments is called only after Seen is durable, so a
// crash after unlinking a comment cannot cause it to be delivered again.
func (b Box) PruneAcknowledgedComments(issueID string, seen map[string]string) error {
	if !validID.MatchString(issueID) {
		return errors.New("invalid acknowledgment issue id")
	}
	base := filepath.Join(b.dir("outbox"), ".acks")
	for _, dir := range []string{base, filepath.Join(base, issueID)} {
		if err := realDir(dir); errors.Is(err, os.ErrNotExist) {
			return nil
		} else if err != nil {
			return err
		}
	}
	root, err := os.OpenRoot(b.dir("outbox"))
	if err != nil {
		return err
	}
	defer root.Close()
	dir := filepath.Join(".acks", issueID)
	entries, err := os.ReadDir(filepath.Join(b.dir("outbox"), dir))
	if err != nil {
		return err
	}
	if len(entries) == 0 {
		return nil
	}
	eligible := make(map[string]bool, len(seen))
	for _, id := range seen {
		eligible[id] = true
	}
	for _, e := range entries {
		id, ok := strings.CutSuffix(e.Name(), ".ack")
		if !ok || !eligible[id] || !validID.MatchString(id) || e.Type() != 0 {
			continue
		}
		name := id + ".json"
		if err := root.Remove(name); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err := syncDir(b.dir("outbox")); err != nil {
			return err
		}
		if err := root.Remove(filepath.Join(dir, e.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return syncDir(filepath.Join(b.dir("outbox"), dir))
}

func atomicJSON(path string, v any) error {
	return writeJSON(path, v, true)
}

func writeJSON(path string, v any, replace bool) error {
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
	if replace {
		if err = os.Rename(f.Name(), path); err != nil {
			return fmt.Errorf("replace state: %w", err)
		}
	} else {
		// A hard link publishes the complete staged event atomically without
		// overwriting any event another Issue writer may already have emitted.
		if err = os.Link(f.Name(), path); err != nil {
			return fmt.Errorf("publish event: %w", err)
		}
	}
	return syncDir(dir)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
