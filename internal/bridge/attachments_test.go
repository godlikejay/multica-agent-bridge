package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
)

type attachmentFixture struct {
	fake
	mu         sync.Mutex
	revision   string
	fetchStart chan struct{}
}

func (f *attachmentFixture) Task(ctx context.Context) (multica.Task, error) {
	t, err := f.fake.Task(ctx)
	t.TriggerCommentID = "visible"
	t.DeliveredCommentIDs = []string{"visible"}
	return t, err
}
func (f *attachmentFixture) Comments(context.Context, string) ([]multica.Comment, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return []multica.Comment{{ID: "visible", Revision: f.revision, Body: "claim input"}, {ID: "later", Revision: "1", Body: "queued"}, {ID: "noted", Body: "/note [mab-context] opted in"}}, nil
}
func (f *attachmentFixture) Attachments(context.Context, string) ([]multica.Attachment, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	rows := []multica.Attachment{{URL: "/api/attachments/issue/download"}, {URL: "/api/attachments/queued/download", CommentID: "later"}, {URL: "/api/attachments/note/download", CommentID: "noted"}}
	if f.revision != "2" {
		rows = append(rows, multica.Attachment{URL: "/api/attachments/input/download", CommentID: "visible"})
	}
	return rows, nil
}
func (f *attachmentFixture) FetchAttachment(ctx context.Context, issue, url string, dst io.Writer) error {
	if issue != "issue-1" || url != "/api/attachments/queued/download" {
		return errors.New("outside synthetic scope")
	}
	_, err := io.WriteString(dst, "\x89PNG\r\n\x1a\nfixture")
	if f.fetchStart != nil {
		close(f.fetchStart)
		<-ctx.Done()
		return ctx.Err()
	}
	return err
}
func attachmentInput(t *testing.T, box mailbox.Box, b mailbox.Message, id string) {
	t.Helper()
	in := b
	in.Kind = "attachment_fetch"
	in.ID = id
	in.Body, in.IssueKey = "", ""
	in.AttachmentURL = "/api/attachments/queued/download"
	dir := filepath.Join(box.Root, "inbox", box.Connection, box.Workspace, b.IssueID)
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(in)
	f := filepath.Join(dir, ".attachment.tmp")
	if err := os.WriteFile(f, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(f, filepath.Join(dir, id+".json")); err != nil {
		t.Fatal(err)
	}
}
func awaitCatalog(t *testing.T, dir string, match func(mailbox.AttachmentCatalog) bool) mailbox.AttachmentCatalog {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(filepath.Join(dir, "catalog.json"))
		var c mailbox.AttachmentCatalog
		if err == nil && json.Unmarshal(raw, &c) == nil && match(c) {
			return c
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("catalog not observed")
	return mailbox.AttachmentCatalog{}
}

func TestAttachmentCatalogRevocationAndExactRunCleanup(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir(), Connection: "conn", Workspace: "workspace"}
	if err := os.Mkdir(filepath.Join(box.Root, "inbox"), 0700); err != nil {
		t.Fatal(err)
	}
	r := &attachmentFixture{revision: "1"}
	_, errs, cancel := run(t, box, r, "run-1")
	base := task(t, box, "run-1")
	dir := filepath.Join(box.Root, "outbox", "conn", "workspace", "attachments", "issue-1", "run-1", "1")
	c := awaitCatalog(t, dir, func(c mailbox.AttachmentCatalog) bool { return c.Status == "ready" })
	if len(c.Issue) != 1 || len(c.Comments["visible"]) != 1 || len(c.Comments["noted"]) != 1 || len(c.Comments["later"]) != 0 || c.Scope != base.Scope {
		t.Fatalf("unexpected scoped manifest %+v", c)
	}
	attachmentInput(t, box, base, "get-file")
	result := await(t, box, func(m mailbox.Message) bool { return m.RequestID == "get-file" })
	if result.Status != "attachment_ready" {
		t.Fatalf("unexpected result: %#v", result)
	}
	data, err := os.ReadFile(filepath.Join(box.Root, filepath.FromSlash(result.AttachmentPath)))
	if err != nil || !strings.HasSuffix(result.AttachmentPath, "get-file.png") || string(data) != "\x89PNG\r\n\x1a\nfixture" {
		t.Fatalf("file %q: %v", result.AttachmentPath, err)
	}
	if r.count() != 0 {
		t.Fatal("attachment posted a remote reply")
	}
	r.mu.Lock()
	r.revision = "2"
	r.mu.Unlock()
	awaitCatalog(t, dir, func(c mailbox.AttachmentCatalog) bool { return c.Status == "ready" && len(c.Comments["visible"]) == 0 })
	neighbor := filepath.Join(filepath.Dir(filepath.Dir(dir)), "unrelated-run", "1")
	if err := os.MkdirAll(neighbor, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(neighbor, "untouched"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	cancel()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := os.Lstat(dir); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("terminal run attachment directory remains")
	}
	if _, err := os.Stat(filepath.Join(neighbor, "untouched")); err != nil {
		t.Fatal("different run was cleaned", err)
	}
}

func TestCancelledAttachmentNeverPublishes(t *testing.T) {
	box := mailbox.Box{Root: t.TempDir()}
	r := &attachmentFixture{revision: "1", fetchStart: make(chan struct{})}
	_, errs, cancel := run(t, box, r, "run-1")
	attachmentInput(t, box, task(t, box, "run-1"), "get-file")
	select {
	case <-r.fetchStart:
	case <-time.After(2 * time.Second):
		t.Fatal("fetch never started")
	}
	cancel()
	if err := <-errs; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	path := filepath.Join(box.Root, "outbox", "attachments", "issue-1", "run-1", "1")
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("cancelled Run's staging spilled", err)
	}
	files, _ := filepath.Glob(filepath.Join(box.Root, "outbox", ".attachment-staging", "*"))
	if len(files) != 0 {
		t.Fatal("staged bytes left behind")
	}
	recs, _ := filepath.Glob(filepath.Join(box.Root, "outbox", "*.json"))
	for _, p := range recs {
		if b, _ := os.ReadFile(p); strings.Contains(string(b), "attachment_ready") {
			t.Fatal("late ready receipt")
		}
	}
}
