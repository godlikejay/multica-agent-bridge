package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const daemonBinary = "/usr/local/bin/multica"
const bridgeBinary = "/usr/local/bin/multica-bridge"
const loginTimeout = 40 * time.Second
const stopTimeout = 8 * time.Second

type disk struct{ runtime, mailbox string }

var containerDisk = disk{RuntimeRoot, MailboxRoot}

// Serve holds the supervisor until terminated. A child failure does not stop its siblings.
func Serve(ctx context.Context, path string, testHTTP bool, log io.Writer) error {
	cfg, err := Load(path, testHTTP)
	if err != nil {
		return err
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	logger := func(id, status string, wait time.Duration) {
		mu.Lock()
		defer mu.Unlock()
		if wait != 0 {
			fmt.Fprintf(log, "[%s] %s; retry in %s\n", id, status, wait)
			return
		}
		fmt.Fprintf(log, "[%s] %s\n", id, status)
	}
	for _, c := range cfg.Connections {
		wg.Add(1)
		go func() {
			defer wg.Done()
			supervise(ctx, func() (time.Duration, string) {
				return runConnection(ctx, containerDisk, c, testHTTP, logger)
			}, func(status string, wait time.Duration) { logger(c.ID, status, wait) }, time.Second, 30*time.Second, time.Minute)
		}()
	}
	wg.Wait()
	return nil
}

type logFunc func(id, status string, wait time.Duration)

func supervise(ctx context.Context, attempt func() (time.Duration, string), log func(string, time.Duration), minDelay, maxDelay, stable time.Duration) {
	delay := minDelay
	for ctx.Err() == nil {
		lifetime, status := attempt()
		if ctx.Err() != nil {
			return
		}
		if lifetime >= stable {
			delay = minDelay
		}
		log(status, delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, maxDelay)
	}
}

func runConnection(ctx context.Context, d disk, c Connection, testHTTP bool, log logFunc) (time.Duration, string) {
	for _, root := range []string{d.runtime, d.mailbox, filepath.Join(d.mailbox, "state")} {
		if err := ensureDir(root); err != nil {
			return 0, "persistence mount unavailable"
		}
	}
	runtime := filepath.Join(d.runtime, c.ID)
	state := filepath.Join(d.mailbox, "state", c.ID)
	// Two separate locks protect both mounts, including a deployment accidentally
	// sharing only one of the two with a second supervisor.
	runtimeUnlock, err := lockDir(runtime)
	if err != nil {
		return 0, "runtime directory unavailable or already in use"
	}
	defer runtimeUnlock()
	stateUnlock, err := lockDir(state)
	if err != nil {
		return 0, "mailbox state unavailable or already in use"
	}
	defer stateUnlock()
	if err := checkBindings(runtime, state, c.ServerURL); err != nil {
		return 0, "endpoint binding changed or unreadable (choose a new connection id)"
	}
	home, root := filepath.Join(runtime, "home"), filepath.Join(runtime, "workspaces")
	for _, dir := range []string{home, root} {
		if err := ensureDir(dir); err != nil {
			return 0, "runtime storage unavailable"
		}
	}
	if err := writeTaskRoute(runtime, c, testHTTP); err != nil {
		return 0, "cannot persist local task route"
	}
	env := childEnv(c, home, root)
	if status := ensureLogin(ctx, runtime, home, c, env); status != "" {
		return 0, status
	}
	started := time.Now()
	log(c.ID, "starting Multica daemon", 0)
	err = runCommand(ctx, env, nil, "--profile", c.ID, "daemon", "start", "--foreground", "--no-auto-update", "--no-auto-reload")
	if err != nil {
		return time.Since(started), "Multica daemon exited with an error"
	}
	return time.Since(started), "Multica daemon stopped"
}

// Retain a profile after successful login. The daemon may refresh its own
// credential; blindly overwriting it with the original bootstrap PAT on every
// restart would undo native refresh. A new mounted token forces another login.
func ensureLogin(ctx context.Context, runtime, home string, c Connection, env []string) string {
	tokenBytes, err := readFile(c.TokenFile, 8192)
	if err != nil {
		return "cannot read token file"
	}
	token := strings.TrimSpace(string(tokenBytes))
	if token == "" || strings.ContainsAny(token, " \t\r\n\x00") {
		return "invalid token file"
	}
	sum := sha256.Sum256([]byte(token))
	marker := filepath.Join(runtime, ".bootstrap-token-sha256")
	fingerprint := hex.EncodeToString(sum[:]) + "\n"
	saved, _ := readFile(marker, 128)
	profile := filepath.Join(home, ".multica", "profiles", c.ID, "config.json")
	if string(saved) == fingerprint {
		if info, err := os.Lstat(profile); err == nil && info.Mode().IsRegular() {
			return ""
		}
	}
	// Supply the mounted token by stdin only. Upstream output is not forwarded.
	loginCtx, cancel := context.WithTimeout(ctx, loginTimeout)
	defer cancel()
	if err := runCommand(loginCtx, env, strings.NewReader(token+"\n"), "--profile", c.ID, "--server-url", c.ServerURL, "login", "--token"); err != nil {
		return "Multica login failed (check endpoint, token, and workspace membership)"
	}
	if err := writePrivateAtomic(marker, fingerprint); err != nil {
		return "cannot persist bootstrap state"
	}
	return ""
}

func writePrivateAtomic(path, value string) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".write-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	_, err = io.WriteString(f, value)
	if err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func childEnv(c Connection, home, root string) []string {
	// No task identity or daemon control variables inherited from the parent
	// shell. Upstream preserves HOME but strips inherited MULTICA_* for workers
	// and injects its own task scope. HOME is reserved against custom overrides.
	return []string{
		"PATH=/usr/local/bin:/usr/bin:/bin",
		"HOME=" + home,
		"MULTICA_SERVER_URL=" + c.ServerURL,
		"MULTICA_APP_URL=" + c.AppURL,
		"MULTICA_DAEMON_DEVICE_NAME=multica-bridge-" + c.ID,
		"MULTICA_AGENT_RUNTIME_NAME=multica-bridge-" + c.ID,
		"MULTICA_WORKSPACES_ROOT=" + root,
		"MULTICA_DAEMON_MAX_CONCURRENT_TASKS=" + strconv.Itoa(c.MaxActiveRuns),
		"MULTICA_HERMES_PATH=" + bridgeBinary,
		"MULTICA_DAEMON_AUTO_UPDATE=false",
		"MULTICA_DAEMON_AUTO_RELOAD=false",
	}
}

func runCommand(ctx context.Context, env []string, stdin io.Reader, args ...string) error {
	return runProcess(ctx, daemonBinary, "/home/runtime", env, stdin, stopTimeout, args...)
}

func runProcess(ctx context.Context, executable, dir string, env []string, stdin io.Reader, grace time.Duration, args ...string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	cmd := exec.Command(executable, args...)
	cmd.Dir, cmd.Env, cmd.Stdin = dir, env, stdin
	// In Docker this is one scoped child process group, not the entire container.
	// os/exec discards stdout and stderr by default; they can expose credentials.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(grace):
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-done
		}
		return ctx.Err()
	}
}

