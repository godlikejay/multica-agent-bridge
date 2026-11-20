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
