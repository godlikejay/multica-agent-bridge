package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
)

type fake struct {
	mu      sync.Mutex
	posts   []string
	postErr error
}

func (*fake) Task(context.Context) (multica.Task, error) {
	return multica.Task{IssueID: "issue-1", Title: "Synthetic fixture"}, nil
}
func (*fake) Comments(context.Context, string) ([]multica.Comment, error) {
	return []multica.Comment{{ID: "comment-1", Revision: "1", Body: "Incoming fixture"}}, nil
}
func (f *fake) Reply(_ context.Context, issue, parent, body string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, body)
	return "remote-" + body, f.postErr
}
func (f *fake) count() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.posts) }
func run(t *testing.T, box mailbox.Box, r *fake, id string) (chan string, chan error, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	out, errs := make(chan string, 1), make(chan error, 1)
	go func() {
		v, e := (Bridge{Box: box, Remote: r, RunID: id, Poll: 15 * time.Millisecond}).Run(ctx)
		out <- v
		errs <- e
	}()
	return out, errs, cancel
}
func await(t *testing.T, box mailbox.Box, match func(mailbox.Message) bool) mailbox.Message {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		files, _ := filepath.Glob(filepath.Join(box.Root, "outbox", "*.json"))
		for _, f := range files {
			raw, e := os.ReadFile(f)
			if e != nil {
				continue
			}
			var m mailbox.Message
			if json.Unmarshal(raw, &m) == nil && match(m) {
				return m
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("expected outbox event not observed")
	return mailbox.Message{}
}
func input(t *testing.T, box mailbox.Box, base mailbox.Message, id, kind, body string) {
	t.Helper()
	m := mailbox.Message{Version: 1, Kind: kind, ID: id, IssueID: base.IssueID, RunID: base.RunID, Epoch: base.Epoch, Body: body}
	raw, _ := json.Marshal(m)
	dir := filepath.Join(box.Root, "inbox")
	tmp := filepath.Join(dir, ".partial")
	if e := os.WriteFile(tmp, raw, 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Rename(tmp, filepath.Join(dir, id+".json")); e != nil {
		t.Fatal(e)
	}
}
func task(t *testing.T, b mailbox.Box, run string) mailbox.Message {
	return await(t, b, func(m mailbox.Message) bool { return m.Kind == "task" && m.RunID == run })
}
func receipt(t *testing.T, b mailbox.Box, req, status string) mailbox.Message {
	return await(t, b, func(m mailbox.Message) bool { return m.Kind == "receipt" && m.RequestID == req && m.Status == status })
}
func TestManualRoundTripAndRestart(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := new(fake)
	_, errs, stop := run(t, box, r, "run-1")
	a := task(t, box, "run-1")
	await(t, box, func(m mailbox.Message) bool {
		return m.Kind == "comment" && m.Body == "Incoming fixture" && m.SourceID == "comment-1"
	})
	if r.count() != 0 {
		t.Fatal("remote comment triggered action")
	}
	input(t, box, a, "reply-1", "reply", "reply")
	receipt(t, box, "reply-1", "reply_posted")
	input(t, box, a, "reply-1", "reply", "reply")
	time.Sleep(45 * time.Millisecond)
	if r.count() != 1 {
		t.Fatal("duplicate reply sent")
	}
	stop()
	if e := <-errs; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	out, errs, _ := run(t, box, r, "run-2")
	b := task(t, box, "run-2")
	if b.Epoch != a.Epoch+1 {
		t.Fatal("epoch did not advance")
	}
	input(t, box, a, "old-complete", "complete", "stale")
	receipt(t, box, "old-complete", "stale")
	input(t, box, b, "done", "complete", "final")
	receipt(t, box, "done", "completion_requested")
	if result := <-out; result != "final" {
		t.Fatal(result)
	}
	if e := <-errs; e != nil {
		t.Fatal(e)
	}
	if r.count() != 2 {
		t.Fatal("stale or replayed request was posted")
	}
}
func TestUncertainWriteStopsFurtherSubmission(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := &fake{postErr: errors.New("lost response")}
	_, _, stop := run(t, box, r, "run-1")
	defer stop()
	a := task(t, box, "run-1")
	input(t, box, a, "attempt", "reply", "uncertain")
	receipt(t, box, "attempt", "delivery_uncertain")
	input(t, box, a, "finish", "complete", "must not post")
	receipt(t, box, "finish", "blocked_uncertain")
	if r.count() != 1 {
		t.Fatal("post repeated or completion slipped through")
	}
}
func TestSymlinkAndStagedFilesIgnored(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	if e := b.Init(); e != nil {
		t.Fatal(e)
	}
	m := mailbox.Message{Version: 1, Kind: "reply", ID: "test", IssueID: "i", RunID: "r", Epoch: 1, Body: "unsafe"}
	raw, _ := json.Marshal(m)
	in := filepath.Join(b.Root, "inbox")
	if e := os.WriteFile(filepath.Join(in, ".stage"), raw, 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(".stage", filepath.Join(in, "test.json")); e != nil {
		t.Fatal(e)
	}
	found, e := b.Inputs()
	if e != nil || len(found) != 0 {
		t.Fatalf("invalid input parsed: %v %v", found, e)
	}
}
func TestWriterLock(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	if e := b.Init(); e != nil {
		t.Fatal(e)
	}
	release, e := b.Lock()
	if e != nil {
		t.Fatal(e)
	}
	defer release()
	if r, e := b.Lock(); e == nil {
		r()
		t.Fatal("second writer admitted")
	} else if !strings.Contains(e.Error(), "writer") {
		t.Fatal(e)
	}
}

func TestRecoveredSendingIntentDoesNotPostOrComplete(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	if err := box.Init(); err != nil {
		t.Fatal(err)
	}
	base := mailbox.Message{Version: 1, IssueID: "issue-1", RunID: "run-1", Epoch: 1}
	input(t, box, base, "lost", "reply", "server saved this")
	pending, err := box.Inputs()
	if err != nil || len(pending) != 1 {
		t.Fatal("missing test input", err)
	}
	s, err := box.Load(base.IssueID)
	if err != nil {
		t.Fatal(err)
	}
	s.RunID = base.RunID
	s.Epoch = base.Epoch
	rec := base
	rec.Kind = "receipt"
	rec.ID = mailbox.ID("receipt", base.IssueID, "lost")
	rec.RequestID = "lost"
	rec.Status = "sending"
	s.Actions["lost"] = mailbox.Action{Digest: pending[0].Digest, Receipt: rec}
	if err = box.Save(s); err != nil {
		t.Fatal(err)
	}
	// Simulate a server that committed the comment immediately before the Bridge
	// lost the response and exited, leaving only the sending intent on disk.
	r := &fake{posts: []string{"server saved this"}}
	_, errs, stop := run(t, box, r, base.RunID)
	receipt(t, box, "lost", "delivery_uncertain")
	a := task(t, box, base.RunID)
	input(t, box, a, "done-after-crash", "complete", "must not finish")
	receipt(t, box, "done-after-crash", "blocked_uncertain")
	stop()
	if e := <-errs; !errors.Is(e, context.Canceled) {
		t.Fatalf("run did not wait for operator: %v", e)
	}
	if r.count() != 1 {
		t.Fatal("replayed an unknown write or posted a completion")
	}
}
