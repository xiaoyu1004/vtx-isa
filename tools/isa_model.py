#!/usr/bin/env python3
"""Shared loader for the VTX-1 machine description.

The YAML source stores each encoding layout exactly once, inside
``format_registry``.  A form only records which operand binds to which payload
field.  :func:`expand_document` materialises the per-form ``fields`` list that
the validator, the specification builder, and the tests consume, so that the
physical layout can never drift between forms that share a format.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import yaml

HEADER_FIELD_NAMES = ("class", "format", "opcode", "guard")
DERIVED_FORM_KEYS = ("class", "format", "fields")
FIXED_GUARD_POLICIES = {"required_pt", "explicit_condition"}
FAMILY_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Operand types whose register file is chosen by the format's source selector.
MIXED_SOURCE_TYPES = {"vsrc32", "vsrc64"}
SELECTOR_KIND = "source_select"
#: Selector encoding per format: field name, then code -> payload field it moves
#: from the VGPR file to the SGPR file.  Code 0 always means "no scalar source".
SELECTOR_LAYOUT = {
    "V1": ("ssrc", {1: "va"}),
    "V2": ("ssrc_sel", {1: "va", 2: "vb"}),
    "VCMP": ("ssrc_sel", {1: "va", 2: "vb"}),
    "V3": ("ssrc_sel", {1: "va", 2: "vb", 3: "vc"}),
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_raw(path: Path | str) -> Any:
    """Load the YAML source without expanding derived encoding fields."""
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.load(stream, Loader=UniqueKeyLoader)


def load_isa(path: Path | str) -> Any:
    """Load the YAML source and expand every form to its full encoding."""
    return expand_document(load_raw(path))


def format_index(document: Mapping[str, Any]) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Map each format name to ``(class_name, format_entry)``."""
    index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    registry = document.get("format_registry")
    if not isinstance(registry, Mapping):
        return index
    for class_name, entries in registry.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                index[entry["name"]] = (str(class_name), entry)
    return index


def class_codes(document: Mapping[str, Any]) -> dict[str, int]:
    codes: dict[str, int] = {}
    for item in document.get("class_registry") or []:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            code = item.get("code")
            if isinstance(code, int):
                codes[item["name"]] = code
    return codes


def iter_forms(document: Mapping[str, Any]):
    """Yield ``(family, form)`` for every form in declaration order."""
    for family in document.get("families") or []:
        if not isinstance(family, Mapping):
            continue
        for form in family.get("forms") or []:
            if isinstance(form, Mapping):
                yield family, form


def header_fields(document: Mapping[str, Any], form: Mapping[str, Any], class_code: int) -> list[dict]:
    header = document.get("encoding", {}).get("header", {})
    fixed_values = {
        "class": class_code,
        "format": form.get("format"),
        "opcode": form.get("opcode"),
    }
    fields: list[dict] = []
    for name in HEADER_FIELD_NAMES:
        spec = header.get(name) if isinstance(header, Mapping) else None
        spec = spec if isinstance(spec, Mapping) else {}
        entry = {
            "name": name,
            "lsb": spec.get("lsb"),
            "width": spec.get("width"),
            "kind": "header",
            "description": spec.get("description", ""),
        }
        if name in fixed_values:
            entry["fixed"] = fixed_values[name]
        elif form.get("guard_policy") in FIXED_GUARD_POLICIES:
            entry["fixed"] = 0
        fields.append(entry)
    return fields


def mixed_source_operands(form: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the form's operands that may name either register file."""
    return [
        operand
        for operand in form.get("operands") or []
        if isinstance(operand, Mapping) and operand.get("type") in MIXED_SOURCE_TYPES
    ]


def payload_fields(form: Mapping[str, Any], format_entry: Mapping[str, Any]) -> list[dict]:
    bound = {
        operand["field"]
        for operand in form.get("operands") or []
        if isinstance(operand, Mapping) and isinstance(operand.get("field"), str)
    }
    values = form.get("field_values") or {}
    notes = form.get("field_notes") or {}
    selectable = bool(mixed_source_operands(form))
    fields: list[dict] = []
    for registered in format_entry.get("fields") or []:
        if not isinstance(registered, Mapping):
            continue
        name = registered.get("name")
        entry = {
            "name": name,
            "lsb": registered.get("lsb"),
            "width": registered.get("width"),
            "kind": registered.get("kind"),
            "description": notes.get(name, registered.get("description", "")),
        }
        if name in bound:
            entry["kind"] = "operand"
        elif registered.get("kind") == SELECTOR_KIND:
            # The selector stays variable only where a mixed source can use it.
            if not selectable:
                entry["must_zero"] = True
        elif name in values:
            entry["fixed"] = values[name]
        else:
            entry["must_zero"] = True
        if "reserved_values" in registered:
            entry["reserved_values"] = list(registered["reserved_values"])
        fields.append(entry)
    return fields


def expand_form(
    document: Mapping[str, Any],
    form: MutableMapping[str, Any],
    formats: Mapping[str, tuple[str, Mapping[str, Any]]],
    codes: Mapping[str, int],
) -> None:
    """Materialise ``class``, ``format`` and ``fields`` on a single form."""
    entry = formats.get(str(form.get("encoding_format")))
    if entry is None:
        return
    class_name, format_entry = entry
    form["class"] = class_name
    form["format"] = format_entry.get("code")
    form["fields"] = header_fields(
        document, form, codes.get(class_name, 0)
    ) + payload_fields(form, format_entry)


def expand_document(document: Any) -> Any:
    """Expand every form in ``document`` in place and return it."""
    if not isinstance(document, MutableMapping):
        return document
    formats = format_index(document)
    codes = class_codes(document)
    for _, form in iter_forms(document):
        expand_form(document, form, formats, codes)
    return document


def family_slug(mnemonic: str) -> str:
    """Derive the stable semantic family id from a family mnemonic."""
    slug = re.sub(r"[^0-9a-z]+", "-", mnemonic.lower()).strip("-")
    return slug
