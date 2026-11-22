package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const controlDir = "/tmp/multica-bridge-control"
const controlTimeout = 3 * time.Second
const controlLimit = 1 << 20

type controlRequest struct {
	Action string `json:"action"`
	ID     string `json:"id,omitempty"`
	All    bool   `json:"all,omitempty"`
}

type ConnectionStatus struct {
	ID      string `json:"id"`
	Enabled bool   `json:"enabled"`
	State   string `json:"state"`
}

type controlResponse struct {
	Connections []ConnectionStatus `json:"connections,omitempty"`
	Error       string             `json:"error,omitempty"`
}

type connectionWorker struct {
	state  string
	cancel context.CancelFunc
	wake   chan struct{}
}

type controller struct {
	mu      sync.Mutex
	path    string
	ids     []string
	stopped map[string]bool
	workers map[string]*connectionWorker
}

func newController(root string, cfg Config) (*controller, error) {
	m := &controller{
		path:    filepath.Join(root, ".stopped-connections.json"),
		stopped: make(map[string]bool),
		workers: make(map[string]*connectionWorker, len(cfg.Connections)),
	}
	if _, err := os.Lstat(m.path); err == nil {
		b, err := readFile(m.path, controlLimit)
		if err != nil || json.Unmarshal(b, &m.stopped) != nil || m.stopped == nil {
			return nil, errors.New("cannot read saved connection state")
		}
		for id, stopped := range m.stopped {
			if !idRE.MatchString(id) || !stopped {
				return nil, errors.New("invalid saved connection state")
			}
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, errors.New("cannot inspect saved connection state")
	}
	for _, c := range cfg.Connections {
		m.ids = append(m.ids, c.ID)
		state := "starting"
		if m.stopped[c.ID] {
			state = "stopped"
		}
		m.workers[c.ID] = &connectionWorker{state: state, wake: make(chan struct{}, 1)}
	}
	return m, nil
}

func (m *controller) request(req controlRequest) controlResponse {
	m.mu.Lock()
	defer m.mu.Unlock()
	if req.Action != "status" && req.Action != "start" && req.Action != "stop" {
		return controlResponse{Error: "unknown connection command"}
	}
	if (req.ID != "" && req.All) || (req.Action != "status" && req.ID == "" && !req.All) {
		return controlResponse{Error: "specify one connection ID or --all"}
	}
	ids := m.ids
	if req.ID != "" {
		if m.workers[req.ID] == nil {
			return controlResponse{Error: "unknown connection ID"}
		}
		ids = []string{req.ID}
	}
	if req.Action != "status" {
		stop := req.Action == "stop"
		next := make(map[string]bool, len(m.stopped))
		for id := range m.stopped {
			next[id] = true
		}
		for _, id := range ids {
			if stop {
				next[id] = true
			} else {
				delete(next, id)
			}
		}
		b, err := json.Marshal(next)
		if err != nil || len(b) > controlLimit || writePrivateAtomic(m.path, string(b)) != nil {
			return controlResponse{Error: "cannot save connection state; no change applied"}
		}
		m.stopped = next
		for _, id := range ids {
			w := m.workers[id]
			switch {
			case stop && w.cancel != nil:
				w.state = "stopping"
				w.cancel()
			case stop:
				w.state = "stopped"
			case w.cancel == nil:
				w.state = "starting"
			}
			select {
			case w.wake <- struct{}{}:
			default:
			}
		}
	}
	var res controlResponse
	for _, id := range ids {
		res.Connections = append(res.Connections, ConnectionStatus{id, !m.stopped[id], m.workers[id].state})
	}
	return res
}

// Each connection has one worker, even when stop and start arrive back to back.
func (m *controller) run(ctx context.Context, id string, supervise func(context.Context, func(string))) {
	w := m.workers[id]
	for ctx.Err() == nil {
		m.mu.Lock()
		if m.stopped[id] {
			w.state = "stopped"
			m.mu.Unlock()
			select {
			case <-w.wake:
			case <-ctx.Done():
			}
			continue
		}
		active, cancel := context.WithCancel(ctx)
		w.cancel = cancel
		w.state = "starting"
		m.mu.Unlock()
		supervise(active, func(state string) {
			m.mu.Lock()
			defer m.mu.Unlock()
			if active.Err() == nil {
				w.state = state
			}
		})
		cancel()
		m.mu.Lock()
		w.cancel = nil
		w.state = "stopped"
		m.mu.Unlock()
	}
}

func listenControl(dir string) (*net.UnixListener, func(), error) {
	unlock, err := lockDir(dir)
	if err != nil {
		return nil, nil, err
	}
	fail := func(err error) (*net.UnixListener, func(), error) {
		unlock()
		return nil, nil, err
	}
	info, err := os.Lstat(dir)
	if err != nil || info.Mode().Perm() != 0700 {
		return fail(errors.New("control directory must be private"))
	}
	path := filepath.Join(dir, "control.sock")
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return fail(errors.New("control path is not a socket"))
		}
		if err := os.Remove(path); err != nil {
			return fail(err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fail(err)
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return fail(err)
	}
	return listener, func() { _ = listener.Close(); unlock() }, nil
}

func (m *controller) serveControl(ctx context.Context, listener *net.UnixListener) {
	for ctx.Err() == nil {
		conn, err := listener.AcceptUnix()
		if err != nil {
			return
		}
		_ = conn.SetDeadline(time.Now().Add(controlTimeout))
		decoder := json.NewDecoder(io.LimitReader(conn, 1024))
		decoder.DisallowUnknownFields()
		var req controlRequest
		res := controlResponse{Error: "invalid control request"}
		if decoder.Decode(&req) == nil && ctx.Err() == nil {
			res = m.request(req)
		}
		_ = json.NewEncoder(conn).Encode(res)
		_ = conn.Close()
	}
}

// Control is the operator CLI. It has no direct access to daemon credentials.
func Control(ctx context.Context, args []string, out io.Writer) error {
	return control(ctx, controlDir, args, out)
}

func control(ctx context.Context, dir string, args []string, out io.Writer) error {
	if len(args) == 0 || len(args) > 2 {
		return errors.New("usage: connection status [ID] | connection {start|stop} {ID|--all}")
	}
	req := controlRequest{Action: args[0]}
	if len(args) == 2 {
		req.ID, req.All = args[1], args[1] == "--all"
		if req.All {
			req.ID = ""
		}
	}
	dialer := net.Dialer{Timeout: controlTimeout}
	conn, err := dialer.DialContext(ctx, "unix", filepath.Join(dir, "control.sock"))
	if err != nil {
		return errors.New("cannot reach the local bridge supervisor")
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(controlTimeout))
	var res controlResponse
	if json.NewEncoder(conn).Encode(req) != nil ||
		json.NewDecoder(io.LimitReader(conn, controlLimit)).Decode(&res) != nil {
		return errors.New("no control response; check connection status before retrying")
	}
	if res.Error != "" {
		return errors.New(res.Error)
	}
	return json.NewEncoder(out).Encode(res)
}
