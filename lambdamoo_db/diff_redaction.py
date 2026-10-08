"""Declarative v1 redaction, applied before IDs, sorting, payloads and indexing.

Grammar: {"schema_version": 1, "rules": [{"selector": SELECTOR,
"action": "conceal"}]}. SELECTOR is {"section": SECTION}, or one of:
 properties: section, definer (decimal ID), name (exact), occurrence (N or '*');
 verbs: section, object (decimal ID), name (exact complete names), occurrence;
 heap: section, waif_class (decimal ID), slot (zero-based integer);
 objects: section, object (decimal ID), field ('name' or 'aliases').
Only a section-only runtime selector accepts action 'reveal_runtime'.
Occurrence defaults to '*'. Rule errors never echo selector values.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from collections import Counter

import attrs

from .database import Anon, MooDatabase, MooObject, ObjNum, Waif, WaifReference
from .diff_types import Comparison, SECTIONS, ReportOptions, canonical_json, event_sort_key, finalize_counts
from .inspection import _ASCII_LOWER, verb_signature


REDACTED = {'type': 'redacted'}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('invalid numeric selector')
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise ValueError('invalid numeric selector')
    number = int(value)
    if number < 0:
        raise ValueError('invalid numeric selector')
    return str(number)


def _ref(value):
    if isinstance(value, WaifReference):
        return 'waif', value.index
    if isinstance(value, Anon) and int(value) != -1:
        return 'anonymous', int(value)
    return None


def _references(value):
    ref = _ref(value)
    if ref:
        yield ref
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _references(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _references(key)
            yield from _references(child)
    elif attrs.has(type(value)) and not isinstance(value, ObjNum):
        for field in attrs.fields(type(value)):
            if hasattr(value, field.name) and field.name != 'code':
                yield from _references(getattr(value, field.name))


def _encoded_ref(value):
    tag = value.get('type') if isinstance(value, dict) else None
    if tag in ('waif', 'anon') and 'value' in value:
        return ('waif' if tag == 'waif' else 'anonymous', int(value['value']))
    return None


class RedactionPolicy:
    def __init__(self, rules):
        self.rules = rules
        self.digest = hashlib.sha256(canonical_json({'schema_version': 1, 'rules': rules})).hexdigest()
        self.bound = False

    @property
    def match_counts(self):
        return list(self.matches) if self.bound else [0 for _ in self.rules]

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict) or set(data) != {'schema_version', 'rules'} or type(data['schema_version']) is not int or data['schema_version'] != 1 or not isinstance(data['rules'], list):
            raise ValueError('invalid redaction document')
        rules = []
        for number, rule in enumerate(data['rules'], 1):
            try:
                if not isinstance(rule, dict) or set(rule) != {'selector', 'action'} or not isinstance(rule['selector'], dict):
                    raise ValueError()
                selector = dict(rule['selector'])
                section = selector.get('section')
                if section not in SECTIONS or rule['action'] not in ('conceal', 'reveal_runtime'):
                    raise ValueError()
                if rule['action'] == 'reveal_runtime' and selector != {'section': 'runtime'}:
                    raise ValueError()
                if set(selector) != {'section'}:
                    if section in ('properties', 'verbs'):
                        identity = 'definer' if section == 'properties' else 'object'
                        if set(selector) not in ({'section', identity, 'name'}, {'section', identity, 'name', 'occurrence'}):
                            raise ValueError()
                        selector[identity] = _number(selector[identity])
                        if not isinstance(selector['name'], str):
                            raise ValueError()
                        occurrence = selector.get('occurrence', '*')
                        if occurrence != '*' and (type(occurrence) is not int or occurrence < 0):
                            raise ValueError()
                        selector['occurrence'] = occurrence
                    elif section == 'heap':
                        if set(selector) != {'section', 'waif_class', 'slot'} or type(selector['slot']) is not int or selector['slot'] < 0:
                            raise ValueError()
                        selector['waif_class'] = _number(selector['waif_class'])
                    elif section == 'objects':
                        if set(selector) != {'section', 'object', 'field'} or selector['field'] not in ('name', 'aliases'):
                            raise ValueError()
                        selector['object'] = _number(selector['object'])
                    else:
                        raise ValueError()
                rules.append({'selector': selector, 'action': rule['action']})
            except (ValueError, TypeError, KeyError):
                raise ValueError(f'invalid redaction rule {number}') from None
        return cls(rules)

    @classmethod
    def from_file(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            raise ValueError('cannot read redaction policy document') from None
        return cls.from_data(data)

    def bind(self, old_db, new_db, options=ReportOptions()):
        if options.stop_after is not None:
            raise ValueError('stop_after cannot be combined with redaction')
        self.options = options
        self.matches = [0 for _ in self.rules]
        self.paths = {'old': {}, 'new': {}}
        self.path_roots = {'old': {}, 'new': {}}
        self.uncertain_properties = {'old': {}, 'new': {}}
        self.anonymous_property_ids = {'old': set(), 'new': set()}
        self.anonymous_verb_ids = {'old': set(), 'new': set()}
        self.whole_property_ids = {'old': set(), 'new': set()}
        self.whole_verb_ids = {'old': set(), 'new': set()}
        self.verb_slots = {'old': {}, 'new': {}}
        self.verb_roots = {'old': {}, 'new': {}}
        self.waif_slots = {'old': {}, 'new': {}}
        self.waif_record_rules = {'old': {}, 'new': {}}
        self.protected = {'old': set(), 'new': set()}
        self.label_ids = set()
        self.protected_root_rules = {'old': {}, 'new': {}}
        self.databases = {'old': old_db, 'new': new_db}
        self.section_rules = {}
        reveal = any(r['action'] == 'reveal_runtime' for r in self.rules)
        conceal_runtime = any(r['action'] == 'conceal' and r['selector'] == {'section': 'runtime'} for r in self.rules)
        self.runtime_concealed = conceal_runtime or not reveal
        for index, rule in enumerate(self.rules):
            if rule['action'] == 'conceal' and set(rule['selector']) == {'section'}:
                self.section_rules.setdefault(rule['selector']['section'], index)
        for side, database in self.databases.items():
            selected = [o for n, o in sorted(database.objects.items()) if not o.anon and
                        (options.object_ids is None or n in options.object_ids)]
            roots = [p.value for o in selected for p in o.properties]
            if options.view == 'checkpoint':
                if 'runtime' in options.sections:
                    # Runtime evidence can own heap graphs even when no
                    # permanent property references their nodes.
                    from .compare import FIELD_COVERAGE
                    roots.extend(getattr(database, field, None) for field, owner in FIELD_COVERAGE[MooDatabase].items() if owner == 'runtime')
                if 'heap' in options.sections and options.object_ids is None:
                    roots.extend(WaifReference(n) for n in database.waifs)
                    roots.extend(Anon(n) for n, o in database.objects.items() if o.anon)
            reached = self._closure(roots, database)
            anonymous = [database.objects[n] for kind, n in sorted(reached) if kind == 'anonymous'] if 'heap' in options.sections else []
            waifs = [(n, database.waifs[n]) for kind, n in sorted(reached) if kind == 'waif'] if 'heap' in options.sections else []
            property_holders = (selected if 'properties' in options.sections else []) + anonymous
            verb_holders = (selected if 'verbs' in options.sections else []) + anonymous
            waif_definitions = {}
            for index, rule in enumerate(self.rules):
                selector = rule['selector']; section = selector['section']
                label_selector = section == 'objects' and (set(selector) == {'section'} or selector.get('field') in ('name', 'aliases'))
                heap_field_selector = section == 'properties' and bool(anonymous or waifs) or section == 'verbs' and bool(anonymous)
                if section not in options.sections and not label_selector and not heap_field_selector:
                    continue
                if set(selector) == {'section'}:
                    # Section policies publish Boolean presence per side,
                    # never hidden membership or graph cardinality. Global
                    # containers exist even when the selected contents are empty.
                    count = int(section in options.sections)
                    if section == 'objects' and section not in options.sections:
                        count = int(bool(selected))  # visible label annotations
                    if section == 'properties':
                        count = int(bool(property_holders or waifs))
                    elif section == 'verbs':
                        count = int(bool(verb_holders))
                    self.matches[index] += count
                    if rule['action'] == 'conceal' and section == 'objects':
                        self.label_ids.update(str(o.id) for o in selected)
                    if rule['action'] == 'conceal' and section == 'properties':
                        for o in property_holders:
                            self.whole_property_ids[side].add(str(o.id))
                            if o.anon:
                                self.anonymous_property_ids[side].add(str(o.id))
                            for slot, prop in enumerate(o.properties):
                                self.paths[side].setdefault((str(o.id), slot), index)
                                self.path_roots[side].setdefault((str(o.id), slot), (index, 'section', 'properties'))
                                self._protect(prop.value, side, index)
                        for number, waif in waifs:
                            for slot, value in waif.props:
                                self._bind_waif_slot(side, number, slot, value, index)
                    if rule['action'] == 'conceal' and section == 'verbs':
                        for o in verb_holders:
                            self.whole_verb_ids[side].add(str(o.id))
                            if o.anon:
                                self.anonymous_verb_ids[side].add(str(o.id))
                            for slot, _ in enumerate(o.verbs):
                                self.verb_slots[side].setdefault((str(o.id), slot), index)
                                self.verb_roots[side].setdefault((str(o.id), slot), (index, 'section', 'verbs'))
                    if rule['action'] == 'conceal' and section == 'heap':
                        # Reached is already a transitive closure. Rewalking
                        # every node's descendants would multiply shared DAGs.
                        self.protected[side].update(reached)
                        for ref in reached:
                            self.protected_root_rules[side].setdefault(ref, index)
                    continue
                if section == 'properties':
                    for o in property_holders:
                        definitions = self._definitions(database, o)
                        valid_layout = self._layout_valid(database, o, definitions)
                        for slot, (definer, name, occurrence) in enumerate(definitions):
                            if str(definer) == selector['definer'] and name == selector['name'] and (selector['occurrence'] == '*' or occurrence == selector['occurrence']):
                                if o.anon and o.properties:
                                    self.anonymous_property_ids[side].add(str(o.id))
                                if not valid_layout:
                                    # Provenance cannot identify one stored
                                    # slot. Conceal the containing root and all
                                    # its heap dependencies instead of guessing.
                                    if o.properties:
                                        self.matches[index] += 1
                                        root = (index, 'uncertain-properties', str(o.id))
                                        self.uncertain_properties[side].setdefault(str(o.id), root)
                                        for actual_slot, prop in enumerate(o.properties):
                                            self.paths[side].setdefault((str(o.id), actual_slot), index)
                                            self.path_roots[side].setdefault((str(o.id), actual_slot), root)
                                            self._protect(prop.value, side, index)
                                    break
                                self.matches[index] += 1
                                self.paths[side].setdefault((str(o.id), slot), index)
                                self.path_roots[side].setdefault((str(o.id), slot), (index, 'property', str(o.id), occurrence))
                                if slot < len(o.properties):
                                    self._protect(o.properties[slot].value, side, index)
                    if selector['name'].startswith(':'):
                        for number, waif in waifs:
                            class_id = int(waif.waif_class)
                            if class_id not in waif_definitions:
                                cls = database.objects.get(class_id)
                                if cls is None or cls.anon:
                                    raise ValueError('waif property provenance unavailable during redaction binding')
                                definitions = self._definitions(database, cls)
                                if not self._layout_valid(database, cls, definitions):
                                    raise ValueError('waif property provenance unavailable during redaction binding')
                                # gen_waif_propdefs compacts colon-prefixed own
                                # definitions, class first then its ancestors.
                                # Occurrences retain the exact definer/name key.
                                waif_definitions[class_id] = [definition for definition in definitions if definition[1].startswith(':')]
                            definitions = waif_definitions[class_id]
                            for slot, value in waif.props:
                                if not 0 <= slot < len(definitions):
                                    continue
                                definer, name, occurrence = definitions[slot]
                                if str(definer) == selector['definer'] and name == selector['name'] and (selector['occurrence'] == '*' or occurrence == selector['occurrence']):
                                    self.matches[index] += 1
                                    self._bind_waif_slot(side, number, slot, value, index)
                elif section == 'verbs':
                    for o in selected:
                        if str(o.id) != selector['object']:
                            continue
                        seen = Counter()
                        for slot, verb in enumerate(o.verbs):
                            signature = verb_signature(verb)
                            occurrence = seen[signature]; seen[signature] += 1
                            if verb.name == selector['name'] and (selector['occurrence'] == '*' or occurrence == selector['occurrence']):
                                self.matches[index] += 1
                                self.verb_slots[side].setdefault((str(o.id), slot), index)
                                self.verb_roots[side].setdefault((str(o.id), slot), (index, 'verb', str(o.id), *signature[1:], occurrence))
                elif section == 'heap':
                    candidates = set(database.waifs) if options.view == 'checkpoint' and options.object_ids is None else {n for kind, n in reached if kind == 'waif'}
                    for number in sorted(candidates):
                        waif = database.waifs.get(number)
                        if waif is None:
                            raise ValueError('dangling heap reference during redaction binding')
                        if str(int(waif.waif_class)) != selector['waif_class']:
                            continue
                        for slot, value in waif.props:
                            if slot == selector['slot']:
                                self.matches[index] += 1
                                self._bind_waif_slot(side, number, slot, value, index)
                elif section == 'objects':
                    for o in selected:
                        if str(o.id) != selector['object']:
                            continue
                        if selector['field'] == 'name':
                            self.matches[index] += 1
                            self.label_ids.add(str(o.id))
                        else:
                            # Object aliases include its aliases slot and #0's
                            # stored dollar names pointing at this numeric ID.
                            for holder in selected:
                                for slot, prop in enumerate(holder.properties):
                                    own_aliases = holder.id == o.id and isinstance(prop.propertyName, str) and prop.propertyName.translate(_ASCII_LOWER) == 'aliases'
                                    if own_aliases or holder.id == 0 and isinstance(prop.value, ObjNum) and int(prop.value) == o.id:
                                        self.matches[index] += 1
                                        self.paths[side].setdefault((str(holder.id), slot), index)
                                        self.path_roots[side].setdefault((str(holder.id), slot), (index, 'aliases', str(o.id)))
                                        self._protect(prop.value, side, index)
                            self.label_ids.add(str(o.id))
        self.bound = True
        return self

    def _bind_waif_slot(self, side, number, slot, value, rule):
        self.waif_slots[side].setdefault((number, slot), rule)
        self.waif_record_rules[side].setdefault(number, rule)
        self._protect(value, side, rule)

    @staticmethod
    def _definitions(database, obj):
        definitions = []
        for ancestor in database.ancestors(obj):
            seen = Counter()
            for prop in ancestor.properties[:ancestor.propdefs_count]:
                name = prop.propertyName
                counter_key = name if isinstance(name, str) else repr(name)
                definitions.append((ancestor.id, name, seen[counter_key]))
                seen[counter_key] += 1
        return definitions

    @staticmethod
    def _layout_valid(database, obj, definitions):
        if len(definitions) != len(obj.properties):
            return False
        identities = [(definer, name, occurrence) for definer, name, occurrence in definitions if isinstance(name, str)]
        if len(identities) != len(definitions) or len(set(identities)) != len(identities):
            return False
        return all(0 <= ancestor.propdefs_count <= len(ancestor.properties) and
                   all(int(parent) == -1 or int(parent) in database.objects for parent in ancestor.parents)
                   for ancestor in database.ancestors(obj))

    @staticmethod
    def _closure(values, database):
        reached = set()
        pending = [ref for value in values for ref in _references(value)]
        while pending:
            ref = pending.pop()
            if ref in reached:
                continue
            reached.add(ref)
            node = database.waifs.get(ref[1]) if ref[0] == 'waif' else database.objects.get(ref[1])
            if node is None or ref[0] == 'anonymous' and not node.anon:
                raise ValueError('dangling heap reference during redaction binding')
            pending.extend(_references(node))
        return reached

    def _protect(self, value, side, rule):
        for ref in self._closure([value], self.databases[side]):
            self.protected[side].add(ref)
            self.protected_root_rules[side].setdefault(ref, rule)

    def _protect_ref(self, ref, side, rule):
        value = WaifReference(ref[1]) if ref[0] == 'waif' else Anon(ref[1])
        self._protect(value, side, rule)

    def _object_number(self, entity, side):
        if entity['type'] == 'permanent' or entity['type'] == 'anonymous' and self.options.view == 'checkpoint':
            return entity['id']
        if entity['type'] == 'anonymous':
            return self.anonymous_numbers[side].get(entity['id'])
        return None

    def _path_root(self, event, side):
        entity = event['entity']; path = event.get(f'{side}_path', [])
        if entity['type'] in ('permanent', 'anonymous'):
            number = self._object_number(entity, side)
            if entity['type'] == 'anonymous' and number in self.anonymous_property_ids[side] and 'properties' in self.section_rules and path and path[0].get('type') == 'field' and path[0].get('name') in ('properties', 'property_layout', 'propdefs_count'):
                return (self.section_rules['properties'], 'section', 'properties'), 1
            if entity['type'] == 'anonymous' and number in self.anonymous_verb_ids[side] and 'verbs' in self.section_rules and path and path[0].get('type') == 'field' and path[0].get('name') in ('verbs', 'verb_order'):
                return (self.section_rules['verbs'], 'section', 'verbs'), 1
            uncertain = self.uncertain_properties[side].get(number)
            if uncertain and path and path[0].get('type') in ('property', 'field') and (
                    path[0].get('type') == 'property' or path[0].get('name') in ('properties', 'property_layout', 'propdefs_count')):
                return uncertain, 1
            for position, step in enumerate(path):
                if step['type'] == 'property':
                    slot = int(step['slot'])
                    rule = self.paths[side].get((number, slot))
                    if rule is not None:
                        return self.path_roots[side][(number, slot)], position + 1
                if step['type'] == 'verb':
                    slot = int(step['slot'])
                    rule = self.verb_slots[side].get((number, slot))
                    protects_section = rule is not None and set(self.rules[rule]['selector']) == {'section'}
                    is_source = position + 1 < len(path) and path[position + 1] == {'type': 'field', 'name': 'code'}
                    if rule is not None and (protects_section or is_source):
                        return self.verb_roots[side][(number, slot)], position + 1
                if position + 1 < len(path) and step == {'type': 'field', 'name': 'properties'} and path[position + 1]['type'] == 'index':
                    slot = int(path[position + 1]['index'])
                    rule = self.paths[side].get((number, slot))
                    if rule is not None:
                        return self.path_roots[side][(number, slot)], position + 2
            if number in self.label_ids and path and path[0] == {'type': 'field', 'name': 'name'}:
                rule = next((i for i, r in enumerate(self.rules) if r['selector'].get('object') == number and r['selector'].get('field') == 'name'), self.section_rules.get('objects', 0))
                return (rule, 'name', number), 1
        if entity['type'] in ('waif', 'anonymous'):
            for position, step in enumerate(path):
                if step['type'] == 'heap' and step.get('kind') == 'waif' and 'raw_id' in step:
                    rule = self.waif_slots[side].get((int(step['raw_id']), int(step.get('slot', -1))))
                    if rule is not None:
                        if self.rules[rule]['selector'] == {'section': 'properties'}:
                            return (rule, 'section', 'properties'), position + 1
                        return (rule, 'waif-slot', entity['id'], int(step['slot'])), position + 1
        return None

    def _event_root(self, event):
        section = event['section']
        if event['entity']['type'] == 'global':
            for side in ('old', 'new'):
                path = event.get(f'{side}_path', [])
                if len(path) == 1 and path[0].get('type') == 'field':
                    counter = path[0].get('name')
                    candidates = []
                    associated = {'total_players': ('players',), 'total_objects': ('objects', 'recycled'),
                                  'total_verbs': ('verbs', 'heap')}.get(counter, ())
                    candidates.extend(self.section_rules[owner] for owner in associated if owner in self.section_rules)
                    if counter == 'total_verbs':
                        for evidence_side in ('old', 'new'):
                            candidates.extend(rule for ref, rule in self.protected_root_rules[evidence_side].items()
                                              if ref[0] == 'anonymous')
                            candidates.extend(self.verb_slots[evidence_side].values())
                    if candidates:
                        # Declared aggregate counters can expose concealed
                        # membership or anonymous programs through serialization.
                        # Join their owning rule before IDs and payloads exist.
                        rule = min(candidates)
                        if set(self.rules[rule]['selector']) == {'section'}:
                            return (rule, 'section', self.rules[rule]['selector']['section'])
                        return (rule, 'derived-counter', counter)
        if section == 'runtime' and self.runtime_concealed:
            return (-1, 'section', 'runtime')
        if section in self.section_rules:
            return (self.section_rules[section], 'section', section)
        if 'recycled' in self.section_rules and 'recycled' in event.get('sections_applicable', ()):
            # Numeric lifecycle events belong to objects and recycled slots.
            # Their container marker must not enumerate concealed membership.
            return (self.section_rules['recycled'], 'section', 'recycled')
        roots = [found[0] for side in ('old', 'new') if (found := self._path_root(event, side)) is not None]
        if roots:
            # OLD/NEW slot insertion must still coalesce under one semantic
            # root: rule and permanent entity identify the protected selector.
            root = min(roots, key=repr)
            return root
        if self.options.view == 'checkpoint' and event['entity']['type'] in ('waif', 'anonymous'):
            ref = (event['entity']['type'], int(event['entity']['id']))
            for side in ('old', 'new'):
                if ref in self.protected[side]:
                    return (self.protected_root_rules[side][ref], 'shared-heap', ref[0])
        for side in ('old', 'new'):
            refs = set()
            self._evidence_refs(event.get(side), refs)
            self._evidence_refs(event.get(f'{side}_path', []), refs)
            for anchor in event.get('details', {}).get('anchors', []) + event.get('details', {}).get('competing_paths', []):
                synthetic = {'entity': anchor.get('entity', {}), f'{side}_path': anchor.get(f'{side}_path', [])}
                root = self._path_root(synthetic, side)
                if root:
                    return root[0]
            protected = refs & self.protected[side]
            if protected:
                rule = min(self.protected_root_rules[side][ref] for ref in protected)
                entity = event['entity']
                return (rule, 'shared-heap', entity['type'])
            # A replacement/uncertainty event may contain the concealed sparse
            # slot only in its finite graph payload, without a leaf heap path.
            # Coalesce that containing root so child counts and graph sizes do
            # not survive through its companion diagnostic event.
            slot_rules = set()
            self._evidence_slot_rules(event.get(side), side, slot_rules)
            if self.options.view == 'checkpoint' and event['entity']['type'] == 'waif' and not event.get(f'{side}_path'):
                # Added/removed raw records have no graph-node wrapper carrying
                # the local number. Their entity supplies the same context.
                number = int(event['entity']['id'])
                rule = self.waif_record_rules[side].get(number)
                if rule is not None:
                    slot_rules.add(rule)
            if slot_rules:
                entity = event['entity']
                return (min(slot_rules), 'waif-containing-root', entity['type'], entity['id'])
            if event['entity']['type'] in ('waif', 'anonymous'):
                for row in self.correspondence:
                    if row.get('id') == event['entity']['id']:
                        key = (row['kind'], int(row[f'{side}_id']))
                        if key in self.protected[side]:
                            return (self.protected_root_rules[side][key], 'shared-heap', row['kind'])
        return None

    def _evidence_slot_rules(self, value, side, result):
        if isinstance(value, dict):
            if value.get('type') == 'graph':
                for node in value.get('nodes', []):
                    if node['kind'] == 'waif':
                        number = int(node['id'])
                        rule = self.waif_record_rules[side].get(number)
                        if rule is not None:
                            result.add(rule)
            for child in value.values():
                self._evidence_slot_rules(child, side, result)
        elif isinstance(value, list):
            for child in value:
                self._evidence_slot_rules(child, side, result)

    @staticmethod
    def _evidence_refs(value, result):
        if isinstance(value, dict):
            ref = _encoded_ref(value)
            if ref:
                result.add(ref)
            for child in value.values():
                RedactionPolicy._evidence_refs(child, result)
        elif isinstance(value, list):
            for child in value:
                RedactionPolicy._evidence_refs(child, result)

    def _sanitize_value(self, value, side, context=None):
        if not isinstance(value, dict):
            return deepcopy(value)
        ref = _encoded_ref(value)
        if ref in self.protected[side]:
            return dict(REDACTED)
        if value.get('type') == 'list' and context and context[0] == 'property-list':
            return {'type': 'list', 'items': [dict(REDACTED) if (context[1], slot) in self.paths[side]
                                            else self._sanitize_value(item, side) for slot, item in enumerate(value['items'])]}
        if value.get('type') == 'graph':
            nodes = []
            for node in value['nodes']:
                ref = (node['kind'], int(node['id']))
                if ref in self.protected[side]:
                    continue
                nodes.append({**node, 'value': self._sanitize_value(node['value'], side, ref)})
            return {'type': 'graph', 'root': self._sanitize_value(value['root'], side, context), 'nodes': nodes}
        if value.get('type') == 'record':
            fields = value['fields']
            kind = value.get('record_type')
            number = fields.get('id', {}).get('value') if kind == 'MooObject' else None
            result = {}
            for field, child in fields.items():
                if kind == 'MooObject' and field == 'name' and number in self.label_ids:
                    result[field] = dict(REDACTED)
                elif kind == 'MooObject' and field in ('properties', 'propdefs_count') and number in self.whole_property_ids[side]:
                    result[field] = dict(REDACTED)
                elif kind == 'MooObject' and field == 'properties' and number in self.uncertain_properties[side]:
                    result[field] = dict(REDACTED)
                elif kind == 'MooObject' and field == 'verbs' and number in self.whole_verb_ids[side]:
                    result[field] = dict(REDACTED)
                elif kind == 'MooObject' and field in ('properties', 'verbs') and child.get('type') == 'list':
                    lookup = self.paths[side] if field == 'properties' else self.verb_slots[side]
                    items = []
                    for slot, item in enumerate(child['items']):
                        rule = lookup.get((number, slot))
                        if rule is not None and (field == 'properties' or set(self.rules[rule]['selector']) == {'section'}):
                            items.append(dict(REDACTED))
                        else:
                            child_context = ('verb', number, slot) if field == 'verbs' else None
                            items.append(self._sanitize_value(item, side, child_context))
                    result[field] = {'type': 'list', 'items': items}
                elif kind == 'Verb' and field == 'code' and context and context[0] == 'verb' and (context[1], context[2]) in self.verb_slots[side]:
                    result[field] = dict(REDACTED)
                elif kind == 'Waif' and field == 'props' and context and child.get('type') == 'list':
                    result[field] = {'type': 'list', 'items': [dict(REDACTED) if (context[1], int(item['items'][0]['value'])) in self.waif_slots[side]
                                                            else self._sanitize_value(item, side) for item in child['items']]}
                else:
                    result[field] = self._sanitize_value(child, side, context)
            return {**value, 'fields': result}
        if value.get('type') == 'map' and context and context[0] == 'permanent':
            plain = {}
            for entry in value.get('entries', []):
                key, child = entry['key'], entry['value']
                if key.get('type') == 'string' and child.get('type') in ('string', 'int'):
                    plain[key['value']] = child['value']
            if plain.get('type') == 'property' and 'slot' in plain and (context[1], int(plain['slot'])) in self.paths[side]:
                return dict(REDACTED)
            if plain.get('type') == 'verb' and 'slot' in plain and (context[1], int(plain['slot'])) in self.verb_slots[side]:
                return dict(REDACTED)
        return {key: [self._sanitize_value(v, side, context) for v in child] if isinstance(child, list)
                     else self._sanitize_value(child, side, context) if isinstance(child, dict) else child
                for key, child in value.items()}

    def apply(self, comparison: Comparison, strict=False):
        if not self.bound:
            raise ValueError('redaction policy must be bound to both snapshots')
        if self.options.stop_after is not None or comparison.header.get('options', {}).get('stop_after') is not None:
            raise ValueError('stop_after cannot be combined with redaction')
        result = deepcopy(comparison)
        self.correspondence = comparison.footer.get('heap_correspondence', [])
        self.anonymous_numbers = {side: {row['id']: row[f'{side}_id'] for row in self.correspondence if row['kind'] == 'anonymous'}
                                  for side in ('old', 'new')}
        concealed = {}
        visible = []
        sanitized_events = 0
        for event in result.events:
            root = self._event_root(event)
            if root is not None:
                current = concealed.setdefault(root, {'definite': False, 'uncertain': False, 'event': event})
                current['definite'] |= event['definite_change']
                current['uncertain'] |= event['kind'] in ('identity_conflict', 'identity_unresolved') or event.get('details', {}).get('concealed_uncertainty', False)
                continue
            sanitized = False
            for side in ('old', 'new'):
                number = self._object_number(event['entity'], side)
                context = ('permanent', number) if number is not None else None
                path = event.get(f'{side}_path', [])
                if path and path[0].get('type') == 'verb':
                    context = ('verb', number, int(path[0]['slot']))
                if path == [{'type': 'field', 'name': 'properties'}] and number is not None:
                    context = ('property-list', number)
                before = canonical_json(event[side])
                event[side] = self._sanitize_value(event[side], side, context)
                sanitized |= before != canonical_json(event[side])
            if event['entity']['id'] in self.label_ids and event['entity']['type'] == 'permanent':
                event['labels'] = {'old': None, 'new': None}
            # Provenance diagnostics can contain alternate protected anchors,
            # names, payloads and hidden collection counts. Keep only static
            # subtype metadata on evidence whose container was sanitized.
            for key in ('anchors', 'competing_paths'):
                # The visible event is not under a concealed root, but another
                # anchor may carry protected label annotations. These paths
                # are dependency evidence rather than its changed coordinates.
                event.get('details', {}).pop(key, None)
            if any(self._object_number(event['entity'], side) in self.whole_verb_ids[side] or
                   any(number == self._object_number(event['entity'], side) for number, _ in self.verb_slots[side])
                   for side in ('old', 'new')):
                if event.get('details', {}).get('subtype') in ('lifecycle', 'verb_definition', 'verb_container'):
                    for key in ('added_lines', 'removed_lines', 'hunks', 'opcodes'):
                        event.get('details', {}).pop(key, None)
            sanitized_events += sanitized
            visible.append(event)
        for coordinate, (root, record) in enumerate(sorted(concealed.items(), key=lambda item: repr(item[0])), 1):
            event = record['event']
            entity = event['entity']
            if root[1] == 'section':
                # Section containers have no public member coordinate. The
                # first changed member must not influence published identity.
                entity = {'type': 'global', 'id': 'database'}
            elif entity['type'] in ('waif', 'anonymous'):
                entity = {'type': entity['type'], 'id': f'H-redacted-{coordinate:06d}'}
            marker = {'type': 'redacted', 'coordinate': f'R{coordinate:06d}'}
            visible.append({'record': 'event', 'category': event['category'], 'section': event['section'],
                            'kind': 'changed' if record['definite'] else 'identity_unresolved', 'definite_change': record['definite'],
                            'entity': entity, 'old_path': [marker], 'new_path': [marker],
                            'old': dict(REDACTED), 'new': dict(REDACTED), 'labels': {'old': None, 'new': None},
                            'details': {'concealed': True, 'concealed_uncertainty': record['uncertain']}})
        result.events = sorted(visible, key=event_sort_key)
        result.header['redaction'] = {'schema_version': 1, 'policy_sha256': self.digest, 'runtime_default_concealed': self.runtime_concealed}
        # Do not publish unredacted paths or heap identities through summaries.
        heap_privacy = any(self.protected[side] or self.waif_slots[side] or self.anonymous_property_ids[side] or self.anonymous_verb_ids[side]
                           for side in ('old', 'new')) or 'heap' in self.section_rules
        if heap_privacy:
            result.footer['heap_correspondence'] = []
            result.footer.pop('orphan_heap_counts', None)
        result.footer['redacted_events'] = len(concealed) + sanitized_events
        result.footer['redacted_values'] = len(concealed) + sanitized_events
        result.footer['redaction_rules'] = [{'rule': i + 1, 'matches': count} for i, count in enumerate(self.matches)]
        if result.footer.get('errors'):
            result.footer['errors'] = [{'message': 'comparison error; details concealed by redaction'}]
        if strict and any(count == 0 for count in self.matches):
            result.footer.setdefault('errors', []).append({'message': 'redaction rule matched no selected records',
                                                         'rules': [i + 1 for i, count in enumerate(self.matches) if count == 0]})
        result = finalize_counts(result)
        result.footer['uncertainty_events'] = sum(event['kind'] in ('identity_conflict', 'identity_unresolved') or
                                                 event.get('details', {}).get('concealed_uncertainty', False) for event in result.events)
        return result
