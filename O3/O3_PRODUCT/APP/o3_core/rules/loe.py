"""O3 LOE Robot v0.1: deterministic derivation from existing Email/Event/Document records.

No LOE source feed, AI decisions, human approvals, or speculative discovery.
All seven rules are defined and executed in this file. A single existing Person
Robot SQLite database also holds Organization and derived LOE tables. Existing
source robot databases are read-only and never modified by this module.

Python >=3.12, standard library only; RDFLib is optional for derived graph export.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterator
from urllib.parse import quote

VERSION = "0.1.0"
REL = "Relationship Tagging"
MOD = "Property Value Modification"
ADD = "Additional Property Assignment"
GROUPS = (REL, MOD, ADD)
SOURCES = {"Email", "Event", "Document"}
KIND = {
    "loe": "loe", "line of effort": "loe", "project": "project",
    "program": "program", "initiative": "initiative", "workstream": "workstream",
}
BAD_NAMES = {"unknown", "n/a", "na", "none", "tbd", "null", "-", "unspecified", "other"}
# A *whole labeled line/field*, not an incidental occurrence of a name in prose.
EXPLICIT_REF = re.compile(
    r"^\s*(Line\s+of\s+Effort|LOE|Project|Program|Initiative|Workstream)"
    r"(?:\s*\[([A-Za-z0-9][A-Za-z0-9._-]{1,31})\])?\s*:\s*(.*?)\s*$",
    flags=re.IGNORECASE,
)
REPLY_PREFIX = re.compile(r"^\s*(?:re|fwd?|aw|sv)\s*:\s*", re.IGNORECASE)


class SourceError(ValueError):
    """Input or identity uncertainty; leave previous derived state unchanged."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_label(value: str) -> str:
    return " ".join(value.split())


def norm_key(value: str) -> str:
    return normalize_label(value).casefold()


def valid_name(value: str) -> bool:
    return (2 <= len(value) <= 120 and norm_key(value) not in BAD_NAMES
            and not any(ord(ch) < 32 for ch in value)
            and not value.startswith(("http://", "https://")))


def identity(namespace: str, kind: str, name: str, code: str | None) -> tuple[str, str]:
    """A code has continuity across label changes; explicit names are provisional."""
    if not namespace or not kind or not valid_name(name):
        raise SourceError("LOE identity needs a namespace, explicit kind and valid name")
    if code:
        basis, key = "scoped_reference_code", ["code", namespace, code.casefold()]
    else:
        basis, key = "scoped_explicit_label", ["label", namespace, kind, norm_key(name)]
    return "loe_" + digest(key), basis


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)


@dataclass
class Evidence:
    source_type: str
    record_id: str
    source_sha: str
    lines: list[tuple[str, str]]


@dataclass
class Context:
    source: Evidence
    namespace: str
    mentions: list[dict] = field(default_factory=list)
    claims: dict[str, dict] = field(default_factory=dict)
    links: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def tag(self, rid: str, field_name: str) -> bool:
        key = (self.source.record_id, "associated_with", rid)
        if key not in self.links:
            self.links[key] = set()
        old_count = len(self.links[key])
        self.links[key].add(field_name)
        return len(self.links[key]) > old_count


@rule("LOE-MOD-001", MOD, ("Email.subject", "Event.summary", "Document.metadata.title",
                                "Document.metadata.subject", "Document.content.heading"),
      "Parse only explicit LOE/Project/Program/Initiative/Workstream field labels; normalize names")
def extract_explicit(ctx: Context) -> bool:
    for path, text in ctx.source.lines:
        if not isinstance(text, str):
            raise SourceError(f"{ctx.source.source_type}.{path} must be text")
        candidate = text
        if ctx.source.source_type == "Email" and path == "subject":
            # Remove conventional reply prefixes only for reference recognition.
            for _ in range(8):
                newer = REPLY_PREFIX.sub("", candidate, count=1)
                if newer == candidate:
                    break
                candidate = newer
        match = EXPLICIT_REF.fullmatch(candidate)
        if not match:
            continue
        name = normalize_label(match.group(3))
        if not valid_name(name):
            ctx.warnings.append(f"{path}: explicit LOE label has invalid or placeholder name")
            continue
        ctx.mentions.append({"kind": KIND[norm_key(match.group(1))],
                             "name": name, "code": match.group(2), "field": path})
        if name != match.group(3):
            ctx.modifications.append({"field":path,"original":match.group(3),"normalized":name})
    return bool(ctx.mentions)


@rule("LOE-MOD-002", MOD, ("Explicit reference type", "Explicit reference code"),
      "Canonicalize typed LOE reference codes and normalized kind tokens")
def normalize_codes(ctx: Context) -> bool:
    changed = False
    for mention in ctx.mentions:
        code = mention["code"]
        if code:
            canonical = code.upper()
            if canonical != code:
                ctx.modifications.append({"field":mention["field"]+".code",
                                          "original":code,"normalized":canonical})
                changed = True
            mention["code"] = canonical
    return changed


