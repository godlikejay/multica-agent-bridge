// Package bridge coordinates one Multica run with the local file mailbox.
package bridge

import (
	"context"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
)

type Remote interface {
	Task(context.Context) (multica.Task, error)
	Comments(context.Context, string) ([]multica.Comment, error)
	Reply(context.Context, string, string, string) (string, error)
}
type Bridge struct {
	Box    mailbox.Box
	Remote Remote
	RunID  string
	Poll   time.Duration
}

func (b Bridge) Run(ctx context.Context) (string, error) {
	if b.RunID == "" || b.Poll <= 0 {
		return "", errors.New("invalid run configuration")
	}
	if err := b.Box.Init(); err != nil {
		return "", err
	}
	release, err := b.Box.Lock()
	if err != nil {
		return "", err
	}
	defer release()
	t, err := b.Remote.Task(ctx)
	if err != nil {
		return "", err
	}
	if t.IssueID == "" {
		return "", errors.New("run is not bound to an issue")
	}
	s, err := b.Box.Load(t.IssueID)
	if err != nil {
		return "", err
	}
	if s.RunID != b.RunID {
		if s.Epoch == ^uint64(0) {
			return "", errors.New("epoch overflow")
		}
		s.RunID = b.RunID
		s.Epoch++
		s.Completion = ""
		s.TaskEvent = nil
		if err = b.Box.Save(s); err != nil {
			return "", err
		}
	}
	// A crash may have happened after a remote post but before its response was saved.
	// Treat that gap as uncertain rather than retrying automatically.
	for id, a := range s.Actions {
		if a.Receipt.Status != "sending" {
			continue
		}
		a.Receipt.Status = "delivery_uncertain"
		s.Actions[id] = a
		if err = b.Box.Save(s); err != nil {
			return "", err
		}
	}
	for _, a := range s.Actions {
		if err = b.Box.Emit(a.Receipt); err != nil {
			return "", err
		}
	}
	if s.Completion != "" {
		return s.Completion, nil
	}
	base := mailbox.Message{Version: 1, IssueID: t.IssueID, RunID: b.RunID, Epoch: s.Epoch}
	if s.TaskEvent == nil {
		m := base
		m.Kind = "task"
		m.ID = mailbox.ID("task", t.IssueID, b.RunID)
		m.IssueKey = t.IssueKey
		m.Body = t.Title
		if t.Description != "" {
			m.Body += "\n\n" + t.Description
		}
		s.TaskEvent = &m
		if err = b.Box.Save(s); err != nil {
			return "", err
		}
	}
	if err = b.Box.Emit(*s.TaskEvent); err != nil {
		return "", err
	}
	ticker := time.NewTicker(b.Poll)
	defer ticker.Stop()
	for {
		if err = ctx.Err(); err != nil {
			return "", err
		}
		// Poll before applying local actions so that a failed sync cannot pass as a clean completion.
		comments, err := b.Remote.Comments(ctx, t.IssueID)
		if err != nil {
			return "", err
		}
		for _, c := range comments {
			if c.ID == "" || c.SourceTaskID == b.RunID {
				continue
			}
			own := false
			for _, a := range s.Actions {
				if a.Receipt.RemoteID == c.ID {
					own = true
					break
				}
			}
			if own {
				continue
			}
			k := mailbox.ID("source", c.ID, c.Revision, c.Body)
			if s.Seen[k] != "" {
				continue
			}
			m := base
			m.Kind = "comment"
			m.ID = mailbox.ID("comment", t.IssueID, b.RunID, k)
			m.SourceID = c.ID
			m.Revision = c.Revision
			m.Body = c.Body
			if err = b.Box.Emit(m); err != nil {
				return "", err
			}
			s.Seen[k] = m.ID
			if err = b.Box.Save(s); err != nil {
				return "", err
			}
		}
		inputs, err := b.Box.Inputs()
		if err != nil {
			return "", err
		}
		actionTask := t
		if t.TriggerCommentID == "" {
			actionTask.AllowedParents = append([]string(nil), t.AllowedParents...)
			for _, c := range comments {
				if c.ID != "" {
					actionTask.AllowedParents = append(actionTask.AllowedParents, c.ID)
				}
			}
		}
		for _, in := range inputs {
			if err = ctx.Err(); err != nil {
				return "", err
			}
			if in.Message.IssueID != t.IssueID {
				continue
			}
			done, err := b.apply(ctx, actionTask, &s, in)
			if err != nil {
				return "", err
			}
			if done {
				return s.Completion, nil
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
		}
	}
}

func (b Bridge) apply(ctx context.Context, t multica.Task, s *mailbox.State, in mailbox.Input) (bool, error) {
	m := in.Message
	if prior, ok := s.Actions[m.ID]; ok {
		if prior.Digest == in.Digest {
			return false, b.Box.Emit(prior.Receipt)
		}
		conflict := mailbox.Message{Version: 1, Kind: "receipt", ID: mailbox.ID("conflict", s.IssueID, m.ID, in.Digest), RequestID: m.ID, IssueID: s.IssueID, RunID: m.RunID, Epoch: m.Epoch, Status: "id_conflict"}
		return false, b.Box.Emit(conflict)
	}
	receipt := mailbox.Message{Version: 1, Kind: "receipt", ID: mailbox.ID("receipt", s.IssueID, m.ID), RequestID: m.ID, IssueID: s.IssueID, RunID: m.RunID, Epoch: m.Epoch}
	put := func(status string) error {
		receipt.Status = status
		s.Actions[m.ID] = mailbox.Action{Digest: in.Digest, Receipt: receipt}
		return b.Box.Save(*s)
	}
	finish := func(status string) (bool, error) {
		if err := put(status); err != nil {
			return false, err
		}
		return false, b.Box.Emit(receipt)
	}
	if m.RunID != s.RunID || m.Epoch != s.Epoch {
		return finish("stale")
	}
	if !utf8.ValidString(m.Body) || strings.ContainsRune(m.Body, 0) {
		return finish("invalid_body")
	}
	// An unresolved request may already have posted. Never progress silently.
	for _, a := range s.Actions {
		if a.Receipt.Status == "delivery_uncertain" && a.Receipt.RunID == s.RunID {
			return finish("blocked_uncertain")
		}
	}
	parent := m.ParentID
	if parent == "" {
		parent = t.TriggerCommentID
	}
	permitted := parent == "" && t.TriggerCommentID == ""
	for _, id := range t.AllowedParents {
		if id == parent {
			permitted = true
			break
		}
	}
	if parent != "" && parent == t.TriggerCommentID {
		permitted = true
	}
	if !permitted {
		return finish("invalid_parent")
	}
	if err := put("sending"); err != nil {
		return false, err
	}
	remoteID, err := b.Remote.Reply(ctx, t.IssueID, parent, m.Body)
	if err != nil || remoteID == "" {
		var re *multica.Error
		if errors.As(err, &re) && !re.Ambiguous {
			return finish("remote_rejected")
		}
		return finish("delivery_uncertain")
	}
	receipt.RemoteID = remoteID
	if m.Kind == "reply" {
		return finish("reply_posted")
	}
	s.Completion = m.Body
	// 'completion_requested' is a local durable decision, not a transport/server acknowledgement.
	if err = put("completion_requested"); err != nil {
		return false, err
	}
	return true, b.Box.Emit(receipt)
}