func ensureDir(dir string) error {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("not a directory")
	}
	return nil
}

func lockDir(dir string) (func(), error) {
	if err := ensureDir(dir); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, ".supervisor.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		f.Close()
		return nil, errors.New("not a regular lock file")
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, err
	}
	return func() { _ = f.Close() }, nil
}

const bindingName = ".server-url"

// Record the immutable endpoint in both persistence areas. Validate both
// existing bindings before either is changed. A new alias is required to repoint.
func checkBindings(runtime, state, endpoint string) error {
	roots := []string{runtime, state}
	missing := make([]string, 0, 2)
	for _, dir := range roots {
		f := filepath.Join(dir, bindingName)
		info, err := os.Lstat(f)
		if errors.Is(err, os.ErrNotExist) {
			names, err := os.ReadDir(dir)
			if err != nil {
				return err
			}
			for _, name := range names {
				if name.Name() != ".supervisor.lock" {
					return errors.New("existing state has no binding")
				}
			}
			missing = append(missing, f)
			continue
		}
		if err != nil || !info.Mode().IsRegular() {
			return errors.New("invalid endpoint binding")
		}
		existing, err := readFile(f, maxConfig)
		if err != nil || string(existing) != endpoint+"\n" {
			return errors.New("endpoint binding mismatch")
		}
	}
	for _, f := range missing {
		if err := writePrivateAtomic(f, endpoint+"\n"); err != nil {
			return err
		}
	}
	return nil
}

const taskRouteName = ".task-route.json"

type TaskRoute struct {
	ConnectionID  string   `json:"connection_id"`
	ServerURL     string   `json:"server_url"`
	WorkspaceIDs  []string `json:"workspace_ids,omitempty"`
	SyntheticHTTP bool     `json:"synthetic_http,omitempty"`
}

func writeTaskRoute(dir string, c Connection, testHTTP bool) error {
	raw, err := json.Marshal(TaskRoute{c.ID, c.ServerURL, c.WorkspaceIDs, testHTTP})
	if err != nil {
		return err
	}
	return writePrivateAtomic(filepath.Join(dir, taskRouteName), string(raw)+"\n")
}

// ResolveTask uses the HOME of the daemon that actually launched this ACP
// worker. Upstream preserves HOME and blocks remote custom_env overrides. The
// per-child route settings come from local supervisor state, never task input.
func ResolveTask(home, endpoint, workspace string) (TaskRoute, error) {
	return containerDisk.resolveTask(home, endpoint, workspace)
}

func (d disk) resolveTask(home, endpoint, workspace string) (TaskRoute, error) {
	prefix := d.runtime + "/"
	if home != d.runtime && !strings.HasPrefix(home, prefix) {
		return TaskRoute{}, nil // legacy
	}
	reject := errors.New("missing or invalid local task route")
	id, ok := strings.CutSuffix(strings.TrimPrefix(home, prefix), "/home")
	if !ok || !idRE.MatchString(id) {
		return TaskRoute{}, reject
	}
	runtime := filepath.Join(d.runtime, id)
	raw, err := readRegularFile(filepath.Join(runtime, taskRouteName), maxConfig)
	var route TaskRoute
	if err != nil || json.Unmarshal(raw, &route) != nil || route.ConnectionID != id {
		return TaskRoute{}, reject
	}
	cfg := Config{Connections: []Connection{{ID: id, ServerURL: route.ServerURL, WorkspaceIDs: route.WorkspaceIDs}}}
	if _, err := cfg.Route(id, endpoint, workspace, route.SyntheticHTTP); err != nil {
		return TaskRoute{}, err
	}
	for _, dir := range []string{runtime, filepath.Join(d.mailbox, "state", id)} {
		got, err := readRegularFile(filepath.Join(dir, bindingName), maxConfig)
		if err != nil || string(got) != route.ServerURL+"\n" {
			return TaskRoute{}, errors.New("connection endpoint binding mismatch")
		}
	}
	return route, nil
}

func readRegularFile(path string, max int) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("not a regular file")
	}
	return readFile(path, max)
}
