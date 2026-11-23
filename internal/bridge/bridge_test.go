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
	issue   string
}

func (f *fake) Task(context.Context) (multica.Task, error) {
	issue := f.issue
	if issue == "" {
		issue = "issue-1"
	}
	return multica.Task{IssueID: issue, Title: "Synthetic fixture"}, nil
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
func run(t *testing.T, box mailbox.Box, r Remote, id string) (chan string, chan error, context.CancelFunc) {
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
		files, _ := filepath.Glob(filepath.Join(box.Root, "outbox", box.Connection, box.Workspace, "*.json"))
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
	m := mailbox.Message{Scope: base.Scope, Version: 1, Kind: kind, ID: id, IssueID: base.IssueID, RunID: base.RunID, Epoch: base.Epoch, Body: body}
	raw, _ := json.Marshal(m)
	dir := filepath.Join(box.Root, "inbox", box.Connection, box.Workspace, base.IssueID)
	if e := os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
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
func waitRemoved(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("not removed: %s", filepath.Base(path))
}

func TestAcknowledgedCommentIsNotReplayed(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := new(fake)
	_, errs, stop := run(t, box, r, "run-1")
	a := task(t, box, "run-1")
	c := await(t, box, func(m mailbox.Message) bool { return m.Kind == "comment" })
	outbox := filepath.Join(box.Root, "outbox")
	ackDir := filepath.Join(outbox, ".acks", a.IssueID)
	if err := os.MkdirAll(ackDir, 0700); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(ackDir, c.ID+".ack")
	if err := os.WriteFile(marker, nil, 0600); err != nil {
		t.Fatal(err)
	}
	// A task marker is not eligible for pruning.
	if err := os.WriteFile(filepath.Join(ackDir, a.ID+".ack"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	event := filepath.Join(outbox, c.ID+".json")
	waitRemoved(t, event)
	waitRemoved(t, marker)
	stop()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	_, errs, stop = run(t, box, r, "run-1")
	task(t, box, "run-1")
	input(t, box, a, "reply-after-ack", "reply", "fixture")
	receipt(t, box, "reply-after-ack", "reply_posted")
	if _, err := os.Stat(event); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("acknowledged comment returned: %v", err)
	}
	stop()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestInboxCleanupAfterRestartUsesExactSavedDigest(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := new(fake)
	_, errs, stop := run(t, box, r, "run-1")
	base := task(t, box, "run-1")
	inDir := filepath.Join(box.Root, "inbox", base.IssueID)
	input(t, box, base, "ok", "reply", "delivered reply")
	ok := filepath.Join(inDir, "ok.json")
	receipt(t, box, "ok", "reply_posted")
	waitRemoved(t, ok)
	stop()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	// Simulate restart with a replacement file at the same name.
	input(t, box, base, "ok", "reply", "different bytes")
	_, errs, stop = run(t, box, r, "run-1")
	receipt(t, box, "ok", "id_conflict")
	if _, err := os.Stat(ok); err != nil {
		t.Fatal("conflicting request was discarded", err)
	}
	stop()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	// Then simulate the same request remaining after the recorded post.
	input(t, box, base, "ok", "reply", "delivered reply")
	_, errs, stop = run(t, box, r, "run-1")
	waitRemoved(t, ok)
	stop()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if r.count() != 1 {
		t.Fatal("already delivered request was posted again")
	}
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
	waitRemoved(t, filepath.Join(box.Root, "inbox", b.IssueID, "done.json"))
	if _, err := os.Stat(filepath.Join(box.Root, "inbox", a.IssueID, "old-complete.json")); err != nil {
		t.Fatal("stale request was discarded", err)
	}
	if r.count() != 2 {
		t.Fatal("stale or replayed request was posted")
	}
}

type followUpRemote struct {
	fake
	trigger   string
	parents   []string
	delivered []string
}

func (f *followUpRemote) Task(ctx context.Context) (multica.Task, error) {
	task, err := f.fake.Task(ctx)
	task.TriggerCommentID, task.AllowedParents = f.trigger, f.parents
	task.DeliveredCommentIDs = f.delivered
	return task, err
}

func (*followUpRemote) Comments(context.Context, string) ([]multica.Comment, error) {
	return []multica.Comment{
		{ID: "history", Revision: "1", Body: "Note only"},
		{ID: "follow-up-1", Revision: "1", Body: "Earlier queued message"},
		{ID: "follow-up-2", Revision: "1", Body: "Latest queued message"},
	}, nil
}

func TestFollowUpInputsSurviveRunHandoff(t *testing.T) {
	for _, reason := range []string{"complete", "cancel"} {
		t.Run(reason, func(t *testing.T) {
			box := mailbox.Box{Root: t.TempDir()}
			oldRemote := &followUpRemote{}
			_, oldErr, cancel := run(t, box, oldRemote, "run-1")
			old := task(t, box, "run-1")
			for _, id := range []string{"history", "follow-up-1", "follow-up-2"} {
				await(t, box, func(m mailbox.Message) bool {
					return m.Kind == "comment" && m.RunID == old.RunID && m.SourceID == id && m.RunInput == ""
				})
			}
			if reason == "complete" {
				input(t, box, old, "done-1", "complete", "Finish prior Run")
				receipt(t, box, "done-1", "completion_requested")
				if err := <-oldErr; err != nil {
					t.Fatal(err)
				}
			} else {
				cancel()
				if err := <-oldErr; !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
			}

			remote := &followUpRemote{
				trigger:   "follow-up-2",
				parents:   []string{"follow-up-2", "follow-up-1", "history"},
				delivered: []string{"follow-up-2", "follow-up-1"},
			}
			_, nextErr, stop := run(t, box, remote, "run-2")
			next := task(t, box, "run-2")
			if next.Epoch != old.Epoch+1 {
				t.Fatalf("next epoch is %d; want %d", next.Epoch, old.Epoch+1)
			}
			sourceRoles := map[string]string{"follow-up-1": "coalesced", "follow-up-2": "trigger"}
			for id, role := range sourceRoles {
				await(t, box, func(m mailbox.Message) bool {
					return m.Kind == "comment" && m.RunID == next.RunID && m.Epoch == next.Epoch &&
						m.SourceID == id && m.RunInput == role
				})
			}
			input(t, box, old, "stale-1", "reply", "Must not cross Runs")
			receipt(t, box, "stale-1", "stale")
			input(t, box, next, "reply-2", "reply", "Reply to the follow-up")
			receipt(t, box, "reply-2", "reply_posted")
			if remote.count() != 1 {
				t.Fatal("expected only the new Run's reply")
			}
			stop()
			if err := <-nextErr; !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			_, resumedErr, stopResumed := run(t, box, remote, "run-2")
			input(t, box, next, "reply-3", "reply", "After daemon retry")
			receipt(t, box, "reply-3", "reply_posted")
			stopResumed()
			if err := <-resumedErr; !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}

			files, err := filepath.Glob(filepath.Join(box.Root, "outbox", "*.json"))
			if err != nil {
				t.Fatal(err)
			}
			counts := map[string]int{}
			for _, file := range files {
				var m mailbox.Message
				raw, err := os.ReadFile(file)
				if err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(raw, &m); err != nil {
					t.Fatal(err)
				}
				if m.Kind == "comment" && m.RunID == next.RunID {
					counts[m.SourceID]++
				}
			}
			if len(counts) != 2 || counts["follow-up-1"] != 1 || counts["follow-up-2"] != 1 {
				t.Fatalf("background history or delivery repeated in next Run: %v", counts)
			}
		})
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
	for _, id := range []string{"attempt", "finish"} {
		if _, err := os.Stat(filepath.Join(box.Root, "inbox", a.IssueID, id+".json")); err != nil {
			t.Fatalf("unsettled request %s was discarded: %v", id, err)
		}
	}
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
	in := filepath.Join(b.Root, "inbox", "i")
	if e := os.MkdirAll(in, 0700); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(filepath.Join(in, ".stage"), raw, 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(".stage", filepath.Join(in, "test.json")); e != nil {
		t.Fatal(e)
	}
	found, e := b.Inputs("i")
	if e != nil || len(found) != 0 {
		t.Fatalf("invalid input parsed: %v %v", found, e)
	}
}
func TestWriterLock(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	if e := b.Init(); e != nil {
		t.Fatal(e)
	}
	release, e := b.Lock("i")
	if e != nil {
		t.Fatal(e)
	}
	defer release()
	if r, e := b.Lock("i"); e == nil {
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
	pending, err := box.Inputs(base.IssueID)
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

func TestConcurrentIssuesAndIndependentCompletion(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	ra, rb := &fake{issue: "issue-a"}, &fake{issue: "issue-b"}
	aOut, aErr, cancelA := run(t, b, ra, "run-a")
	bOut, bErr, _ := run(t, b, rb, "run-b")
	a, ob := task(t, b, "run-a"), task(t, b, "run-b")
	if a.Epoch != 1 || ob.Epoch != 1 {
		t.Fatal("epochs must be per issue")
	}
	if a.IssueID == ob.IssueID {
		t.Fatal("expected two distinct issues")
	}
	// Even an identical local request ID in different issue shelves cannot
	// cross-wire receipts, or cause A to complete when B finishes.
	input(t, b, a, "same-id", "reply", "a reply")
	input(t, b, ob, "same-id", "complete", "b final")
	ar := await(t, b, func(m mailbox.Message) bool {
		return m.RequestID == "same-id" && m.IssueID == a.IssueID && m.Status == "reply_posted"
	})
	br := await(t, b, func(m mailbox.Message) bool {
		return m.RequestID == "same-id" && m.IssueID == ob.IssueID && m.Status == "completion_requested"
	})
	if ar.ID == br.ID {
		t.Fatal("receipt IDs are not issue-qualified")
	}
	if s := <-bOut; s != "b final" {
		t.Fatal(s)
	}
	if e := <-bErr; e != nil {
		t.Fatal(e)
	}
	select {
	case <-aOut:
		t.Fatal("the other run ended")
	default:
	}
	input(t, b, a, "continue-a", "reply", "later a reply")
	receipt(t, b, "continue-a", "reply_posted")
	if ra.count() != 2 || rb.count() != 1 {
		t.Fatalf("incorrect posts: A=%d B=%d", ra.count(), rb.count())
	}
	cancelA()
	if e := <-aErr; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
}

func TestSameIssueAdmissionAndRetiredRun(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	r := new(fake)
	_, e1, c1 := run(t, b, r, "r1")
	task(t, b, "r1")
	_, collision, _ := run(t, b, r, "r2")
	if e := <-collision; e == nil {
		t.Fatal("second worker entered same issue")
	}
	c1()
	if e := <-e1; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	_, e2, c2 := run(t, b, r, "r2")
	task(t, b, "r2")
	c2()
	if e := <-e2; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	_, retired, _ := run(t, b, r, "r1")
	if e := <-retired; e == nil || !strings.Contains(e.Error(), "superseded") {
		t.Fatalf("retired run admitted: %v", e)
	}
}

func TestQuarantineFollowsIssueNotOtherRuns(t *testing.T) {
	b := mailbox.Box{Root: t.TempDir()}
	bad := &fake{issue: "a", postErr: errors.New("interrupted delivery")}
	good := &fake{issue: "b"}
	_, aErr, stop := run(t, b, bad, "a1")
	a := task(t, b, "a1")
	input(t, b, a, "uncertain-a", "reply", "maybe sent")
	receipt(t, b, "uncertain-a", "delivery_uncertain")
	stop()
	if e := <-aErr; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	_, _, stop2 := run(t, b, bad, "a2")
	defer stop2()
	gOut, gErr, _ := run(t, b, good, "b1")
	a2, bb := task(t, b, "a2"), task(t, b, "b1")
	input(t, b, a2, "replacement-a", "complete", "not safe")
	input(t, b, bb, "finish-b", "complete", "unrelated")
	receipt(t, b, "replacement-a", "blocked_uncertain")
	receipt(t, b, "finish-b", "completion_requested")
	if got := <-gOut; got != "unrelated" {
		t.Fatal(got)
	}
	if e := <-gErr; e != nil {
		t.Fatal(e)
	}
	if bad.count() != 1 || good.count() != 1 {
		t.Fatal("uncertain replay or blocked a separate issue")
	}
}

type recoveryRemote struct {
	fake
	snapshot []multica.Comment
	readErr  error
	reads    int
}

func (r *recoveryRemote) Task(ctx context.Context) (multica.Task, error) {
	task, err := r.fake.Task(ctx)
	task.AgentID = "agent-a"
	return task, err
}
func (r *recoveryRemote) Comments(_ context.Context, _ string) ([]multica.Comment, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.reads++
	return append([]multica.Comment(nil), r.snapshot...), r.readErr
}
func (r *recoveryRemote) snapshotAfter(comments []multica.Comment, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.snapshot = comments
	r.readErr = err
}
func (r *recoveryRemote) totalReads() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.reads
}

func TestReadRecoveryIsBoundedAndNeverPostsWhileDegraded(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	bad := &recoveryRemote{readErr: &multica.Error{Code: "transport"}}
	_, errA, stop := run(t, box, bad, "run-a")
	defer stop()
	a := task(t, box, "run-a")
	await(t, box, func(m mailbox.Message) bool {
		return m.Kind == "sync" && m.Status == "retrying" && m.Revision == "1" && m.RunID == a.RunID
	})
	input(t, box, a, "during-retry", "reply", "approved reply")
	good := &fake{issue: "other"}
	goodOut, goodErr, _ := run(t, box, good, "run-b")
	ob := task(t, box, "run-b")
	input(t, box, ob, "done-b", "complete", "B proceeds")
	if got := <-goodOut; got != "B proceeds" {
		t.Fatal(got)
	}
	if err := <-goodErr; err != nil {
		t.Fatal(err)
	}
	if bad.count() != 0 {
		t.Fatal("posted during failed comment synchronization")
	}
	bad.snapshotAfter(nil, nil)
	await(t, box, func(m mailbox.Message) bool {
		return m.Kind == "sync" && m.Status == "recovered" && m.Revision == "2" && m.RunID == a.RunID
	})
	receipt(t, box, "during-retry", "reply_posted")
	if bad.count() != 1 {
		t.Fatal("approved queued reply not posted exactly once")
	}
	stop()
	if e := <-errA; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}

	for _, tc := range []struct {
		name  string
		fault *multica.Error
		reads int
	}{
		{"temporary-exhaustion", &multica.Error{Code: "rate_limited", Status: 429}, maxReadFailures},
		{"invalid-terminal", &multica.Error{Code: "unauthorized", Status: 401}, 1},
		{"incomplete-terminal", &multica.Error{Code: "comments_truncated"}, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := &recoveryRemote{readErr: tc.fault}
			b := mailbox.Box{Root: t.TempDir()}
			_, errs, cancel := run(t, b, r, "bounded")
			defer cancel()
			if got := <-errs; !errors.Is(got, tc.fault) || r.totalReads() != tc.reads {
				t.Fatalf("failure=%v reads=%d", got, r.totalReads())
			}
			await(t, b, func(m mailbox.Message) bool {
				return m.Kind == "sync" && m.Status == "failed"
			})
			if r.count() != 0 {
				t.Fatal("unexpected write")
			}
		})
	}
}

func TestLostFinalCommentReconcilesAcrossReplacementWithoutCompleting(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r1 := &recoveryRemote{fake: fake{postErr: errors.New("committed but response lost")}}
	_, err1, stop1 := run(t, box, r1, "original")
	a := task(t, box, "original")
	input(t, box, a, "maybe-final", "complete", "old final summary")
	original := receipt(t, box, "maybe-final", "delivery_uncertain")
	stop1()
	if e := <-err1; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	ledger, err := box.Load(a.IssueID)
	if err != nil {
		t.Fatal(err)
	}
	intent := ledger.Actions["maybe-final"].Intent
	if intent == nil || intent.AgentID != "agent-a" || !strings.HasPrefix(intent.Marker, "<!-- multica-bridge:") {
		t.Fatalf("missing durable exact correlation: %+v", intent)
	}
	payload := r1.posts[0]
	if !strings.HasSuffix(payload, "\n\n"+intent.Marker) || mailbox.ID(payload) != intent.PayloadDigest {
		t.Fatal("intent mismatch")
	}
	exact := multica.Comment{
		ID: "actual-comment", IssueID: a.IssueID, AuthorType: "agent", AuthorID: "agent-a",
		SourceTaskID: a.RunID, Body: payload, Revision: "1",
	}
	r2 := &recoveryRemote{}
	wrong := exact
	wrong.SourceTaskID = "replacement"
	r2.snapshotAfter([]multica.Comment{wrong}, nil)
	out2, err2, stop2 := run(t, box, r2, "replacement")
	defer stop2()
	b := task(t, box, "replacement")
	input(t, box, b, "blocked", "reply", "new approval while blocked")
	receipt(t, box, "blocked", "blocked_uncertain")

	duplicate := exact
	duplicate.ID = "another-exact-comment"
	r2.snapshotAfter([]multica.Comment{exact, duplicate}, nil)
	input(t, box, b, "still-blocked", "reply", "ambiguous matches are unsafe")
	receipt(t, box, "still-blocked", "blocked_uncertain")

	r2.snapshotAfter([]multica.Comment{exact}, nil)
	confirmed := receipt(t, box, "maybe-final", "delivery_confirmed")
	waitRemoved(t, filepath.Join(box.Root, "inbox", a.IssueID, "maybe-final.json"))
	if confirmed.ID == original.ID || confirmed.RunID != a.RunID || confirmed.Epoch != a.Epoch || confirmed.RemoteID != exact.ID {
		t.Fatalf("invalid reconciliation receipt: %+v", confirmed)
	}
	again := receipt(t, box, "maybe-final", "delivery_uncertain")
	if original != again {
		t.Fatal("original immutable receipt changed")
	}
	select {
	case <-out2:
		t.Fatal("reconciliation improperly completed replacement run")
	default:
	}
	input(t, box, b, "new-final", "complete", "new approval")
	receipt(t, box, "new-final", "completion_requested")
	if got := <-out2; got != "new approval" {
		t.Fatal(got)
	}
	if e := <-err2; e != nil {
		t.Fatal(e)
	}
	if r1.count() != 1 || r2.count() != 1 {
		t.Fatal("original write repeated or new approval did not submit once")
	}
}

type preflightRemote struct {
	recoveryRemote
	muPreflight  sync.Mutex
	safeFailures int
}

func (r *preflightRemote) Reply(ctx context.Context, issue, parent, body string) (string, error) {
	r.muPreflight.Lock()
	if r.safeFailures > 0 {
		r.safeFailures--
		r.muPreflight.Unlock()
		return "", &multica.Error{Code: "transport", BeforePost: true}
	}
	r.muPreflight.Unlock()
	return r.fake.Reply(ctx, issue, parent, body)
}

func TestTemporaryPresendLookupDoesNotQuarantineOrDuplicate(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := &preflightRemote{safeFailures: 2}
	out, errs, cancel := run(t, box, r, "preflight")
	defer cancel()
	a := task(t, box, "preflight")
	approved := strings.Repeat("x", mailbox.MaxBody)
	input(t, box, a, "preflight-complete", "complete", approved)
	if got := <-out; got != approved {
		t.Fatalf("completion length %d", len(got))
	}
	if err := <-errs; err != nil {
		t.Fatal(err)
	}
	if r.count() != 1 || len(r.posts[0]) <= mailbox.MaxBody || !strings.HasPrefix(r.posts[0], approved+"\n\n<!-- ") {
		t.Fatal("32 KiB action or safe preflight retry failed")
	}
	s, err := box.Load(a.IssueID)
	if err != nil || s.Actions["preflight-complete"].Receipt.Status != "completion_requested" || s.LastSyncedAt == "" {
		t.Fatalf("incorrect persisted result: %v", err)
	}
}

func TestIndependentEndpointsWithIdenticalRemoteIDs(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "inbox"), 0700); err != nil {
		t.Fatal(err)
	}
	a := mailbox.Box{Root: root, Connection: "cloud", Workspace: "ws"}
	b := mailbox.Box{Root: root, Connection: "work", Workspace: "ws"}
	ra, rb := new(fake), new(fake)
	_, aErr, stopA := run(t, a, ra, "same-run")
	bOut, bErr, _ := run(t, b, rb, "same-run")
	ta, tb := task(t, a, "same-run"), task(t, b, "same-run")
	if ta.ID != tb.ID || ta.IssueID != tb.IssueID || ta.Scope == tb.Scope {
		t.Fatal("expected identical remote IDs in two distinct local scopes")
	}

	// Move an A-bound action into the B inbox without changing its payload.
	input(t, b, ta, "foreign", "complete", "never post this")
	input(t, b, tb, "valid-b", "reply", "first B reply")
	receipt(t, b, "valid-b", "reply_posted")
	if ra.count() != 0 || rb.count() != 1 {
		t.Fatal("a copied request crossed connections")
	}

	stopA()
	if err := <-aErr; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	input(t, b, tb, "done-b", "complete", "B final while A stopped")
	receipt(t, b, "done-b", "completion_requested")
	if got, err := <-bOut, <-bErr; err != nil || got != "B final while A stopped" || rb.count() != 2 {
		t.Fatalf("healthy worker did not finish independently: %q, %v, posts=%d", got, err, rb.count())
	}

	_, newAErr, stopNewA := run(t, a, ra, "replacement-run")
	ta2 := task(t, a, "replacement-run")
	if ta2.Epoch != ta.Epoch+1 {
		t.Fatal("A did not preserve its own ledger across restart")
	}
	input(t, a, ta2, "fresh-a", "reply", "A recovered")
	receipt(t, a, "fresh-a", "reply_posted")
	stopNewA()
	if err := <-newAErr; !errors.Is(err, context.Canceled) || ra.count() != 1 {
		t.Fatalf("unexpected A restart outcome: %v, posts=%d", err, ra.count())
	}
}
