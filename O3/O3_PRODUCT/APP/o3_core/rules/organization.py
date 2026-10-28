"""O3 Organization Robot v0.1: deterministic derivation from existing Person records.

Authoritative Python-native rules: relationship tagging, value modification,
and additional property assignment. No primary Organization source stream,
AI, human decisions, speculative name matching, or network access.

The robot extends the EXISTING Person Robot SQLite database with derived
Organization tables. It never edits Person records or the immutable archive.
Python 3.12+, standard library only. RDFLib is optional for one-way export.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable
from urllib.parse import quote

VERSION = "0.1.0"
RELATIONSHIP = "Relationship Tagging"
MODIFICATION = "Property Value Modification"
ADDITION = "Additional Property Assignment"
GROUPS = (RELATIONSHIP, MODIFICATION, ADDITION)
INVALID_LABELS = frozenset({"unknown", "n/a", "na", "none", "not specified", "not provided", "null", "-"})


class SourceError(ValueError):
    """Invalid upstream state or ambiguous data: leave prior results intact."""


def digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def display_label(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceError("Organization-related profile labels must be strings or null")
    cleaned = " ".join(value.split())
    return "" if cleaned.casefold() in INVALID_LABELS else cleaned


def label_key(value: str) -> str:
    return " ".join(value.casefold().split())


def organization_id(scope: str, kind: str, name: str, parent_id: str | None = None) -> str:
    if not scope.strip() or kind not in ("organization", "department") or not label_key(name):
        raise SourceError("Organization identity requires scope, recognized kind and name")
    if kind == "department" and not parent_id:
        raise SourceError("Department requires a verified companyName parent anchor")
    if kind == "organization" and parent_id:
        raise SourceError("Top-level organization cannot have a source-inferred parent")
    return "org_" + digest([scope, kind, parent_id or "", label_key(name)])


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)


@dataclass
class Context:
    person_id: str
    scope: str
    source_sha: str
    source: dict
    company: str = ""
    department: str = ""
    candidates: dict[str, dict] = field(default_factory=dict)
    links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    company_id: str | None = None
    department_id: str | None = None

    def candidate(self, kind: str, name: str, parent_id: str | None = None) -> str:
        rid = organization_id(self.scope, kind, name, parent_id)
        self.candidates[rid] = {
            "record_id": rid, "scope": self.scope, "kind": kind, "name": name,
            "parent_id": parent_id, "identity_basis": "scoped_source_label"
        }
        return rid

    def link(self, source: str | None, predicate: str, target: str | None, evidence: str) -> bool:
        if not source or not target or source == target:
            return False
        key = (source, predicate, target)
        if key in self.links:
            return False
        self.links[key] = evidence
        return True


# -- Property Value Modification (working representation only, never source) --

@rule("ORG-MOD-001", MODIFICATION, ("Person.companyName",),
      "Normalize source organization label for exact scoped identity matching")
def normalize_company(ctx: Context) -> bool:
    original = ctx.source.get("companyName")
    ctx.company = display_label(original)
    if ctx.company != (original or ""):
        ctx.modifications.append({"field": "companyName", "original": original, "normalized": ctx.company})
        return True
    return False


@rule("ORG-MOD-002", MODIFICATION, ("Person.department",),
      "Normalize department label; do not infer division/branch/team from words")
def normalize_department(ctx: Context) -> bool:
    original = ctx.source.get("department")
    ctx.department = display_label(original)
    if ctx.department != (original or ""):
        ctx.modifications.append({"field": "department", "original": original, "normalized": ctx.department})
        return True
    return False


# -- Additional Property Assignment: derive Organization records and their IDs --

@rule("ORG-ADD-001", ADDITION, ("Person.companyName",),
      "Derive one Organization with a stable scoped label ID from explicit companyName")
def add_organization(ctx: Context) -> bool:
    if not ctx.company:
        return False
    ctx.company_id = ctx.candidate("organization", ctx.company)
    return True


@rule("ORG-ADD-002", ADDITION, ("Person.companyName", "Person.department"),
      "Derive a department Organization only within an identified parent scope")
def add_department(ctx: Context) -> bool:
    if not ctx.department:
        return False
    if not ctx.company_id:
        ctx.warnings.append("Department without usable companyName: no Organization created")
        return False
    if label_key(ctx.department) == label_key(ctx.company):
        ctx.warnings.append("Department equals organization label: self-hierarchy suppressed")
        return False
    ctx.department_id = ctx.candidate("department", ctx.department, ctx.company_id)
    return True


# -- Relationship Tagging: explicit Person evidence, no inferred cross-source tags --

@rule("ORG-REL-001", RELATIONSHIP, ("Person.companyName",),
      "Tag Person affiliated_with Organization from explicit companyName evidence")
def link_affiliation(ctx: Context) -> bool:
    return ctx.link(ctx.person_id, "affiliated_with", ctx.company_id, "Person.companyName")


@rule("ORG-REL-002", RELATIONSHIP, ("Person.companyName", "Person.department"),
      "Tag Person member_of uniquely scoped department Organization")
def link_membership(ctx: Context) -> bool:
    return ctx.link(ctx.person_id, "member_of", ctx.department_id, "Person.companyName+department")


@rule("ORG-REL-003", RELATIONSHIP, ("Person.companyName", "Person.department"),
      "Tag Organization part_of parent Organization for explicit department scope")
def link_hierarchy(ctx: Context) -> bool:
    return ctx.link(ctx.department_id, "part_of", ctx.company_id, "Person.companyName+department")


def evaluate(person_id: str, scope: str, source_sha: str, profile: dict) -> Context:
    if not person_id or not isinstance(scope, str) or not scope.strip() or not isinstance(profile, dict):
        raise SourceError("Person identity and normalized profile required")
    ctx = Context(person_id, scope, source_sha, profile)
    for group in (MODIFICATION, ADDITION, RELATIONSHIP):
        for spec in RULES:
            if spec.group != group:
                continue
            applied = spec.execute(ctx)
            ctx.outcomes.append({"rule_id": spec.rule_id, "result": "applied" if applied else "no_change"})
    return ctx


def person_fingerprint(scope: str, person_id: str, profile: dict) -> str:
    # Include only the two fields that affect Organization. Never reprocess a
    # profile because its job title, phone, email or unrelated fields changed.
    return digest({"version": VERSION, "scope": scope, "person_id": person_id,
                   "companyName": profile.get("companyName"), "department": profile.get("department")})



# Persistence and orchestration live exclusively in o3_core.engine.
