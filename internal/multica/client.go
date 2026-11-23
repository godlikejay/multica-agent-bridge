// Package multica implements the task-scoped API contract pinned to upstream
// multica-ai/multica 220791df038b0a507556d91d7be899c4ba8a463d.
// It never calls daemon lifecycle endpoints or retries a comment submission.
package multica

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const maxResponse = 2 << 20
const maxPages = 20
const maxTotalResponse = 8 << 20
const maxReply = 256 << 10

type Config struct {
	URL, Token, WorkspaceID, AgentID, RunID string
	ExpectedRuntimeID                       string
	// A trusted supervisor route ignores task-controlled transport settings.
	TrustedRoute, SyntheticHTTP bool
}

// String and GoString keep ordinary diagnostics from accidentally printing credentials.
func (c Config) String() string   { return "multica.Config{credentials:redacted}" }
func (c Config) GoString() string { return c.String() }

type Task struct {
	IssueID, IssueKey, Title, Description, TriggerCommentID, AgentID string
	AllowedParents                                                   []string
	DeliveredCommentIDs, CancelledRunIDs                             []string
}
type Comment struct{ ID, IssueID, Body, Revision, AuthorID, AuthorType, SourceTaskID, ParentID, DeletedAt, CreatedAt string }

// RunInputRole consults the claim receipt when available. An empty delivered
// list is authoritative; only older responses fall back to the planned IDs.
func (t Task) RunInputRole(id string) string {
	if t.DeliveredCommentIDs != nil {
		if !slices.Contains(t.DeliveredCommentIDs, id) {
			return ""
		}
	} else if id != t.TriggerCommentID && !slices.Contains(t.AllowedParents, id) {
		return ""
	}
	if id == t.TriggerCommentID {
		return "trigger"
	}
	return "coalesced"
}

// Error deliberately contains neither server bodies, request URLs nor credentials.
// Ambiguous means a POST may have reached the server. Callers must reconcile the
// outcome; blindly retrying can publish duplicate comments.
type Error struct {
	Code      string
	Status    int
	Ambiguous bool
	// BeforePost is true only when a read-only check failed before constructing the POST.
	BeforePost bool
}

func (e *Error) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("multica: %s (HTTP %d)", e.Code, e.Status)
	}
	return "multica: " + e.Code
}
func fail(code string) error { return &Error{Code: code} }

type Client struct {
	cfg   Config
	base  *url.URL
	http  *http.Client
	mu    sync.RWMutex
	scope *Task
}

func FromEnv() (Config, error) {
	c := TaskConfigFromEnv()
	_, err := New(c)
	return c, err
}

