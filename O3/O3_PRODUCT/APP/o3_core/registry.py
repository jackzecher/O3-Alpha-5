"""One Python-native rule registration mechanism; no JSON rule interpreter."""
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class Rule:
    rule_id: str
    group: str
    inputs: tuple[str, ...]
    description: str
    execute: Callable


def new_registry(groups: tuple[str, ...]):
    items: list[Rule] = []

    def register(rule_id: str, group: str, inputs: tuple[str, ...], description: str):
        if group not in groups or not rule_id or not inputs or not description:
            raise ValueError('Invalid rule metadata')
        if any(x.rule_id==rule_id for x in items):
            raise ValueError('Duplicate rule ID: '+rule_id)

        def apply(fn: Callable):
            items.append(Rule(rule_id,group,inputs,description,fn))
            return fn
        return apply
    return items,register
