package mailbox

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func fixtureBox(t *testing.T) Box {
	t.Helper()
	b := Box{Root: t.TempDir()}
	if err := b.Init(); err != nil {
		t.Fatal(err)
	}
	return b
}

func TestPerIssueWriterLock(t *testing.T) {
	b := fixtureBox(t)
	unlockA, err := b.Lock("issue-a")
	if err != nil {
		t.Fatal(err)
	}
	defer unlockA()
	if u, err := b.Lock("issue-a"); err == nil {
		u()
		t.Fatal("same Issue acquired twice")
	}
	unlockB, err := b.Lock("issue-b")
	if err != nil {
		t.Fatal(err)
	}
	defer unlockB()
	if u, err := b.Lock("../escape"); err == nil {
		u()
		t.Fatal("unsafe Issue id accepted")
	}
}

func TestIssueShelves(t *testing.T) {
	b := fixtureBox(t)
	if in, err := b.Inputs("absent"); err != nil || len(in) != 0 {
		t.Fatalf("missing shelf: %v %v", in, err)
	}
	if _, err := os.Lstat(filepath.Join(b.dir("inbox"), "absent")); !os.IsNotExist(err) {
		t.Fatalf("shelf should not be created: %v", err)
	}
	if _, err := b.Inputs("../escape"); err == nil {
		t.Fatal("unsafe Issue id accepted")
	}
	for _, id := range []string{"issue-a", "issue-b"} {
		if err := os.Mkdir(filepath.Join(b.dir("inbox"), id), 0700); err != nil {
			t.Fatal(err)
		}
	}
	write := func(shelf, id, issue string) {
		t.Helper()
		m := Message{Version: 1, Kind: "reply", ID: id, IssueID: issue, RunID: "run", Epoch: 1, Body: "synthetic"}
		raw, _ := json.Marshal(m)
		if err := os.WriteFile(filepath.Join(b.dir("inbox"), shelf, id+".json"), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write("", "legacy", "issue-a")
	write("issue-a", "b", "issue-a")
	write("issue-a", "a", "issue-a")
	write("issue-a", "foreign", "issue-b")
	write("issue-b", "c", "issue-b")
	if err := os.Symlink("issue-b", filepath.Join(b.dir("inbox"), "symlink")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("../issue-b/c.json", filepath.Join(b.dir("inbox"), "issue-a", "c.json")); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		issue string
		ids   []string
	}{{"issue-a", []string{"a", "b"}}, {"issue-b", []string{"c"}}} {
		in, err := b.Inputs(test.issue)
		if err != nil || len(in) != len(test.ids) {
			t.Fatalf("inputs for %s: %v %v", test.issue, in, err)
		}
		for i, id := range test.ids {
			if in[i].Message.ID != id {
				t.Fatalf("input %d: got %s want %s", i, in[i].Message.ID, id)
			}
		}
	}
	if _, err := b.Inputs("symlink"); err == nil {
		t.Fatal("linked shelf accepted")
	}
}

func TestOutboxNoClobber(t *testing.T) {
	b := fixtureBox(t)
	first := Message{Version: 1, Kind: "task", ID: "same", IssueID: "issue-a"}
	second := first
	second.IssueID = "issue-b"
	var wg sync.WaitGroup
	results := make(chan Message, 2)
	for _, m := range []Message{first, second} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if b.Emit(m) == nil {
				results <- m
			}
		}()
	}
	wg.Wait()
	close(results)
	if len(results) != 1 {
		t.Fatalf("published %d conflicting messages", len(results))
	}
	published := <-results
	raw, err := os.ReadFile(filepath.Join(b.dir("outbox"), "same.json"))
	if err != nil {
		t.Fatal(err)
	}
	var actual Message
	if err := json.Unmarshal(raw, &actual); err != nil || actual != published {
		t.Fatalf("outbox contents %v %v", actual, err)
	}
	if err := b.Emit(published); err != nil {
		t.Fatalf("matching replay: %v", err)
	}
}

