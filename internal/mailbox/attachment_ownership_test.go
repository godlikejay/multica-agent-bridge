package mailbox

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestAttachmentCleanupLeavesAgentOwnedFiles(t *testing.T) {
	box := Box{Root: t.TempDir()}
	if err := box.Init(); err != nil {
		t.Fatal(err)
	}
	put := func(id string) string {
		t.Helper()
		p, err := box.StoreAttachment(context.Background(),
			Message{ID: id, IssueID: "issue", RunID: "run", Epoch: 1},
			func(w io.Writer) error {
				_, err := io.WriteString(w, "synthetic attachment")
				return err
			})
		if err != nil {
			t.Fatal(err)
		}
		return filepath.Join(box.Root, filepath.FromSlash(p))
	}
	source := put("copy")
	moved := put("move")
	deleted := put("delete")
	agentDir := t.TempDir()
	copyDst, moveDst := filepath.Join(agentDir, "copy"), filepath.Join(agentDir, "move")
	data, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(copyDst, data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(moved, moveDst); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(deleted); err != nil {
		t.Fatal(err)
	}
	if err := box.CleanAttachments("issue", "run", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(source); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("MAB's original copy still exists: %v", err)
	}
	for _, p := range []string{copyDst, moveDst} {
		got, err := os.ReadFile(p)
		if err != nil || string(got) != string(data) {
			t.Fatalf("agent-owned file %q changed: %v", p, err)
		}
	}
}
