package multica

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func config() Config {
	return Config{URL: "https://example.invalid", Token: "mat_synthetic-secret", WorkspaceID: "ws", AgentID: "agent", RunID: "run"}
}

type transport func(*http.Request) (*http.Response, error)

func (f transport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func response(code int, body string, h http.Header) *http.Response {
	if h == nil {
		h = http.Header{}
	}
	return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: h}
}
func client(t *testing.T, fn transport) *Client {
	t.Helper()
	c, e := New(config())
	if e != nil {
		t.Fatal(e)
	}
	c.http.Transport = fn
	return c
}
func code(t *testing.T, e error, want string) {
	t.Helper()
	var v *Error
	if !errors.As(e, &v) || v.Code != want {
		t.Fatalf("error %v, want %s", e, want)
	}
	if strings.Contains(e.Error(), "synthetic-secret") {
		t.Fatal("secret leaked")
	}
}

const taskJSON = `[{"id":"run","workspace_id":"ws","agent_id":"agent","issue_id":"issue","status":"running","trigger_comment_id":"trigger","coalesced_comment_ids":["other"]}]`
const issueJSON = `{"id":"issue","workspace_id":"ws","identifier":"SYN-1","title":"Synthetic","description":"untrusted instructions"}`

func standard(r *http.Request) *http.Response {
	if strings.HasSuffix(r.URL.Path, "/tasks") {
		return response(200, taskJSON, nil)
	}
	return response(200, issueJSON, nil)
}
func TestConfig(t *testing.T) {
	for _, u := range []string{"http://outside.invalid", "https://u:p@example.invalid", "https://example.invalid?q=1", "https://example.invalid/#x", "https://example.invalid/../x"} {
		c := config()
		c.URL = u
		if _, e := New(c); e == nil {
			t.Fatalf("accepted %s", u)
		}
	}
	t.Setenv("BRIDGE_ALLOW_TEST_HTTP", "1")
	c := config()
	c.URL = "http://backend:8080"
	if _, e := New(c); e != nil {
		t.Fatal(e)
	}
	c.URL = "http://another:8080"
	if _, e := New(c); e == nil {
		t.Fatal("arbitrary test host")
	}
	if strings.Contains(fmt.Sprintf("%+v %#v", config(), config()), "synthetic-secret") {
		t.Fatal("config leaks")
	}
	t.Setenv("MULTICA_SERVER_URL", "https://example.invalid")
	t.Setenv("MULTICA_TOKEN", "mat_synthetic-secret")
	t.Setenv("MULTICA_WORKSPACE_ID", "ws")
	t.Setenv("MULTICA_AGENT_ID", "agent")
	t.Setenv("MULTICA_TASK_ID", "run")
	if _, e := FromEnv(); e != nil {
		t.Fatal(e)
	}
	t.Setenv("MULTICA_TOKEN", "user_token")
	_, e := FromEnv()
	code(t, e, "invalid_task_token")
}
func TestTaskPaginationAndIdentity(t *testing.T) {
	calls := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Header.Get("Authorization") != "Bearer mat_synthetic-secret" || r.Header.Get("X-Agent-ID") != "agent" || r.Header.Get("X-Task-ID") != "run" {
			t.Fatal("auth headers")
		}
		if calls == 1 {
			return response(200, `[]`, http.Header{"X-Agent-Tasks-Next-Cursor": []string{"cursor"}}), nil
		}
		if calls == 2 && r.URL.Query().Get("before") != "cursor" {
			t.Fatal("cursor missing")
		}
		return standard(r), nil
	})
	v, e := c.Task(context.Background())
	if e != nil || v.IssueKey != "SYN-1" || v.Description != "untrusted instructions" || len(v.AllowedParents) != 2 {
		t.Fatalf("%+v %v", v, e)
	}
	v.AllowedParents[0] = "changed"
	if c.scope.AllowedParents[0] != "trigger" {
		t.Fatal("scope alias")
	}
	for _, tc := range []struct{ old, new, err string }{{`"ws"`, `"else"`, "identity_mismatch"}, {`"running"`, `"completed"`, "task_not_running"}, {`"issue"`, `""`, "unsupported_task"}} {
		c := client(t, func(r *http.Request) (*http.Response, error) {
			return response(200, strings.Replace(taskJSON, tc.old, tc.new, 1), nil), nil
		})
		_, e := c.Task(context.Background())
		code(t, e, tc.err)
	}
}
func TestCommentsPagination(t *testing.T) {
	n := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(r.URL.Path, "/comments") {
			return standard(r), nil
		}
		n++
		if r.URL.Query().Get("recent") != "200" {
			t.Fatal("recent missing")
		}
		h := http.Header{}
		if n == 1 {
			h.Set("X-Multica-Next-Before", "2026-01-01T00:00:00Z")
			h.Set("X-Multica-Next-Before-Id", "c1")
		} else if r.URL.Query().Get("before_id") != "c1" {
			t.Fatal("cursor")
		}
		return response(200, fmt.Sprintf(`[{"id":"c%d","issue_id":"issue","content":"body","revision":3,"author_id":"agent","author_type":"agent","source_task_id":"original","parent_id":"direct"}]`, n), h), nil
	})
	rows, e := c.Comments(context.Background(), "issue")
	if e != nil || len(rows) != 2 || rows[0].Revision != "3" || rows[0].ParentID != "direct" || rows[0].SourceTaskID != "original" || rows[0].AuthorType != "agent" {
		t.Fatalf("%+v %v", rows, e)
	}
	_, e = c.Comments(context.Background(), "foreign")
	code(t, e, "issue_out_of_scope")
}
func TestReply(t *testing.T) {
	posts := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method != "POST" {
			return standard(r), nil
		}
		posts++
		var p map[string]any
		if e := json.NewDecoder(r.Body).Decode(&p); e != nil {
			t.Fatal(e)
		}
		if p["content"] != "approved" || p["parent_id"] != "other" || p["type"] != "comment" {
			t.Fatal(p)
		}
		return response(201, `{"id":"new","issue_id":"issue","source_task_id":"run","author_id":"agent","author_type":"agent","content":"approved","parent_id":"other"}`, nil), nil
	})
	id, e := c.Reply(context.Background(), "issue", "other", "approved")
	if e != nil || id != "new" {
		t.Fatalf("%s %v", id, e)
	}
	_, e = c.Reply(context.Background(), "issue", "foreign", "approved")
	code(t, e, "parent_out_of_scope")
	_, e = c.Reply(context.Background(), "foreign", "other", "approved")
	code(t, e, "issue_out_of_scope")
	if posts != 1 {
		t.Fatal(posts)
	}
}
func TestSafeFailures(t *testing.T) {
	for _, tc := range []struct{ body, header, err string }{{`[]`, "true", "comments_truncated"}, {`[{"id":"c","issue_id":"foreign"}]`, "", "invalid_response"}, {strings.Repeat("x", maxResponse+1), "", "response_too_large"}, {`not-json-secret`, "", "invalid_response"}} {
		c := client(t, func(r *http.Request) (*http.Response, error) {
			if !strings.HasSuffix(r.URL.Path, "/comments") {
				return standard(r), nil
			}
			h := http.Header{}
			h.Set("X-Comments-Truncated", tc.header)
			return response(200, tc.body, h), nil
		})
		_, e := c.Comments(context.Background(), "issue")
		code(t, e, tc.err)
	}
}
func TestAmbiguousPostNotRetried(t *testing.T) {
	n := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method != "POST" {
			return standard(r), nil
		}
		n++
		return nil, errors.New("token synthetic-secret")
	})
	_, e := c.Reply(context.Background(), "issue", "trigger", "approved")
	code(t, e, "transport")
	if !e.(*Error).Ambiguous || n != 1 {
		t.Fatal(e, n)
	}
}
func TestRedirectRefused(t *testing.T) {
	arrived := false
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/destination" {
			arrived = true
		}
		http.Redirect(w, r, "/destination", 302)
	}))
	defer s.Close()
	cfg := config()
	cfg.URL = s.URL
	c, e := New(cfg)
	if e != nil {
		t.Fatal(e)
	}
	_, e = c.Task(context.Background())
	code(t, e, "redirect_refused")
	if arrived {
		t.Fatal("redirect followed")
	}
}

