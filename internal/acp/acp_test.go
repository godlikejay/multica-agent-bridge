package acp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"
)

type harness struct {
	input  *io.PipeWriter
	output *bufio.Scanner
	frames chan map[string]any
	done   chan error
}

func newHarness(t *testing.T, ctx context.Context, handler Handler) *harness {
	t.Helper()
	r, w := io.Pipe()
	outR, outW := io.Pipe()
	h := &harness{w, bufio.NewScanner(outR), make(chan map[string]any, 1), make(chan error, 1)}
	go func() { defer outW.Close(); h.done <- Serve(ctx, r, outW, handler) }()
	go func() {
		defer close(h.frames)
		for h.output.Scan() {
			var f map[string]any
			if json.Unmarshal(h.output.Bytes(), &f) != nil {
				return
			}
			h.frames <- f
		}
	}()
	t.Cleanup(func() { w.Close(); outR.Close() })
	return h
}
func (h *harness) send(t *testing.T, id any, method string, params map[string]any) {
	t.Helper()
	m := map[string]any{"jsonrpc": "2.0", "method": method, "params": params}
	if id != nil {
		m["id"] = id
	}
	if err := json.NewEncoder(h.input).Encode(m); err != nil {
		t.Fatal(err)
	}
}
func (h *harness) next(t *testing.T) map[string]any {
	t.Helper()
	select {
	case v, ok := <-h.frames:
		if !ok {
			t.Fatal("ACP output closed")
		}
		return v
	case <-time.After(3 * time.Second):
		t.Fatal("ACP response timeout")
		return nil
	}
}
func result(t *testing.T, f map[string]any, id any) map[string]any {
	t.Helper()
	if f["jsonrpc"] != "2.0" || f["id"] != id {
		t.Fatalf("unexpected envelope: %+v", f)
	}
	v, ok := f["result"].(map[string]any)
	if !ok {
		t.Fatalf("unexpected result: %+v", f)
	}
	return v
}
func (h *harness) setup(t *testing.T) string {
	t.Helper()
	h.send(t, 1, "initialize", map[string]any{"protocolVersion": 1})
	i := result(t, h.next(t), float64(1))
	if i["protocolVersion"] != float64(1) || i["agentInfo"].(map[string]any)["version"] != Version {
		t.Fatalf("wrong handshake: %+v", i)
	}
	h.send(t, "new", "session/new", map[string]any{"cwd": "/workspace/only-data", "mcpServers": []any{}})
	s := result(t, h.next(t), "new")
	if s["models"].(map[string]any)["currentModelId"] != modelID {
		t.Fatalf("wrong model: %+v", s)
	}
	return s["sessionId"].(string)
}
func prompt(s string) map[string]any {
	return map[string]any{"sessionId": s, "prompt": []map[string]string{{"type": "text", "text": "example"}, {"type": "text", "text": "second"}}}
}
func (h *harness) wait(t *testing.T, want error) {
	t.Helper()
	select {
	case err := <-h.done:
		if !errors.Is(err, want) {
			t.Fatalf("Serve err = %v, want %v", err, want)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Serve did not stop")
	}
}

func TestHandshakeAndPrompt(t *testing.T) {
	calls := make(chan Prompt, 2)
	h := newHarness(t, context.Background(), func(_ context.Context, p Prompt) (string, error) { calls <- p; return "operator's final reply", nil })
	s := h.setup(t)
	h.send(t, 3, "session/resume", map[string]any{"sessionId": "unknown", "cwd": "/workspace/only-data"})
	e := h.next(t)
	if !strings.Contains(e["error"].(map[string]any)["message"].(string), "Session not found") {
		t.Fatalf("wrong lost-session response: %+v", e)
	}
	h.send(t, 4, "session/resume", map[string]any{"sessionId": s, "cwd": "/workspace/only-data"})
	if result(t, h.next(t), float64(4))["sessionId"] != s {
		t.Fatal("known resume did not keep session")
	}
	h.send(t, 5, "session/set_model", map[string]any{"sessionId": s, "modelId": modelID})
	result(t, h.next(t), float64(5))
	if len(calls) != 0 {
		t.Fatal("probe invoked handler")
	}
	h.send(t, "prompt", "session/prompt", prompt(s))
	f := h.next(t)
	p := f["params"].(map[string]any)
	u := p["update"].(map[string]any)
	if f["method"] != "session/update" || p["sessionId"] != s || u["sessionUpdate"] != "agent_message_chunk" || u["content"].(map[string]any)["text"] != "operator's final reply" {
		t.Fatalf("bad output: %+v", f)
	}
	if result(t, h.next(t), "prompt")["stopReason"] != "end_turn" {
		t.Fatal("wrong stop reason")
	}
	if got := <-calls; got != (Prompt{s, "/workspace/only-data", "example\nsecond"}) {
		t.Fatalf("wrong prompt: %+v", got)
	}
	h.input.Close()
	h.wait(t, nil)
}

func TestWaitingPromptCancellation(t *testing.T) {
	started, cancelled := make(chan struct{}), make(chan struct{})
	h := newHarness(t, context.Background(), func(ctx context.Context, p Prompt) (string, error) {
		close(started)
		<-ctx.Done()
		close(cancelled)
		return "late text must not be sent", ctx.Err()
	})
	s := h.setup(t)
	h.send(t, 3, "session/prompt", prompt(s))
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("handler not started")
	}
	h.send(t, 4, "session/prompt", prompt(s))
	f := h.next(t)
	if f["id"] != float64(4) || f["error"] == nil {
		t.Fatalf("concurrent prompt accepted: %+v", f)
	}
	h.send(t, nil, "session/cancel", map[string]any{"sessionId": s})
	if result(t, h.next(t), float64(3))["stopReason"] != "cancelled" {
		t.Fatal("wrong stop reason")
	}
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("handler context not cancelled")
	}
	h.send(t, 5, "session/set_model", map[string]any{"sessionId": s, "modelId": modelID})
	result(t, h.next(t), float64(5))
	h.input.Close()
	h.wait(t, nil)
}

