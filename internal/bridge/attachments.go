package bridge

import (
	"context"
	"io"
	"sort"
	"time"

	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
)

type attachmentRemote interface {
	Attachments(context.Context, string) ([]multica.Attachment, error)
	FetchAttachment(context.Context, string, string, io.Writer) error
}

func scopedAttachmentCatalog(ctx context.Context, r attachmentRemote, base mailbox.Message, comments map[string]string) mailbox.AttachmentCatalog {
	catalog := mailbox.AttachmentCatalog{IssueID: base.IssueID, RunID: base.RunID, Epoch: base.Epoch,
		Status: "unavailable", Issue: []mailbox.AttachmentInfo{}, Comments: map[string][]mailbox.AttachmentInfo{}}
	short, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	rows, err := r.Attachments(short, base.IssueID)
	if err != nil {
		return catalog
	}
	catalog.Status = "ready"
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].URL < rows[j].URL })
	for _, a := range rows {
		m := mailbox.AttachmentInfo{URL: a.URL, Filename: a.Filename, ContentType: a.ContentType, SizeBytes: a.SizeBytes}
		switch {
		case a.CommentID == "":
			catalog.Issue = append(catalog.Issue, m)
		default:
			if _, eligible := comments[a.CommentID]; !eligible {
				continue
			}
			catalog.Comments[a.CommentID] = append(catalog.Comments[a.CommentID], m)
		}
	}
	return catalog
}

func (b Bridge) storeAttachment(ctx context.Context, r attachmentRemote, t multica.Task, m mailbox.Message) (string, error) {
	short, cancel := context.WithTimeout(ctx, 40*time.Second)
	defer cancel()
	return b.Box.StoreAttachment(short, m, func(dst io.Writer) error {
		return r.FetchAttachment(short, t.IssueID, m.AttachmentURL, dst)
	})
}
