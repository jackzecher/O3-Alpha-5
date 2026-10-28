"""Read-only RDFLib projection and audit of authoritative O3 SQLite state.

No persistent RDF store, cache, SHACL dependency, or shadow property database.
The ontology file is the vocabulary authority; FIELDS is only the necessary
adapter between normalized Python record dictionaries and ontology predicates.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from .store import KINDS

O3 = Namespace('urn:o3:ontology:')
RECORD = 'urn:o3:record:'

# The same projection adapter from v0.1. Source JSON is preserved in the
# immutable archive. Large documents and structured content are not copied.
FIELDS = {
 'Email': {
   'mailboxId':('mailbox_id',), 'messageId':('message_id',),
   'internetMessageId':('internet_message_id',), 'conversationId':('conversation_id',),
   'emailSubject':('subject',), 'emailSourceFolder':('source_folder',),
   'emailImportance':('importance',), 'emailReceivedUtc':('received_at_utc',),
   'emailSentUtc':('sent_at_utc',), 'emailDirection':('~direction',),
   'emailBodyText':('~body_text',)
 },
 'Event': {
   'calendarScope':('@scope',),'eventUid':('uid',),'recurrenceKey':('~recurrence_key',),
   'eventSummary':('summary',),'eventDescription':('description',),
   'eventLocation':('location',),'eventStatus':('status',),'eventSequence':('sequence',),
   'eventAllDay':('~all_day',),'eventStartUtc':('~start_utc',),
   'eventEndUtc':('~end_utc',),'eventDurationSeconds':('~duration_seconds',),
   'recurrenceRole':('~recurrence_role',)
 },
 'Person': {
   'directoryId':('id',),'directoryScope':('@scope',),
   'personEmailAddress':('~addresses',),'personDisplayLabel':('~display_label',),
   'personJobTitle':('jobTitle',),'personOfficeLocation':('officeLocation',),
   'personBusinessPhone':('businessPhones',)
 },
 'Document': {
   'documentScope':('@scope',),'documentFilename':('source','filename'),
   'documentFileType':('source','file_type'), 'documentMimeType':('source','mime_type'),
   'documentSourceId':('source','source_id'), 'documentOriginType':('origin','type'),
   'documentTitle':('metadata','title'),'documentDisplayTitle':('~display_title',),
   'documentExtractionStatus':('extraction','status')
 },
 'Organization': {
   'organizationScope':('@scope',),'organizationName':('name',),
   'organizationKind':('kind',)
 },
 'LineOfEffort': {
   'loeNamespace':('@scope',),'loeName':('name',),
   'loeKind':('kind',),'loeReferenceCode':('reference_code',)
 }
}

SUPPORTED_RANGES = frozenset((XSD.string, XSD.boolean, XSD.integer, XSD.nonNegativeInteger, XSD.dateTime))
MAX_LITERAL_CHARS = 500000  # Existing conservative size bound on in-memory RDF text.


def record_uri(record_id: str) -> URIRef:
    return URIRef(RECORD + quote(record_id, safe=''))


@dataclass(frozen=True)
class PropertySpec:
    range: URIRef
    min_count: int
    max_count: int | None
    allowed: frozenset[str]


class Ontology:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.definition = Graph()
        self.definition.parse(self.path, format='turtle')

        # Materialize URI lookups ONCE, rather than searching the ontology for
        # every relationship generated from SQLite.
        self.allowed: dict[str, tuple[set[str], set[str]]] = {}
        self.predicates: dict[str, URIRef] = {}
        for pred in self.definition.subjects(RDF.type, OWL.ObjectProperty):
            labels = list(self.definition.objects(pred, O3.prototypePredicate))
            if len(labels) != 1 or not isinstance(labels[0], Literal):
                raise ValueError(f'Missing/duplicate prototype predicate: {pred}')
            key = str(labels[0])
            if key in self.predicates:
                raise ValueError('Duplicate ontology relationship key: ' + key)
            sources = self._kinds(pred, O3.allowedSource)
            targets = self._kinds(pred, O3.allowedTarget)
            if not sources or not targets:
                raise ValueError(f'Missing predicate source/target types: {pred}')
            self.allowed[key] = (sources, targets)
            self.predicates[key] = pred
        if len(self.allowed) != 14:
            raise ValueError('Expected 14 supported relationship predicates')

        for kind in KINDS:
            if (O3[kind], RDF.type, OWL.Class) not in self.definition:
                raise ValueError('Missing Record Class: ' + kind)
            if (O3[kind], RDFS.subClassOf, O3.Record) not in self.definition:
                raise ValueError('Record subclass declaration missing: ' + kind)
        if (O3.Record, RDF.type, OWL.Class) not in self.definition:
            raise ValueError('Missing common Record class')

        declared = {str(p).removeprefix(str(O3))
                    for p in self.definition.subjects(RDF.type, OWL.DatatypeProperty)}
        mapped = {name for definitions in FIELDS.values() for name in definitions} | {'identityBasis'}
        if declared != mapped:
            raise ValueError('Ontology/property projection mismatch: missing=' +
                             repr(sorted(mapped - declared)) + ' extra=' + repr(sorted(declared - mapped)))
        self.properties: dict[str, PropertySpec] = {}
        for kind, fields in FIELDS.items():
            for name in fields:
                self._load_property(name, kind)
        self._load_property('identityBasis', 'Record')

    def _kinds(self, subject: URIRef, predicate: URIRef) -> set[str]:
        values = set(self.definition.objects(subject, predicate))
        if not values.issubset({O3[kind] for kind in KINDS}):
            raise ValueError('Unknown class in relation constraint: ' + str(subject))
        return {kind for kind in KINDS if O3[kind] in values}

    def _load_property(self, name: str, domain: str) -> None:
        uri = O3[name]
        domains = list(self.definition.objects(uri, RDFS.domain))
        ranges = list(self.definition.objects(uri, RDFS.range))
        if domains != [O3[domain]] or len(ranges) != 1 or ranges[0] not in SUPPORTED_RANGES:
            raise ValueError('Missing/ambiguous domain or unsupported range: ' + name)
        def count(pred, default):
            vals = list(self.definition.objects(uri, pred))
            if len(vals) > 1:
                raise ValueError('Multiple cardinality values: ' + name)
            if not vals: return default
            try:
                number = int(vals[0])
            except (ValueError, TypeError) as exc:
                raise ValueError('Invalid cardinality: ' + name) from exc
            if number < 0 or str(vals[0]) != str(number):
                raise ValueError('Invalid cardinality: ' + name)
            return number
        lower = count(O3.minCount, 0)
        upper = count(O3.maxCount, None)
        if upper is not None and lower > upper:
            raise ValueError('Impossible cardinality: ' + name)
        permitted = frozenset(str(v) for v in self.definition.objects(uri, O3.allowedLiteral))
        if permitted and ranges[0] != XSD.string:
            raise ValueError('Non-string enumeration needs explicit parsing: ' + name)
        self.properties[name] = PropertySpec(ranges[0], lower, upper, permitted)

    @staticmethod
    def _value(row, props: dict, added: dict, path: tuple[str, ...]):
        if path[0] == '@scope':
            return row['scope']
        if path[0].startswith('~'):
            return added.get(path[0][1:])
        value = props
        for field in path:
            if not isinstance(value, dict):
                return None
            value = value.get(field)
        return value

    @staticmethod
    def _literal(value, expected: URIRef) -> Literal | None:
        # No coercion of source types into a different datatype, even if a
        # string looks like a number. Such cases are visible in the audit.
        if expected == XSD.boolean:
            return Literal(value, datatype=XSD.boolean) if type(value) is bool else None
        if expected in (XSD.integer, XSD.nonNegativeInteger):
            if type(value) is not int or (expected == XSD.nonNegativeInteger and value < 0):
                return None
            return Literal(value, datatype=expected)
        if not isinstance(value, str) or not value or len(value) > MAX_LITERAL_CHARS:
            return None
        if expected == XSD.string:
            return Literal(value)
        if expected == XSD.dateTime:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
            except ValueError:
                return None
            return Literal(parsed, datatype=XSD.dateTime)
        return None

    def _project_values(self, record, props: dict, added: dict, name: str, path: tuple[str, ...]):
        raw = self._value(record, props, added, path)
        if raw is None:
            return [], 0
        values = raw if isinstance(raw, list) else [raw]
        spec = self.properties[name]
        literals = []
        invalid = 0
        for value in values:
            lit = self._literal(value, spec.range)
            if lit is None or (spec.allowed and str(lit) not in spec.allowed):
                invalid += 1
            else:
                literals.append(lit)
        return literals, invalid

    def graph(self, store) -> Graph:
        """Create an independent snapshot; never persist or mutate RDF facts."""
        graph = Graph()
        for prefix, ns in self.definition.namespaces():
            graph.bind(prefix, ns)
        graph += self.definition
        for row in store.records():
            rid, kind = row['record_id'], row['record_type']
            if kind not in FIELDS:
                raise ValueError('Unknown record kind in store: ' + kind)
            node = record_uri(rid)
            graph.add((node, RDF.type, O3.Record))
            graph.add((node, RDF.type, O3[kind]))
            if row['identity_basis']:
                graph.add((node, O3.identityBasis, Literal(row['identity_basis'])))
            props, added = json.loads(row['properties_json']), json.loads(row['added_json'])
            for name, path in FIELDS[kind].items():
                literals, _ = self._project_values(row, props, added, name, path)
                for literal in literals:
                    graph.add((node, O3[name], literal))
        for source, predicate, target in store.active_relations():
            uri = self.predicates.get(predicate)
            if uri is None:
                raise RuntimeError('Predicate absent from ontology: ' + predicate)
            graph.add((record_uri(source), uri, record_uri(target)))
        return graph

    def audit(self, store) -> dict:
        """Check RDF projection versus SQLite and ontology declarations; read-only."""
        graph = self.graph(store)
        errors: list[str] = []
        active = store.records()
        ids = {record_uri(row['record_id']) for row in active}
        projected = set(graph.subjects(RDF.type, O3.Record))
        if ids != projected:
            errors.append(f'Active record identity mismatch: SQL={len(ids)} RDF={len(projected)}')
        data_count = 0
        projected_by_kind = {kind: 0 for kind in KINDS}
        for row in active:
            rid, kind = row['record_id'], row['record_type']
            node = record_uri(rid)
            projected_by_kind[kind] += 1
            classes = {cls for cls in graph.objects(node, RDF.type) if cls in {O3[k] for k in KINDS}}
            if classes != {O3[kind]}:
                errors.append(f'{rid}: wrong RDF record class')
            props, added = json.loads(row['properties_json']), json.loads(row['added_json'])
            for name, path in FIELDS[kind].items():
                spec = self.properties[name]
                expected, invalid = self._project_values(row, props, added, name, path)
                actual = set(graph.objects(node, O3[name]))
                # RDF triples are sets; identical array elements collapse naturally.
                if actual != set(expected):
                    errors.append(f'{rid}: {name}: projection disagrees with source property')
                if invalid:
                    errors.append(f'{rid}: {name}: {invalid} unprojectable or disallowed value(s)')
                if len(actual) < spec.min_count or (spec.max_count is not None and len(actual) > spec.max_count):
                    errors.append(f'{rid}: {name}: cardinality {len(actual)} outside {spec.min_count}..{spec.max_count}')
                data_count += len(actual)
            if row['identity_basis'] and len(set(graph.objects(node, O3.identityBasis))) != 1:
                errors.append(f'{rid}: identityBasis not projected')
            # No data property should appear on the wrong record class.
            permitted = set(FIELDS[kind]) | {'identityBasis'}
            for predicate in set(graph.predicates(node, None)):
                if (predicate, RDF.type, OWL.DatatypeProperty) in self.definition:
                    if str(predicate).removeprefix(str(O3)) not in permitted:
                        errors.append(f'{rid}: foreign data property {predicate}')
        sql_relations = set(store.active_relations())
        # URI escaping matters for unusual IDs; compare canonical URI tuples instead.
        sql_uris = {(record_uri(s), key, record_uri(t)) for s, key, t in sql_relations}
        rdf_uris = {(s, key, t) for key, pred in self.predicates.items()
                    for s, _, t in graph.triples((None, pred, None))}
        if sql_uris != rdf_uris:
            errors.append(f'Relationship projection mismatch: SQL={len(sql_uris)} RDF={len(rdf_uris)}')
        kinds_by_id = {row['record_id']:row['record_type'] for row in active}
        for src, relation, dst in sql_relations:
            classes = self.allowed.get(relation)
            if classes is None:
                errors.append(f'Unknown relation: {relation}')
            elif kinds_by_id.get(src) not in classes[0] or kinds_by_id.get(dst) not in classes[1]:
                errors.append(f'Invalid relation endpoints: {relation} {src} {dst}')
        return {
            'ok': not errors,
            'records': len(active),
            'records_by_type': projected_by_kind,
            'defined_record_types': len(KINDS),
            'defined_data_properties': len(self.properties),
            'defined_relationship_types': len(self.predicates),
            'projected_data_values': data_count + sum(bool(row['identity_basis']) for row in active),
            'projected_relationships': len(rdf_uris),
            'rdf_triples_total': len(graph),
            'errors': errors,
        }

    def export(self, store, path: Path) -> int:
        graph = self.graph(store)
        graph.serialize(destination=str(path), format='turtle')
        return len(graph)
