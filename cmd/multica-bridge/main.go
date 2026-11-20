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
)

func main() {
	args := os.Args[1:]
	if (len(args) == 1 && (args[0] == "version" || args[0] == "--version")) || (len(args) == 2 && args[0] == "acp" && args[1] == "--version") {
		fmt.Println("multica-bridge " + acp.Version)
		return
	}
	if len(args) != 1 || args[0] != "acp" {
		fmt.Fprintln(os.Stderr, "usage: multica-bridge acp | --version")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err := acp.Serve(ctx, os.Stdin, os.Stdout, func(ctx context.Context, _ acp.Prompt) (string, error) {
		cfg, err := multica.FromEnv()
		if err != nil {
			return "", err
		}
		remote, err := multica.New(cfg)
		if err != nil {
			return "", err
		}
		interval := 30 * time.Second
		if v := os.Getenv("BRIDGE_POLL_INTERVAL"); v != "" {
			interval, err = time.ParseDuration(v)
			if err != nil || interval < time.Second || interval > time.Minute {
				return "", fmt.Errorf("BRIDGE_POLL_INTERVAL must be between 1s and 1m")
			}
		}
		return (bridge.Bridge{Box: mailbox.Box{Root: os.Getenv("BRIDGE_ROOT")}, Remote: remote, RunID: cfg.RunID, Poll: interval}).Run(ctx)
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "bridge ACP:", err)
		os.Exit(1)
	}
}
