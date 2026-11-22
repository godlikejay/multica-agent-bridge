package supervisor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestEndpointPersistenceAndACPScopes(t *testing.T) {
	tmp := t.TempDir()
	d := disk{runtime: filepath.Join(tmp, "runtime"), mailbox: filepath.Join(tmp, "mailbox")}
	c := Connection{ID: "work", ServerURL: "https://work.example.invalid", WorkspaceIDs: []string{"project-a", "project-b"}}
	dir, state := filepath.Join(d.runtime, c.ID), filepath.Join(d.mailbox, "state", c.ID)
	for _, path := range []string{dir, state} {
		if err := ensureDir(path); err != nil {
			t.Fatal(err)
		}
	}
	if err := checkBindings(dir, state, c.ServerURL); err != nil {
		t.Fatal(err)
	}
	if err := writeTaskRoute(dir, c, false); err != nil {
		t.Fatal(err)
	}
	if err := checkBindings(dir, state, "https://another.example.invalid"); err == nil {
		t.Fatal("endpoint repoint unexpectedly accepted")
	}
	for _, ws := range c.WorkspaceIDs {
		route, err := d.resolveTask(filepath.Join(dir, "home"), c.ServerURL, ws)
		if err != nil || route.ConnectionID != c.ID || route.ServerURL != c.ServerURL || route.SyntheticHTTP {
			t.Fatalf("route %q: %#v, %v", ws, route, err)
		}
	}
	for _, scope := range []struct{ home, endpoint, workspace string }{
		{filepath.Join(dir, "home"), "https://another.example.invalid", "project-a"},
		{filepath.Join(dir, "home"), c.ServerURL, "another-project"},
		{filepath.Join(d.runtime, "other", "home"), c.ServerURL, "project-a"},
		{filepath.Join(dir, "other"), c.ServerURL, "project-a"},
	} {
		if _, err := d.resolveTask(scope.home, scope.endpoint, scope.workspace); err == nil {
			t.Fatalf("bad scope accepted: %#v", scope)
		}
	}
	route, err := d.resolveTask("/home/runtime", c.ServerURL, "project-a")
	if err != nil || route.ConnectionID != "" {
		t.Fatalf("legacy route altered: %#v %v", route, err)
	}
	if err := os.Remove(filepath.Join(dir, bindingName)); err != nil {
		t.Fatal(err)
	}
	if _, err := d.resolveTask(filepath.Join(dir, "home"), c.ServerURL, "project-a"); err == nil {
		t.Fatal("missing runtime binding accepted")
	}
	replacement := filepath.Join(d.runtime, "new-on-disk")
	if err := ensureDir(replacement); err != nil {
		t.Fatal(err)
	}
	if err := checkBindings(replacement, state, "https://another.example.invalid"); err == nil {
		t.Fatal("same mailbox repointed after runtime loss")
	}
	if _, err := os.Lstat(filepath.Join(replacement, bindingName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("wrote new binding before old verification: %v", err)
	}
}

func TestChildEnvironmentLacksInheritedSecretsOrScopes(t *testing.T) {
	t.Setenv("BRIDGE_ROOT", "/bad/path")
	t.Setenv("MULTICA_TOKEN", "sensitive")
	t.Setenv("HTTPS_PROXY", "http://proxy.example.invalid")
	c := Connection{ID: "work", ServerURL: "https://api.example.invalid", AppURL: "https://app.example.invalid", MaxActiveRuns: 2}
	env := childEnv(c, "/bridge-runtime/work/home", "/bridge-runtime/work/workspaces")
	for _, s := range []string{"HOME=/bridge-runtime/work/home", "MULTICA_SERVER_URL=" + c.ServerURL, "MULTICA_APP_URL=" + c.AppURL, "MULTICA_DAEMON_MAX_CONCURRENT_TASKS=2"} {
		if !slices.Contains(env, s) {
			t.Errorf("missing %q", s)
		}
	}
	if got := strings.Join(env, "\n"); strings.Contains(got, "sensitive") || strings.Contains(got, "/bad/path") || strings.Contains(got, "HTTPS_PROXY") {
		t.Fatal("leaked unrelated env")
	}
}

func TestChildEnvironmentDoesNotInventAppURL(t *testing.T) {
	t.Setenv("MULTICA_APP_URL", "https://unrelated.example.invalid")
	c := Connection{ID: "work", ServerURL: "https://api.example.invalid", MaxActiveRuns: 2}
	for _, entry := range childEnv(c, "/bridge-runtime/work/home", "/bridge-runtime/work/workspaces") {
		if strings.HasPrefix(entry, "MULTICA_APP_URL=") {
			t.Fatal("API-only connection must not set a browser URL")
		}
	}
}

func TestSharedStateLocks(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "lock")
	unlock, err := lockDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if other, err := lockDir(dir); err == nil {
		other()
		unlock()
		t.Fatal("concurrent lock succeeded")
	}
	unlock()
	again, err := lockDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	again()
}

