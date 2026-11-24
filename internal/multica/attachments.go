package multica

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"unicode"
)

const MaxAttachment = 32 << 20

// Attachment contains public metadata only. upstreamID and storageOrigin are
// used exclusively to verify the inventory match and retrieve bytes.
type Attachment struct {
	URL           string `json:"url"`
	Filename      string `json:"filename"`
	ContentType   string `json:"content_type"`
	SizeBytes     int64  `json:"size_bytes"`
	CommentID     string `json:"-"`
	upstreamID    string
	storageOrigin string
}

type wireAttachment struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace_id"`
	IssueID     string `json:"issue_id"`
	CommentID   string `json:"comment_id"`
	MarkdownURL string `json:"markdown_url"`
	StorageURL  string `json:"url"`
	Filename    string `json:"filename"`
	ContentType string `json:"content_type"`
	SizeBytes   int64  `json:"size_bytes"`
}

func attachmentPath(id string) string { return "/api/attachments/" + id + "/download" }

func parseAttachmentURL(raw string) (*url.URL, bool) {
	if raw == "" || len(raw) > 4096 || strings.ContainsAny(raw, "\\\r\n\t") || strings.IndexFunc(raw, unicode.IsControl) >= 0 {
		return nil, false
	}
	u, err := url.Parse(raw)
	if err != nil || u.Opaque != "" || u.User != nil || u.Fragment != "" {
		return nil, false
	}
	return u, true
}

func stableAttachmentURL(raw, id string) bool {
	u, ok := parseAttachmentURL(raw)
	if !ok || u.RawQuery != "" || u.ForceQuery || u.Path == "" {
		return false
	}
	if !u.IsAbs() {
		return u.Host == "" && u.EscapedPath() == attachmentPath(id) && raw == attachmentPath(id)
	}
	return u.Host != "" && (u.Scheme == "https" || (u.Scheme == "http" && strings.HasSuffix(u.Path, attachmentPath(id))))
}

