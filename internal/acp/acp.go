// Package acp implements the minimal, local-only ACP protocol used by the
// pinned Multica Hermes provider. It never opens a workspace or runs a tool.
package acp

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

// Version is the development version advertised in the ACP handshake.
const Version = "0.1.0-dev"

const modelID = "bridge:manual"
const maxFrameBytes = 1 << 20
const maxSessions = 64

type Prompt struct {
	SessionID string
	CWD       string
	Text      string
}
type Handler func(context.Context, Prompt) (string, error)

type request struct {
	Version string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}
type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}
type sessionParams struct {
	CWD        string            `json:"cwd"`
	SessionID  string            `json:"sessionId"`
	MCPServers []json.RawMessage `json:"mcpServers"`
}
type promptParams struct {
	SessionID string `json:"sessionId"`
	Blocks    []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"prompt"`
}
type active struct {
	id        json.RawMessage
	session   string
	cancel    context.CancelFunc
	cancelled bool
}
type outcome struct {
	text string
	err  error
}
type frame struct {
	data []byte
	err  error
	done bool
}

func models() map[string]any {
	return map[string]any{"models": map[string]any{"currentModelId": modelID, "availableModels": []map[string]string{{"modelId": modelID, "name": "Manual bridge", "description": "Waits for an explicit local operator action"}}}}
}
func validID(id json.RawMessage) bool {
	if len(id) == 0 {
		return true
	}
	switch id[0] {
	case '"', '-', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9':
		return true
	}
	return string(id) == "null"
}

// Serve handles newline-delimited JSON-RPC. Closing input or cancelling ctx
// cancels a waiting handler. The caller owns the streams; to interrupt a
// blocking read after Serve returns, close in if it is an io.ReadCloser.
func Serve(ctx context.Context, in io.Reader, out io.Writer, handle Handler) error {
	if handle == nil {
		return errors.New("ACP prompt handler is required")
	}
	ctx, stop := context.WithCancel(ctx)
	defer stop()
	scanEvents := make(chan frame)
	completed := make(chan outcome, 1)
	go func() {
		scan := bufio.NewScanner(in)
		scan.Buffer(make([]byte, 4096), maxFrameBytes)
		send := func(f frame) bool {
			select {
			case scanEvents <- f:
				return true
			case <-ctx.Done():
				return false
			}
		}
		for scan.Scan() {
			if !send(frame{data: append([]byte(nil), scan.Bytes()...)}) {
				return
			}
		}
		send(frame{err: scan.Err(), done: true})
	}()
	encoder := json.NewEncoder(out)
	send := func(msg map[string]any) error {
		msg["jsonrpc"] = "2.0"
		if err := encoder.Encode(msg); err != nil {
			return fmt.Errorf("write ACP response: %w", err)
		}
		return nil
	}
	ok := func(id json.RawMessage, result any) error {
		if len(id) == 0 {
			return nil
		}
		return send(map[string]any{"id": id, "result": result})
	}
	fail := func(id json.RawMessage, code int, message string) error {
		if len(id) == 0 {
			return nil
		}
		return send(map[string]any{"id": id, "error": rpcError{code, message}})
	}
	nullID := json.RawMessage("null")
	sessions := map[string]string{}
	initialized := false
	var current *active
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case result := <-completed:
			a := current
			current = nil
			a.cancel()
			if a.cancelled {
				continue
			}
			switch {
			case errors.Is(result.err, context.Canceled):
				if err := ok(a.id, map[string]string{"stopReason": "cancelled"}); err != nil {
					return err
				}
			case result.err != nil:
				// A Handler upstream error could contain task tokens; don't echo it.
				if err := fail(a.id, -32603, "Bridge prompt failed"); err != nil {
					return err
				}
			default:
				if result.text != "" {
					if err := send(map[string]any{"method": "session/update", "params": map[string]any{"sessionId": a.session, "update": map[string]any{"sessionUpdate": "agent_message_chunk", "content": map[string]string{"type": "text", "text": result.text}}}}); err != nil {
						return err
					}
				}
				if err := ok(a.id, map[string]string{"stopReason": "end_turn"}); err != nil {
					return err
				}
			}
		case f := <-scanEvents:
			if f.done {
				return f.err
			}
			if len(strings.TrimSpace(string(f.data))) == 0 {
				continue
			}
			var r request
			if !json.Valid(f.data) {
				if err := fail(nullID, -32700, "Parse error"); err != nil {
					return err
				}
				continue
			}
			if json.Unmarshal(f.data, &r) != nil || r.Version != "2.0" || r.Method == "" || !validID(r.ID) {
				if err := fail(nullID, -32600, "Invalid request"); err != nil {
					return err
				}
				continue
			}
			// Notifications cannot start a session or business task.
			if len(r.ID) == 0 && r.Method != "session/cancel" {
				continue
			}
			bad := func() error { return fail(r.ID, -32602, "Invalid params") }
			missing := func() error { return fail(r.ID, -32000, "Session not found") }
			var err error
			if !initialized && r.Method != "initialize" {
				err = fail(r.ID, -32000, "Initialize first")
				if err != nil {
					return err
				}
				continue
			}
			switch r.Method {
			case "initialize":
				var p struct {
					ProtocolVersion int `json:"protocolVersion"`
				}
				if json.Unmarshal(r.Params, &p) != nil || p.ProtocolVersion < 1 {
					err = bad()
					break
				}
				initialized = true
				err = ok(r.ID, map[string]any{"protocolVersion": 1, "agentInfo": map[string]string{"name": "multica-agent-bridge", "title": "Multica Agent Bridge", "version": Version}, "agentCapabilities": map[string]any{"loadSession": false, "mcpCapabilities": map[string]bool{"http": false, "sse": false}, "promptCapabilities": map[string]bool{"image": false, "audio": false, "embeddedContext": false}}})
			case "session/new":
				var p sessionParams
				if json.Unmarshal(r.Params, &p) != nil || p.CWD == "" || len(p.MCPServers) != 0 {
					err = bad()
					break
				}
				if len(sessions) >= maxSessions {
					err = fail(r.ID, -32000, "Session limit reached")
					break
				}
				var b [16]byte
				if _, err = rand.Read(b[:]); err != nil {
					return fmt.Errorf("create ACP session ID: %w", err)
				}
				id := hex.EncodeToString(b[:])
				sessions[id] = p.CWD
				result := models()
				result["sessionId"] = id
				err = ok(r.ID, result)
			case "session/resume":
				var p sessionParams
				if json.Unmarshal(r.Params, &p) != nil || p.SessionID == "" || len(p.MCPServers) != 0 {
					err = bad()
					break
				}
				cwd, exists := sessions[p.SessionID]
				if !exists {
					err = missing()
					break
				}
				if p.CWD != "" && p.CWD != cwd {
					err = bad()
					break
				}
				result := models()
				result["sessionId"] = p.SessionID
				err = ok(r.ID, result)
			case "session/set_model":
				var p struct {
					SessionID string `json:"sessionId"`
					ModelID   string `json:"modelId"`
				}
				if json.Unmarshal(r.Params, &p) != nil || p.SessionID == "" {
					err = bad()
					break
				}
				if _, exists := sessions[p.SessionID]; !exists {
					err = missing()
					break
				}
				if p.ModelID != modelID {
					err = bad()
					break
				}
				err = ok(r.ID, struct{}{})
			case "session/prompt":
				var p promptParams
				if json.Unmarshal(r.Params, &p) != nil || p.SessionID == "" || len(p.Blocks) == 0 {
					err = bad()
					break
				}
				cwd, exists := sessions[p.SessionID]
				if !exists {
					err = missing()
					break
				}
				if current != nil {
					err = fail(r.ID, -32000, "A prompt is already active")
					break
				}
				texts := make([]string, 0, len(p.Blocks))
				for _, b := range p.Blocks {
					if b.Type != "text" {
						break
					}
					texts = append(texts, b.Text)
				}
				if len(texts) != len(p.Blocks) || strings.TrimSpace(strings.Join(texts, "\n")) == "" {
					err = bad()
					break
				}
				promptCtx, cancel := context.WithCancel(ctx)
				current = &active{id: r.ID, session: p.SessionID, cancel: cancel}
				go func(p Prompt) { text, err := handle(promptCtx, p); completed <- outcome{text, err} }(Prompt{p.SessionID, cwd, strings.Join(texts, "\n")})
			case "session/cancel":
				var p sessionParams
				if json.Unmarshal(r.Params, &p) != nil || p.SessionID == "" {
					err = bad()
					break
				}
				if _, exists := sessions[p.SessionID]; !exists {
					err = missing()
					break
				}
				if a := current; a != nil && a.session == p.SessionID && !a.cancelled {
					a.cancelled = true
					a.cancel()
					err = ok(a.id, map[string]string{"stopReason": "cancelled"})
					if err != nil {
						return err
					}
				}
				err = ok(r.ID, struct{}{})
			default:
				err = fail(r.ID, -32601, "Method not found")
			}
			if err != nil {
				return err
			}
		}
	}
}