func TestRestartBackoffAndReset(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var got []time.Duration
	alwaysFail := func() (time.Duration, string) {
		if len(got) == 3 {
			return time.Second, "exit after steady run"
		}
		return 0, "startup error"
	}
	supervise(ctx, alwaysFail, func(_ string, next time.Duration) {
		got = append(got, next)
		if len(got) == 4 {
			cancel()
		}
	}, time.Millisecond, 3*time.Millisecond, time.Second)
	want := []time.Duration{time.Millisecond, 2 * time.Millisecond, 3 * time.Millisecond, time.Millisecond}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("backoffs %v, want %v", got, want)
	}
}

func TestRunProcessHonorsCancellation(t *testing.T) {
	dir := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	started := time.Now()
	err := runProcess(ctx, "/bin/sh", dir, []string{"PATH=/usr/bin:/bin"}, nil, 20*time.Millisecond, "-c", "trap '' TERM; sleep 10")
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > 2*time.Second {
		t.Fatalf("process failed to stop: %v", err)
	}
}

func TestDaemonShutdownLetsActiveWorkerCleanUp(t *testing.T) {
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)

	// This synthetic daemon explicitly asks its worker to stop after its own
	// cleanup. The worker detects an accidental process-group SIGTERM.
	const fakeDaemon = `
trap '
    : > stopping
    while [ ! -f allow-cleanup ]; do sleep 0.01; done
    : > finish-worker
    wait "$worker"
    : > daemon-clean
    exit 0
' TERM
/bin/sh -c '
    trap ": > worker-interrupted; exit 1" TERM
    : > worker-ready
    while [ ! -f finish-worker ]; do sleep 0.01; done
    : > worker-clean
' &
worker=$!
wait "$worker"
`
	const oldScaledGrace = 120 * time.Millisecond
	grace := time.Duration((daemonStopTimeout / (8 * time.Second))) * oldScaledGrace
	go func() {
		done <- runProcess(ctx, "/bin/sh", dir, []string{"PATH=/usr/bin:/bin"}, nil, grace, "-c", fakeDaemon)
	}()
	waitMarker := func(name string) {
		t.Helper()
		timer := time.NewTimer(3 * time.Second)
		defer timer.Stop()
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			if _, err := os.Stat(filepath.Join(dir, name)); err == nil {
				return
			}
			select {
			case err := <-done:
				t.Fatalf("daemon exited before %s: %v", name, err)
			case <-timer.C:
				t.Fatalf("timeout waiting for %s", name)
			case <-ticker.C:
			}
		}
	}
	waitMarker("worker-ready")
	cancel()
	waitMarker("stopping")
	select {
	case err := <-done:
		t.Fatalf("daemon exited before active cleanup completed: %v", err)
	case <-time.After(oldScaledGrace + 30*time.Millisecond):
	}
	if err := os.WriteFile(filepath.Join(dir, "allow-cleanup"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("unexpected stop result: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("daemon did not finish")
	}
	for _, name := range []string{"worker-clean", "daemon-clean"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			t.Fatalf("missing graceful cleanup marker %s: %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "worker-interrupted")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("worker received premature group SIGTERM (stat=%v)", err)
	}
}