func TestRetiredRunsStateCompatibility(t *testing.T) {
	b := fixtureBox(t)
	original := []byte(`{"version":1,"issue_id":"issue-a","run_id":"old","epoch":1,"seen":{},"actions":{}}`)
	if err := os.WriteFile(b.statePath("issue-a"), original, 0600); err != nil {
		t.Fatal(err)
	}
	s, err := b.Load("issue-a")
	if err != nil || s.RetiredRuns != nil {
		t.Fatalf("legacy load: %+v %v", s, err)
	}
	s.RetiredRuns = map[string]bool{"old": true}
	s.RunID = "new"
	s.Epoch++
	if err := b.Save(s); err != nil {
		t.Fatal(err)
	}
	s, err = b.Load("issue-a")
	if err != nil || !s.RetiredRuns["old"] || s.RunID != "new" || s.Epoch != 2 {
		t.Fatalf("updated load: %+v %v", s, err)
	}
}

func TestMailboxConnectionScope(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "inbox"), 0700); err != nil {
		t.Fatal(err)
	}
	a := Box{Root: root, Connection: "cloud", Workspace: "workspace-a"}
	b := Box{Root: root, Connection: "work", Workspace: "workspace-a"}
	c := Box{Root: root, Connection: "cloud", Workspace: "workspace-b"}
	var releases []func()
	defer func() {
		for _, release := range releases {
			release()
		}
	}()
	for _, box := range []Box{a, b, c} {
		if err := box.Init(); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(box.dir("inbox")); !os.IsNotExist(err) {
			t.Fatalf("operator did not create the inbox: %v", err)
		}
		if inputs, err := box.Inputs("same"); err != nil || len(inputs) != 0 {
			t.Fatalf("missing scoped inbox: %v, %v", inputs, err)
		}
		release, err := box.Lock("same")
		if err != nil {
			t.Fatal(err)
		}
		releases = append(releases, release)
		if err := box.Emit(Message{Version: 1, ID: "same", IssueID: "same"}); err != nil {
			t.Fatal(err)
		}
		raw, err := os.ReadFile(filepath.Join(box.dir("outbox"), "same.json"))
		var e Message
		if err != nil || json.Unmarshal(raw, &e) != nil || e.Scope != box.scope() {
			t.Fatalf("event missing local scope: %s (%v)", raw, err)
		}
		state, err := box.Load("same")
		if err != nil || state.Scope != box.scope() {
			t.Fatalf("cannot load fresh scoped state: %v, %v", state, err)
		}
		if err := box.Save(state); err != nil {
			t.Fatal(err)
		}
	}
	// Even identical Issue, Run, and request IDs cannot replay across scopes.
	shelf := filepath.Join(a.dir("inbox"), "same")
	if err := os.MkdirAll(shelf, 0700); err != nil {
		t.Fatal(err)
	}
	for id, scope := range map[string]Scope{"right": a.scope(), "other-connection": b.scope(), "other-workspace": c.scope(), "legacy": {}} {
		m := Message{Scope: scope, Version: 1, ID: id, Kind: "reply", IssueID: "same", RunID: "same", Epoch: 1, Body: "synthetic"}
		raw, _ := json.Marshal(m)
		if err := os.WriteFile(filepath.Join(shelf, id+".json"), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	inputs, err := a.Inputs("same")
	if err != nil || len(inputs) != 1 || inputs[0].Message.ID != "right" {
		t.Fatalf("cross-scope input: %v, %v", inputs, err)
	}
	raw, err := os.ReadFile(b.statePath("same"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(a.statePath("same"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Load("same"); err == nil {
		t.Fatal("copied state accepted in the wrong scope")
	}
}

func TestMailboxScopeCannotBePartialOrSymlinked(t *testing.T) {
	root := t.TempDir()
	for _, box := range []Box{{Root: root, Connection: "cloud"}, {Root: root, Connection: "../bad", Workspace: "workspace"}} {
		if err := box.Init(); err == nil {
			t.Fatal("invalid scope accepted")
		}
	}
	if err := os.Mkdir(filepath.Join(root, "inbox"), 0700); err != nil {
		t.Fatal(err)
	}
	a := Box{Root: root, Connection: "cloud", Workspace: "workspace"}
	if err := a.Init(); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(root, "inbox", "cloud")); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Inputs("same"); err == nil {
		t.Fatal("symlinked inbox connection accepted")
	}
}
