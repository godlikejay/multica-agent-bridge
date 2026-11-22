package supervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

const controlTestTimeout = 4 * time.Second

func controlFixture() Config {
	return Config{Connections: []Connection{
		{ID: "alpha", ServerURL: "https://a.example.invalid", TokenFile: "/fixture/alpha-token-no-print", MaxActiveRuns: 1},
		{ID: "beta", ServerURL: "https://b.example.invalid", TokenFile: "/fixture/beta-token-no-print", MaxActiveRuns: 1},
	}}
}

func fixtureController(t *testing.T, root string) *controller {
	t.Helper()
	m, err := newController(root, controlFixture())
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func mustControl(t *testing.T, m *controller, action, id string, all bool) controlResponse {
	t.Helper()
	req := controlRequest{Action: action, ID: id, All: all}
	resp := m.request(req)
	if resp.Error != "" {
		t.Fatalf("%s id=%q all=%v: %s", action, id, all, resp.Error)
	}
	return resp
}

func statuses(resp controlResponse) map[string]ConnectionStatus {
	got := make(map[string]ConnectionStatus)
	for _, c := range resp.Connections {
		got[c.ID] = c
	}
	return got
}

func waitControl(t *testing.T, m *controller, id, want string, enabled bool) {
	t.Helper()
	limit := time.NewTimer(controlTestTimeout)
	defer limit.Stop()
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
	for {
		s := statuses(mustControl(t, m, "status", "", true))[id]
		if s.ID == id && s.State == want && s.Enabled == enabled {
			return
		}
		select {
		case <-limit.C:
			t.Fatalf("%s state=%q enabled=%v, want %q enabled=%v", id, s.State, s.Enabled, want, enabled)
		case <-tick.C:
		}
	}
}

type workerSignals struct {
	started   chan string
	cancelled chan string
	exited    chan string
	mu        sync.Mutex
	active    map[string]int
	peak      map[string]int
	launches  map[string]int
}

func newWorkerSignals() *workerSignals {
	return &workerSignals{
		started: make(chan string, 20), cancelled: make(chan string, 20), exited: make(chan string, 20),
		active: make(map[string]int), peak: make(map[string]int), launches: make(map[string]int),
	}
}

func (w *workerSignals) hold(id string, exitGate <-chan struct{}) func(context.Context, func(string)) {
	return func(ctx context.Context, phase func(string)) {
		w.mu.Lock()
		w.active[id]++
		w.launches[id]++
		if w.active[id] > w.peak[id] {
			w.peak[id] = w.active[id]
		}
		w.mu.Unlock()
		phase("running")
		w.started <- id
		<-ctx.Done()
		w.cancelled <- id
		if exitGate != nil {
			<-exitGate
		}
		w.mu.Lock()
		w.active[id]--
		w.mu.Unlock()
		w.exited <- id
	}
}

func (w *workerSignals) counts(id string) (launches, active, peak int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.launches[id], w.active[id], w.peak[id]
}

func waitSignal(t *testing.T, signal <-chan string, expected string) {
	t.Helper()
	select {
	case got := <-signal:
		if got != expected {
			t.Fatalf("worker signal = %q, want %q", got, expected)
		}
	case <-time.After(controlTestTimeout):
		t.Fatalf("no worker signal for %q", expected)
	}
}

func runControlWorker(t *testing.T, ctx context.Context, m *controller, id string, work func(context.Context, func(string))) <-chan struct{} {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		m.run(ctx, id, work)
	}()
	return done
}

func waitWorkerLoop(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(controlTestTimeout):
		t.Fatal("worker supervisor did not exit after context cancellation")
	}
}

