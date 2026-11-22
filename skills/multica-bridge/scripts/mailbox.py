#!/usr/bin/env python3
"""Local Multica mailbox utility; setup can resolve slugs via an existing Bridge."""

import argparse
import fcntl
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from typing import NamedTuple

MAX_FILE = 256 * 1024
MAX_BODY = 32 * 1024
MAX_READ_BODY = 32 * 1024
MAX_LIST = 100
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
BINDING = ("issue_id", "run_id", "epoch")
SCOPE_FIELDS = ("connection_id", "workspace_id")
EVENTS = {"task", "comment", "receipt", "sync"}
UNREAD_KINDS = {"task", "comment"}
KINDS = {"reply", "complete"}
TERMINAL = {"completion_requested"}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
LOCAL_CONFIG = Path(__file__).resolve().parent.parent / "mailbox.local.json"


class MailboxError(Exception):
    pass


class Outbox(NamedTuple):
    directory: int
    scope: tuple


def scope_fields(scope):
    return dict(zip(SCOPE_FIELDS, scope))


def matches_scope(record, scope):
    if not scope:
        return not any(key in record for key in SCOPE_FIELDS)
    return all(record.get(key) == value for key, value in scope_fields(scope).items())


def valid_id(v):
    return isinstance(v, str) and ID.fullmatch(v) is not None


def require_id(v):
    if not valid_id(v):
        raise MailboxError("invalid mailbox ID")
    return v


