#!/usr/bin/env python3
"""Operator-run, two-service synthetic Multica acceptance. Never runs an agent."""
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE / ".local"
MB = ROOT / "bridge"
MAX_FILE = 256 * 1024
SCOPE = ("connection_id", "workspace_id", "issue_id", "run_id", "epoch")
PORTS = {"a": (13100, 18180), "b": (13200, 18280)}
# Reuse the existing tested HTTP/status helper and its redirect restrictions.
spec = importlib.util.spec_from_file_location("single_fixture", HERE.parent / "fixture.py")
single = importlib.util.module_from_spec(spec)
spec.loader.exec_module(single)
single.OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), single.NoRedirect)


def uuid_field(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected a canonical Multica UUID")
    return value


def exclusive(path, content):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    return True


def init():
    if os.getuid() == 0:
        raise SystemExit("Run the synthetic fixture as a regular host user.")
    (ROOT / "secrets").mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / "runtime").mkdir(exist_ok=True, mode=0o700)
    for name in PORTS:
        for mailbox in ("inbox", "outbox", "state"):
            (MB / mailbox / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [f"LOCAL_UID={os.getuid()}", f"LOCAL_GID={os.getgid()}"]
    for name in PORTS:
        lines.extend(f"{name.upper()}_{key}={secrets.token_hex(32)}" for key in ("DB_PASSWORD", "JWT_SECRET"))
    exclusive(ROOT / "compose.env", "\n".join(lines) + "\n")
    config = {"connections": [
        {"id": name, "server_url": f"http://backend-{name}:8080",
         "app_url": f"http://localhost:{web}",
         "token_file": f"/run/secrets/{name}.pat", "max_active_runs": 2}
        for name, (web, _) in PORTS.items()
    ]}
    exclusive(ROOT / "connections.json", json.dumps(config, indent=2) + "\n")
    print("Created missing disposable paths/configs. No accounts or Docker services started.")


def token_path(name):
    return ROOT / "secrets" / f"{name}.pat"


def validate_token(token):
    if not token or len(token) > 8192 or any(ch.isspace() or ch == "\0" for ch in token):
        raise ValueError("Expected a single synthetic PAT")
    return token


def save_token(name):
    if not os.isatty(0):
        raise SystemExit("Use an interactive terminal to enter a synthetic local PAT.")
    path = token_path(name)
    if path.exists() or path.is_symlink():
        raise SystemExit(f"Synthetic token {name} already exists; no overwrite performed.")
    value = validate_token(getpass.getpass(f"Synthetic PAT from localhost:{PORTS[name][0]} (hidden): "))
    if not exclusive(path, value + "\n"):
        raise SystemExit("Token file already exists.")
    print(f"Saved local synthetic token for {name}; value not displayed.")


def read_token(name):
    path = token_path(name)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError(f"Need an owner-only synthetic PAT file for {name}")
    return validate_token(path.read_text().rstrip("\r\n"))


def address(name):
    web, api = PORTS[name]
    # Only local published fixture URLs are ever queried; do not read them
    # from CLI arguments, environment variables, config, or issue data.
    single.API = f"http://127.0.0.1:{api}"
    single.WEB = f"http://localhost:{web}"


def health():
    for name in PORTS:
        print(f"Synthetic endpoint {name}:")
        address(name)
        single.health()


def load(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError("Expected a regular non-symlink mailbox event file")
    with path.open("rb") as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("Oversized mailbox event")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def read_task(name, file):
    path = Path(file)
    event = load(path)
    workspace = uuid_field(event.get("workspace_id"))
    direct_outbox = MB / "outbox" / name / workspace
    if path.resolve() != direct_outbox / path.name:
        raise ValueError(f"Choose a direct outbox event from synthetic connection {name}")
    if (event.get("version") != 1 or event.get("kind") != "task" or
            event.get("connection_id") != name or
            type(event.get("epoch")) is not int or event["epoch"] < 1):
        raise ValueError("Expected a scoped v1 initial task event")
    uuid_field(event.get("issue_id"))
    uuid_field(event.get("run_id"))
    body = event.get("body")
    if not isinstance(body, str) or not any(tag in body for tag in (single.MARKER, single.ESCAPED_MARKER)):
        raise ValueError("Only explicitly marked synthetic tasks are accepted")
    return event


def bound_issue(token, event):
    address(event["connection_id"])
    issue = single.get("/api/issues/" + event["issue_id"], token)
    if (issue.get("id") != event["issue_id"] or
            issue.get("workspace_id") != event["workspace_id"] or
            issue.get("identifier") != event.get("issue_key") or not event.get("issue_key")):
        raise ValueError("Server Issue, workspace or display key does not match its task event")
    # Local event alone does not authorize writes: server confirms this is a
    # marked synthetic Issue at the exact API host and belongs to the event's Run.
    if not any(tag in (issue.get("description") or "") for tag in (single.MARKER, single.ESCAPED_MARKER)):
        raise ValueError("Server Issue is not marked synthetic")
    return issue


def action(event, kind, body):
    inbox = MB / "inbox" / event["connection_id"] / event["workspace_id"] / event["issue_id"]
    if inbox.resolve() != inbox:
        raise ValueError("Symlink mailbox path refused")
    inbox.mkdir(parents=True, mode=0o700, exist_ok=True)
    request_id = "e2e-" + uuid.uuid4().hex
    msg = {field: event[field] for field in SCOPE}
    msg.update(version=1, kind=kind, id=request_id, body=body)
    tmp = inbox / ("." + request_id + ".tmp")
    if not exclusive(tmp, json.dumps(msg) + "\n"):
        raise ValueError("Synthetic request collision")
    tmp.replace(inbox / (request_id + ".json"))
    print(f"{event['connection_id']}: wrote {kind} request {request_id}", flush=True)
    return request_id


def wait_receipt(event, request_id, expected):
    outbox = MB / "outbox" / event["connection_id"] / event["workspace_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        for path in outbox.glob("*.json"):
            row = load(path)
            if row.get("kind") != "receipt" or row.get("request_id") != request_id:
                continue
            if any(row.get(field) != event[field] for field in SCOPE) or row.get("status") != expected:
                raise ValueError(f"Unexpected receipt for {request_id}; do not retry a send blindly")
            uuid_field(row.get("remote_id"))
            return row
        time.sleep(1)
    raise ValueError(f"No receipt for {request_id}; do not retry a send blindly")


def comments(token, event):
    address(event["connection_id"])
    rows = single.get("/api/issues/" + event["issue_id"] + "/comments?recent=200", token)
    # This is a disposable two-Issue fixture; a long thread isn't a test pass.
    if (not isinstance(rows, list) or len(rows) >= 200 or
            any(row.get("issue_id") != event["issue_id"] for row in rows)):
        raise ValueError("Unexpected or potentially truncated comments response")
    return [row for row in rows if not row.get("deleted_at")]


def verify_comment(receipt, body, own, other):
    matches = [x for x in own if (x.get("content") or "").split("\n\n", 1)[0] == body]
    if (len(matches) != 1 or matches[0].get("id") != receipt["remote_id"] or
            matches[0].get("source_task_id") != receipt["run_id"] or
            matches[0].get("issue_id") != receipt["issue_id"] or
            matches[0].get("author_type") != "agent" or
            any(body in (x.get("content") or "") for x in other)):
        raise ValueError("Reply was absent, duplicated, or attached to a different service/Issue/Run")


def compose(*args):
    return subprocess.check_output([
        "docker", "compose", "--env-file", ".local/compose.env", "-f", "compose.yaml", "--profile",
        "runtime", *args,
    ], cwd=HERE, timeout=90, text=True)


def daemon(name, verb):
    return compose("exec", "-T", "-e", f"HOME=/bridge-runtime/{name}/home", "runtime",
                   "multica", "--profile", name, "daemon", verb, *(
                       ("--output", "json") if verb == "status" else ()))


def running_pid(name):
    health = json.loads(daemon(name, "status"))
    pid = health.get("pid")
    if health.get("status") == "running" and health.get("profile") == name and isinstance(pid, int) and pid > 0:
        return pid
    return None


def wait_pid(name, previous=None):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pid = running_pid(name)
        if pid and pid != previous:
            return pid
        time.sleep(2)
    raise ValueError(f"Daemon {name} never reached running with expected PID")


def check_status(token, event, want):
    address(event["connection_id"])
    if single.run_status(event["run_id"], token) != want:
        raise ValueError(f"Server Run {event['run_id']} on {event['connection_id']} is not {want}")


def wait_completed(token, event):
    address(event["connection_id"])
    single.wait_completed(event["run_id"], token)


def test_endpoint_down(name):
    address(name)
    try:
        single.get("/readyz")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (502, 503, 504):
            return
        raise
    except (ConnectionError, urllib.error.URLError, TimeoutError):
        return
    raise ValueError(f"Endpoint {name} unexpectedly reachable during outage")


def accept(path_a, path_b):
    a, b = read_task("a", path_a), read_task("b", path_b)
    token_a, token_b = read_token("a"), read_token("b")
    one_container = compose("ps", "-q", "runtime").split()
    if len(one_container) != 1:
        raise ValueError("Expected exactly one running Bridge runtime container")
    if (a["issue_id"] == b["issue_id"] or a["run_id"] == b["run_id"] or
            bound_issue(token_a, a)["identifier"] != bound_issue(token_b, b)["identifier"]):
        raise ValueError("Use distinct Issues and Runs with matching display keys on the two services")
    pid_a, pid_b = running_pid("a"), running_pid("b")
    if not pid_a or not pid_b or pid_a == pid_b:
        raise ValueError("Expected two running Daemon processes inside the same container")
    check_status(token_a, a, "running")
    check_status(token_b, b, "running")
    print(f"Start: one container; A PID {pid_a}, B PID {pid_b}; shared key {a['issue_key']}.")

    proof = uuid.uuid4().hex
    body_a, body_b, body_outage, end_a, end_b = [
        f"{single.MARKER}: {proof} {phase}" for phase in ("a-reply", "b-reply", "b-during-a-outage", "a-complete", "b-complete")
    ]
    id_a = action(a, "reply", body_a)
    id_b = action(b, "reply", body_b)
    ra = wait_receipt(a, id_a, "reply_posted")
    rb = wait_receipt(b, id_b, "reply_posted")
    ca, cb = comments(token_a, a), comments(token_b, b)
    verify_comment(ra, body_a, ca, cb)
    verify_comment(rb, body_b, cb, ca)

    # Complete A while B remains active. Then interrupt A's daemon and keep
    # its backend down until B posts AND its own server verifies that reply.
    ea = wait_receipt(a, action(a, "complete", end_a), "completion_requested")
    wait_completed(token_a, a)
    verify_comment(ea, end_a, comments(token_a, a), comments(token_b, b))
    check_status(token_b, b, "running")
    outage_r = None
    try:
        compose("stop", "backend-a")
        daemon("a", "stop")
        test_endpoint_down("a")
        if running_pid("a") == pid_a or running_pid("b") != pid_b:
            raise ValueError("Only A's original Daemon must stop")
        outage_r = wait_receipt(b, action(b, "reply", body_outage), "reply_posted")
        # A is inaccessible here. Check ownership again against both services
        # immediately after restoration.
        verify_comment(outage_r, body_outage, comments(token_b, b), [])
        test_endpoint_down("a")
        if running_pid("b") != pid_b:
            raise ValueError("B restarted during A's outage")
        print(f"Verified B reply on exact Run {b['run_id']} while A was unavailable.")
    finally:
        compose("up", "-d", "--no-deps", "backend-a")

    new_pid = wait_pid("a", previous=pid_a)
    if running_pid("b") != pid_b or compose("ps", "-q", "runtime").split() != one_container:
        raise ValueError("B or the shared runtime container restarted")
    check_status(token_a, a, "completed")
    check_status(token_b, b, "running")
    eb = wait_receipt(b, action(b, "complete", end_b), "completion_requested")
    wait_completed(token_b, b)
    ca, cb = comments(token_a, a), comments(token_b, b)
    for receipt, body, own, other in ((ra, body_a, ca, cb), (ea, end_a, ca, cb),
                                      (rb, body_b, cb, ca), (outage_r, body_outage, cb, ca),
                                      (eb, end_b, cb, ca)):
        verify_comment(receipt, body, own, other)
    print(f"TWO-SERVICE E2E PASS: A {a['issue_id']}/{a['run_id']} completed, "
          f"A PID {pid_a} -> {new_pid}; B {b['issue_id']}/{b['run_id']} completed, "
          f"B PID {pid_b} unchanged; exact server reply IDs {ra['remote_id']}, "
          f"{rb['remote_id']}, {outage_r['remote_id']}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "health", "credentials"):
        commands.add_parser(name)
    commands.add_parser("token").add_argument("name", choices=PORTS)
    p_accept = commands.add_parser("accept")
    p_accept.add_argument("event_a")
    p_accept.add_argument("event_b")
    arg = parser.parse_args()
    try:
        if arg.command == "init":
            init()
        elif arg.command == "health":
            health()
        elif arg.command == "token":
            save_token(arg.name)
        elif arg.command == "credentials":
            for name in PORTS:
                read_token(name)
        else:
            accept(arg.event_a, arg.event_b)
    except (OSError, KeyError, TypeError, subprocess.SubprocessError, ValueError) as exc:
        # HTTP or subprocess exceptions may include their full arguments.
        if type(exc) is ValueError:
            print(str(exc), flush=True)
        raise SystemExit(f"Two-service fixture failed ({type(exc).__name__}); no E2E pass.") from None


if __name__ == "__main__":
    main()