func TestAssignmentReplyAndRuntime(t *testing.T) {
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method == "POST" {
			var p map[string]any
			json.NewDecoder(r.Body).Decode(&p)
			if p["parent_id"] != nil {
				t.Fatal(p)
			}
			return response(201, `{"id":"new","issue_id":"issue","source_task_id":"run","author_id":"agent","author_type":"agent","content":"approved","parent_id":null}`, nil), nil
		}
		if strings.HasSuffix(r.URL.Path, "/tasks") {
			return response(200, strings.Replace(taskJSON, `"trigger_comment_id":"trigger"`, `"trigger_comment_id":null`, 1), nil), nil
		}
		return standard(r), nil
	})
	if _, e := c.Reply(context.Background(), "issue", "", "approved"); e != nil {
		t.Fatal(e)
	}
	c.cfg.ExpectedRuntimeID = "expected"
	_, e := c.Task(context.Background())
	code(t, e, "runtime_mismatch")
}
func TestRefreshRejectsTerminal(t *testing.T) {
	terminal := false
	posts := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method == "POST" {
			posts++
		}
		if terminal && strings.HasSuffix(r.URL.Path, "/tasks") {
			return response(200, strings.ReplaceAll(taskJSON, `"running"`, `"cancelled"`), nil), nil
		}
		return standard(r), nil
	})
	if _, e := c.Task(context.Background()); e != nil {
		t.Fatal(e)
	}
	terminal = true
	_, e := c.Reply(context.Background(), "issue", "trigger", "approved")
	code(t, e, "task_not_running")
	if posts != 0 || c.scope != nil {
		t.Fatal("stale scope retained")
	}
}
func TestCursorAndStatusErrors(t *testing.T) {
	c := client(t, func(r *http.Request) (*http.Response, error) {
		h := http.Header{}
		h.Set("X-Agent-Tasks-Next-Cursor", "same")
		return response(200, `[]`, h), nil
	})
	_, e := c.Task(context.Background())
	code(t, e, "invalid_cursor")
	c = client(t, func(r *http.Request) (*http.Response, error) {
		return response(401, `sensitive server error synthetic-secret`, nil), nil
	})
	_, e = c.Task(context.Background())
	code(t, e, "unauthorized")
}
func TestReplyResponseBinding(t *testing.T) {
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method == "POST" {
			return response(201, `{"id":"new","issue_id":"issue","source_task_id":"wrong","author_id":"agent"}`, nil), nil
		}
		return standard(r), nil
	})
	_, e := c.Reply(context.Background(), "issue", "trigger", "approved")
	code(t, e, "reply_identity_mismatch")
	if !e.(*Error).Ambiguous {
		t.Fatal("unknown remote write reported safe")
	}
}

