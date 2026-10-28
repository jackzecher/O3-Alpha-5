"""O3 Document Robot v0.1: deterministic Document Corpus JSON processing.

Input contract: PROVISIONAL o3-document-v1 (converter not yet verified).
Exactly three operations: Relationship Tagging, Property Value Modification,
Additional Property Assignment. Rules and their metadata live together here.

Read-only O3Data input; immutable, content-addressed JSON archives; SQLite
results. No AI, human review, file conversion, network calls, or speculative
entities. Standard library only; RDFLib optional for on-demand RDF export.
Python 3.12+.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Callable
from urllib.parse import quote

VERSION = "0.1.0"
SCHEMA_VERSION = "o3-document-v1"
RELATIONSHIP = "Relationship Tagging"
MODIFICATION = "Property Value Modification"
ADDITION = "Additional Property Assignment"
GROUPS = (RELATIONSHIP, MODIFICATION, ADDITION)
MAX_CORPUS_BYTES = 128 * 1024 * 1024
SUPPORTED_TYPES = frozenset({"docx", "pdf", "pptx", "xlsx", "csv", "txt", "html", "htm", "md", "rtf"})

# ONE source-format contract for Word/PDF/PPT/Excel/CSV/Text/HTML/Markdown/RTF.
# Content element extensions are permitted so structure isn't lost.
CORPUS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:o3:document-corpus:o3-document-v1",
    "title": "O3 Standard Document Corpus (provisional)",
    "type": "object",
    "required": ["schema_version", "source", "origin", "metadata", "content", "extraction"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "source": {
            "type": "object", "required": ["filename", "file_type"],
            "properties": {
                "filename": {"type": "string", "pattern": "\\S"},
                "file_type": {"type": "string", "minLength": 1, "description": "docx, pdf, pptx, xlsx, csv, txt, html, htm, md, or rtf (case and dot normalized by robot)"},
                "mime_type": {"type": ["string", "null"]},
                "size_bytes": {"type": ["integer", "null"], "minimum": 0},
                "sha256": {"type": ["string", "null"], "pattern": "^[A-Fa-f0-9]{64}$"},
                "source_id": {"type": ["string", "null"]},
                "created_utc": {"type": ["string", "null"]},
                "modified_utc": {"type": ["string", "null"]},
            }, "additionalProperties": True,
        },
        "origin": {
            "type": "object", "required": ["type"],
            "properties": {
                "type": {"enum": ["file", "email_attachment"]},
                "mailbox_id": {"type": ["string", "null"]},
                "message_id": {"type": ["string", "null"]},
                "attachment_id": {"type": ["string", "null"]},
            }, "additionalProperties": True,
        },
        "metadata": {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"]},
                "authors": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": ["string", "null"]},
                "language": {"type": ["string", "null"]},
                "document_created_utc": {"type": ["string", "null"]},
                "document_modified_utc": {"type": ["string", "null"]},
                "security_markings": {"type": "array", "items": {"type": "string"}},
            }, "additionalProperties": True,
        },
        "content": {
            "type": "object", "required": ["units"],
            "properties": {"units": {"type": "array", "items": {
                "type": "object", "required": ["type", "elements"],
                "properties": {
                    "type": {"type": "string", "minLength": 1},
                    "elements": {"type": "array", "items": {
                        "type": "object", "required": ["type"],
                        "properties": {"type": {"type": "string", "minLength": 1}},
                        "additionalProperties": True,
                    }},
                }, "additionalProperties": True,
            }}}, "additionalProperties": True,
        },
        "extraction": {
            "type": "object", "required": ["status"],
            "properties": {
                "status": {"enum": ["complete", "partial", "failed"]},
                "method": {"type": ["string", "null"]},
                "converter_version": {"type": ["string", "null"]},
                "completed_utc": {"type": ["string", "null"]},
                "warnings": {"type": "array", "items": {"type": "string"}},
            }, "additionalProperties": True,
        },
    },
    "allOf": [
        {"if": {"properties": {"origin": {"properties": {"type": {"const": "file"}}}}},
         "then": {"properties": {"source": {
             "required": ["source_id"],
             "properties": {"source_id": {"type": "string", "pattern": "\\S"}},
         }}}},
        {"if": {"properties": {"origin": {"properties": {"type": {"const": "email_attachment"}}}}},
         "then": {"properties": {"origin": {
             "required": ["mailbox_id", "message_id", "attachment_id"],
             "properties": {x: {"type": "string", "pattern": "\\S"}
                            for x in ("mailbox_id", "message_id", "attachment_id")},
         }}}},
    ],
    "additionalProperties": True,
}


class SourceError(ValueError):
    """Source capture malformed or cannot be identified; safe to retry."""


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def normalize_email(v: object) -> str | None:
    s = text(v)
    if s.count("@") != 1 or any(ch.isspace() for ch in s):
        return None
    user, domain = s.rsplit("@", 1)
    if not user or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return s.casefold()


def utc_timestamp(v: object) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        t = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None or t.utcoffset() is None:
        return None  # Never invent a timezone.
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def typed_str_or_null(obj: dict, name: str, loc: str) -> None:
    if name in obj and obj[name] is not None and not isinstance(obj[name], str):
        raise SourceError(f"{loc}.{name} must be a string or null")


def _require_nonempty_str(obj: dict, key: str, where: str) -> None:
    if not text(obj.get(key)):
        raise SourceError(f"{where}.{key} is required and must be nonempty")


def validate_corpus(value: object) -> dict:
    """Validate the stable v1 fields the robot consumes without third-party packages.

    Extra converter-specific structured content is preserved verbatim; this is
    a runtime shape check, not an implementation of arbitrary JSON Schema.
    """
    if not isinstance(value, dict):
        raise SourceError("Document corpus must be an object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SourceError("Unsupported document corpus schema_version")
    for part in ("source", "origin", "metadata", "content", "extraction"):
        if not isinstance(value.get(part), dict):
            raise SourceError(f"{part} must be an object")
    src, origin, meta, content, ext = (value[k] for k in ("source", "origin", "metadata", "content", "extraction"))
    _require_nonempty_str(src, "filename", "source")
    _require_nonempty_str(src, "file_type", "source")
    if text(src["file_type"]).casefold().removeprefix(".") not in SUPPORTED_TYPES:
        raise SourceError("Unsupported document source.file_type")
    for field_name in ("mime_type", "sha256", "source_id", "created_utc", "modified_utc"):
        typed_str_or_null(src, field_name, "source")
    if "size_bytes" in src and src["size_bytes"] is not None:
        if type(src["size_bytes"]) is not int or src["size_bytes"] < 0:
            raise SourceError("source.size_bytes must be a nonnegative integer")
    if src.get("sha256") is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", src["sha256"]):
        raise SourceError("source.sha256 must be a 64-character hexadecimal checksum")
    if origin.get("type") not in ("file", "email_attachment"):
        raise SourceError("origin.type must be file or email_attachment")
    for key in ("mailbox_id", "message_id", "attachment_id"):
        typed_str_or_null(origin, key, "origin")
    if origin["type"] == "email_attachment":
        for key in ("mailbox_id", "message_id", "attachment_id"):
            _require_nonempty_str(origin, key, "origin")
    else:
        _require_nonempty_str(src, "source_id", "source")
    for key in ("title", "subject", "language", "document_created_utc", "document_modified_utc"):
        typed_str_or_null(meta, key, "metadata")
    for key in ("authors", "security_markings"):
        if key in meta and (not isinstance(meta[key], list) or any(not isinstance(x, str) for x in meta[key])):
            raise SourceError(f"metadata.{key} must be an array of strings")
    units = content.get("units")
    if not isinstance(units, list):
        raise SourceError("content.units must be an array")
    for i, unit in enumerate(units):
        if not isinstance(unit, dict) or not text(unit.get("type")) or not isinstance(unit.get("elements"), list):
            raise SourceError(f"content.units[{i}] requires type and elements array")
        for j, element in enumerate(unit["elements"]):
            if not isinstance(element, dict) or not text(element.get("type")):
                raise SourceError(f"content.units[{i}].elements[{j}] requires type")
            if "text" in element and element["text"] is not None and not isinstance(element["text"], str):
                raise SourceError(f"content.units[{i}].elements[{j}].text must be string or null")
            if "rows" in element and (not isinstance(element["rows"], list) or any(not isinstance(r, list) for r in element["rows"])):
                raise SourceError(f"content.units[{i}].elements[{j}].rows must be array of rows")
    if ext.get("status") not in ("complete", "partial", "failed"):
        raise SourceError("extraction.status must be complete, partial or failed")
    for key in ("method", "converter_version", "completed_utc"):
        typed_str_or_null(ext, key, "extraction")
    if "warnings" in ext and (not isinstance(ext["warnings"], list) or any(not isinstance(x, str) for x in ext["warnings"])):
        raise SourceError("extraction.warnings must be an array of strings")
    return value


def document_identity(corpus: dict, scope: str) -> tuple[str, str, str]:
    """Stable source identity, never filename or content hash as entity identity.

    Email attachment: scope + (mailbox, provider message ID, attachment ID).
    Standalone file: scope + converter-provided stable source_id.
    """
    scope = text(scope)
    if not scope:
        raise SourceError("Source scope is required")
    origin, src = corpus["origin"], corpus["source"]
    if origin["type"] == "email_attachment":
        parts = ["email_attachment", scope, text(origin["mailbox_id"]).casefold(),
                 text(origin["message_id"]), text(origin["attachment_id"])]
        basis = "email_attachment"
    else:
        parts = ["file", scope, text(src["source_id"])]
        basis = "source_id"
    key = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return "document_" + digest(key.encode("utf-8")), basis, key


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)


@dataclass
class PersonIndex:
    """Person Robot-compatible exported index: [{record_id, addresses:[...]}]."""
    by_address: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_list(cls, entries: object) -> "PersonIndex":
        if not isinstance(entries, list):
            raise SourceError("Person index must be an array")
        idx = cls()
        for p in entries:
            if not isinstance(p, dict) or not text(p.get("record_id")) or not isinstance(p.get("addresses"), list):
                raise SourceError("Person index entries need record_id and addresses")
            for addr in p["addresses"]:
                if not isinstance(addr, str):
                    raise SourceError("Person index address must be a string")
                clean = normalize_email(addr)
                if clean:
                    idx.by_address.setdefault(clean, set()).add(text(p["record_id"]))
        return idx

    def resolve(self, addr: object) -> str | None:
        possible = self.by_address.get(normalize_email(addr) or "", set())
        return next(iter(possible)) if len(possible) == 1 else None


class EmailLookup:
    """Structural interface; implementation uses the unified O3 repository."""
    def resolve(self, mailbox_id: str, message_id: str) -> str | None:
        raise NotImplementedError


@dataclass
class Context:
    source: dict
    record_id: str
    basis: str
    people: PersonIndex
    emails: EmailLookup | None
    normalized: dict = field(init=False)
    additional: dict = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.normalized = copy.deepcopy(self.source)

    def replace(self, property_path: str, before: object, after: object) -> bool:
        if before == after:
            return False
        self.modifications.append({"property": property_path,
                                   "source_value": before, "normalized_value": after})
        return True

    def link(self, predicate: str, target: str | None, evidence: str) -> bool:
        if not target or target == self.record_id:
            return False
        key = (self.record_id, predicate, target)
        if key in self.links:
            return False
        self.links[key] = evidence
        return True


# --- Property Value Modification (derived representation, source unchanged) ---

@rule("DOC-MOD-001", MODIFICATION, ("source.filename",),
      "Trim boundary whitespace on the display filename, preserving case and extension")
def normalize_filename(ctx: Context) -> bool:
    src = ctx.normalized["source"]
    before = src["filename"]
    after = before.strip()
    if ctx.replace("source.filename", before, after):
        src["filename"] = after
        return True
    return False


@rule("DOC-MOD-002", MODIFICATION, ("source.file_type", "source.mime_type"),
      "Canonicalize file type and MIME tokens, without changing source content")
def normalize_format(ctx: Context) -> bool:
    src = ctx.normalized["source"]
    changed = False
    before = src["file_type"]
    after = before.strip().casefold().removeprefix(".")
    if ctx.replace("source.file_type", before, after):
        src["file_type"] = after
        changed = True
    if isinstance(src.get("mime_type"), str):
        before = src["mime_type"]
        after = before.strip().casefold()
        if ctx.replace("source.mime_type", before, after):
            src["mime_type"] = after
            changed = True
    return changed


@rule("DOC-MOD-003", MODIFICATION,
      ("source.created_utc", "source.modified_utc", "metadata.document_created_utc",
       "metadata.document_modified_utc", "extraction.completed_utc"),
      "Normalize offset-aware ISO timestamps to UTC; leave missing or ambiguous values alone")
def normalize_dates(ctx: Context) -> bool:
    changed = False
    for section, keys in (("source", ("created_utc", "modified_utc")),
                          ("metadata", ("document_created_utc", "document_modified_utc")),
                          ("extraction", ("completed_utc",))):
        target = ctx.normalized[section]
        for key in keys:
            before = target.get(key)
            if before is None:
                continue
            after = utc_timestamp(before)
            if after is None:
                ctx.warnings.append(f"{section}.{key}: no valid timezone-aware timestamp; source retained")
                continue
            if ctx.replace(f"{section}.{key}", before, after):
                target[key] = after
                changed = True
    return changed


@rule("DOC-MOD-004", MODIFICATION,
      ("metadata.title", "metadata.subject", "metadata.language", "metadata.authors"),
      "Trim metadata labels and author entries; never rewrite authoritative security markings")
def normalize_descriptive(ctx: Context) -> bool:
    meta = ctx.normalized["metadata"]
    changed = False
    for key in ("title", "subject", "language"):
        value = meta.get(key)
        if isinstance(value, str) and ctx.replace(f"metadata.{key}", value, value.strip()):
            meta[key] = value.strip()
            changed = True
    if "authors" in meta:
        before = meta["authors"]
        after = [name.strip() for name in before]
        if ctx.replace("metadata.authors", before, after):
            meta["authors"] = after
            changed = True
    return changed


# --- Additional Property Assignment ---

@rule("DOC-ADD-001", ADDITION, ("source.source_id", "origin"),
      "Assign stable Document Record ID using explicit source or attachment identity")
def add_record_id(ctx: Context) -> bool:
    ctx.additional["record_id"] = ctx.record_id
    return True


@rule("DOC-ADD-002", ADDITION, ("metadata.title", "source.filename"),
      "Assign a nonsemantic display title, preferring supplied title then filename")
def add_display_title(ctx: Context) -> bool:
    ctx.additional["display_title"] = text(ctx.normalized["metadata"].get("title")) or ctx.normalized["source"]["filename"]
    return True


@rule("DOC-ADD-003", ADDITION, ("content.units",),
      "Assign small structural counts without duplicating extracted content")
def add_counts(ctx: Context) -> bool:
    units = ctx.normalized["content"]["units"]
    unit_counts: dict[str, int] = {}
    element_counts: dict[str, int] = {}
    for unit in units:
        kind = unit["type"].casefold()
        unit_counts[kind] = unit_counts.get(kind, 0) + 1
        for element in unit["elements"]:
            kind = element["type"].casefold()
            element_counts[kind] = element_counts.get(kind, 0) + 1
    ctx.additional["structure_counts"] = {
        "units": len(units),
        "elements": sum(element_counts.values()),
        "units_by_type": dict(sorted(unit_counts.items())),
        "elements_by_type": dict(sorted(element_counts.items())),
    }
    return True


# --- Relationship Tagging ---

@rule("DOC-REL-001", RELATIONSHIP,
      ("origin.type", "origin.mailbox_id", "origin.message_id", "origin.attachment_id"),
      "Link an email attachment to exactly one existing Email via the Email Robot database")
def tag_parent_email(ctx: Context) -> bool:
    origin = ctx.normalized["origin"]
    if origin["type"] != "email_attachment":
        return False
    if ctx.emails is None:
        ctx.warnings.append("Email lookup unavailable; attachment relationship remains unresolved")
        return False
    target = ctx.emails.resolve(origin["mailbox_id"], origin["message_id"])
    if not target:
        ctx.warnings.append("Attachment Email not found uniquely; relationship unresolved")
        return False
    return ctx.link("attached_to", target, "origin.mailbox_id+message_id+attachment_id")


@rule("DOC-REL-002", RELATIONSHIP, ("metadata.authors",),
      "Link authors to existing Persons only when an explicit author email resolves uniquely")
def tag_author(ctx: Context) -> bool:
    changed = False
    for i, author in enumerate(ctx.normalized["metadata"].get("authors", [])):
        if normalize_email(author) is None:
            continue  # Names alone do not establish unique Person identity.
        if ctx.link("authored_by", ctx.people.resolve(author), f"metadata.authors[{i}]"):
            changed = True
    return changed


def evaluate(corpus: dict, record_id: str, basis: str, *, people: PersonIndex | None = None,
             emails: EmailLookup | None = None) -> Context:
    validate_corpus(corpus)
    ctx = Context(corpus, record_id, basis, people or PersonIndex(), emails)
    # Deterministic canonical sequence: normalize, assign, relate.
    for group in (MODIFICATION, ADDITION, RELATIONSHIP):
        for item in RULES:
            if item.group == group:
                result = item.execute(ctx)
                ctx.outcomes.append({"rule_id": item.rule_id, "group": group, "applied": bool(result)})
    if ctx.normalized["extraction"]["status"] != "complete":
        ctx.warnings.append("Extraction incomplete; corpus stored without inventing missing content")
    return ctx



# Persistence and orchestration live exclusively in o3_core.engine.