func TestControllerTargetedStopStartAndAllStop(t *testing.T) {
	m := fixtureController(t, t.TempDir())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w := newWorkerSignals()
	alpha := runControlWorker(t, ctx, m, "alpha", w.hold("alpha", nil))
	waitSignal(t, w.started, "alpha")
	beta := runControlWorker(t, ctx, m, "beta", w.hold("beta", nil))
	waitSignal(t, w.started, "beta")
	waitControl(t, m, "alpha", "running", true)
	waitControl(t, m, "beta", "running", true)

	mustControl(t, m, "stop", "alpha", false)
	waitSignal(t, w.cancelled, "alpha")
	waitSignal(t, w.exited, "alpha")
	waitControl(t, m, "alpha", "stopped", false)
	waitControl(t, m, "beta", "running", true)
	if launches, active, peak := w.counts("beta"); launches != 1 || active != 1 || peak != 1 {
		t.Fatalf("targeted stop disturbed sibling: launches=%d active=%d peak=%d", launches, active, peak)
	}

	mustControl(t, m, "start", "alpha", false)
	waitSignal(t, w.started, "alpha")
	waitControl(t, m, "alpha", "running", true)
	mustControl(t, m, "stop", "", true)
	waitControl(t, m, "alpha", "stopped", false)
	waitControl(t, m, "beta", "stopped", false)
	if launches, active, peak := w.counts("alpha"); launches != 2 || active != 0 || peak != 1 {
		t.Fatalf("alpha launches=%d active=%d peak=%d", launches, active, peak)
	}
	if _, active, _ := w.counts("beta"); active != 0 {
		t.Fatal("all stop left beta alive")
	}
	cancel()
	waitWorkerLoop(t, alpha)
	waitWorkerLoop(t, beta)
}

func TestControllerStopStartCancelsBeforeReplacement(t *testing.T) {
	m := fixtureController(t, t.TempDir())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w := newWorkerSignals()
	gate := make(chan struct{})
	var release sync.Once
	defer release.Do(func() { close(gate) })
	worker := runControlWorker(t, ctx, m, "alpha", w.hold("alpha", gate))
	waitSignal(t, w.started, "alpha")
	mustControl(t, m, "stop", "alpha", false)
	waitSignal(t, w.cancelled, "alpha")
	waitControl(t, m, "alpha", "stopping", false)
	mustControl(t, m, "start", "alpha", false)
	// A start arriving while the old daemon is draining changes the desired
	// state but must not spawn a replacement while that daemon is alive.
	waitControl(t, m, "alpha", "stopping", true)
	if launches, active, peak := w.counts("alpha"); launches != 1 || active != 1 || peak != 1 {
		t.Fatalf("replacement overlapped old worker: launches=%d active=%d peak=%d", launches, active, peak)
	}
	release.Do(func() { close(gate) })
	waitSignal(t, w.exited, "alpha")
	waitSignal(t, w.started, "alpha")
	waitControl(t, m, "alpha", "running", true)
	if launches, active, peak := w.counts("alpha"); launches != 2 || active != 1 || peak != 1 {
		t.Fatalf("unexpected worker counts: launches=%d active=%d peak=%d", launches, active, peak)
	}
	cancel()
	waitWorkerLoop(t, worker)
}