// TaskConfigFromEnv reads the daemon-issued values; callers must validate them with New.
func TaskConfigFromEnv() Config {
	return Config{URL: os.Getenv("MULTICA_SERVER_URL"), Token: os.Getenv("MULTICA_TOKEN"), WorkspaceID: os.Getenv("MULTICA_WORKSPACE_ID"), AgentID: os.Getenv("MULTICA_AGENT_ID"), RunID: os.Getenv("MULTICA_TASK_ID"), ExpectedRuntimeID: os.Getenv("BRIDGE_EXPECTED_RUNTIME_ID")}
}
func validID(s string) bool {
	if len(s) == 0 || len(s) > 128 {
		return false
	}
	for _, r := range s {
		if !((r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_') {
			return false
		}
	}
	return true
}
func New(c Config) (*Client, error) {
	if !validID(c.WorkspaceID) || !validID(c.AgentID) || !validID(c.RunID) {
		return nil, fail("invalid_identity")
	}
	if !strings.HasPrefix(c.Token, "mat_") || len(c.Token) <= 4 || len(c.Token) > 8192 || strings.ContainsAny(c.Token, " \t\r\n\x00") {
		return nil, fail("invalid_task_token")
	}
	u, err := url.Parse(c.URL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return nil, fail("invalid_server_url")
	}
	loopback := u.Hostname() == "localhost"
	if ip := net.ParseIP(u.Hostname()); ip != nil && ip.IsLoopback() {
		loopback = true
	}
	plainAllowed := loopback || (os.Getenv("BRIDGE_ALLOW_TEST_HTTP") == "1" && u.Hostname() == "backend")
	if c.TrustedRoute {
		plainAllowed = c.SyntheticHTTP && (u.Hostname() == "backend-a" || u.Hostname() == "backend-b")
	}
	if u.Scheme != "https" && !(u.Scheme == "http" && plainAllowed) {
		return nil, fail("insecure_server_url")
	}
	for _, s := range strings.Split(u.Path, "/") {
		if s == ".." || s == "." {
			return nil, fail("invalid_server_url")
		}
	}
	u.Path = strings.TrimRight(u.Path, "/")
	u.RawPath = ""
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if c.TrustedRoute {
		// Custom agent environments can carry HTTP(S)_PROXY. Do not let them
		// redirect a route pinned by the local supervisor.
		tr.Proxy = nil
	}
	tr.MaxResponseHeaderBytes = 64 << 10
	return &Client{cfg: c, base: u, http: &http.Client{Transport: tr, Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (c *Client) request(ctx context.Context, method, path string, q url.Values, payload any, out any) (http.Header, int, error) {
	u := *c.base
	u.Path += path
	u.RawQuery = q.Encode()
	var body io.Reader
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return nil, 0, fail("invalid_request")
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
	if err != nil {
		return nil, 0, fail("invalid_request")
	}
	req.Header.Set("Authorization", "Bearer "+c.cfg.Token)
	req.Header.Set("X-Agent-ID", c.cfg.AgentID)
	req.Header.Set("X-Task-ID", c.cfg.RunID)
	req.Header.Set("Accept", "application/json")
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, &Error{Code: "transport", Ambiguous: method == http.MethodPost}
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		code := "http_error"
		switch {
		case resp.StatusCode == 401:
			code = "unauthorized"
		case resp.StatusCode == 403:
			code = "forbidden"
		case resp.StatusCode == 429:
			code = "rate_limited"
		case resp.StatusCode >= 300 && resp.StatusCode < 400:
			code = "redirect_refused"
		}
		return nil, 0, &Error{Code: code, Status: resp.StatusCode, Ambiguous: method == http.MethodPost && resp.StatusCode >= 500}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxResponse+1))
	if err != nil {
		return nil, 0, &Error{Code: "response_read", Ambiguous: method == http.MethodPost}
	}
	if len(data) > maxResponse {
		return nil, 0, &Error{Code: "response_too_large", Ambiguous: method == http.MethodPost}
	}
	if err = json.Unmarshal(data, out); err != nil {
		return nil, 0, &Error{Code: "invalid_response", Ambiguous: method == http.MethodPost}
	}
	return resp.Header, len(data), nil
}

type wireTask struct {
	ID          string   `json:"id"`
	AgentID     string   `json:"agent_id"`
	WorkspaceID string   `json:"workspace_id"`
	IssueID     string   `json:"issue_id"`
	Status      string   `json:"status"`
	RuntimeID   string   `json:"runtime_id"`
	Trigger     string   `json:"trigger_comment_id"`
	Parents     []string `json:"coalesced_comment_ids"`
	Delivered   []string `json:"delivered_comment_ids"`
	Restricted  bool     `json:"restricted"`
}

func cloneTask(t Task) Task {
	t.AllowedParents = slices.Clone(t.AllowedParents)
	t.DeliveredCommentIDs = slices.Clone(t.DeliveredCommentIDs)
	t.CancelledRunIDs = slices.Clone(t.CancelledRunIDs)
	return t
}

// Task resolves the exact injected RunID; prompts are never parsed for identity.
func (c *Client) Task(ctx context.Context) (Task, error) {
	// A failed refresh must not leave a previously validated scope usable.
	c.mu.Lock()
	c.scope = nil
	c.mu.Unlock()
	cursor := ""
	seen := map[string]bool{}
	total := 0
	var cancelled []wireTask
	for page := 0; page < maxPages; page++ {
		q := url.Values{"limit": {"200"}}
		if cursor != "" {
			q.Set("before", cursor)
		}
		var rows []wireTask
		headers, n, err := c.request(ctx, http.MethodGet, "/api/agents/"+c.cfg.AgentID+"/tasks", q, nil, &rows)
		if err != nil {
			return Task{}, err
		}
		total += n
		if total > maxTotalResponse {
			return Task{}, fail("response_too_large")
		}
		var found *wireTask
		for i := range rows {
			if rows[i].Status == "cancelled" {
				cancelled = append(cancelled, rows[i])
			}
			if rows[i].ID == c.cfg.RunID {
				if found != nil {
					return Task{}, fail("ambiguous_task")
				}
				found = &rows[i]
			}
		}
		if found != nil {
			t := *found
			if t.Status != "running" {
				return Task{}, fail("task_not_running")
			}
			if c.cfg.ExpectedRuntimeID != "" && t.RuntimeID != c.cfg.ExpectedRuntimeID {
				return Task{}, fail("runtime_mismatch")
			}
			if t.Restricted {
				return Task{}, fail("restricted_task")
			}
			if t.AgentID != c.cfg.AgentID || t.WorkspaceID != c.cfg.WorkspaceID {
				return Task{}, fail("identity_mismatch")
			}
			if !validID(t.IssueID) {
				return Task{}, fail("unsupported_task")
			}
			task := Task{IssueID: t.IssueID, TriggerCommentID: t.Trigger, AgentID: t.AgentID, DeliveredCommentIDs: t.Delivered}
			for _, row := range cancelled {
				if validID(row.ID) && row.IssueID == t.IssueID && row.WorkspaceID == t.WorkspaceID && row.AgentID == t.AgentID {
					task.CancelledRunIDs = append(task.CancelledRunIDs, row.ID)
				}
			}
			for _, id := range task.DeliveredCommentIDs {
				if !validID(id) {
					return Task{}, fail("invalid_response")
				}
			}
			parents := map[string]bool{}
			for _, p := range append([]string{t.Trigger}, t.Parents...) {
				if p == "" {
					continue
				}
				if !validID(p) {
					return Task{}, fail("invalid_response")
				}
				if !parents[p] {
					task.AllowedParents = append(task.AllowedParents, p)
					parents[p] = true
				}
			}
			var issue struct {
				ID          string `json:"id"`
				WorkspaceID string `json:"workspace_id"`
				Key         string `json:"identifier"`
				Title       string `json:"title"`
				Description string `json:"description"`
			}
			_, _, err = c.request(ctx, http.MethodGet, "/api/issues/"+task.IssueID, nil, nil, &issue)
			if err != nil {
				return Task{}, err
			}
			if issue.ID != task.IssueID || issue.WorkspaceID != c.cfg.WorkspaceID {
				return Task{}, fail("identity_mismatch")
			}
			task.IssueKey, task.Title, task.Description = issue.Key, issue.Title, issue.Description
			copy := cloneTask(task)
			c.mu.Lock()
			c.scope = &copy
			c.mu.Unlock()
			return task, nil
		}
		cursor = headers.Get("X-Agent-Tasks-Next-Cursor")
		if cursor == "" {
			return Task{}, fail("task_not_found")
		}
		if len(cursor) > 2048 || seen[cursor] {
			return Task{}, fail("invalid_cursor")
		}
		seen[cursor] = true
	}
	return Task{}, fail("page_limit")
}
func (c *Client) scoped(ctx context.Context, issueID string) (Task, error) {
	c.mu.RLock()
	var t *Task
	if c.scope != nil {
		copy := cloneTask(*c.scope)
		t = &copy
	}
	c.mu.RUnlock()
	if t == nil {
		v, e := c.Task(ctx)
		if e != nil {
			return Task{}, e
		}
		t = &v
	}
	if issueID != t.IssueID {
		return Task{}, fail("issue_out_of_scope")
	}
	return *t, nil
}

type wireComment struct {
	ID           string `json:"id"`
	IssueID      string `json:"issue_id"`
	Content      string `json:"content"`
	Revision     int64  `json:"revision"`
	AuthorID     string `json:"author_id"`
	AuthorType   string `json:"author_type"`
	ParentID     string `json:"parent_id"`
	DeletedAt    string `json:"deleted_at"`
	SourceTaskID string `json:"source_task_id"`
	CreatedAt    string `json:"created_at"`
}

// Comments obtains complete recent-thread pages. Oversized/partial threads are
// an explicit error, never a successful empty or silently truncated snapshot.
func (c *Client) Comments(ctx context.Context, issueID string) ([]Comment, error) {
	if _, err := c.scoped(ctx, issueID); err != nil {
		return nil, err
	}
	q := url.Values{"recent": {"200"}}
	seen := map[string]bool{}
	ids := map[string]bool{}
	result := []Comment{}
	total := 0
	for page := 0; page < maxPages; page++ {
		var rows []wireComment
		headers, n, err := c.request(ctx, http.MethodGet, "/api/issues/"+issueID+"/comments", q, nil, &rows)
		if err != nil {
			return nil, err
		}
		total += n
		if total > maxTotalResponse {
			return nil, fail("response_too_large")
		}
		if strings.EqualFold(headers.Get("X-Comments-Truncated"), "true") {
			return nil, fail("comments_truncated")
		}
		for _, r := range rows {
			if !validID(r.ID) || r.IssueID != issueID || r.Revision < 0 {
				return nil, fail("invalid_response")
			}
			if ids[r.ID] {
				continue
			}
			ids[r.ID] = true
			createdAt := ""
			if when, err := time.Parse(time.RFC3339Nano, r.CreatedAt); err == nil {
				createdAt = when.UTC().Format(time.RFC3339Nano)
			}
			result = append(result, Comment{ID: r.ID, IssueID: r.IssueID, Body: r.Content, Revision: strconv.FormatInt(r.Revision, 10), AuthorID: r.AuthorID, AuthorType: r.AuthorType, SourceTaskID: r.SourceTaskID, ParentID: r.ParentID, DeletedAt: r.DeletedAt, CreatedAt: createdAt})
		}
		before, id := headers.Get("X-Multica-Next-Before"), headers.Get("X-Multica-Next-Before-Id")
		if before == "" && id == "" {
			return result, nil
		}
		if _, e := time.Parse(time.RFC3339Nano, before); e != nil || !validID(id) {
			return nil, fail("invalid_cursor")
		}
		key := before + "/" + id
		if seen[key] {
			return nil, fail("invalid_cursor")
		}
		seen[key] = true
		q.Set("before", before)
		q.Set("before_id", id)
	}
	return nil, fail("page_limit")
}

// Reply is not an approval mechanism. The caller must first validate the local
// operator's explicit inbox action. It never retries a potentially delivered POST.
func (c *Client) Reply(ctx context.Context, issueID, parentID, body string) (string, error) {
	if strings.TrimSpace(body) == "" || len(body) > maxReply || !utf8.ValidString(body) || strings.ContainsRune(body, 0) {
		return "", fail("invalid_reply")
	}
	t, err := c.Task(ctx)
	if err != nil {
		return "", beforePost(err)
	}
	if issueID != t.IssueID {
		return "", fail("issue_out_of_scope")
	}
	if t.TriggerCommentID != "" {
		ok := false
		for _, p := range t.AllowedParents {
			if parentID == p {
				ok = true
			}
		}
		if !ok {
			return "", fail("parent_out_of_scope")
		}
	} else if parentID != "" {
		if !validID(parentID) {
			return "", fail("parent_out_of_scope")
		}
		comments, e := c.Comments(ctx, issueID)
		if e != nil {
			return "", beforePost(e)
		}
		ok := false
		for _, r := range comments {
			if r.ID == parentID && r.DeletedAt == "" {
				ok = true
			}
		}
		if !ok {
			return "", fail("parent_out_of_scope")
		}
	}
	var parent *string
	if parentID != "" {
		parent = &parentID
	}
	payload := struct {
		Content string  `json:"content"`
		Type    string  `json:"type"`
		Parent  *string `json:"parent_id"`
	}{body, "comment", parent}
	var response wireComment
	_, _, err = c.request(ctx, http.MethodPost, "/api/issues/"+issueID+"/comments", nil, payload, &response)
	if err != nil {
		return "", err
	}
	if !validID(response.ID) || response.IssueID != issueID || response.SourceTaskID != c.cfg.RunID ||
		response.AuthorID != c.cfg.AgentID || response.AuthorType != "agent" || response.ParentID != parentID ||
		response.Content != body || response.DeletedAt != "" {
		return "", &Error{Code: "reply_identity_mismatch", Ambiguous: true}
	}
	return response.ID, nil
}

func beforePost(err error) error {
	var remote *Error
	if errors.As(err, &remote) {
		cloned := *remote
		cloned.BeforePost = true
		return &cloned
	}
	return err
}