@rule("LOE-ADD-001", ADD, ("Explicit kind", "Name", "Reference code", "Namespace"),
      "Assign deterministic LOE Record ID from scoped explicit code or provisional typed label")
def assign_ids(ctx: Context) -> bool:
    for mention in ctx.mentions:
        rid, basis = identity(ctx.namespace, mention["kind"], mention["name"], mention["code"])
        mention["record_id"], mention["identity_basis"] = rid, basis
    return bool(ctx.mentions)


@rule("LOE-ADD-002", ADD, ("Explicit name", "Explicit kind", "Reference code"),
      "Assign LOE name, kind, and reference code from explicit source evidence only")
def assign_properties(ctx: Context) -> bool:
    for mention in ctx.mentions:
        rid = mention["record_id"]
        proposed = {"record_id":rid,"namespace":ctx.namespace, "kind":mention["kind"],
                    "name":mention["name"], "reference_code":mention["code"],
                    "identity_basis":mention["identity_basis"]}
        existing = ctx.claims.get(rid)
        if existing and (existing["kind"] != proposed["kind"]
                         or norm_key(existing["name"]) != norm_key(proposed["name"])):
            ctx.warnings.append(f"{rid}: inconsistent LOE labels in one source; identity excluded")
            ctx.claims[rid] = {**proposed, "conflict":True}
        elif existing and existing.get("conflict"):
            pass
        else:
            ctx.claims[rid] = proposed
    return bool(ctx.claims)


@rule("LOE-REL-001", REL, ("Email.subject", "Email.body_text first line"),
      "Connect Email to explicitly named LOEs; do not infer from ordinary message prose")
def link_email(ctx: Context) -> bool:
    return _link_source(ctx, "Email")


@rule("LOE-REL-002", REL, ("Event.summary", "Event.description first line"),
      "Connect active Event to explicitly named LOEs; no Person/Organization inference")
def link_event(ctx: Context) -> bool:
    return _link_source(ctx, "Event")


@rule("LOE-REL-003", REL, ("Document.metadata.title", "Document.metadata.subject", "Document.heading"),
      "Connect Document to explicitly named LOEs in metadata or structured headings")
def link_document(ctx: Context) -> bool:
    return _link_source(ctx, "Document")


def _link_source(ctx: Context, source_type: str) -> bool:
    if ctx.source.source_type != source_type:
        return False
    changed = False
    for mention in ctx.mentions:
        rid = mention["record_id"]
        if not ctx.claims[rid].get("conflict"):
            changed = ctx.tag(rid, mention["field"]) or changed
    return changed


def evaluate(src: Evidence, *, namespace: str) -> Context:
    if src.source_type not in SOURCES or not src.record_id or not namespace.strip():
        raise SourceError("Existing source Record ID, supported type and namespace required")
    ctx = Context(source=src, namespace=namespace.strip().casefold())
    for group in (MOD, ADD, REL):
        for spec in RULES:
            if spec.group == group:
                done = spec.execute(ctx)
                ctx.outcomes.append({"rule_id":spec.rule_id,"result":"applied" if done else "no_change"})
    return ctx


def first_line(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for line in value.splitlines()[:3]:  # Only the first meaningful line near the top, never quoted history.
        if line.strip():
            return line.strip()
    return None


def relevant_lines(source_type: str, properties: dict, added: dict) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    def put(path: str, value: object):
        if isinstance(value, str) and value.strip():
            result.append((path, value))

    if source_type == "Email":
        put("subject", properties.get("subject"))
        put("body_text.first_line", first_line(added.get("body_text")))
    elif source_type == "Event":
        put("summary", properties.get("summary"))
        put("description.first_line", first_line(properties.get("description")))
    elif source_type == "Document":
        if properties.get("extraction", {}).get("status") == "failed":
            return []
        meta = properties.get("metadata") or {}
        put("metadata.title", meta.get("title"))
        put("metadata.subject", meta.get("subject"))
        units = (properties.get("content") or {}).get("units") or []
        if not isinstance(units, list):
            raise SourceError("Document content.units must be an array")
        for ui, unit in enumerate(units):
            if not isinstance(unit, dict):
                raise SourceError("Document unit is not an object")
            elements = unit.get("elements", [])
            if not isinstance(elements, list):
                raise SourceError("Document elements must be an array")
            for ei, element in enumerate(elements):
                if isinstance(element, dict) and element.get("type", "").casefold() in {
                    "heading", "title", "section_heading"}:
                    put(f"content.units[{ui}].elements[{ei}].text", element.get("text"))
    return result


def input_fingerprint(src: Evidence, namespace: str) -> str:
    # Fields unrelated to LOE discovery never cause reassessment.
    return digest({"version":VERSION,"namespace":namespace,"source_type":src.source_type,
                   "record_id":src.record_id,"lines":src.lines})



# Persistence and orchestration live exclusively in o3_core.engine.
