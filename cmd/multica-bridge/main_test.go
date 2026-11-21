package main

import (
	"errors"
	"testing"

	"github.com/godlikejay/multica-agent-bridge/internal/mailbox"
	"github.com/godlikejay/multica-agent-bridge/internal/multica"
	"github.com/godlikejay/multica-agent-bridge/internal/supervisor"
)

func TestSupervisedMailboxIgnoresAgentEnvironment(t *testing.T) {
	t.Setenv("HOME", "/bridge-runtime/company/home")
	t.Setenv("BRIDGE_ROOT", "/wrong-mailbox")
	t.Setenv("BRIDGE_ALLOW_TEST_HTTP", "1")
	t.Setenv("MULTICA_BRIDGE_CONNECTION", "other")
	task := multica.Config{
		URL: "https://api.company.example.invalid/", WorkspaceID: "workspace-2", ExpectedRuntimeID: "remote-override",
	}
	box, err := taskMailbox(&task, func(home, endpoint, workspace string) (supervisor.TaskRoute, error) {
		if home != "/bridge-runtime/company/home" || endpoint != task.URL || workspace != task.WorkspaceID {
			t.Fatal("wrong scope passed to trusted resolver")
		}
		return supervisor.TaskRoute{ConnectionID: "company", ServerURL: "https://api.company.example.invalid"}, nil
	})
	want := mailbox.Box{Root: "/bridge", Connection: "company", Workspace: "workspace-2"}
	if err != nil || box != want || !task.TrustedRoute || task.SyntheticHTTP || task.ExpectedRuntimeID != "" || task.URL != "https://api.company.example.invalid" {
		t.Fatalf("unexpected mailbox/scope %v (%v): %v", box, task, err)
	}
	bindingErr := errors.New("binding mismatch")
	box, err = taskMailbox(&task, func(_, _, _ string) (supervisor.TaskRoute, error) {
		return supervisor.TaskRoute{}, bindingErr
	})
	if box != (mailbox.Box{}) || !errors.Is(err, bindingErr) {
		t.Fatalf("binding failure fell through instead of stopping: %v / %v", box, err)
	}
}
