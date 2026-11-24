package mailbox

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const MaxAttachment = 32 << 20

type AttachmentInfo struct {
	URL         string `json:"url"`
	Filename    string `json:"filename"`
	ContentType string `json:"content_type"`
	SizeBytes   int64  `json:"size_bytes"`
}

type AttachmentCatalog struct {
	Scope
	Version  int                         `json:"version"`
	IssueID  string                      `json:"issue_id"`
	RunID    string                      `json:"run_id"`
	Epoch    uint64                      `json:"epoch"`
	Status   string                      `json:"status"`
	Issue    []AttachmentInfo            `json:"issue"`
	Comments map[string][]AttachmentInfo `json:"comments"`
}

func (b Box) attachmentParts(issue, run string, epoch uint64) ([]string, error) {
	if !validID.MatchString(issue) || !validID.MatchString(run) || epoch == 0 {
		return nil, errors.New("invalid attachment binding")
	}
	return []string{"attachments", issue, run, strconv.FormatUint(epoch, 10)}, nil
}

func (b Box) attachmentOutbox() (string, error) {
	dir := b.Root
	if err := realDir(dir); err != nil {
		return "", err
	}
	parts := []string{"outbox"}
	if b.Connection != "" || b.Workspace != "" {
		if !validID.MatchString(b.Connection) || !validID.MatchString(b.Workspace) {
			return "", errors.New("invalid attachment scope")
		}
		parts = append(parts, b.Connection, b.Workspace)
	}
	for _, part := range parts {
		dir = filepath.Join(dir, part)
		if err := realDir(dir); err != nil {
			return "", err
		}
	}
	return dir, nil
}

func (b Box) attachmentDir(parts []string, create bool) (string, error) {
	// The mailbox outbox mount and its scoped ancestors follow the same
	// trusted-local-operator lifetime rules as other mailbox files.
	dir, err := b.attachmentOutbox()
	if err != nil {
		return "", err
	}
	for _, part := range parts {
		dir = filepath.Join(dir, part)
		var err error
		if create {
			err = makeDir(dir)
		} else {
			err = realDir(dir)
		}
		if err != nil {
			return "", err
		}
	}
	return dir, nil
}

func (b Box) PublishAttachmentCatalog(a AttachmentCatalog) error {
	parts, err := b.attachmentParts(a.IssueID, a.RunID, a.Epoch)
	if err != nil {
		return err
	}
	dir, err := b.attachmentDir(parts, true)
	if err != nil {
		return err
	}
	a.Scope = b.scope()
	a.Version = 1
	return atomicJSON(filepath.Join(dir, "catalog.json"), a)
}

// StoreAttachment stages bytes on the Outbox volume and makes the opaque
// result name visible only after a successful, still-current retrieval.
func (b Box) StoreAttachment(ctx context.Context, m Message, fetch func(io.Writer) error) (string, error) {
	if !validID.MatchString(m.ID) {
		return "", errors.New("invalid attachment request")
	}
	parts, err := b.attachmentParts(m.IssueID, m.RunID, m.Epoch)
	if err != nil {
		return "", err
	}
	dir, err := b.attachmentDir(parts, true)
	if err != nil {
		return "", err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "", err
	}
	var existingBytes int64
	existingCount := 0
	for _, e := range entries {
		if e.Name() == "catalog.json" || strings.HasPrefix(e.Name(), ".") || !e.Type().IsRegular() {
			continue
		}
		if info, eErr := e.Info(); eErr == nil {
			existingBytes += info.Size()
			existingCount++
		}
	}
	if existingCount >= 8 || existingBytes >= 64<<20 {
		return "", errors.New("attachment quota exceeded")
	}
	outbox, err := b.attachmentOutbox()
	if err != nil {
		return "", err
	}
	staging := filepath.Join(outbox, ".attachment-staging")
	if err := makeDir(staging); err != nil {
		return "", err
	}
	f, err := os.CreateTemp(staging, ".stage-"+ID("attachment-staging", m.IssueID, m.RunID, strconv.FormatUint(m.Epoch, 10))+"-")
	if err != nil {
		return "", err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err := fetch(f); err != nil {
		return "", err
	}
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if info.Size() > MaxAttachment || existingBytes+info.Size() > 64<<20 {
		return "", errors.New("attachment quota exceeded")
	}
	buf := make([]byte, 512)
	n, err := f.ReadAt(buf, 0)
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	suffix := map[string]string{
		"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "application/pdf": ".pdf",
	}[http.DetectContentType(buf[:n])]
	if err := f.Sync(); err != nil {
		return "", err
	}
	if err := f.Close(); err != nil {
		return "", err
	}
	if _, err := b.attachmentDir(parts, false); err != nil {
		return "", err
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	name := m.ID + suffix
	path := filepath.Join(dir, name)
	if err := os.Link(f.Name(), path); err != nil {
		return "", err
	}
	if err := ctx.Err(); err != nil {
		os.Remove(path)
		return "", err
	}
	if err := syncDir(dir); err != nil {
		os.Remove(path)
		return "", err
	}
	rel, err := filepath.Rel(b.Root, path)
	if err != nil {
		os.Remove(path)
		return "", err
	}
	return filepath.ToSlash(rel), nil
}

// removeAttachmentTree traverses only bridge-created relative paths; a symlink
// is treated as a leaf and never followed into another attachment scope.
func removeAttachmentTree(root *os.Root, name string) error {
	fi, err := root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if fi.IsDir() {
		child, err := root.OpenRoot(name)
		if err != nil {
			return err
		}
		defer child.Close()
		f, err := child.Open(".")
		if err != nil {
			return err
		}
		current, err := f.Stat()
		if err != nil || !os.SameFile(fi, current) {
			f.Close()
			return errors.New("attachment directory changed")
		}
		entries, err := f.ReadDir(-1)
		f.Close()
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if err := removeAttachmentTree(child, entry.Name()); err != nil {
				return err
			}
		}
	}
	err = root.Remove(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// CleanAttachments is called under the per-Issue writer lock and targets
// the exact terminal, superseded, or restarted Run epoch only.
func (b Box) CleanAttachments(issue, run string, epoch uint64) error {
	parts, err := b.attachmentParts(issue, run, epoch)
	if err != nil {
		return err
	}
	dir, err := b.attachmentDir(parts[:len(parts)-1], false)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err == nil {
		root, err := os.OpenRoot(dir)
		if err != nil {
			return err
		}
		err = removeAttachmentTree(root, parts[len(parts)-1])
		root.Close()
		if err != nil {
			return err
		}
	}
	outbox, err := b.attachmentOutbox()
	if err != nil {
		return err
	}
	stage := filepath.Join(outbox, ".attachment-staging")
	if err := realDir(stage); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	root, err := os.OpenRoot(stage)
	if err != nil {
		return err
	}
	defer root.Close()
	f, err := root.Open(".")
	if err != nil {
		return err
	}
	entries, err := f.ReadDir(-1)
	f.Close()
	if err != nil {
		return err
	}
	prefix := ".stage-" + ID("attachment-staging", issue, run, strconv.FormatUint(epoch, 10)) + "-"
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			if err := root.Remove(e.Name()); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
	}
	return nil
}