func TestWaitingPromptEOFAndContext(t *testing.T) {
	for _, byEOF := range []bool{true, false} {
		t.Run(map[bool]string{true: "EOF", false: "lifecycle context"}[byEOF], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			started, done := make(chan struct{}), make(chan struct{})
			h := newHarness(t, ctx, func(ctx context.Context, _ Prompt) (string, error) {
				close(started)
				<-ctx.Done()
				close(done)
				return "", ctx.Err()
			})
			s := h.setup(t)
			h.send(t, 3, "session/prompt", prompt(s))
			select {
			case <-started:
			case <-time.After(3 * time.Second):
				t.Fatal("handler not started")
			}
			if byEOF {
				h.input.Close()
				h.wait(t, nil)
			} else {
				cancel()
				h.wait(t, context.Canceled)
			}
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("still running after disconnect")
			}
		})
	}
}

func TestUnsupportedInputsAndRedactedHandlerFailure(t *testing.T) {
	calls := 0
	h := newHarness(t, context.Background(), func(context.Context, Prompt) (string, error) {
		calls++
		return "do-not-leak-text", errors.New("do-not-leak-secret")
	})
	s := h.setup(t)
	h.send(t, 3, "session/new", map[string]any{"cwd": "/workspace/ignored", "mcpServers": []map[string]string{{"type": "stdio", "command": "not-executed"}}})
	if h.next(t)["error"] == nil {
		t.Fatal("MCP negotiation accepted")
	}
	h.send(t, 4, "session/prompt", map[string]any{"sessionId": s, "prompt": []map[string]string{{"type": "resource_link", "uri": "file:///forbidden"}}})
	if h.next(t)["error"] == nil {
		t.Fatal("unsupported prompt block accepted")
	}
	h.send(t, 5, "session/prompt", prompt(s))
	f := h.next(t)
	payload, _ := json.Marshal(f)
	if f["id"] != float64(5) || f["error"] == nil || strings.Contains(string(payload), "do-not-leak") {
		t.Fatalf("unexpected/redaction failure: %s", payload)
	}
	h.input.Close()
	h.wait(t, nil)
	if calls != 1 {
		t.Fatalf("unexpected task handler count: %d", calls)
	}
}
