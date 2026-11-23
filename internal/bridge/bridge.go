// Package bridge coordinates one Multica run with the local file mailbox.
package bridge

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log"
	"reflect"
	"strconv"
	"strings"
	"time"
	"unicode"
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
	t, err := b.Remote.Task(ctx)
	if err != nil {
		return "", err
	}
	if t.IssueID == "" {
		return "", errors.New("run is not bound to an issue")
	}
	// The Daemon owns admission and starts one worker per Run. Locally, only
	// workers for the same authoritative Issue contend for the same journal.
	release, err := b.Box.Lock(t.IssueID)
	if err != nil {
		return "", err
	}
	defer release()
	scope, err := b.Remote.Task(ctx)
	if err != nil {
		return "", err
	}
	if scope.IssueID != t.IssueID {
		return "", errors.New("run issue binding changed")
	}
	t = scope
	s, err := b.Box.Load(t.IssueID)
	if err != nil {
		return "", err
	}
	if s.Sync != nil {
		if err = b.Box.Emit(*s.Sync); err != nil {
			return "", err
		}
	}
	if s.RunID != b.RunID {
		if s.RetiredRuns[b.RunID] {
			return "", errors.New("run binding was already superseded")
		}
		if s.RetiredRuns == nil {
			s.RetiredRuns = map[string]bool{}
		}
		if s.RunID != "" {
			s.RetiredRuns[s.RunID] = true
		}
		if s.Epoch == ^uint64(0) {
			return "", errors.New("epoch overflow")
		}
		s.RunID = b.RunID
		s.Epoch++
		s.Completion = ""
		s.TaskEvent = nil
		s.Sync = nil
		s.LastSyncedAt = ""
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
		if err = b.emitReceipts(a); err != nil {
			return "", err
		}
	}
	if err = b.Box.CleanComments(&s, t.CancelledRunIDs); err != nil {
		return "", err
	}
	if s.Completion != "" {
		inputs, err := b.Box.Inputs(s.IssueID)
		if err != nil {
			return "", err
		}
		for _, in := range inputs {
			if err := b.cleanupDelivered(&s, in); err != nil {
				// Delivery has already been persisted. Cleanup failure must
				// neither imply a new send nor prevent ACP completion.
				log.Print("bridge: delivered Inbox cleanup deferred")
			}
		}
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
	failures := 0
	retry := func(readErr error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		failures++
		if !transientRead(readErr) || failures >= maxReadFailures {
			if err := b.setSync(&s, base, "failed"); err != nil {
				return err
			}
			return readErr
		}
		if err := b.setSync(&s, base, "retrying"); err != nil {
			return err
		}
		return wait(ctx, retryDelay(b.Poll, failures))
	}
poll:
	for {
		if err = ctx.Err(); err != nil {
			return "", err
		}
		// Poll before applying local actions so that a failed sync cannot pass as a clean completion.
		comments, err := b.Remote.Comments(ctx, t.IssueID)
		if err != nil {
			if err = retry(err); err != nil {
				return "", err
			}
			continue
		}
		recovering := s.Sync != nil && (s.Sync.Status == "retrying" || s.Sync.Status == "failed")
		if recovering {
			// Refresh authoritative scope after a degraded read, before acting
			// on the recovered snapshot (including reconciliation receipts).
			fresh, scopeErr := b.Remote.Task(ctx)
			if scopeErr == nil && (fresh.IssueID != t.IssueID || fresh.AgentID != t.AgentID) {
				scopeErr = errors.New("run issue or agent binding changed during recovery")
			}
			if scopeErr != nil {
				if err = retry(scopeErr); err != nil {
					return "", err
				}
				continue
			}
			t = fresh
		}
		if err = b.reconcile(&s, comments); err != nil {
			return "", err
		}
		observedInputs := make(map[string]string, len(t.DeliveredCommentIDs))
		for _, c := range comments {
			if c.ID == "" || c.DeletedAt != "" || c.SourceTaskID == b.RunID {
				continue
			}
			own := false
			for _, a := range s.Actions {
				if a.Receipt.RemoteID == c.ID || (a.Reconciliation != nil && a.Reconciliation.RemoteID == c.ID) {
					own = true
					break
				}
			}
			if own {
				continue
			}
			id, e := b.emitComment(&s, base, c, t.RunInputRole(c.ID))
			if e != nil {
				return "", e
			}
			if id != "" {
				observedInputs[c.ID] = id
			}
		}
		if err = b.Box.CleanComments(&s, t.CancelledRunIDs); err != nil {
			return "", err
		}
		manifest := readyInputs(t.DeliveredCommentIDs, observedInputs)
		now := time.Now().UTC()
		lastSync, _ := time.Parse(time.RFC3339Nano, s.LastSyncedAt)
		manifestChanged := s.Sync != nil && !reflect.DeepEqual(s.Sync.InputIDs, manifest)
		if s.Sync == nil || recovering || manifestChanged || now.Sub(lastSync) >= 30*time.Second {
			s.LastSyncedAt = now.Format(time.RFC3339Nano)
			var syncErr error
			switch {
			case recovering:
				syncErr = b.publishSync(&s, base, "recovered", manifest)
			case s.Sync == nil || manifestChanged:
				syncErr = b.publishSync(&s, base, "healthy", manifest)
			default:
				syncErr = b.Box.Save(s)
			}
			if syncErr != nil {
				return "", syncErr
			}
		}
		inputs, err := b.Box.Inputs(t.IssueID)
		if err != nil {
			return "", err
		}
		actionTask := t
		if t.TriggerCommentID == "" {
			actionTask.AllowedParents = append([]string(nil), t.AllowedParents...)
			for _, c := range comments {
				if c.ID != "" && c.DeletedAt == "" {
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
				var remote *multica.Error
				if errors.As(err, &remote) && remote.BeforePost && transientRead(err) {
					if err = retry(err); err != nil {
						return "", err
					}
					continue poll
				}
				return "", err
			}
			if err := b.cleanupDelivered(&s, in); err != nil {
				// Delivery has already been persisted. Cleanup failure must
				// neither imply a new send nor prevent ACP completion.
				log.Print("bridge: delivered Inbox cleanup deferred")
			}
			if done {
				return s.Completion, nil
			}
		}
		failures = 0
		if err = wait(ctx, b.Poll); err != nil {
			return "", err
		}
	}
}

func (b Bridge) apply(ctx context.Context, t multica.Task, s *mailbox.State, in mailbox.Input) (bool, error) {
	m := in.Message
	if prior, ok := s.Actions[m.ID]; ok {
		if prior.Digest == in.Digest {
			return false, b.emitReceipts(prior)
		}
		conflict := mailbox.Message{Version: 1, Kind: "receipt", ID: mailbox.ID("conflict", s.IssueID, m.ID, in.Digest), RequestID: m.ID, IssueID: s.IssueID, RunID: m.RunID, Epoch: m.Epoch, Status: "id_conflict"}
		return false, b.Box.Emit(conflict)
	}
	receipt := mailbox.Message{Version: 1, Kind: "receipt", ID: mailbox.ID("receipt", s.IssueID, m.ID), RequestID: m.ID, IssueID: s.IssueID, RunID: m.RunID, Epoch: m.Epoch}
	var intent *mailbox.SendIntent
	put := func(status string) error {
		receipt.Status = status
		if receipt.RemoteID != "" {
			intent = nil // Correlation is only needed until the comment is confirmed.
		}
		s.Actions[m.ID] = mailbox.Action{Digest: in.Digest, Receipt: receipt, Intent: intent}
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
		if a.Receipt.Status == "delivery_uncertain" && a.Reconciliation == nil {
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
	// Include an operation-specific inert Markdown comment so a lost response
	// can be matched later without inferring delivery from ordinary wording.
	payload := m.Body
	if t.AgentID != "" {
		var nonce [16]byte
		if _, err := rand.Read(nonce[:]); err != nil {
			return false, errors.New("cannot generate delivery correlation marker")
		}
		marker := "<!-- multica-bridge:" + hex.EncodeToString(nonce[:]) + " -->"
		payload += "\n\n" + marker
		intent = &mailbox.SendIntent{AgentID: t.AgentID, ParentID: parent, Marker: marker, PayloadDigest: mailbox.ID(payload)}
	}
	if err := put("sending"); err != nil {
		return false, err
	}
	remoteID, err := b.Remote.Reply(ctx, t.IssueID, parent, payload)
	if err != nil || remoteID == "" {
		var re *multica.Error
		if errors.As(err, &re) && !re.Ambiguous {
			if re.BeforePost && transientRead(err) {
				// This process has positive proof it never attempted the POST.
				// Clear durable sending intent only for that narrow case. A
				// crash before this save remains conservatively uncertain.
				delete(s.Actions, m.ID)
				if saveErr := b.Box.Save(*s); saveErr != nil {
					return false, saveErr
				}
				return false, err
			}
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

const maxReadFailures = 6

func transientRead(err error) bool {
	var remote *multica.Error
	return errors.As(err, &remote) &&
		(remote.Code == "transport" || remote.Code == "response_read" ||
			remote.Status == 408 || remote.Status == 429 || (remote.Status >= 500 && remote.Status <= 599))
}

func retryDelay(poll time.Duration, failures int) time.Duration {
	const maxDelay = 30 * time.Second
	if poll >= maxDelay {
		return maxDelay
	}
	for i := 1; i < failures; i++ {
		if poll >= maxDelay/2 {
			return maxDelay
		}
		poll *= 2
	}
	return poll
}

func wait(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// Issue comments have stable identity independently of the observing Run.
// Run input edges refer to that body and have their own acknowledgment.
func (b Bridge) emitComment(s *mailbox.State, base mailbox.Message, c multica.Comment, role string) (string, error) {
	source := mailbox.ID("source", c.ID, c.Revision, c.Body)
	bodyKey := "issue-comment:" + source
	bodyID := mailbox.ID("issue-comment", s.IssueID, source)
	runKey := mailbox.ID("run-input", base.RunID, role, source)
	runID := ""
	if role != "" {
		runID = s.Seen[runKey]
	}
	needsRef := role != "" && (runID == "" || s.CommentRefs[runID].Pending)
	// An already-read body can be fetched again when it is assigned to a new
	// Run. Its stable ID preserves the separate Issue read acknowledgment.
	if s.Seen[bodyKey] == "" || needsRef {
		m := mailbox.Message{Version: 1, Kind: "comment", ID: bodyID, IssueID: s.IssueID,
			RunID: "issue-context", Epoch: 1, Body: c.Body, SourceID: c.ID, Revision: c.Revision, CreatedAt: c.CreatedAt}
		if backgroundNote(c.Body) {
			m.Discussion = "ordinary"
		}
		if err := b.Box.Emit(m); err != nil {
			return "", err
		}
		s.Seen[bodyKey] = bodyID
		if err := b.Box.Save(*s); err != nil {
			return "", err
		}
	}
	if !needsRef {
		return runID, nil
	}
	m := base
	m.Kind = "comment"
	m.ID = mailbox.ID("comment", s.IssueID, base.RunID, runKey)
	m.SourceID, m.Revision, m.CreatedAt = c.ID, c.Revision, c.CreatedAt
	m.RunInput, m.BodyRef = role, bodyID
	if s.CommentRefs == nil {
		s.CommentRefs = map[string]mailbox.CommentRef{}
	}
	ref := mailbox.CommentRef{Run: base.RunID, Body: bodyID, Pending: true}
	s.CommentRefs[m.ID] = ref
	s.Seen[runKey] = m.ID
	if err := b.Box.Save(*s); err != nil {
		return "", err
	}
	if err := b.Box.Emit(m); err != nil {
		return "", err
	}
	ref.Pending = false
	s.CommentRefs[m.ID] = ref
	if err := b.Box.Save(*s); err != nil {
		return "", err
	}
	return m.ID, nil
}

// Upstream reserves the first token for human-only notes. A second explicit
// marker opts this note into MAB context without making it a Run trigger.
func backgroundNote(body string) bool {
	body = strings.TrimLeft(body, " \t\r\n")
	end := strings.IndexFunc(body, unicode.IsSpace)
	if end < 0 || !strings.EqualFold(body[:end], "/note") {
		return false
	}
	fields := strings.Fields(body[end:])
	return len(fields) != 0 && fields[0] == "[mab-context]"
}

// Absence is different from a definite empty claim. A successful API read
// alone does not prove that all of the inputs assigned by Multica were returned.
func readyInputs(expected []string, observed map[string]string) *[]string {
	if expected == nil {
		return nil
	}
	ids := make([]string, 0, len(expected))
	seen := make(map[string]bool, len(expected))
	for _, sourceID := range expected {
		eventID := observed[sourceID]
		if eventID == "" {
			return nil
		}
		if !seen[eventID] {
			ids = append(ids, eventID)
			seen[eventID] = true
		}
	}
	return &ids
}

func (b Bridge) setSync(s *mailbox.State, base mailbox.Message, status string) error {
	return b.publishSync(s, base, status, nil)
}

func (b Bridge) publishSync(s *mailbox.State, base mailbox.Message, status string, inputIDs *[]string) error {
	if s.Sync != nil && s.Sync.Status == status && reflect.DeepEqual(s.Sync.InputIDs, inputIDs) {
		return nil
	}
	if s.SyncSequence == ^uint64(0) {
		return errors.New("sync sequence overflow")
	}
	s.SyncSequence++
	base.Kind = "sync"
	base.Status = status
	base.InputIDs = inputIDs
	base.ObservedAt = time.Now().UTC().Format(time.RFC3339Nano)
	base.LastSyncedAt = s.LastSyncedAt
	base.Revision = strconv.FormatUint(s.SyncSequence, 10)
	base.ID = mailbox.ID("sync", s.IssueID, base.RunID, base.Revision)
	s.Sync = &base
	if err := b.Box.Save(*s); err != nil {
		return err
	}
	return b.Box.Emit(base)
}

// The caller has already persisted and emitted the receipt. Completion
// requested confirms only the comment: Completion is retained for ACP replay.
func (b Bridge) cleanupDelivered(s *mailbox.State, in mailbox.Input) error {
	m := in.Message
	a, ok := s.Actions[m.ID]
	if !ok || a.Digest != in.Digest || a.Receipt.RunID != m.RunID || a.Receipt.Epoch != m.Epoch {
		return nil
	}
	receipt := &a.Receipt
	if a.Reconciliation != nil {
		receipt = a.Reconciliation
	}
	if receipt.RemoteID == "" {
		return nil
	}
	switch receipt.Status {
	case "reply_posted", "completion_requested", "delivery_confirmed":
		return b.Box.RemoveDelivered(s.IssueID, m.ID, in.Digest)
	}
	return nil
}

func (b Bridge) emitReceipts(a mailbox.Action) error {
	if err := b.Box.Emit(a.Receipt); err != nil {
		return err
	}
	if a.Reconciliation != nil {
		return b.Box.Emit(*a.Reconciliation)
	}
	return nil
}

func (b Bridge) reconcile(s *mailbox.State, comments []multica.Comment) error {
	for id, a := range s.Actions {
		intent := a.Intent
		if a.Receipt.Status != "delivery_uncertain" || a.Reconciliation != nil || intent == nil ||
			intent.AgentID == "" || intent.Marker == "" || intent.PayloadDigest == "" {
			continue
		}
		var matched string
		for _, c := range comments {
			if c.ID == "" || c.DeletedAt != "" || c.IssueID != s.IssueID ||
				c.AuthorType != "agent" || c.AuthorID != intent.AgentID || c.SourceTaskID != a.Receipt.RunID ||
				c.ParentID != intent.ParentID || !strings.HasSuffix(c.Body, "\n\n"+intent.Marker) ||
				mailbox.ID(c.Body) != intent.PayloadDigest {
				continue
			}
			if matched != "" {
				matched = ""
				break
			}
			matched = c.ID
		}
		if matched == "" {
			continue
		}
		resolved := a.Receipt
		resolved.ID = mailbox.ID("reconciliation", s.IssueID, resolved.RunID, resolved.RequestID)
		resolved.Status = "delivery_confirmed"
		resolved.RemoteID = matched
		a.Reconciliation = &resolved
		a.Intent = nil
		s.Actions[id] = a
		if err := b.Box.Save(*s); err != nil {
			return err
		}
		if err := b.emitReceipts(a); err != nil {
			return err
		}
	}
	return nil
}