def unique_object(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise MailboxError("duplicate JSON key")
        obj[k] = v
    return obj


def file_bytes(path, limit, directory=None):
    with os.fdopen(os.open(path, FILE_FLAGS, dir_fd=directory), "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise MailboxError("expected a size-bounded regular file")
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise MailboxError("file exceeds size limit")
        return raw


def json_file(path, directory=None):
    raw = file_bytes(path, MAX_FILE, directory)
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MailboxError("invalid UTF-8 JSON") from exc
    if not isinstance(obj, dict):
        raise MailboxError("expected a JSON object")
    return obj


def valid_binding(obj):
    return (type(obj.get("version")) is int and obj["version"] == 1
            and valid_id(obj.get("issue_id"))
            and isinstance(obj.get("run_id"), str) and bool(obj["run_id"])
            and type(obj.get("epoch")) is int and 0 < obj["epoch"] < 2**64)


def bind(obj):
    return tuple(obj[k] for k in BINDING)


def load_event(outbox, event_id):
    require_id(event_id)
    e = json_file(event_id + ".json", outbox.directory)
    if not matches_scope(e, outbox.scope):
        raise MailboxError("event connection or workspace does not match selected mailbox")
    if not valid_binding(e) or e.get("id") != event_id or e.get("kind") not in tuple(EVENTS):
        raise MailboxError("invalid outbox event")
    return e


def events(outbox):
    # Ignore invalid/partial files like the Bridge. Never follow file links.
    with os.scandir(outbox.directory) as it:
        for entry in it:
            if not entry.name.endswith(".json") or not valid_id(entry.name[:-5]):
                continue
            try:
                e = load_event(outbox, entry.name[:-5])
                info = entry.stat(follow_symlinks=False)
            except (OSError, MailboxError):
                continue
            yield info.st_mtime_ns, e


def metadata(e):
    keys = ("id", "kind", *BINDING, *SCOPE_FIELDS, "issue_key", "source_id", "revision",
            "request_id", "status", "remote_id", "observed_at", "last_synced_at")
    return {k: (e[k][:256] if isinstance(e[k], str) else e[k])
            for k in keys if k in e and type(e[k]) in (str, int)}


def emit(obj):
    # Keep remote control characters safely escaped even on a terminal.
    print(json.dumps(obj, ensure_ascii=True, separators=(",", ":")))


def bounded_body(text):
    if not isinstance(text, str):
        raise MailboxError("expected UTF-8 text body")
    raw = text.encode("utf-8")
    if len(raw) > MAX_BODY or not text.strip() or "\0" in text:
        raise MailboxError("body must be nonblank UTF-8, NUL-free, at most 32 KiB")
    return text


def draft(outbox, event_id, kind, body_file):
    e = load_event(outbox, event_id)
    if e["kind"] not in {"task", "comment"}:
        raise MailboxError("draft must name a task or comment event")
    body = bounded_body(file_bytes(body_file, MAX_BODY).decode("utf-8"))
    return {"event_id": event_id, "request": {
        "version": 1, "id": uuid.uuid4().hex, "kind": kind, "body": body,
        **dict(zip(BINDING, bind(e))), **scope_fields(outbox.scope), **parent_fields(e, kind),
    }}


def parent_fields(event, kind):
    if kind != "reply" or event["kind"] != "comment":
        return {}
    parent_id = event.get("source_id")
    if not valid_id(parent_id):
        raise MailboxError("selected comment has no valid source ID")
    return {"parent_id": parent_id}


def resolve_request(outbox, item):
    if set(item) != {"event_id", "request"} or not isinstance(item["request"], dict):
        raise MailboxError("expected a draft with event_id and request")
    request = item["request"]
    if not matches_scope(request, outbox.scope):
        raise MailboxError("draft connection or workspace does not match selected mailbox")
    required = {"version", "id", "kind", "body", *BINDING, *scope_fields(outbox.scope)}
    if (not required <= set(request) or set(request) - required - {"parent_id"}
            or not valid_binding(request) or request["kind"] not in tuple(KINDS)):
        raise MailboxError("invalid draft request fields")
    require_id(request["id"])
    bounded_body(request["body"])
    e = load_event(outbox, item["event_id"])
    if e["kind"] not in {"task", "comment"} or bind(e) != bind(request):
        raise MailboxError("draft does not match the named task/comment event")
    parent = parent_fields(e, request["kind"])
    if {k: request[k] for k in ("parent_id",) if k in request} != parent:
        raise MailboxError("draft reply parent does not match the selected event; make a new draft")
    return request


def rebind_draft(outbox, item, event_id):
    original = resolve_request(outbox, item)
    e = load_event(outbox, event_id)
    if e["kind"] not in UNREAD_KINDS or e["issue_id"] != original["issue_id"]:
        raise MailboxError("rebind must select a task/comment event for the same issue")
    if e["epoch"] <= original["epoch"]:
        raise MailboxError("rebind must select a newer epoch for the same issue")
    # Choosing a different event in the same target run must not make a
    # second submission of the original draft possible.
    material = json.dumps(["rebind-v1", original["id"], *outbox.scope, *bind(e)], ensure_ascii=True)
    rid = hashlib.sha256(material.encode("ascii")).hexdigest()
    request = {k: v for k, v in original.items() if k != "parent_id"}
    request.update(dict(zip(BINDING, bind(e))))
    request.update(parent_fields(e, original["kind"]))
    request["id"] = rid
    return {"event_id": event_id, "request": request}


def sync_order(when, event):
    rev = event.get("revision")
    sequence = int(rev) if isinstance(rev, str) and rev.isascii() and rev.isdigit() and len(rev) <= 20 else 0
    return event["epoch"], sequence, when, event["id"]


def validate(outbox, inbox, request):
    snapshots = set()
    receipt_ids = set()
    uncertain = set()
    confirmed = set()
    newest_sync = None
    for when, other in events(outbox):
        if other["issue_id"] != request["issue_id"]:
            continue
        if other["kind"] == "task" and other["epoch"] >= request["epoch"]:
            snapshots.add(bind(other))
        if other["kind"] == "sync" and bind(other) == bind(request):
            key = sync_order(when, other)
            if newest_sync is None or key > newest_sync[0]:
                newest_sync = key, other.get("status")
        if other["kind"] != "receipt":
            continue
        rid = other.get("request_id")
        status = other.get("status")
        if valid_id(rid):
            if status == "delivery_uncertain":
                uncertain.add((bind(other), rid))
            if status == "delivery_confirmed":
                confirmed.add((bind(other), rid))
        if bind(other) == bind(request):
            if status in tuple(TERMINAL):
                raise MailboxError("local receipts show a completed run; inspect Multica")
            if valid_id(rid):
                receipt_ids.add(rid)
    if uncertain - confirmed:
        raise MailboxError("an earlier delivery on this issue is uncertain; inspect Multica")
    if newest_sync is not None and newest_sync[1] == "failed":
        raise MailboxError("local sync evidence marks this run failed; inspect Multica")
    if snapshots != {bind(request)}:
        raise MailboxError("draft run is stale, ambiguous or not backed by a current task snapshot")
    if request["id"] in receipt_ids:
        raise MailboxError("request ID already has a receipt; do not resend")
    with os.scandir(inbox) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or not valid_id(entry.name[:-5]):
                continue
            if entry.name == request["id"] + ".json":
                raise MailboxError("request ID already exists; do not resend")
            try:
                other = json_file(entry.name, inbox)
            except (OSError, MailboxError):
                continue
            if (matches_scope(other, outbox.scope) and valid_binding(other) and bind(other) == bind(request)
                    and other.get("id") == entry.name[:-5] and other["id"] not in receipt_ids):
                raise MailboxError("this run has an unreceipted request; inspect it before submitting another")


def issue_inbox(inbox, issue_id, create=False):
    name = require_id(issue_id)
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=inbox)
        except FileExistsError:
            pass
        else:
            os.fsync(inbox)
    try:
        return os.open(name, DIR_FLAGS, dir_fd=inbox)
    except OSError as exc:
        raise MailboxError("issue inbox must be a real directory; links are refused") from exc


def confirm(request, event_id, rebind_from=None):
    # stdin/stdout may be pipes; approval must come over the controlling tty.
    # This is an interaction safeguard, not authentication or a security boundary.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        raise MailboxError("operator confirmation requires a controlling /dev/tty") from exc
    try:
        if not os.isatty(fd):
            raise MailboxError("operator confirmation requires a real terminal")
        details = {"event_id": event_id, **request}
        if rebind_from is not None:
            details["rebind_from"] = {k: v for k, v in rebind_from.items() if k != "body"}
        view = json.dumps(details, indent=2, ensure_ascii=False)
        # Keep ordinary Chinese and other text readable, but render terminal
        # controls and bidi formatting as visible escapes, even in pasted text.
        view = "".join(c if c == "\n" or not (unicodedata.category(c).startswith("C")
                       or unicodedata.category(c) in {"Zl", "Zp"})
                       else json.dumps(c, ensure_ascii=True)[1:-1] for c in view)
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as output:
            output.write("Confirm local submission. Only continue if you authorize this exact action.\n")
            output.write(view + "\n")
            output.write("Type the full request ID to publish (anything else cancels): ")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="replace") as input_:
            return input_.readline(130).rstrip("\r\n") == request["id"]
    finally:
        os.close(fd)


