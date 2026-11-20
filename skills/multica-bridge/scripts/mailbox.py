#!/usr/bin/env python3
"""Local Multica mailbox utility; no network, agent execution, or state writes."""

import argparse
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
EVENTS = {"task", "comment", "receipt"}
KINDS = {"reply", "complete"}
TERMINAL = {"completion_requested", "delivery_uncertain", "blocked_uncertain"}
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
            and all(isinstance(obj.get(k), str) and bool(obj[k]) for k in BINDING[:2])
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
            "request_id", "status", "remote_id")
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
        **dict(zip(BINDING, bind(e))),
    }}


def validate(outbox, inbox, item):
    if set(item) != {"event_id", "request"} or not isinstance(item["request"], dict):
        raise MailboxError("expected a draft with event_id and request")
    request = item["request"]
    if (set(request) != {"version", "id", "kind", "body", *BINDING}
            or not valid_binding(request) or request["kind"] not in tuple(KINDS)):
        raise MailboxError("invalid draft request fields")
    require_id(request["id"])
    bounded_body(request["body"])
    e = load_event(outbox, item["event_id"])
    if e["kind"] not in {"task", "comment"} or bind(e) != bind(request):
        raise MailboxError("draft does not match the named task/comment event")
    snapshots = set()
    receipt_ids = set()
    for _, other in events(outbox):
        if other["issue_id"] != request["issue_id"]:
            continue
        if other["kind"] == "task" and other["epoch"] >= request["epoch"]:
            snapshots.add(bind(other))
        if other["kind"] == "receipt" and bind(other) == bind(request):
            if other.get("status") in tuple(TERMINAL):
                raise MailboxError("local receipts show a completed or uncertain run; inspect Multica")
            rid = other.get("request_id")
            if valid_id(rid):
                receipt_ids.add(rid)
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
    return request


def confirm(request, event_id):
    # stdin/stdout may be pipes; approval must come over the controlling tty.
    # This is an interaction safeguard, not authentication or a security boundary.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        raise MailboxError("operator confirmation requires a controlling /dev/tty") from exc
    try:
        if not os.isatty(fd):
            raise MailboxError("operator confirmation requires a real terminal")
        view = json.dumps({"event_id": event_id, **request}, indent=2, ensure_ascii=False)
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


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", help="absolute mailbox root; defaults to BRIDGE_ROOT")
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    commands.add_parser("read").add_argument("event_id")
    commands.add_parser("receipt").add_argument("request_id")
    d = commands.add_parser("draft")
    d.add_argument("event_id")
    d.add_argument("--kind", required=True, choices=sorted(KINDS))
    d.add_argument("--body-file", required=True, type=Path)
    commands.add_parser("submit").add_argument("draft_path", type=Path)
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
                newest = heapq.nlargest(args.limit, events(outbox), key=lambda x: (x[0], x[1]["id"]))
                emit([metadata(e) for _, e in newest])
            elif args.command == "read":
                e = load_event(outbox, args.event_id)
                body = e.get("body", "")
                if not isinstance(body, str):
                    raise MailboxError("invalid event body")
                raw = body.encode("utf-8")
                emit({**metadata(e), "body": raw[:MAX_READ_BODY].decode("utf-8", errors="ignore"),
                      "body_truncated": len(raw) > MAX_READ_BODY})
            elif args.command == "receipt":
                request_id = require_id(args.request_id)
                found = heapq.nlargest(MAX_LIST, (x for x in events(outbox)
                                       if x[1]["kind"] == "receipt" and x[1].get("request_id") == request_id),
                                       key=lambda x: (x[0], x[1]["id"]))
                emit([metadata(e) for _, e in found])
            elif args.command == "draft":
                emit(draft(outbox, args.event_id, args.kind, args.body_file))
            elif args.command == "submit":
                item = json_file(args.draft_path)
                inbox = os.open("inbox", DIR_FLAGS, dir_fd=root_fd)
                try:
                    request = validate(outbox, inbox, item)
                    if not confirm(request, item["event_id"]):
                        raise MailboxError("submission cancelled; nothing was written")
                    # Re-read latest outbox evidence after waiting for operator input.
                    validate(outbox, inbox, item)
                    publish(inbox, request)
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
