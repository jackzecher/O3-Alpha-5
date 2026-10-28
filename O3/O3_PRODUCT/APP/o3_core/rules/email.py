"""O3 Email Robot, prototype 0.1.

Python-native rule definitions for three deterministic operations:
  1. relationship tagging
  2. property value modification
  3. additional property assignment

Source JSON is read-only and archived byte-for-byte. No network, AI or human
step, external workflow, or ontology schema migration is performed.
Target Python: 3.12+. Runtime: standard library only.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable, Mapping

VERSION = "0.1.0"
RELATIONSHIP = "Relationship Tagging"
MODIFICATION = "Property Value Modification"
ADDITION = "Additional Property Assignment"
GROUPS = (RELATIONSHIP, MODIFICATION, ADDITION)
MAX_FILE_BYTES = 32 * 1024 * 1024  # Reject unexpected giant captures; configurable later.


class SourceError(ValueError):
    """Invalid or inconsistent source record, recoverable on a later scan."""


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)

def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _key(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _email(value: object) -> str:
    text = _key(value)
    if text.count("@") != 1 or any(ch.isspace() for ch in text):
        return ""
    user, domain = text.rsplit("@", 1)
    if not user or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return text.casefold()


def _datetime_utc(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None  # Do not invent a timezone.
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _folder(value: object) -> str | None:
    v = _key(value).casefold().replace("\\", "/").strip("/")
    if v in {"inbox"}:
        return "Inbox"
    if v in {"sent", "sent items", "sentitems"}:
        return "Sent"
    return None


def _clean_json_shape(data: object) -> dict:
    """Validate the fields used by this robot, not the entire provisional contract."""
    if not isinstance(data, dict):
        raise SourceError("Email JSON must be an object")
    if data.get("schema_version") not in (None, "em-raw-v1"):
        raise SourceError("Unsupported source schema_version")
    text_fields = (
        "schema_version", "capture_id", "mailbox_id", "source_folder",
        "message_id", "internet_message_id", "conversation_id", "in_reply_to",
        "subject", "received_at_utc", "sent_at_utc", "captured_at_utc",
        "importance", "body_html",
    )
    for name in text_fields:
        if name in data and data[name] is not None and not isinstance(data[name], str):
            raise SourceError(f"Invalid type for {name}: expected string or null")
    if "has_attachments" in data and data["has_attachments"] is not None and not isinstance(data["has_attachments"], bool):
        raise SourceError("Invalid type for has_attachments")
    for field_name in ("from", "to", "cc"):
        value = data.get(field_name)
        if value is None:
            continue
        if field_name == "from":
            if not isinstance(value, dict):
                raise SourceError("Invalid type for from")
            values = [value]
        else:
            if not isinstance(value, list):
                raise SourceError(f"Invalid type for {field_name}")
            values = value
        for person in values:
            if not isinstance(person, dict):
                raise SourceError(f"Invalid participant in {field_name}")
            for subfield in ("name", "address"):
                if person.get(subfield) is not None and not isinstance(person[subfield], str):
                    raise SourceError(f"Invalid {field_name}.{subfield}")
    a = data.get("attachments")
    if a is not None:
        if not isinstance(a, list) or any(not isinstance(x, dict) for x in a):
            raise SourceError("Invalid attachments array")
        for item in a:
            v = item.get("size_bytes")
            if v is not None and (type(v) is not int or v < 0):
                raise SourceError("Invalid attachment size_bytes")
    return data


class BodyText(HTMLParser):
    """Minimal plain-text projection, not an HTML sanitizer or renderer."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "head"}:
            self.ignored += 1
        elif not self.ignored and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "head"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in {"p", "div", "li", "tr"}:
            self.chunks.append("\n")

    def handle_data(self, data: str):
        if not self.ignored:
            self.chunks.append(data)

    def result(self) -> str:
        # Normalize whitespace without copying HTML layout or executing it.
        text = "".join(self.chunks)
        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()


@dataclass
class PersonIndex:
    """Read-only resolved Person records supplied by the O3 context layer.

    Never creates a Person from a display name or guesses a unique identity.
    """
    mapping: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_list(cls, persons: object) -> "PersonIndex":
        if not isinstance(persons, list):
            raise SourceError("Person context must be a list")
        out: dict[str, set[str]] = {}
        for p in persons:
            if not isinstance(p, dict) or not isinstance(p.get("record_id"), str):
                raise SourceError("Person context needs record_id")
            rid = _key(p["record_id"])
            addresses = p.get("addresses", [])
            if not rid or not isinstance(addresses, list):
                raise SourceError("Person context needs nonempty record_id and addresses list")
            for address in addresses:
                normalized = _email(address)
                if normalized:
                    out.setdefault(normalized, set()).add(rid)
        return cls(out)

    def resolve(self, address: object) -> str | None:
        candidates = self.mapping.get(_email(address), set())
        return next(iter(candidates)) if len(candidates) == 1 else None


