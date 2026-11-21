#!/usr/bin/env python3
"""Local Multica mailbox utility; no network, agent execution, or Bridge state access."""

import argparse
import fcntl
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
import uuid

MAX_FILE = 256 * 1024
MAX_BODY = 32 * 1024
MAX_READ_BODY = 32 * 1024
MAX_LIST = 100
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
BINDING = ("issue_id", "run_id", "epoch")
EVENTS = {"task", "comment", "receipt", "sync"}
UNREAD_KINDS = {"task", "comment"}
KINDS = {"reply", "complete"}
TERMINAL = {"completion_requested"}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class MailboxError(Exception):
    pass


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
    e = json_file(event_id + ".json", outbox)
    if not valid_binding(e) or e.get("id") != event_id or e.get("kind") not in tuple(EVENTS):
        raise MailboxError("invalid outbox event")
    return e


def events(outbox):
    # Ignore invalid/partial files like the Bridge. Never follow file links.
    with os.scandir(outbox) as it:
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
    keys = ("id", "kind", *BINDING, "issue_key", "source_id", "revision",
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
        **dict(zip(BINDING, bind(e))), **parent_fields(e, kind),
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
    required = {"version", "id", "kind", "body", *BINDING}
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
    material = json.dumps(["rebind-v1", original["id"], *bind(e)], ensure_ascii=True)
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
            if (valid_binding(other) and bind(other) == bind(request)
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


def pickup_dir(root_fd, create=False):
    if create:
        try:
            os.mkdir("pickup", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
    try:
        return os.open("pickup", DIR_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        if not create:
            return None
        raise


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


def update_pickup(root_fd, issue, mutate):
    directory = pickup_dir(root_fd, create=True)
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
    return {"issue_id": issue, "unread_count": unread_count,
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


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", help="absolute mailbox root; defaults to BRIDGE_ROOT")
    commands = p.add_subparsers(dest="command", required=True)
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
    return p


def run(args):
    root = args.root or os.environ.get("BRIDGE_ROOT")
    if not root or not os.path.isabs(root):
        raise MailboxError("provide an absolute --root or BRIDGE_ROOT")
    root_fd = os.open(root, DIR_FLAGS)
    try:
        outbox = os.open("outbox", DIR_FLAGS, dir_fd=root_fd)
        try:
            if args.command == "list":
                if not 1 <= args.limit <= MAX_LIST:
                    raise MailboxError(f"limit must be 1–{MAX_LIST}")
                issue_id = require_id(args.issue_id) if args.issue_id is not None else None
                run_id = require_id(args.run_id) if args.run_id is not None else None
                selected = (x for x in events(outbox)
                            if (issue_id is None or x[1]["issue_id"] == issue_id)
                            and (run_id is None or x[1]["run_id"] == run_id))
                directory = pickup_dir(root_fd) if args.unread else None
                try:
                    ack_map = {}
                    def unread(e):
                        if e["kind"] not in UNREAD_KINDS:
                            return False
                        issue = e["issue_id"]
                        if issue not in ack_map:
                            ack_map[issue] = set(read_pickup(directory, issue)["acknowledged"])
                        return e["id"] not in ack_map[issue]
                    if args.unread:
                        selected = (x for x in selected if unread(x[1]))
                    newest = heapq.nlargest(args.limit, selected, key=lambda x: (x[0], x[1]["id"]))
                    emit([metadata(e) for _, e in newest])
                finally:
                    if directory is not None:
                        os.close(directory)
            elif args.command == "read":
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
                update_pickup(root_fd, event["issue_id"], acknowledge)
                emit({"issue_id": event["issue_id"], "event_id": event["id"], "status": "acknowledged_locally"})
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
                    update_pickup(root_fd, issue, set_pointers)
                directory = pickup_dir(root_fd)
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
                inbox = os.open("inbox", DIR_FLAGS, dir_fd=root_fd)
                try:
                    shelf = issue_inbox(inbox, request["issue_id"], create=True)
                    try:
                        validate(outbox, shelf, request)
                        if args.rebind:
                            validate_rebind_source(outbox, shelf, original)
                        if not confirm(request, item["event_id"], original["request"] if args.rebind else None):
                            raise MailboxError("submission cancelled; no request was written")
                        # Re-check the named event and latest evidence after operator input.
                        # The shelf must still be the same real directory, not a link or replacement.
                        resolve_request(outbox, item)
                        if args.rebind and rebind_draft(outbox, original, args.rebind) != item:
                            raise MailboxError("rebind target changed during confirmation")
                        verified = issue_inbox(inbox, request["issue_id"])
                        try:
                            if not os.path.samestat(os.fstat(shelf), os.fstat(verified)):
                                raise MailboxError("issue inbox changed during confirmation")
                            validate(outbox, verified, request)
                            if args.rebind:
                                validate_rebind_source(outbox, verified, original)
                            publish(verified, request)
                        finally:
                            os.close(verified)
                    finally:
                        os.close(shelf)
                    emit({"request_id": request["id"], "status": "submitted_to_local_inbox"})
                finally:
                    os.close(inbox)
        finally:
            os.close(outbox)
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