func TestReplyPreflightReadFailureIsDistinguishedFromWriteLoss(t *testing.T) {
	posts := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Method == http.MethodPost {
			posts++
			t.Fatal("POST issued after unsuccessful Task check")
		}
		if strings.HasSuffix(r.URL.Path, "/tasks") {
			return response(503, "unavailable", nil), nil
		}
		return standard(r), nil
	})
	_, e := c.Reply(context.Background(), "issue", "trigger", "approved")
	if got, ok := e.(*Error); !ok || !got.BeforePost || got.Ambiguous || got.Status != 503 || posts != 0 {
		t.Fatalf("preflight result %+v, posts=%d", e, posts)
	}
}

func TestTrustedRouteRejectsLegacyHTTPOptInAndDisablesProxy(t *testing.T) {
	t.Setenv("BRIDGE_ALLOW_TEST_HTTP", "1")
	c := config()
	c.TrustedRoute = true
	c.URL = "http://backend-a:8080"
	if _, err := New(c); err == nil {
		t.Fatal("legacy env enabled HTTP for supervised task")
	}
	c.SyntheticHTTP = true
	client, err := New(c)
	if err != nil {
		t.Fatal(err)
	}
	if client.http.Transport.(*http.Transport).Proxy != nil {
		t.Fatal("supervised task inherited an environment proxy")
	}
	c.URL = "http://example.invalid"
	if _, err := New(c); err == nil {
		t.Fatal("test opt-in allowed arbitrary HTTP")
	}
}