func TestControllerEnabledSelectionSurvivesNewInstance(t *testing.T) {
	root := t.TempDir()
	first := fixtureController(t, root)
	mustControl(t, first, "stop", "alpha", false)

	next := fixtureController(t, root)
	overview := statuses(mustControl(t, next, "status", "", true))
	if len(overview) != 2 || overview["alpha"].Enabled || !overview["beta"].Enabled {
		t.Fatalf("new controller did not preserve only alpha's disabled setting: %#v", overview)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w := newWorkerSignals()
	alpha := runControlWorker(t, ctx, next, "alpha", w.hold("alpha", nil))
	beta := runControlWorker(t, ctx, next, "beta", w.hold("beta", nil))
	waitSignal(t, w.started, "beta")
	waitControl(t, next, "alpha", "stopped", false)
	waitControl(t, next, "beta", "running", true)
	if launches, _, _ := w.counts("alpha"); launches != 0 {
		t.Fatal("persistently disabled connection started a worker")
	}

	mustControl(t, next, "start", "alpha", false)
	waitSignal(t, w.started, "alpha")
	cancel()
	waitWorkerLoop(t, alpha)
	waitWorkerLoop(t, beta)

	last := fixtureController(t, root)
	overview = statuses(mustControl(t, last, "status", "", true))
	if !overview["alpha"].Enabled || !overview["beta"].Enabled {
		t.Fatalf("process cancellation overrode persisted enabled state: %#v", overview)
	}
}

func TestControllerBadSelectionAndRedactedStatus(t *testing.T) {
	m := fixtureController(t, t.TempDir())
	for _, req := range []controlRequest{
		{Action: "stop"},
		{Action: "stop", ID: "alpha", All: true},
		{Action: "start", ID: "unconfigured"},
		{Action: "arbitrary-action-from-issue", ID: "alpha"},
	} {
		if r := m.request(req); r.Error == "" {
			t.Errorf("invalid request accepted: %#v", req)
		}
	}
	status := mustControl(t, m, "status", "", true)
	all := statuses(status)
	if len(all) != 2 || !all["alpha"].Enabled || !all["beta"].Enabled {
		t.Fatalf("invalid request changed selection: %#v", all)
	}
	payload, err := json.Marshal(status)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"alpha-token-no-print", "beta-token-no-print", "token_file"} {
		if strings.Contains(strings.ToLower(string(payload)), secret) {
			t.Fatalf("status includes token input %q", secret)
		}
	}
}

func TestControllerReportsRetryingPhase(t *testing.T) {
	m := fixtureController(t, t.TempDir())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	continueRunning := make(chan struct{})
	worker := runControlWorker(t, ctx, m, "alpha", func(ctx context.Context, phase func(string)) {
		phase("retrying")
		select {
		case <-continueRunning:
		case <-ctx.Done():
			return
		}
		phase("running")
		<-ctx.Done()
	})
	waitControl(t, m, "alpha", "retrying", true)
	close(continueRunning)
	waitControl(t, m, "alpha", "running", true)
	cancel()
	waitWorkerLoop(t, worker)
}

func TestConnectionCLIOverUnixSocket(t *testing.T) {
	// Unix socket paths have a short kernel limit independent of the test name.
	dir, err := os.MkdirTemp("", "mab-ctl-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	m := fixtureController(t, dir)
	listener, closeControl, err := listenControl(dir)
	if errors.Is(err, syscall.EPERM) {
		t.Skipf("test sandbox disallows Unix sockets: %v", err)
	}
	if err != nil {
		t.Fatal(err)
	}
	defer closeControl()
	if _, closeDuplicate, err := listenControl(dir); err == nil {
		closeDuplicate()
		t.Fatal("duplicate controller should not replace a live socket")
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		m.serveControl(ctx, listener)
	}()
	defer func() { cancel(); _ = listener.Close(); waitWorkerLoop(t, done) }()
	call := func(args ...string) map[string]ConnectionStatus {
		t.Helper()
		var out bytes.Buffer
		if err := control(ctx, dir, args, &out); err != nil {
			t.Fatal(err)
		}
		var res controlResponse
		if err := json.Unmarshal(out.Bytes(), &res); err != nil {
			t.Fatal(err)
		}
		return statuses(res)
	}
	if got := call("stop", "--all"); len(got) != 2 || got["alpha"].Enabled || got["beta"].Enabled {
		t.Fatalf("stop --all = %#v", got)
	}
	if got := call("start", "beta"); len(got) != 1 || !got["beta"].Enabled {
		t.Fatalf("start beta = %#v", got)
	}
	if got := call("status", "alpha"); len(got) != 1 || got["alpha"].State != "stopped" {
		t.Fatalf("status alpha = %#v", got)
	}
	if err := control(ctx, dir, []string{"stop"}, &bytes.Buffer{}); err == nil {
		t.Fatal("stop without an explicit selection succeeded")
	}
}
