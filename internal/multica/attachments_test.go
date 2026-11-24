package multica

import (
	"context"
	"io"
	"net/http"
	"net/netip"
	"strings"
	"testing"
)

func blobResponse(body string) *http.Response {
	r := response(200, body, nil)
	r.ContentLength = -1
	return r
}

const attachmentRows = `[{"id":"file-1","workspace_id":"ws","issue_id":"issue","comment_id":null,"url":"https://storage.example.invalid/private-object","markdown_url":"https://web.example.invalid/api/attachments/file-1/download","filename":"screen.png","content_type":"image/png","size_bytes":3},
	{"id":"file-2","workspace_id":"ws","issue_id":"issue","comment_id":"queued-comment","url":"","markdown_url":"/api/attachments/file-2/download","filename":"not-automatic","size_bytes":3}]`

func TestAttachmentLookupScopeAndCurrentRun(t *testing.T) {
	hits := 0
	terminal := false
	c := client(t, func(r *http.Request) (*http.Response, error) {
		if r.Header.Get("Authorization") != "Bearer mat_synthetic-secret" || r.URL.Host != "example.invalid" {
			t.Fatal("wrong request trust boundary")
		}
		if strings.HasSuffix(r.URL.Path, "/tasks") && terminal {
			return response(200, strings.Replace(taskJSON, `"running"`, `"cancelled"`, 1), nil), nil
		}
		switch r.URL.Path {
		case "/api/issues/issue/attachments":
			return response(200, attachmentRows, nil), nil
		case "/api/attachments/file-2/download":
			hits++
			terminal = hits == 2
			return blobResponse("abc"), nil
		default:
			return standard(r), nil
		}
	})
	ctx := context.Background()
	for _, u := range []string{"https://unrelated.example.invalid/api/attachments/file-2/download", "/api/attachments/not-in-issue/download", "https://web.example.invalid/api/attachments/file-1/download?X-Amz-Signature=fake"} {
		code(t, c.FetchAttachment(ctx, "issue", u, io.Discard), "attachment_not_in_issue")
	}
	if hits != 0 {
		t.Fatal("nonmember reached byte endpoint")
	}
	code(t, c.FetchAttachment(ctx, "another-issue", "/api/attachments/file-2/download", io.Discard), "issue_out_of_scope")
	var bytes strings.Builder
	if err := c.FetchAttachment(ctx, "issue", "/api/attachments/file-2/download", &bytes); err != nil || bytes.String() != "abc" {
		t.Fatalf("valid same-Issue URL: %v / %q", err, bytes.String())
	}
	// file-2 is from a different (not granted or delivered) comment. The
	// explicit operator read intentionally authorizes Issue membership.
	code(t, c.FetchAttachment(ctx, "issue", "/api/attachments/file-2/download", io.Discard), "task_not_running")
}

func TestAttachmentRedirectUsesStorageWithoutCredentials(t *testing.T) {
	redirect := "https://storage.example.invalid/object?X-Amz-Signature=discard"
	storageCalls := 0
	c := client(t, func(r *http.Request) (*http.Response, error) {
		switch r.URL.Path {
		case "/api/issues/issue/attachments":
			return response(200, attachmentRows, nil), nil
		case "/api/attachments/file-1/download":
			h := make(http.Header)
			h.Set("Location", redirect)
			return response(302, "", h), nil
		default:
			return standard(r), nil
		}
	})
	c.storageHTTP.Transport = transport(func(r *http.Request) (*http.Response, error) {
		storageCalls++
		if r.URL.Host != "storage.example.invalid" || r.URL.Query().Get("X-Amz-Signature") != "discard" {
			t.Fatalf("unexpected storage route %s", r.URL.Host)
		}
		for _, h := range []string{"Authorization", "X-Agent-ID", "X-Task-ID", "Cookie", "Referer"} {
			if r.Header.Get(h) != "" {
				t.Fatalf("unexpected forwarded credential header %s", h)
			}
		}
		return blobResponse("abc"), nil
	})
	ctx := context.Background()
	u := "https://web.example.invalid/api/attachments/file-1/download"
	var data strings.Builder
	if err := c.FetchAttachment(ctx, "issue", u, &data); err != nil || data.String() != "abc" || storageCalls != 1 {
		t.Fatalf("approved storage: %v %q calls=%d", err, data.String(), storageCalls)
	}
	for _, where := range []string{"http://storage.example.invalid/refuse", "https://outside.example.invalid/refuse", "https://u:p@storage.example.invalid/bad", "https://storage.example.invalid:8443/bad", "/api/attachments/file-1/download"} {
		redirect = where
		code(t, c.FetchAttachment(ctx, "issue", u, io.Discard), "attachment_redirect_refused")
	}
	if storageCalls != 1 {
		t.Fatal("unapproved redirect was followed")
	}
	for _, ip := range []string{"127.0.0.1", "10.0.0.1", "169.254.169.254", "100.100.100.200", "::1", "::ffff:192.168.1.1", "fc00::1"} {
		if publicStorageAddress(netip.MustParseAddr(ip)) {
			t.Fatalf("unexpected route to %s", ip)
		}
	}
}

func TestAttachmentBodyMustMatchInventorySize(t *testing.T) {
	c := client(t, func(r *http.Request) (*http.Response, error) {
		switch r.URL.Path {
		case "/api/issues/issue/attachments":
			return response(200, attachmentRows, nil), nil
		case "/api/attachments/file-1/download":
			return blobResponse("larger than advertised"), nil
		default:
			return standard(r), nil
		}
	})
	var out strings.Builder
	code(t, c.FetchAttachment(context.Background(), "issue", "/api/attachments/file-1/download", &out), "attachment_size_mismatch")
	if out.Len() > 4 {
		t.Fatal("unbounded download before mismatch")
	}
}
