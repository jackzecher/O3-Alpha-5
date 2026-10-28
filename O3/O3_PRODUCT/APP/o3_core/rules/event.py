"""O3 Event Robot 0.1: deterministic iCalendar (VEVENT) rule registry.

The source ICS is read-only and archived unchanged. Python executes three
kinds of inspectable rules against parsed VEVENT components: relationship
tagging, property value modification, and additional property assignment.

No AI, human approval, email creation, recurrence expansion, or network access.
Python 3.12+, standard library only (RDFLib optional for export).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Callable
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.1.0"
RELATIONSHIP = "Relationship Tagging"
MODIFICATION = "Property Value Modification"
ADDITION = "Additional Property Assignment"
GROUPS = (RELATIONSHIP, MODIFICATION, ADDITION)
MAX_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 10000


class SourceError(ValueError):
    """Invalid or incomplete source; retain prior good state and retry later."""


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def stable_id(*parts: str) -> str:
    return "event_" + sha(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_address(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    address = value.strip()
    if address.lower().startswith("mailto:"):
        address = unquote(address[7:])
    if address.count("@") != 1 or any(c.isspace() for c in address):
        return None
    left, domain = address.rsplit("@", 1)
    if not left or not domain or domain.startswith(".") or domain.endswith(".") or "." not in domain:
        return None
    return address.casefold()


def unfold(raw: str) -> list[str]:
    """RFC 5545 continuation folds, without modifying source archive bytes."""
    result: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")):
            if not result:
                raise SourceError("Unexpected initial folded line")
            result[-1] += line[1:]
        elif line:
            result.append(line)
    return result


def split_unquoted(s: str, delimiter: str) -> list[str]:
    sections = []
    acc = []
    quoted = False
    for c in s:
        if c == '"':
            quoted = not quoted
        if c == delimiter and not quoted:
            sections.append("".join(acc))
            acc = []
        else:
            acc.append(c)
    if quoted:
        raise SourceError("Unclosed quoted property parameter")
    sections.append("".join(acc))
    return sections


@dataclass(frozen=True)
class IProp:
    name: str
    params: dict[str, str]
    raw: str


def parse_line(line: str) -> IProp:
    # Only property parameters are quoted. The value may freely contain quotes
    # and colons, so never parse the whole line with a quote-aware splitter.
    quoted = False
    separator = -1
    for i, c in enumerate(line):
        if c == '"':
            quoted = not quoted
        elif c == ":" and not quoted:
            separator = i
            break
    if separator < 0 or quoted:
        raise SourceError("Malformed iCalendar content line")
    lhs, raw = line[:separator], line[separator + 1:]
    parts = split_unquoted(lhs, ";")
    name = parts[0].upper()
    if not re.fullmatch(r"[A-Z0-9-]+", name):
        raise SourceError("Invalid iCalendar property name")
    params: dict[str, str] = {}
    for token in parts[1:]:
        k, sep, v = token.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9-]+", k):
            raise SourceError("Invalid iCalendar property parameter")
        k = k.upper()
        if k in params:
            raise SourceError("Duplicate iCalendar property parameter")
        params[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    return IProp(name, params, raw)


def unescape_text(value: str) -> str:
    """Unescape RFC 5545 TEXT syntax only, not arbitrary source content."""
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value)


@dataclass(frozen=True)
class Event:
    props: dict[str, list[IProp]]
    canonical_lines: tuple[str, ...]

    def one(self, field: str, required: bool = False) -> IProp | None:
        candidates = self.props.get(field, [])
        if len(candidates) > 1:
            raise SourceError(f"Duplicate singleton VEVENT property: {field}")
        if required and not candidates:
            raise SourceError(f"Missing VEVENT property: {field}")
        return candidates[0] if candidates else None

    def many(self, field: str) -> list[IProp]:
        return self.props.get(field, [])

    def fingerprint(self) -> str:
        return sha("\n".join(self.canonical_lines).encode("utf-8"))


def parse_calendar(raw: bytes) -> tuple[dict[str, str], list[Event]]:
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as ex:
        raise SourceError("ICS must be UTF-8 (invalid or incomplete source)") from ex
    stack: list[str] = []
    calendar: dict[str, str] = {}
    events: list[Event] = []
    current: dict[str, list[IProp]] | None = None
    event_lines: list[str] = []
    for line in unfold(decoded):
        p = parse_line(line)
        if p.name == "BEGIN":
            if calendar.get("_closed"):
                raise SourceError("Multiple VCALENDAR components are unsupported")
            component = p.raw.upper()
            if not stack and component != "VCALENDAR":
                raise SourceError("Expected VCALENDAR first")
            if stack and component == "VEVENT" and stack != ["VCALENDAR"]:
                raise SourceError("Nested VEVENT is unsupported")
            if component == "VEVENT":
                current, event_lines = {}, [line]
            elif current is not None:
                event_lines.append(line)
            stack.append(component)
        elif p.name == "END":
            if not stack or stack[-1] != p.raw.upper():
                raise SourceError("Unbalanced iCalendar component")
            if current is not None:
                event_lines.append(line)
                if p.raw.upper() == "VEVENT":
                    events.append(Event(current, tuple(event_lines)))
                    current = None
                    event_lines = []
                    if len(events) > MAX_EVENTS:
                        raise SourceError("Too many VEVENT records in one calendar")
            stack.pop()
            if not stack and p.raw.upper() == "VCALENDAR":
                # Data after END:VCALENDAR is not silently interpreted as part of the calendar.
                calendar["_closed"] = "yes"
        else:
            if not stack or calendar.get("_closed"):
                raise SourceError("Content outside the complete VCALENDAR")
            if stack == ["VCALENDAR"]:
                if p.name in calendar and p.name in {"VERSION", "METHOD"}:
                    raise SourceError(f"Duplicate calendar property: {p.name}")
                calendar[p.name] = p.raw
            if current is not None:
                event_lines.append(line)
                if stack == ["VCALENDAR", "VEVENT"]:
                    current.setdefault(p.name, []).append(p)
    if stack or calendar.pop("_closed", None) != "yes" or calendar.get("VERSION") != "2.0":
        raise SourceError("Expected complete VCALENDAR VERSION:2.0")
    if not events:
        # An empty calendar is valid, but it will mark prior VEVENTs in this source absent.
        pass
    return calendar, events


def temporal(prop: IProp, warnings: list[str] | None = None) -> dict:
    text = prop.raw.strip()
    date_only = prop.params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", text))
    if date_only:
        try:
            v = datetime.strptime(text, "%Y%m%d").date()
        except ValueError as ex:
            raise SourceError("Invalid iCalendar DATE") from ex
        return {"kind": "date", "local": v.isoformat(), "timezone": None, "utc": None}
    if not re.fullmatch(r"\d{8}T\d{6}Z?", text):
        raise SourceError("Invalid iCalendar DATE-TIME")
    is_utc = text.endswith("Z")
    try:
        naive = datetime.strptime(text.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError as ex:
        raise SourceError("Invalid iCalendar DATE-TIME value") from ex
    if is_utc:
        if prop.params.get("TZID"):
            raise SourceError("UTC DATE-TIME cannot also specify TZID")
        utc = naive.replace(tzinfo=timezone.utc)
        return {"kind": "datetime", "local": utc.isoformat(timespec="seconds"),
                "timezone": "UTC", "utc": utc.isoformat(timespec="seconds").replace("+00:00", "Z")}
    tzid = prop.params.get("TZID")
    if not tzid:
        return {"kind": "datetime", "local": naive.isoformat(timespec="seconds"),
                "timezone": "floating", "utc": None}
    result = {"kind": "datetime", "local": naive.isoformat(timespec="seconds"),
              "timezone": tzid, "utc": None}
    try:
        z = ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        if warnings is not None:
            warnings.append(f"TZID {tzid!r}: no trusted local timezone definition; UTC unassigned")
        return result
    # Ambiguous and nonexistent local times must not be converted speculatively.
    aware0 = naive.replace(tzinfo=z, fold=0)
    aware1 = naive.replace(tzinfo=z, fold=1)
    if aware0.utcoffset() != aware1.utcoffset():
        if warnings is not None:
            warnings.append(f"TZID {tzid!r}: ambiguous or invalid local time; UTC unassigned")
        return result
    actual = aware0.astimezone(timezone.utc).astimezone(z).replace(tzinfo=None)
    if actual != naive:
        if warnings is not None:
            warnings.append(f"TZID {tzid!r}: nonexistent local time; UTC unassigned")
        return result
    result["utc"] = aware0.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return result


def original_occurrence_key(e: Event) -> str:
    ref = e.one("RECURRENCE-ID")
    if ref is None:
        return "master"
    v = temporal(ref)
    if v["kind"] == "date":
        return "date:" + v["local"]
    if v["utc"]:
        return "utc:" + v["utc"]
    return "local:" + v["timezone"] + ":" + v["local"]


def identity(scope: str, event: Event) -> tuple[str, str, str]:
    uid = event.one("UID", required=True).raw.strip()
    if not uid:
        raise SourceError("Empty VEVENT UID")
    key = original_occurrence_key(event)
    return stable_id(scope, uid, key), uid, key


def duration_seconds(value: str) -> int:
    # RFC duration subset: weeks OR days/time. Reject negative/zero durations.
    m = re.fullmatch(r"([+]?)P(?:(\d+)W|(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", value)
    if not m or not any(m.group(i) for i in range(2, 7)):
        raise SourceError("Invalid or unsupported DURATION")
    total = (int(m[2] or 0) * 7 * 86400 + int(m[3] or 0) * 86400
             + int(m[4] or 0) * 3600 + int(m[5] or 0) * 60 + int(m[6] or 0))
    if total < 0:
        raise SourceError("Invalid negative DURATION")
    return total


from ..registry import new_registry
RULES, rule = new_registry(GROUPS)


@dataclass
class PersonIndex:
    """Read-only Person index adapter: records already identified by O3."""
    by_address: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_list(cls, items: object) -> "PersonIndex":
        if not isinstance(items, list):
            raise SourceError("Person index must be a list")
        mapping: dict[str, set[str]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("record_id"), str) or not item["record_id"].strip():
                raise SourceError("Person index entry requires record_id")
            addresses = item.get("addresses", [])
            if not isinstance(addresses, list):
                raise SourceError("Person index entry requires addresses list")
            for value in addresses:
                email = clean_address(value)
                if email:
                    mapping.setdefault(email, set()).add(item["record_id"].strip())
        return cls(mapping)

    def resolve(self, address: str | None) -> str | None:
        matches = self.by_address.get(clean_address(address) or "", set())
        return next(iter(matches)) if len(matches) == 1 else None


@dataclass
class Context:
    event: Event
    record_id: str
    scope: str
    uid: str
    occurrence_key: str
    people: PersonIndex
    master_by_uid: dict[str, str]
    normalized: dict = field(default_factory=dict)
    additions: dict = field(default_factory=dict)
    modifications: list[dict] = field(default_factory=list)
    links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)

    def put(self, name: str, old: object, new: object) -> bool:
        if old == new:
            return False
        self.modifications.append({"property": name, "source_value": old, "normalized_value": new})
        return True

    def link(self, predicate: str, target: str | None, evidence: str) -> bool:
        if not target or target == self.record_id:
            return False
        key = (self.record_id, predicate, target)
        if key in self.links:
            return False
        self.links[key] = evidence
        return True


# -------- Property Value Modification --------

@rule("EVT-MOD-001", MODIFICATION, ("UID", "RECURRENCE-ID", "RELATED-TO"),
      "Trim opaque identifier boundaries without changing identifier casing")
def normalize_identifiers(ctx: Context) -> bool:
    changed = False
    for name in ("UID", "RELATED-TO"):
        fields = ctx.event.many(name)
        values = [p.raw.strip() for p in fields]
        ctx.normalized[name.lower().replace("-", "_")] = values[0] if name == "UID" else values
        for i, p in enumerate(fields):
            changed |= ctx.put(name if name == "UID" else f"{name}[{i}]", p.raw, values[i])
    ref = ctx.event.one("RECURRENCE-ID")
    if ref:
        ctx.normalized["recurrence_id"] = temporal(ref, ctx.warnings)
    return changed


@rule("EVT-MOD-002", MODIFICATION, ("SUMMARY", "DESCRIPTION", "LOCATION"),
      "Unescape RFC 5545 textual fields without inventing content")
def normalize_text(ctx: Context) -> bool:
    changed = False
    for name in ("SUMMARY", "DESCRIPTION", "LOCATION"):
        p = ctx.event.one(name)
        if not p:
            continue
        value = unescape_text(p.raw)
        ctx.normalized[name.lower()] = value
        changed |= ctx.put(name, p.raw, value)
    return changed


@rule("EVT-MOD-003", MODIFICATION, ("DTSTART", "DTEND", "DURATION", "DTSTAMP", "LAST-MODIFIED"),
      "Parse event date, time, and duration without guessing missing timezones")
def normalize_temporal(ctx: Context) -> bool:
    changed = False
    for name in ("DTSTART", "DTEND", "DTSTAMP", "LAST-MODIFIED"):
        p = ctx.event.one(name)
        if p:
            parsed = temporal(p, ctx.warnings)
            ctx.normalized[name.lower().replace("-", "_")] = parsed
            changed = True  # A typed projection has been established.
    duration = ctx.event.one("DURATION")
    if duration:
        ctx.normalized["duration_seconds"] = duration_seconds(duration.raw)
        changed = True
    if ctx.event.one("DTEND") and duration:
        raise SourceError("VEVENT cannot supply DTEND and DURATION together")
    status = ctx.event.one("STATUS")
    if not ctx.event.one("DTSTART") and (status is None or status.raw.upper() != "CANCELLED"):
        raise SourceError("Non-cancelled VEVENT requires DTSTART")
    return changed


@rule("EVT-MOD-004", MODIFICATION, ("ORGANIZER", "ATTENDEE"),
      "Normalize valid mailto participant addresses for exact identity matching")
def normalize_people(ctx: Context) -> bool:
    changed = False
    for name in ("ORGANIZER", "ATTENDEE"):
        fields = ctx.event.many(name)
        if name == "ORGANIZER" and len(fields) > 1:
            raise SourceError("VEVENT has multiple ORGANIZER values")
        records = []
        for i, p in enumerate(fields):
            normalized = clean_address(p.raw)
            if normalized is None and p.raw.strip():
                ctx.warnings.append(f"{name}[{i}]: not a resolvable email address")
            records.append({"address": normalized, "raw": p.raw, "params": p.params})
            changed |= ctx.put(f"{name}[{i}]", p.raw, normalized) if normalized else False
        ctx.normalized[name.lower()] = (records[0] if records else None) if name == "ORGANIZER" else records
    return changed


@rule("EVT-MOD-005", MODIFICATION, ("STATUS", "SEQUENCE", "TRANSP", "RRULE"),
      "Normalize recognized lifecycle tokens and integer sequence; preserve unknowns")
def normalize_status(ctx: Context) -> bool:
    changed = False
    for name, allowed in (("STATUS", {"TENTATIVE", "CONFIRMED", "CANCELLED"}),
                          ("TRANSP", {"OPAQUE", "TRANSPARENT"})):
        p = ctx.event.one(name)
        if p:
            value = p.raw.strip().upper()
            ctx.normalized[name.lower()] = value if value in allowed else p.raw
            if value not in allowed:
                ctx.warnings.append(f"{name}: unrecognized token, original preserved")
            changed |= ctx.put(name, p.raw, ctx.normalized[name.lower()])
    seq = ctx.event.one("SEQUENCE")
    if seq:
        if not seq.raw.strip().isdigit():
            raise SourceError("Invalid VEVENT SEQUENCE")
        ctx.normalized["sequence"] = int(seq.raw.strip())
        changed = True
    recurrence = ctx.event.one("RRULE")
    if recurrence:
        ctx.normalized["rrule"] = recurrence.raw.strip()
    # Preserve RDATE/EXDATE as source-style text, without recurrence expansion.
    for name in ("RDATE", "EXDATE"):
        ctx.normalized[name.lower()] = [{"value": p.raw, "params": p.params}
                                        for p in ctx.event.many(name)]
    return changed


# -------- Additional Property Assignment --------

@rule("EVT-ADD-001", ADDITION, ("UID", "RECURRENCE-ID"),
      "Assign a permanent O3 Event ID from calendar scope and source event identity")
def add_id(ctx: Context) -> bool:
    ctx.additions["record_id"] = ctx.record_id
    return True


@rule("EVT-ADD-002", ADDITION, ("DTSTART", "DTEND", "DURATION"),
      "Derive all-day, UTC date bounds, and duration only when determinable")
def add_temporal_projection(ctx: Context) -> bool:
    start = ctx.normalized.get("dtstart")
    if start is None:
        return False
    ctx.additions["all_day"] = start["kind"] == "date"
    if start["utc"]:
        ctx.additions["start_utc"] = start["utc"]
    end = ctx.normalized.get("dtend")
    if end is not None and start["kind"] != end["kind"]:
        raise SourceError("DTSTART and DTEND use incompatible value types")
    seconds = ctx.normalized.get("duration_seconds")
    if seconds is None and end is not None:
        if start["kind"] == "date":
            seconds = int((date.fromisoformat(end["local"]) - date.fromisoformat(start["local"])).total_seconds())
        elif start["utc"] and end["utc"]:
            seconds = int((datetime.fromisoformat(end["utc"].replace("Z", "+00:00"))
                           - datetime.fromisoformat(start["utc"].replace("Z", "+00:00"))).total_seconds())
        elif start["timezone"] == "floating" and end["timezone"] == "floating":
            seconds = int((datetime.fromisoformat(end["local"])
                           - datetime.fromisoformat(start["local"])).total_seconds())
    if seconds is not None:
        if seconds < 0:
            raise SourceError("VEVENT ends before its start")
        ctx.additions["duration_seconds"] = seconds
    elif end is None:
        # All-day events with no DTEND last one day per RFC 5545.
        ctx.additions["duration_seconds"] = 86400 if start["kind"] == "date" else 0
    if end and end["utc"]:
        ctx.additions["end_utc"] = end["utc"]
    elif end is None and start["utc"] and seconds is not None:
        utc = datetime.fromisoformat(start["utc"].replace("Z", "+00:00")) + timedelta(seconds=seconds)
        ctx.additions["end_utc"] = utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    return True


@rule("EVT-ADD-003", ADDITION, ("RRULE", "RDATE", "RECURRENCE-ID"),
      "Identify single, recurring-series master, or detached exception")
def add_recurrence_role(ctx: Context) -> bool:
    if ctx.event.one("RECURRENCE-ID"):
        role = "Exception"
    elif ctx.event.one("RRULE") or ctx.event.many("RDATE"):
        role = "Series"
    else:
        role = "Single"
    ctx.additions["recurrence_role"] = role
    return True


# -------- Relationship Tagging --------

@rule("EVT-REL-001", RELATIONSHIP, ("ORGANIZER",),
      "Link organizer only when its email address uniquely identifies a Person")
def organizer(ctx: Context) -> bool:
    part = ctx.normalized.get("organizer")
    return ctx.link("organized_by", ctx.people.resolve(part.get("address")), "ORGANIZER") if part else False


@rule("EVT-REL-002", RELATIONSHIP, ("ATTENDEE",),
      "Link invitees to uniquely resolved Persons; never infer actual attendance")
def invitees(ctx: Context) -> bool:
    result = False
    for i, part in enumerate(ctx.normalized.get("attendee", [])):
        if ctx.link("invites", ctx.people.resolve(part.get("address")), f"ATTENDEE[{i}]"):
            result = True
    return result


@rule("EVT-REL-003", RELATIONSHIP, ("RELATED-TO",),
      "Link explicitly related calendar Events only to existing same-calendar master UIDs")
def related_to(ctx: Context) -> bool:
    result = False
    for i, uid in enumerate(ctx.normalized.get("related_to", [])):
        target = ctx.master_by_uid.get(uid)
        if ctx.link("related_to", target, f"RELATED-TO[{i}]"):
            result = True
    return result


@rule("EVT-REL-004", RELATIONSHIP, ("UID", "RECURRENCE-ID"),
      "Connect a detached exception to its existing series master; no synthetic occurrence")
def exception_of(ctx: Context) -> bool:
    if ctx.occurrence_key == "master":
        return False
    return ctx.link("exception_of", ctx.master_by_uid.get(ctx.uid), "RECURRENCE-ID")


def evaluate(event: Event, scope: str, people: PersonIndex, master_by_uid: dict[str, str]) -> Context:
    rid, uid, rec_key = identity(scope, event)
    ctx = Context(event, rid, scope, uid, rec_key, people, master_by_uid)
    for group in (MODIFICATION, ADDITION, RELATIONSHIP):
        for entry in RULES:
            if entry.group != group:
                continue
            try:
                applied = bool(entry.execute(ctx))
            except (ValueError, TypeError, KeyError) as ex:
                raise SourceError(f"{entry.rule_id}: {ex}") from ex
            ctx.outcomes.append({"id": entry.rule_id, "result": "applied" if applied else "no_change"})
    return ctx



# Persistence and orchestration live exclusively in o3_core.engine.
