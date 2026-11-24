package mailbox

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestAttachmentStorageRefusesScopeSymlink(t *testing.T) {
	root := t.TempDir()
	box := Box{Root: root, Connection: "source", Workspace: "w"}
	if err := os.Mkdir(filepath.Join(root, "inbox"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := box.Init(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "outbox", "source")
	if err := os.Rename(path, path+"-saved"); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	if err := os.Mkdir(filepath.Join(outside, "w"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, path); err != nil {
		t.Fatal(err)
	}
	m := Message{ID: "request", IssueID: "issue", RunID: "run", Epoch: 1}
	if _, err := box.StoreAttachment(context.Background(), m, func(io.Writer) error {
		t.Error("fetch called with unsafe local scope")
		return nil
	}); err == nil {
		t.Fatal("attachment store followed scope symlink")
	}
	if err := box.CleanAttachments("issue", "run", 1); err == nil {
		t.Fatal("attachment cleaner followed scope symlink")
	}
	if _, err := os.Stat(filepath.Join(outside, "w", "attachments")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("external attachment tree changed", err)
	}
}

func TestAttachmentCleanupUnlinksLeafSymlinkOnly(t *testing.T) {
	box := Box{Root: t.TempDir()}
	if err := box.Init(); err != nil {
		t.Fatal(err)
	}
	if err := box.PublishAttachmentCatalog(AttachmentCatalog{IssueID: "issue", RunID: "run", Epoch: 1}); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	kept := filepath.Join(root, "kept")
	if err := os.WriteFile(kept, []byte("must remain"), 0600); err != nil {
		t.Fatal(err)
	}
	run := filepath.Join(box.Root, "outbox", "attachments", "issue", "run", "1")
	if err := os.Symlink(root, filepath.Join(run, "foreign")); err != nil {
		t.Fatal(err)
	}
	if err := box.CleanAttachments("issue", "run", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(run); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("scoped run stale after cleanup", err)
	}
	if data, err := os.ReadFile(kept); err != nil || string(data) != "must remain" {
		t.Fatal("cleanup traversed symlink", err)
	}
}
