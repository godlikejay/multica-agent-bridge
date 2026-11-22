package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/godlikejay/multica-agent-bridge/internal/acp"
	"github.com/godlikejay/multica-agent-bridge/internal/bridge"
	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
	"github.com/godlikejay/multica-agent-bridge/internal/supervisor"
)

func main() {
	args := os.Args[1:]
	if (len(args) == 1 && (args[0] == "version" || args[0] == "--version")) || (len(args) == 2 && args[0] == "acp" && args[1] == "--version") {
		fmt.Println("multica-bridge " + acp.Version)
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if len(args) == 3 && args[0] == "serve" && args[1] == "--config" {
		if err := supervisor.Serve(ctx, args[2], os.Getenv(supervisor.TestHTTPEnv) == "1", os.Stderr); err != nil {
			fmt.Fprintln(os.Stderr, "bridge supervisor:", err)
			os.Exit(1)
		}
		return
	}
	if len(args) > 0 && args[0] == "connection" {
		if err := supervisor.Control(ctx, args[1:], os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, "bridge connection:", err)
			os.Exit(1)
		}
		return
	}
	if len(args) != 1 || args[0] != "acp" {
		fmt.Fprintln(os.Stderr, "usage: multica-bridge acp | --version | serve --config PATH | connection {start|stop|status}")
		os.Exit(2)
	}
	err := acp.Serve(ctx, os.Stdin, os.Stdout, func(ctx context.Context, _ acp.Prompt) (string, error) {
		cfg := multica.TaskConfigFromEnv()
		box, err := taskMailbox(&cfg, supervisor.ResolveTask)
		if err != nil {
			return "", err
		}
		remote, err := multica.New(cfg)
		if err != nil {
			return "", err
		}
		interval := 30 * time.Second
		if v := os.Getenv("BRIDGE_POLL_INTERVAL"); !cfg.TrustedRoute && v != "" {
			interval, err = time.ParseDuration(v)
			if err != nil || interval < time.Second || interval > time.Minute {
				return "", fmt.Errorf("BRIDGE_POLL_INTERVAL must be between 1s and 1m")
			}
		}
		return (bridge.Bridge{Box: box, Remote: remote, RunID: cfg.RunID, Poll: interval}).Run(ctx)
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "bridge ACP:", err)
		os.Exit(1)
	}
}

// taskMailbox never accepts a remotely configurable environment value as the
// destination for a supervised task. The daemon-owned variables are reserved by
// the pinned upstream version; the endpoint and workspace still must match.
func taskMailbox(task *multica.Config, resolve func(home, endpoint, workspace string) (supervisor.TaskRoute, error)) (mailbox.Box, error) {
	route, err := resolve(os.Getenv("HOME"), task.URL, task.WorkspaceID)
	if err != nil {
		return mailbox.Box{}, err
	}
	if route.ConnectionID == "" {
		return mailbox.Box{Root: os.Getenv("BRIDGE_ROOT")}, nil
	}
	task.URL = route.ServerURL
	task.ExpectedRuntimeID = "" // legacy per-runtime setting is not a connection identity
	task.TrustedRoute = true
	task.SyntheticHTTP = route.SyntheticHTTP
	return mailbox.Box{Root: supervisor.MailboxRoot, Connection: route.ConnectionID, Workspace: task.WorkspaceID}, nil
}