def publish(inbox, request):
    name = require_id(request["id"]) + ".json"
    temporary = ".stage-" + uuid.uuid4().hex
    payload = (json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(payload) > MAX_FILE:
        raise MailboxError("serialized request exceeds size limit")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=inbox)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # Same-directory hard link makes complete bytes visible atomically and
        # fails if the final path already exists. No risky check-plus-rename.
        try:
            os.link(temporary, name, src_dir_fd=inbox, dst_dir_fd=inbox, follow_symlinks=False)
        except FileExistsError as exc:
            raise MailboxError("request ID already exists; not overwritten") from exc
        try:
            os.fsync(inbox)
        except OSError as exc:
            raise MailboxError("request may already be visible; inspect its receipt before retrying") from exc
    finally:
        os.unlink(temporary, dir_fd=inbox)


def scoped_dir(root_fd, name, scope, create=False, optional=False):
    # Open each descendant relative to an already checked directory, refusing
    # symlinks at every namespace component.
    fd = os.dup(root_fd)
    try:
        for component in (name, *scope):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.dup(fd)
    except FileNotFoundError:
        if optional and not create:
            return None
        raise
    finally:
        os.close(fd)


def available_scopes(root_fd):
    result = []
    outbox = scoped_dir(root_fd, "outbox", (), optional=True)
    if outbox is None:
        return result
    try:
        with os.scandir(outbox) as connections:
            for entry in connections:
                if not valid_id(entry.name):
                    continue
                try:
                    connection = os.open(entry.name, DIR_FLAGS, dir_fd=outbox)
                except OSError:
                    continue
                try:
                    with os.scandir(connection) as workspaces:
                        for workspace in workspaces:
                            if valid_id(workspace.name) and workspace.is_dir(follow_symlinks=False):
                                result.append(scope_fields((entry.name, workspace.name)))
                finally:
                    os.close(connection)
        return sorted(result, key=lambda item: tuple(item[key] for key in SCOPE_FIELDS))
    finally:
        os.close(outbox)