@dataclass
class Context:
    raw: dict
    record_id: str
    identity_basis: str
    file_folder: str | None
    people: PersonIndex
    mail: sqlite3.Connection | None
    normalized: dict = field(init=False)
    additional: dict = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.normalized = copy.deepcopy(self.raw)

    def replace(self, field_path: str, previous: object, current: object) -> bool:
        if previous == current:
            return False
        self.modifications.append({"property": field_path, "source_value": previous, "normalized_value": current})
        return True

    def link_person(self, predicate: str, field_path: str, address: object) -> bool:
        target = self.people.resolve(address)
        if target is None:
            return False
        key = (self.record_id, predicate, target)
        if key in self.links:
            return False
        self.links[key] = field_path
        return True

    def link_reply(self, in_reply_to: str) -> bool:
        if not self.mail or not _key(self.normalized.get("mailbox_id")):
            return False
        mb = _key(self.normalized["mailbox_id"]).casefold()
        target = self.mail.resolve_internet(mb, in_reply_to, except_id=self.record_id)
        if not target:
            return False
        self.links[(self.record_id, "replies_to", target)] = "in_reply_to"
        return True


def identity(data: dict, source_sha: str) -> tuple[str, str]:
    """Provisional and conservative identity policy; no fuzzy entity merging.

    Mailbox+provider message ID preferred; mailbox+Internet Message-ID fallback.
    Without identifiers, content hash gives only provisional identity.
    """
    mailbox = _key(data.get("mailbox_id")).casefold()
    message = _key(data.get("message_id"))
    internet = _key(data.get("internet_message_id"))
    if mailbox and message:
        parts = ("provider", mailbox, message)
    elif mailbox and internet:
        parts = ("internet", mailbox, internet)
    else:
        parts = ("provisional_content", source_sha)
    key_bytes = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "mail_" + _sha(key_bytes), parts[0]


# -------- Property Value Modification rules --------

@rule("MAIL-MOD-001", MODIFICATION, ("from.address", "to[].address", "cc[].address"),
      "Trim and casefold syntactically valid participant addresses for matching")
def normalize_participants(ctx: Context) -> bool:
    changed = False
    for attr in ("from", "to", "cc"):
        data = ctx.normalized.get(attr)
        entries = [data] if attr == "from" and isinstance(data, dict) else (data if isinstance(data, list) else [])
        for i, entry in enumerate(entries):
            old = entry.get("address")
            normal = _email(old)
            if normal and ctx.replace(f"{attr}.address" if attr == "from" else f"{attr}[{i}].address", old, normal):
                entry["address"] = normal
                changed = True
    return changed


@rule("MAIL-MOD-002", MODIFICATION, ("received_at_utc", "sent_at_utc", "captured_at_utc"),
      "Convert timezone-qualified timestamps to normalized UTC ISO 8601")
def normalize_dates(ctx: Context) -> bool:
    changed = False
    for field_name in ("received_at_utc", "sent_at_utc", "captured_at_utc"):
        old = ctx.normalized.get(field_name)
        new = _datetime_utc(old)
        if new and ctx.replace(field_name, old, new):
            ctx.normalized[field_name] = new
            changed = True
        elif isinstance(old, str) and old and new is None:
            ctx.warnings.append(f"{field_name}: unparseable or missing timezone, retained unchanged")
    return changed


@rule("MAIL-MOD-003", MODIFICATION, ("importance",),
      "Normalize known Outlook importance values without inventing importance")
def normalize_importance(ctx: Context) -> bool:
    old = ctx.normalized.get("importance")
    if not isinstance(old, str):
        return False
    standard = {"low": "Low", "normal": "Normal", "high": "High"}
    new = standard.get(old.strip().casefold())
    if new and ctx.replace("importance", old, new):
        ctx.normalized["importance"] = new
        return True
    return False


@rule("MAIL-MOD-004", MODIFICATION, ("source_folder",),
      "Canonicalize recognized source folder labels in O3's internal representation")
def normalize_folder(ctx: Context) -> bool:
    old = ctx.normalized.get("source_folder")
    new = _folder(old)
    if new and ctx.replace("source_folder", old, new):
        ctx.normalized["source_folder"] = new
        return True
    return False


@rule("MAIL-MOD-005", MODIFICATION,
      ("mailbox_id", "message_id", "internet_message_id", "in_reply_to", "conversation_id"),
      "Trim surrounding whitespace on opaque source identifiers")
