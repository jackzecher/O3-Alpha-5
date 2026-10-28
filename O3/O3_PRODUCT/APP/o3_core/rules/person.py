"""O3 Person Robot v0.1: deterministic Profile JSON -> Person processing.

One Python-native rule registry; three authorized operations only:
  Relationship Tagging, Property Value Modification, Additional Property Assignment.

Source files are read-only and archived byte-for-byte. No AI, human review,
network calls, Organization creation, or person merges by name/email.
Python 3.12+; standard library only. RDFLib is optional for on-demand export.
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
import sqlite3
import sys
import uuid
from typing import Callable
from urllib.parse import quote

VERSION = "0.1.0"
RELATIONSHIP = "Relationship Tagging"
MODIFICATION = "Property Value Modification"
ADDITION = "Additional Property Assignment"
GROUPS = (RELATIONSHIP, MODIFICATION, ADDITION)
MAX_BYTES = 4 * 1024 * 1024
DEFAULT_SCOPE = "o3data"  # One configured DoD source domain; identity scope is provisional.


class SourceError(ValueError):
    """Invalid or ambiguous source; do not damage previously committed data."""


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stripped(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def email(v: object) -> str | None:
    s = stripped(v)
    if s.count("@") != 1 or any(ch.isspace() for ch in s):
        return None
    local, domain = s.rsplit("@", 1)
    if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return s.casefold()  # Consistent with O3 Email/Event prototype identity adapters.


def label_key(v: object) -> str:
    return " ".join(stripped(v).casefold().split())


def directory_id(v: object) -> str:
    raw = stripped(v)
    if not raw:
        raise SourceError("Missing directory id; Person identity cannot be established")
    try:
        return str(uuid.UUID(raw))  # Normalize GUID case/format only when it is a GUID.
    except ValueError:
        return raw  # Non-GUID identifiers remain opaque and case-preserved.


def person_id(scope: str, directory_identifier: str) -> str:
    if not scope or not directory_identifier:
        raise SourceError("Nonempty directory scope and identifier required")
    parts = [scope, directory_identifier]
    return "person_" + sha(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def source_profile(data: object) -> dict:
    """Focus importer recognizes direct Graph profile or a JSON {body:{...}} envelope.

    Other top-level metadata is retained in the immutable original, not copied into
    a Person identity or silently interpreted as ontology properties.
    """
    if not isinstance(data, dict):
        raise SourceError("Profile JSON must be an object")
    p = data.get("body") if isinstance(data.get("body"), dict) else data
    if not isinstance(p.get("id"), str) or not p["id"].strip():
        raise SourceError("Profile must contain a nonempty directory id")
    directory_id(p["id"])
    for name in ("displayName", "mail", "userPrincipalName", "jobTitle", "department",
                 "companyName", "officeLocation"):
        if name in p and p[name] is not None and not isinstance(p[name], str):
            raise SourceError(f"{name}: expected string or null")
    phones = p.get("businessPhones")
    if phones is not None and (not isinstance(phones, list) or
                               any(not isinstance(ph, str) for ph in phones)):
        raise SourceError("businessPhones: expected string array or null")
    if "accountEnabled" in p and p["accountEnabled"] is not None and not isinstance(p["accountEnabled"], bool):
        raise SourceError("accountEnabled: expected boolean or null")
    if not (email(p.get("mail")) or email(p.get("userPrincipalName"))):
        raise SourceError("Neither mail nor userPrincipalName contains a usable address")
    return p


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)


@dataclass
class OrganizationIndex:
    """Read-only index of existing O3 Organization identities.

    Expected records: {record_id, kind:'company'|'department', name, company?}.
    Departments require an explicit company anchor. No fuzzy matching or creation.
    """
    companies: dict[str, set[str]] = field(default_factory=dict)
    departments: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    @classmethod
    def from_list(cls, raw: object) -> "OrganizationIndex":
        if not isinstance(raw, list):
            raise SourceError("Organization index must be a JSON list")
        idx = cls()
        for item in raw:
            if not isinstance(item, dict):
                raise SourceError("Organization index entry must be an object")
            rid = stripped(item.get("record_id"))
            kind = stripped(item.get("kind")).casefold()
            name = label_key(item.get("name"))
            if not rid or not name or kind not in ("company", "department"):
                raise SourceError("Organization entry needs record_id, name, kind (company/department)")
            if kind == "company":
                idx.companies.setdefault(name, set()).add(rid)
            else:
                company = label_key(item.get("company"))
                if not company:
                    raise SourceError("Department organization needs company scope")
                idx.departments.setdefault((company, name), set()).add(rid)
        return idx

    @staticmethod
    def unique(mapping: dict, key: object) -> str | None:
        matches = mapping.get(key, set())
        return next(iter(matches)) if len(matches) == 1 else None

    def company(self, name: object) -> str | None:
        return self.unique(self.companies, label_key(name))

    def department(self, company: object, department: object) -> str | None:
        if not label_key(company) or not label_key(department):
            return None
        return self.unique(self.departments, (label_key(company), label_key(department)))


@dataclass
class Context:
    raw: dict
    record_id: str
    scope: str
    organizations: OrganizationIndex
    normalized: dict = field(init=False)
    additional: dict = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    outcomes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.normalized = copy.deepcopy(self.raw)

    def replace(self, key: str, original: object, updated: object) -> bool:
        if original == updated:
            return False
        self.modifications.append({"property": key, "source_value": original, "normalized_value": updated})
        return True

    def link(self, predicate: str, target: str | None, evidence: str) -> bool:
        if not target or target == self.record_id:
            return False
        key = (self.record_id, predicate, target)
        if key in self.links:
            return False
        self.links[key] = evidence
        return True


# --- Property Value Modification: only deterministic representation changes ---

@rule("PER-MOD-001", MODIFICATION, ("mail", "userPrincipalName"),
      "Normalize syntactically valid profile email and UPN addresses for matching")
def normalize_addresses(ctx: Context) -> bool:
    changed = False
    for key in ("mail", "userPrincipalName"):
        old = ctx.normalized.get(key)
        if old is None:
            continue
        new = email(old)
        if new is None:
            if old:
                ctx.warnings.append(f"{key}: invalid address, preserved but not used to resolve identity")
            continue
        if ctx.replace(key, old, new):
            ctx.normalized[key] = new
            changed = True
    return changed


@rule("PER-MOD-002", MODIFICATION, ("displayName", "jobTitle", "department", "companyName", "officeLocation"),
      "Trim boundary whitespace on profile labels without changing their meaning")
def normalize_labels(ctx: Context) -> bool:
    changed = False
    for key in ("displayName", "jobTitle", "department", "companyName", "officeLocation"):
        old = ctx.normalized.get(key)
        if isinstance(old, str) and ctx.replace(key, old, old.strip()):
            ctx.normalized[key] = old.strip()
            changed = True
    return changed


@rule("PER-MOD-003", MODIFICATION, ("businessPhones",),
      "Trim business telephone entries, retaining source order and all values")
def normalize_phones(ctx: Context) -> bool:
    old = ctx.normalized.get("businessPhones")
    if not isinstance(old, list):
        return False
    new = [p.strip() for p in old]
    if ctx.replace("businessPhones", old, new):
        ctx.normalized["businessPhones"] = new
        return True
    return False


@rule("PER-MOD-004", MODIFICATION, ("id",),
      "Normalize the directory identifier (GUID when recognized) without changing identity")
def normalize_directory_id(ctx: Context) -> bool:
    old = ctx.normalized.get("id")
    new = directory_id(old)
    if ctx.replace("id", old, new):
        ctx.normalized["id"] = new
        return True
    return False


# --- Additional Property Assignment: minimal useful record projections ---

@rule("PER-ADD-001", ADDITION, ("id",), "Assign a stable Person Record ID from scoped directory id")
def assign_id(ctx: Context) -> bool:
    ctx.additional["record_id"] = ctx.record_id
    return True


@rule("PER-ADD-002", ADDITION, ("mail", "userPrincipalName"),
      "Collect valid, unique-within-profile addresses for existing Email/Event Person lookup")
def assign_addresses(ctx: Context) -> bool:
    addresses = sorted({addr for key in ("mail", "userPrincipalName")
                        if (addr := email(ctx.normalized.get(key)))})
    ctx.additional["addresses"] = addresses
    return bool(addresses)


@rule("PER-ADD-003", ADDITION, ("displayName", "mail", "userPrincipalName"),
      "Provide a display label, falling back to an available address without inventing a name")
def assign_display_label(ctx: Context) -> bool:
    label = stripped(ctx.normalized.get("displayName"))
    if not label:
        label = email(ctx.normalized.get("mail")) or email(ctx.normalized.get("userPrincipalName")) or ""
    if label:
        ctx.additional["display_label"] = label
        return True
    return False


# --- Relationship Tagging: only confirmed, unique existing targets ---

@rule("PER-REL-001", RELATIONSHIP, ("companyName",),
      "Link Person to uniquely identified existing Organization matching companyName")
def tag_company(ctx: Context) -> bool:
    return ctx.link("affiliated_with", ctx.organizations.company(ctx.normalized.get("companyName")), "companyName")


@rule("PER-REL-002", RELATIONSHIP, ("companyName", "department"),
      "Link Person to uniquely identified existing department Organization within its company")
def tag_department(ctx: Context) -> bool:
    return ctx.link("member_of", ctx.organizations.department(ctx.normalized.get("companyName"),
                                                                ctx.normalized.get("department")),
                    "companyName+department")


def evaluate(raw: dict, record_id: str, scope: str, organizations: OrganizationIndex | None = None) -> Context:
    ctx = Context(raw, record_id, scope, organizations or OrganizationIndex())
    for group in (MODIFICATION, ADDITION, RELATIONSHIP):
        for r in RULES:
            if r.group != group:
                continue
            try:
                applied = bool(r.execute(ctx))
            except (TypeError, ValueError) as ex:
                raise SourceError(f"Rule {r.rule_id} failed: {ex}") from ex
            ctx.outcomes.append({"id": r.rule_id, "result": "applied" if applied else "no_change"})
    return ctx



# Persistence and orchestration live exclusively in o3_core.engine.