def verify_scope_directory(root_fd, name, scope, directory):
    if not scope:
        return
    current = scoped_dir(root_fd, name, scope)
    try:
        if not os.path.samestat(os.fstat(current), os.fstat(directory)):
            raise MailboxError("mailbox namespace changed during confirmation")
    finally:
        os.close(current)


def pickup_dir(root_fd, scope, create=False):
    return scoped_dir(root_fd, "pickup", scope, create=create, optional=True)


def read_pickup(directory, issue):
    issue = require_id(issue)
    empty = {"version": 1, "issue_id": issue, "acknowledged": []}
    if directory is None:
        return empty
    try:
        state = json_file(issue + ".json", directory)
    except FileNotFoundError:
        return empty
    if (set(state) - {"version", "issue_id", "acknowledged", "worktree", "draft"}
            or type(state.get("version")) is not int or state["version"] != 1
            or state.get("issue_id") != issue or not isinstance(state.get("acknowledged"), list)
            or not all(valid_id(v) for v in state["acknowledged"])
            or any(k in state and (not isinstance(state[k], str) or not os.path.isabs(state[k]))
                   for k in ("worktree", "draft"))):
        raise MailboxError("invalid local pickup record")
    return state


def update_pickup(root_fd, scope, issue, mutate):
    directory = pickup_dir(root_fd, scope, create=True)
    try:
        # Stable local lock serializes short read/modify/write transactions.
        lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise MailboxError("pickup lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_pickup(directory, issue)
            mutate(state)
            raw = (json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(raw) > MAX_FILE:
                raise MailboxError("local pickup record is full; no changes written")
            temp = ".pickup-" + uuid.uuid4().hex
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, issue + ".json", src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temp, dir_fd=directory)
                except FileNotFoundError:
                    pass
            return state
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def pickup_status(outbox, directory, issue):
    state = read_pickup(directory, issue)
    acked = set(state["acknowledged"])
    latest = None
    newest_sync = None
    unread_count = 0
    for mtime, e in events(outbox):
        if e["issue_id"] != issue:
            continue
        if e["kind"] == "task" and (latest is None or (e["epoch"], mtime, e["id"]) > latest[0]):
            latest = ((e["epoch"], mtime, e["id"]), e)
        if e["kind"] == "sync":
            key = sync_order(mtime, e)
            if newest_sync is None or key > newest_sync[0]:
                newest_sync = (key, e)
        if e["kind"] in UNREAD_KINDS and e["id"] not in acked:
            unread_count += 1
    task = latest[1] if latest else None
    sync = newest_sync[1] if newest_sync else None
    return {**scope_fields(outbox.scope), "issue_id": issue, "unread_count": unread_count,
            "latest_task": metadata(task) if task else None,
            "latest_sync": metadata(sync) if sync and task and bind(sync) == bind(task) else None,
            **{k: state[k] for k in ("worktree", "draft") if k in state}}


def lookup_receipts(outbox, rid, issue, run_id):
    def matches():
        matched_issue = issue
        for when, e in events(outbox):
            if (e["kind"] != "receipt" or e.get("request_id") != rid
                    or (issue is not None and e["issue_id"] != issue)
                    or (run_id is not None and e["run_id"] != run_id)):
                continue
            if matched_issue is not None and e["issue_id"] != matched_issue:
                raise MailboxError("request ID matches multiple issues; specify --issue-id")
            matched_issue = e["issue_id"]
            yield when, metadata(e)
    return [e for _, e in heapq.nlargest(MAX_LIST, matches(),
            key=lambda x: (x[1].get("status") == "delivery_confirmed", x[0], x[1]["id"]))]


def validate_rebind_source(outbox, shelf, original):
    prior = original["request"]
    states = {e.get("status") for _, e in events(outbox)
              if e["kind"] == "receipt" and bind(e) == bind(prior)
              and e.get("request_id") == prior["id"] and isinstance(e.get("status"), str)}
    if states.intersection({"reply_posted", "completion_requested", "delivery_uncertain", "delivery_confirmed"}):
        raise MailboxError("original request may already have posted; do not rebind")
    try:
        os.stat(prior["id"] + ".json", dir_fd=shelf, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not states.intersection({"stale", "invalid_parent", "invalid_body", "remote_rejected", "blocked_uncertain"}):
        raise MailboxError("original request is pending or unresolved; do not rebind")


def pointer(path):
    if not os.path.isabs(path):
        raise MailboxError("pickup pointers must be absolute local paths")
    return path


def selected_scope(connection, workspace):
    if (connection is None) != (workspace is None):
        raise MailboxError("specify --connection and --workspace together")
    return (require_id(connection), require_id(workspace)) if connection is not None else ()


def check_root(root):
    if not isinstance(root, str) or not os.path.isabs(root):
        raise MailboxError("configure an absolute mailbox root with setup, --root or BRIDGE_ROOT")
    return root


def route_pair(value):
    pieces = value.split(":")
    if len(pieces) != 2:
        raise MailboxError("use --route CONNECTION:WORKSPACE")
    return selected_scope(*pieces)


def workspace_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        return None
    return canonical if value.lower() == canonical else None


def workspace_entries(install, connection):
    compose = install / "compose.yaml"
    if not compose.is_file():
        raise MailboxError("slug lookup requires the Bridge Compose directory; set --install-dir")
    command = ["docker", "compose", "--project-directory", str(install), "-f", str(compose),
               "exec", "-T", "-e", f"HOME=/bridge-runtime/{connection}/home", "bridge",
               "multica", "--profile", connection, "workspace", "list", "--output", "json"]
    try:
        result = subprocess.run(command, cwd=install, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=20, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MailboxError(f"cannot look up workspaces for {connection}; check the running Bridge and Docker access") from exc
    try:
        entries = json.loads(result.stdout, object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MailboxError("invalid Multica workspace list") from exc
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise MailboxError("invalid Multica workspace list")
    return entries


def resolve_setup_routes(root, install_dir, values):
    install = Path(check_root(install_dir)) if install_dir is not None else Path(root).parent
    by_connection = {}
    pairs = []
    for connection, workspace in map(route_pair, values):
        identifier = workspace_uuid(workspace)
        if identifier is None:
            if connection not in by_connection:
                by_connection[connection] = workspace_entries(install, connection)
            matches = [entry for entry in by_connection[connection] if entry.get("slug") == workspace]
            if len(matches) != 1 or not (identifier := workspace_uuid(matches[0].get("id"))):
                raise MailboxError(f"no unique workspace with slug {workspace} on {connection}")
        pairs.append((connection, identifier))
    return pairs


def load_local_config():
    try:
        config = json_file(LOCAL_CONFIG)
    except FileNotFoundError:
        return None, ()
    routes = config.get("routes")
    if set(config) != {"root", "routes"} or not isinstance(routes, list):
        raise MailboxError("invalid mailbox.local.json; run setup again")
    pairs = []
    for entry in routes:
        if not isinstance(entry, dict) or set(entry) != set(SCOPE_FIELDS):
            raise MailboxError("invalid project route; run setup again")
        pairs.append(tuple(require_id(entry[key]) for key in SCOPE_FIELDS))
    return check_root(config["root"]), tuple(dict.fromkeys(pairs))


def save_local_config(root, install_dir, values):
    root = check_root(root)
    fd = os.open(root, DIR_FLAGS)
    try:
        discovered = scoped_dir(fd, "outbox", ())
        os.close(discovered)
    finally:
        os.close(fd)
    routes = resolve_setup_routes(root, install_dir, values)
    config = {"root": root, "routes": [scope_fields(pair) for pair in dict.fromkeys(routes)]}
    fd, staged = tempfile.mkstemp(prefix=".mailbox-config-", dir=LOCAL_CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, LOCAL_CONFIG)
    finally:
        Path(staged).unlink(missing_ok=True)
    emit({"status": "configured", "config_path": str(LOCAL_CONFIG), **config})


def list_events(root_fd, routes, args):
    if not 1 <= args.limit <= MAX_LIST:
        raise MailboxError(f"limit must be 1–{MAX_LIST}")
    issue_id = require_id(args.issue_id) if args.issue_id is not None else None
    run_id = require_id(args.run_id) if args.run_id is not None else None

    def selected():
        for scope in routes:
            # A configured workspace may not have received its first task yet.
            outbox_fd = scoped_dir(root_fd, "outbox", scope, optional=True)
            if outbox_fd is None:
                continue
            directory = None
            try:
                directory = pickup_dir(root_fd, scope) if args.unread else None
                ack_map = {}
                for when, e in events(Outbox(outbox_fd, scope)):
                    issue = e["issue_id"]
                    if issue_id is not None and issue != issue_id:
                        continue
                    if run_id is not None and e["run_id"] != run_id:
                        continue
                    if args.unread:
                        if e["kind"] not in UNREAD_KINDS:
                            continue
                        if issue not in ack_map:
                            ack_map[issue] = set(read_pickup(directory, issue)["acknowledged"])
                        if e["id"] in ack_map[issue]:
                            continue
                    yield when, scope, e
            finally:
                if directory is not None:
                    os.close(directory)
                os.close(outbox_fd)

    newest = heapq.nlargest(args.limit, selected(), key=lambda x: (x[0], x[1], x[2]["id"]))
    emit([metadata(e) for _, _, e in newest])


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", help="absolute mailbox root; overrides project setup (BRIDGE_ROOT is a fallback)")
    p.add_argument("--connection", help="locally configured connection ID (requires --workspace)")
    p.add_argument("--workspace", help="exact Multica workspace ID (requires --connection)")
    commands = p.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="save local mailbox settings next to the installed SKILL.md")
    setup.add_argument("mailbox_root")
    setup.add_argument("--route", action="append", default=[], metavar="CONNECTION:SLUG_OR_UUID")
    setup.add_argument("--install-dir", help="absolute Bridge Compose install directory; defaults to the parent of the mailbox root")
    commands.add_parser("scopes", help="list available multi-connection outboxes")
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--issue-id")
    listing.add_argument("--run-id")
    listing.add_argument("--unread", action="store_true")
    commands.add_parser("read").add_argument("event_id")
    receipt = commands.add_parser("receipt")
    receipt.add_argument("request_id")
    receipt.add_argument("--issue-id")
    receipt.add_argument("--run-id")
    commands.add_parser("ack").add_argument("event_id")
    commands.add_parser("status").add_argument("issue_id")
    pickup = commands.add_parser("pickup")
    pickup.add_argument("issue_id")
    cwd = pickup.add_mutually_exclusive_group()
    cwd.add_argument("--worktree")
    cwd.add_argument("--clear-worktree", action="store_true")
    saved = pickup.add_mutually_exclusive_group()
    saved.add_argument("--draft")
    saved.add_argument("--clear-draft", action="store_true")
    d = commands.add_parser("draft")
    d.add_argument("event_id")
    d.add_argument("--kind", required=True, choices=sorted(KINDS))
    d.add_argument("--body-file", required=True, type=Path)
    submit = commands.add_parser("submit")
    submit.add_argument("draft_path", type=Path)
    submit.add_argument("--rebind", metavar="NEW_EVENT_ID")
    submit.add_argument("--yes", action="store_true", help="submit an authorized scoped reply or completion without a terminal prompt")
    return p


def run(args):
    explicit_scope = selected_scope(args.connection, args.workspace)
    if args.command == "setup":
        if args.root is not None or explicit_scope:
            raise MailboxError("use setup /absolute/path [--route CONNECTION:SLUG_OR_UUID ...]")
        save_local_config(args.mailbox_root, args.install_dir, args.route)
        return
    saved_root, saved_routes = load_local_config()
    root = check_root(args.root or saved_root or os.environ.get("BRIDGE_ROOT"))
    same_root = saved_root is not None and os.path.realpath(root) == os.path.realpath(saved_root)
    routes = saved_routes if same_root else ()
    if explicit_scope and routes and explicit_scope not in routes:
        raise MailboxError("connection/workspace is not in this project's configured routes")
    scope = explicit_scope or (routes[0] if len(routes) == 1 and args.command != "scopes" else ())
    if len(routes) > 1 and args.command not in {"list", "scopes"} and not explicit_scope:
        raise MailboxError("multiple project routes; use the selected event's --connection and --workspace from list")
    if args.command == "submit" and args.yes and (args.rebind or not scope):
        raise MailboxError("--yes requires a scoped request and cannot be used with --rebind")
    root_fd = os.open(root, DIR_FLAGS)
    try:
        if args.command == "scopes":
            if scope:
                raise MailboxError("scopes lists all outboxes; do not specify selectors")
            emit(available_scopes(root_fd))
            return
        if args.command == "list":
            list_events(root_fd, (explicit_scope,) if explicit_scope else routes or ((),), args)
            return
        outbox = Outbox(scoped_dir(root_fd, "outbox", scope), scope)
        try:
            if args.command == "read":
                e = load_event(outbox, args.event_id)
                body = e.get("body", "")
                if not isinstance(body, str):
                    raise MailboxError("invalid event body")
                raw = body.encode("utf-8")
                emit({**metadata(e), "body": raw[:MAX_READ_BODY].decode("utf-8", errors="ignore"),
                      "body_truncated": len(raw) > MAX_READ_BODY})
            elif args.command == "receipt":
                issue = require_id(args.issue_id) if args.issue_id is not None else None
                run_id = require_id(args.run_id) if args.run_id is not None else None
                emit(lookup_receipts(outbox, require_id(args.request_id), issue, run_id))
            elif args.command == "ack":
                event = load_event(outbox, args.event_id)
                if event["kind"] not in UNREAD_KINDS:
                    raise MailboxError("only task/comment events can be acknowledged")
                def acknowledge(state):
                    if event["id"] not in state["acknowledged"]:
                        state["acknowledged"].append(event["id"])
                update_pickup(root_fd, scope, event["issue_id"], acknowledge)
                emit({**scope_fields(scope), "issue_id": event["issue_id"], "event_id": event["id"], "status": "acknowledged_locally"})
            elif args.command in {"status", "pickup"}:
                issue = require_id(args.issue_id)
                if args.command == "pickup" and (args.worktree is not None or args.draft is not None
                                                  or args.clear_worktree or args.clear_draft):
                    if not any(e["issue_id"] == issue and e["kind"] == "task" for _, e in events(outbox)):
                        raise MailboxError("no local task snapshot for this issue")
                    def set_pointers(state):
                        for k in ("worktree", "draft"):
                            value = getattr(args, k)
                            if value is not None:
                                state[k] = pointer(value)
                            elif getattr(args, "clear_" + k):
                                state.pop(k, None)
                    update_pickup(root_fd, scope, issue, set_pointers)
                directory = pickup_dir(root_fd, scope)
                try:
                    emit(pickup_status(outbox, directory, issue))
                finally:
                    if directory is not None:
                        os.close(directory)
            elif args.command == "draft":
                emit(draft(outbox, args.event_id, args.kind, args.body_file))
            elif args.command == "submit":
                original = json_file(args.draft_path)
                item = rebind_draft(outbox, original, args.rebind) if args.rebind else original
                request = resolve_request(outbox, item)
                inbox = scoped_dir(root_fd, "inbox", scope, create=bool(scope))
                try:
                    shelf = issue_inbox(inbox, request["issue_id"], create=True)
                    try:
                        validate(outbox, shelf, request)
                        if args.rebind:
                            validate_rebind_source(outbox, shelf, original)
                        if not args.yes and not confirm(request, item["event_id"], original["request"] if args.rebind else None):
                            raise MailboxError("submission cancelled; no request was written")
                        verified = issue_inbox(inbox, request["issue_id"])
                        try:
                            # Serialize the final check and publish across CLI writers,
                            # without holding the lock while an operator reviews a draft.
                            fcntl.flock(verified, fcntl.LOCK_EX)
                            verify_scope_directory(root_fd, "outbox", scope, outbox.directory)
                            verify_scope_directory(root_fd, "inbox", scope, inbox)
                            if not os.path.samestat(os.fstat(shelf), os.fstat(verified)):
                                raise MailboxError("issue inbox changed during confirmation")
                            resolve_request(outbox, item)
                            if args.rebind and rebind_draft(outbox, original, args.rebind) != item:
                                raise MailboxError("rebind target changed during confirmation")
                            validate(outbox, verified, request)
                            if args.rebind:
                                validate_rebind_source(outbox, verified, original)
                            publish(verified, request)
                        finally:
                            os.close(verified)
                    finally:
                        os.close(shelf)
                    emit({**scope_fields(scope), "request_id": request["id"], "status": "submitted_to_local_inbox"})
                finally:
                    os.close(inbox)
        finally:
            os.close(outbox.directory)
    finally:
        os.close(root_fd)


def main(argv=None):
    try:
        run(parser().parse_args(argv))
        return 0
    except (MailboxError, UnicodeError, OSError) as exc:
        print(f"mailbox: {str(exc)!r}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("mailbox: interrupted; inspect the inbox before retrying", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