func storageOrigin(raw string) string {
	u, ok := parseAttachmentURL(raw)
	if !ok || u.Scheme != "https" || u.Hostname() == "" || (u.Port() != "" && u.Port() != "443") {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

func (c *Client) Attachments(ctx context.Context, issueID string) ([]Attachment, error) {
	if _, err := c.scoped(ctx, issueID); err != nil {
		return nil, err
	}
	var rows []wireAttachment
	if _, _, err := c.request(ctx, http.MethodGet, "/api/issues/"+issueID+"/attachments", nil, nil, &rows); err != nil {
		return nil, err
	}
	if len(rows) > 512 {
		return nil, fail("attachment_inventory_too_large")
	}
	out := make([]Attachment, 0, len(rows))
	for _, r := range rows {
		if r.IssueID != issueID || r.WorkspaceID != c.cfg.WorkspaceID || !validID(r.ID) || (r.CommentID != "" && !validID(r.CommentID)) || r.SizeBytes < 0 {
			return nil, fail("attachment_identity_mismatch")
		}
		stable := r.MarkdownURL
		if !stableAttachmentURL(stable, r.ID) {
			stable = attachmentPath(r.ID)
		}
		name := strings.Map(func(ch rune) rune {
			if unicode.IsControl(ch) {
				return -1
			}
			return ch
		}, r.Filename)
		if len(name) > 255 {
			name = "attachment"
		}
		media := strings.Map(func(ch rune) rune {
			if unicode.IsControl(ch) {
				return -1
			}
			return ch
		}, r.ContentType)
		if len(media) > 128 {
			media = ""
		}
		out = append(out, Attachment{URL: stable, Filename: name, ContentType: media, SizeBytes: r.SizeBytes, CommentID: r.CommentID, upstreamID: r.ID, storageOrigin: storageOrigin(r.StorageURL)})
	}
	return out, nil
}

func (c *Client) matchesAttachment(raw string, a Attachment) bool {
	if raw == a.URL || raw == attachmentPath(a.upstreamID) {
		return true
	}
	u := *c.base
	u.Path += attachmentPath(a.upstreamID)
	return raw == u.String()
}

// FetchAttachment never dereferences a user-provided URL. It first refreshes
// the exact running task, matches an Issue inventory entry, then uses the
// authenticated route constructed from the verified upstream identifier.
func (c *Client) FetchAttachment(ctx context.Context, issueID, original string, dst io.Writer) error {
	if _, ok := parseAttachmentURL(original); !ok {
		return fail("invalid_attachment_url")
	}
	t, err := c.Task(ctx)
	if err != nil {
		return err
	}
	if t.IssueID != issueID {
		return fail("issue_out_of_scope")
	}
	list, err := c.Attachments(ctx, issueID)
	if err != nil {
		return err
	}
	var found *Attachment
	for i := range list {
		if c.matchesAttachment(original, list[i]) {
			if found != nil {
				return fail("ambiguous_attachment")
			}
			found = &list[i]
		}
	}
	if found == nil {
		return fail("attachment_not_in_issue")
	}
	if found.SizeBytes > MaxAttachment {
		return fail("attachment_too_large")
	}
	u := *c.base
	u.Path += attachmentPath(found.upstreamID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return fail("invalid_request")
	}
	req.Header.Set("Authorization", "Bearer "+c.cfg.Token)
	req.Header.Set("X-Agent-ID", c.cfg.AgentID)
	req.Header.Set("X-Task-ID", c.cfg.RunID)
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := c.http.Do(req)
	if err != nil {
		return fail("attachment_transport")
	}
	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusTemporaryRedirect {
		location := resp.Header.Get("Location")
		resp.Body.Close()
		target, ok := parseAttachmentURL(location)
		if !ok || found.storageOrigin == "" || storageOrigin(location) != found.storageOrigin || target.Scheme != "https" ||
			strings.EqualFold(target.Hostname(), c.base.Hostname()) {
			return fail("attachment_redirect_refused")
		}
		// A new request, separate client, no Authorization, task identity,
		// cookies, Referer, proxy, or redirect chain. Resolve and pin only
		// public storage addresses when dialing.
		external, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
		if err != nil {
			return fail("invalid_request")
		}
		external.Header.Set("Accept-Encoding", "identity")
		resp, err = c.storageHTTP.Do(external)
		if err != nil {
			return fail("attachment_transport")
		}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return &Error{Code: "attachment_download_failed", Status: resp.StatusCode}
	}
	if resp.ContentLength > MaxAttachment || (resp.ContentLength >= 0 && resp.ContentLength != found.SizeBytes) {
		return fail("attachment_size_mismatch")
	}
	n, err := io.Copy(dst, io.LimitReader(resp.Body, found.SizeBytes+1))
	if err != nil {
		return fail("attachment_read_failed")
	}
	if n != found.SizeBytes {
		return fail("attachment_size_mismatch")
	}
	fresh, err := c.Task(ctx)
	if err != nil {
		return err
	}
	if fresh.IssueID != t.IssueID || fresh.AgentID != t.AgentID {
		return fail("identity_mismatch")
	}
	return ctx.Err()
}

func publicStorageAddress(ip netip.Addr) bool {
	ip = ip.Unmap()
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !netip.MustParsePrefix("100.64.0.0/10").Contains(ip) &&
		!netip.MustParsePrefix("192.0.0.0/24").Contains(ip) && !netip.MustParsePrefix("198.18.0.0/15").Contains(ip) &&
		!netip.MustParsePrefix("2001::/32").Contains(ip) && !netip.MustParsePrefix("2002::/16").Contains(ip) &&
		!netip.MustParsePrefix("64:ff9b::/96").Contains(ip) && !netip.MustParsePrefix("64:ff9b:1::/48").Contains(ip)
}

func dialPublicStorage(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil || port != "443" {
		return nil, fail("attachment_storage_route_refused")
	}
	ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(ips) == 0 {
		return nil, fail("attachment_storage_route_refused")
	}
	for _, ip := range ips {
		if !publicStorageAddress(ip) {
			return nil, fail("attachment_storage_route_refused")
		}
	}
	dialer := net.Dialer{}
	for _, ip := range ips {
		conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
		if err == nil {
			return conn, nil
		}
	}
	return nil, fail("attachment_storage_unreachable")
}