def normalize_ids(ctx: Context) -> bool:
    changed = False
    for field_name in ("mailbox_id", "message_id", "internet_message_id", "in_reply_to", "conversation_id"):
        old = ctx.normalized.get(field_name)
        if isinstance(old, str) and ctx.replace(field_name, old, old.strip()):
            ctx.normalized[field_name] = old.strip()
            changed = True
    return changed


# -------- Additional Property Assignment rules --------

@rule("MAIL-ADD-001", ADDITION, ("message_id", "mailbox_id", "internet_message_id"),
      "Assign stable internal record ID using conservative source identifiers")
def assign_record_id(ctx: Context) -> bool:
    ctx.additional["record_id"] = ctx.record_id
    return True


@rule("MAIL-ADD-002", ADDITION, ("source_folder",),
      "Derive Incoming or Outgoing only when mailbox folder evidence is consistent")
def assign_direction(ctx: Context) -> bool:
    json_folder = _folder(ctx.normalized.get("source_folder"))
    if json_folder and ctx.file_folder and json_folder != ctx.file_folder:
        ctx.warnings.append("source_folder contradicts folder path; direction unassigned")
        return False
    folder = json_folder or ctx.file_folder
    if folder not in {"Inbox", "Sent"}:
        return False
    ctx.additional["direction"] = "Incoming" if folder == "Inbox" else "Outgoing"
    return True


@rule("MAIL-ADD-003", ADDITION, ("body_html",),
      "Derive searchable plain text from body HTML, without changing the original")
def assign_body_text(ctx: Context) -> bool:
    html = ctx.normalized.get("body_html")
    if not isinstance(html, str):
        return False
    parser = BodyText()
    parser.feed(html)
    parser.close()
    ctx.additional["body_text"] = parser.result()
    return True


# -------- Relationship Tagging rules --------

@rule("MAIL-REL-001", RELATIONSHIP, ("from.address",),
      "Connect sender to exactly one already-resolved Person")
def sender(ctx: Context) -> bool:
    part = ctx.normalized.get("from")
    return ctx.link_person("sent_by", "from.address", part.get("address")) if isinstance(part, dict) else False


@rule("MAIL-REL-002", RELATIONSHIP, ("to[].address",),
      "Connect each To recipient to exactly one already-resolved Person")
def to_recipients(ctx: Context) -> bool:
    parts = ctx.normalized.get("to") or []
    return any_result(ctx.link_person("sent_to", f"to[{i}].address", p.get("address")) for i, p in enumerate(parts))


@rule("MAIL-REL-003", RELATIONSHIP, ("cc[].address",),
      "Connect each Cc recipient to exactly one already-resolved Person")
def cc_recipients(ctx: Context) -> bool:
    parts = ctx.normalized.get("cc") or []
    return any_result(ctx.link_person("copied_to", f"cc[{i}].address", p.get("address")) for i, p in enumerate(parts))


@rule("MAIL-REL-004", RELATIONSHIP, ("in_reply_to", "mailbox_id"),
      "Connect Replies To only when exactly one indexed Email matches in the same mailbox")
def replies_to(ctx: Context) -> bool:
    ref = _key(ctx.normalized.get("in_reply_to"))
    return ctx.link_reply(ref) if ref else False


def any_result(values) -> bool:
    """Evaluate *every* recipient even if an earlier one resolves successfully."""
    found = False
    for item in values:
        if item:
            found = True
    return found


def evaluate(data: dict, record_id: str, basis: str, *, people: PersonIndex | None = None,
             mail: sqlite3.Connection | None = None, file_folder: str | None = None) -> Context:
    ctx = Context(data, record_id, basis, file_folder, people or PersonIndex(), mail)
    # Modifications before tags: normalized addresses must feed relationship rules.
    for group in (MODIFICATION, ADDITION, RELATIONSHIP):
        for entry in RULES:
            if entry.group != group:
                continue
            try:
                applied = bool(entry.execute(ctx))
            except (ValueError, TypeError) as ex:
                raise SourceError(f"Rule {entry.rule_id} failed: {ex}") from ex
            ctx.outcomes.append({"id": entry.rule_id, "result": "applied" if applied else "no_change"})
    flag = data.get("has_attachments")
    items = data.get("attachments")
    if flag is False and items:
        ctx.warnings.append("has_attachments=false but attachment descriptors are present; source retained")
    elif flag is True and isinstance(items, list) and not items:
        ctx.warnings.append("has_attachments=true but no attachment descriptors; source retained")
    return ctx


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")



# Persistence and orchestration live in the unified engine.
