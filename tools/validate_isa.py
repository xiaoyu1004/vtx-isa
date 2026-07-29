#!/usr/bin/env python3
"""Validate the vtx-isa specification schema and conformance vectors."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ISA = REPO_ROOT / "isa" / "vtx1" / "isa.yaml"
DEFAULT_SCHEMA = REPO_ROOT / "isa" / "vtx1" / "schema.json"
DEFAULT_VECTORS = REPO_ROOT / "tests" / "conformance" / "encoding_vectors.json"
MACHINE_WORD_RE = re.compile(r"^0x[0-9A-F]{16}$")
INTEGER_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*)$")
REGISTER_PAIR_RE = re.compile(r"\b([sv])(\d+):([sv])(\d+)\b")
REGISTER_OPERAND_WIDTHS = {
    "sgpr32": {8},
    "sgpr64": {8},
    "vgpr32": {8},
    "vgpr64": {8},
    "vpred": {4, 8},
    "barrier_token": {8},
}
REGISTER_FILES = {
    "sgpr32": "SGPR",
    "sgpr64": "SGPR",
    "vgpr32": "VGPR",
    "vgpr64": "VGPR",
    "vpred": "VPRED",
    "barrier_token": "VGPR",
}
EXECUTION_DOMAINS = {
    "system",
    "scalar",
    "vector",
    "warp_control",
    "warp_collective",
    "cta_sync",
    "warp_matrix",
}
ISA_CLASSES = {
    "SYS",
    "SALU",
    "VALU",
    "MEMORY",
    "CONTROL",
    "SYNC",
    "CROSSLANE",
    "MATRIX",
}
SCALAR_CONTROL_MNEMONICS = {"CALL", "CALL.IND", "JUMP.IND", "RET"}
REQUIRED_CONTROL_MNEMONICS = {
    "SSY",
    "BRA",
    "BRA.P",
    "JOIN",
    "EXIT",
    "CALL",
    "CALL.IND",
    "RET",
    "JUMP.IND",
}
ATOMIC_ORDERS = {"RELAXED", "ACQUIRE", "RELEASE", "ACQ_REL"}
ATOMIC_SCOPES = {"CTA", "DEVICE", "SYSTEM"}
ATOMIC_ORDER_CODES = {"RELAXED": 0, "ACQUIRE": 1, "RELEASE": 2, "ACQ_REL": 3}
ATOMIC_SCOPE_CODES = {"CTA": 0, "DEVICE": 1, "SYSTEM": 2}
BARRIER_FORMS = {
    "F061": {
        "family_mnemonic": "BAR.SYNC",
        "form_mnemonic": "BAR.SYNC.CTA",
        "triple": ("SYNC", 0, 3),
        "syntax": "BAR.SYNC.CTA 3",
        "assembly": "BAR.SYNC.CTA 3",
        "machine_word": "0x0018000000000185",
        "operands": [("barrier", "barrier_id", "control", "slot3")],
    },
    "F062": {
        "family_mnemonic": "BAR.ARRIVE",
        "form_mnemonic": "BAR.ARRIVE.CTA",
        "triple": ("SYNC", 0, 4),
        "syntax": "BAR.ARRIVE.CTA v5, 3",
        "assembly": "BAR.ARRIVE.CTA v5, 3",
        "machine_word": "0x0018000000280205",
        "operands": [
            ("token", "barrier_token", "write", "a"),
            ("barrier", "barrier_id", "control", "slot3"),
        ],
    },
    "F063": {
        "family_mnemonic": "BAR.WAIT",
        "form_mnemonic": "BAR.WAIT.CTA",
        "triple": ("SYNC", 0, 5),
        "syntax": "BAR.WAIT.CTA 3, v5",
        "assembly": "BAR.WAIT.CTA 3, v5",
        "machine_word": "0x0018000000280285",
        "operands": [
            ("barrier", "barrier_id", "control", "slot3"),
            ("token", "barrier_token", "read", "a"),
        ],
    },
}
VGPR_TAG_EXCEPTIONS = {
    ("F025", "b32.reg", "V_MOV.B32"): "copy_source_tag",
    ("F062", "cta", "BAR.ARRIVE.CTA"): "create_tag",
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


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    family_count: int = 0
    form_count: int = 0
    vector_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, location: str, message: str) -> None:
        self.errors.append(f"{location}: {message}")

    def warn(self, location: str, message: str) -> None:
        self.warnings.append(f"{location}: {message}")


def parse_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and INTEGER_RE.fullmatch(value.strip()):
        try:
            return int(value.replace("_", ""), 0)
        except ValueError:
            return None
    return None


def load_isa(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.load(stream, Loader=UniqueKeyLoader)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def get_families(document: Mapping[str, Any]) -> Any:
    return document.get("families")


def get_forms(family: Mapping[str, Any]) -> Any:
    return family.get("forms")


def get_word_bits(document: Mapping[str, Any]) -> int:
    return parse_integer(document.get("word_bits")) or 64


def normalize_fields(container: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    fields = container.get("fields")
    if not isinstance(fields, list):
        return []
    result: list[tuple[str, Mapping[str, Any]]] = []
    for index, item in enumerate(fields):
        if isinstance(item, Mapping):
            name = item.get("name")
            result.append((str(name) if name is not None else f"#{index + 1}", item))
        else:
            result.append((f"#{index + 1}", {}))
    return result


def _json_path(parts: Sequence[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def validate_schema(document: Any, schema: Any, report: ValidationReport) -> None:
    if not isinstance(schema, Mapping):
        report.error("$schema", "schema.json root must be an object")
        return
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
    except SchemaError as exc:
        report.error("$schema", f"invalid Draft 2020-12 schema: {exc.message}")
        return
    errors = sorted(
        validator.iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    for error in errors:
        report.error(_json_path(list(error.absolute_path)), f"schema: {error.message}")


def _range(data: Mapping[str, Any]) -> tuple[int, int] | None:
    low = parse_integer(data.get("lsb"))
    width = parse_integer(data.get("width"))
    if low is None or width is None or width < 1:
        return None
    return low, low + width - 1


def _constraint_value(data: Mapping[str, Any]) -> int | None:
    fixed = parse_integer(data.get("fixed"))
    if fixed is not None:
        return fixed
    if data.get("must_zero") is True:
        return 0
    return None


def format_machine_word(value: int) -> str:
    return f"0x{value:016X}"


def fixed_machine_word(form: Mapping[str, Any]) -> int:
    word = 0
    for _, data in normalize_fields(form):
        bit_range = _range(data)
        value = _constraint_value(data)
        if bit_range is not None and value is not None:
            word |= value << bit_range[0]
    return word


def form_key(family: Mapping[str, Any], form: Mapping[str, Any]) -> str:
    return f"{family.get('id')}/{form.get('id')}"


def _validate_fields(
    form: Mapping[str, Any],
    location: str,
    word_bits: int,
    report: ValidationReport,
) -> dict[str, tuple[int, int, Mapping[str, Any]]]:
    fields: dict[str, tuple[int, int, Mapping[str, Any]]] = {}
    occupied: dict[int, str] = {}
    for name, data in normalize_fields(form):
        field_location = f"{location}[{name}]"
        if name.startswith("#"):
            report.error(field_location, "field name is missing")
        elif name in fields:
            report.error(field_location, "duplicate field name")
        bit_range = _range(data)
        if bit_range is None:
            report.error(field_location, "lsb/width is missing or invalid")
            continue
        low, high = bit_range
        if low < 0 or high >= word_bits:
            report.error(field_location, f"range [{high}:{low}] is outside {word_bits} bits")
            continue
        fields[name] = (low, high, data)
        for bit in range(low, high + 1):
            if bit in occupied:
                report.error(field_location, f"bit {bit} overlaps field {occupied[bit]!r}")
            else:
                occupied[bit] = name
        value = _constraint_value(data)
        if value is not None and not 0 <= value < (1 << (high - low + 1)):
            report.error(field_location, f"fixed value {value} does not fit field width")
        if "fixed" in data and data.get("must_zero") is True:
            report.error(field_location, "fixed and must_zero cannot both be present")
        reserved_values = data.get("reserved_values")
        if reserved_values is not None:
            if not isinstance(reserved_values, list) or not reserved_values:
                report.error(field_location, "reserved_values must be a non-empty list")
            else:
                parsed_reserved = [parse_integer(item) for item in reserved_values]
                if any(item is None for item in parsed_reserved):
                    report.error(field_location, "reserved_values must contain integers")
                else:
                    concrete_reserved = [item for item in parsed_reserved if item is not None]
                    if len(concrete_reserved) != len(set(concrete_reserved)):
                        report.error(field_location, "reserved_values must be unique")
                    limit = 1 << (high - low + 1)
                    if any(not 0 <= item < limit for item in concrete_reserved):
                        report.error(field_location, "reserved value does not fit field width")
                    if value in concrete_reserved:
                        report.error(field_location, "fixed value cannot also be reserved")
    missing = sorted(set(range(word_bits)) - set(occupied))
    if missing:
        report.error(location, f"does not cover all {word_bits} bits; missing {missing}")
    return fields


def _registry_maps(
    document: Mapping[str, Any],
    report: ValidationReport,
) -> tuple[dict[str, int], dict[str, dict[int, Mapping[str, Any]]]]:
    class_codes: dict[str, int] = {}
    registry = document.get("class_registry")
    if isinstance(registry, list):
        for index, item in enumerate(registry):
            if not isinstance(item, Mapping):
                continue
            name, code = item.get("name"), parse_integer(item.get("code"))
            if not isinstance(name, str) or code is None:
                continue
            if name in class_codes or code in class_codes.values():
                report.error(f"$.class_registry[{index}]", "class name/code is not unique")
            class_codes[name] = code

    formats_by_class: dict[str, dict[int, Mapping[str, Any]]] = {}
    raw_formats = document.get("format_registry")
    if not isinstance(raw_formats, Mapping):
        report.error("$.format_registry", "must be an object")
        return class_codes, formats_by_class
    for class_name, entries in raw_formats.items():
        location = f"$.format_registry.{class_name}"
        formats_by_class[str(class_name)] = {}
        if class_name not in class_codes:
            report.error(location, "format registry references an unknown class")
        if not isinstance(entries, list):
            report.error(location, "must be a list")
            continue
        names: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                report.error(f"{location}[{index}]", "format entry must be an object")
                continue
            code, name = parse_integer(entry.get("code")), entry.get("name")
            if code is None or not isinstance(name, str):
                continue
            if code in formats_by_class[str(class_name)] or name in names:
                report.error(f"{location}[{index}]", "format name/code is not unique within class")
            formats_by_class[str(class_name)][code] = entry
            names.add(name)
            _validate_payload_layout(entry, f"{location}[{name}]", report)
    return class_codes, formats_by_class


def _validate_payload_layout(
    format_entry: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    occupied: dict[int, str] = {}
    fields = format_entry.get("fields")
    if not isinstance(fields, list):
        report.error(f"{location}.fields", "must be a list")
        return
    for index, data in enumerate(fields):
        if not isinstance(data, Mapping):
            report.error(f"{location}.fields[{index}]", "must be an object")
            continue
        name = str(data.get("name", f"#{index + 1}"))
        bit_range = _range(data)
        if bit_range is None:
            report.error(f"{location}.fields[{name}]", "invalid lsb/width")
            continue
        low, high = bit_range
        if low < 19 or high > 63:
            report.error(f"{location}.fields[{name}]", "payload field is outside bits 19..63")
        for bit in range(low, high + 1):
            if bit in occupied:
                report.error(f"{location}.fields[{name}]", f"bit {bit} overlaps {occupied[bit]!r}")
            occupied[bit] = name
    missing = sorted(set(range(19, 64)) - set(occupied))
    if missing:
        report.error(f"{location}.fields", f"payload layout is incomplete; missing {missing}")


def _validate_header(
    document: Mapping[str, Any],
    form: Mapping[str, Any],
    fields: Mapping[str, tuple[int, int, Mapping[str, Any]]],
    class_codes: Mapping[str, int],
    location: str,
    report: ValidationReport,
) -> None:
    header = document.get("encoding", {}).get("header", {})
    expected_values = {
        "class": class_codes.get(str(form.get("class"))),
        "format": parse_integer(form.get("format")),
        "opcode": parse_integer(form.get("opcode")),
    }
    for name in ("class", "format", "opcode", "guard"):
        field_location = f"{location}.fields[{name}]"
        if name not in fields:
            report.error(field_location, "required header field is missing")
            continue
        low, high, data = fields[name]
        specification = header.get(name) if isinstance(header, Mapping) else None
        if not isinstance(specification, Mapping):
            report.error(f"$.encoding.header.{name}", "header specification is missing")
        else:
            expected_low = parse_integer(specification.get("lsb"))
            expected_width = parse_integer(specification.get("width"))
            if low != expected_low or high - low + 1 != expected_width:
                report.error(field_location, "position does not match encoding.header")
        if name in expected_values and _constraint_value(data) != expected_values[name]:
            report.error(
                field_location,
                f"fixed value must equal form {name}={expected_values[name]}",
            )

    guard = fields.get("guard")
    if guard is None:
        return
    guard_value = _constraint_value(guard[2])
    policy = form.get("guard_policy")
    if policy in {"required_pt", "explicit_condition"}:
        if guard_value != 0:
            report.error(
                f"{location}.fields[guard]",
                f"guard_policy {policy!r} requires fixed PT (guard=0)",
            )
    elif policy == "optional" and guard_value is not None:
        report.error(
            f"{location}.fields[guard]",
            "optional guard must remain variable",
        )


def _validate_format_registration(
    form: Mapping[str, Any],
    fields: Mapping[str, tuple[int, int, Mapping[str, Any]]],
    formats_by_class: Mapping[str, Mapping[int, Mapping[str, Any]]],
    location: str,
    report: ValidationReport,
) -> None:
    class_name = form.get("class")
    format_code = parse_integer(form.get("format"))
    class_formats = formats_by_class.get(str(class_name))
    if class_formats is None or format_code not in class_formats:
        report.error(
            f"{location}.format",
            f"format {format_code!r} is not registered for class {class_name!r}",
        )
        return
    entry = class_formats[format_code]
    if form.get("encoding_format") != entry.get("name"):
        report.error(
            f"{location}.encoding_format",
            f"must be {entry.get('name')!r} for {class_name}/{format_code}",
        )
    registered = [
        (item.get("name"), parse_integer(item.get("lsb")), parse_integer(item.get("width")))
        for item in entry.get("fields", [])
        if isinstance(item, Mapping)
    ]
    actual = [
        (name, low, high - low + 1)
        for name, (low, high, _) in fields.items()
        if low >= 19
    ]
    if actual != registered:
        report.error(
            f"{location}.fields",
            "payload layout does not exactly match the class-specific format registry",
        )


def _validate_operands(
    document: Mapping[str, Any],
    form: Mapping[str, Any],
    fields: Mapping[str, tuple[int, int, Mapping[str, Any]]],
    location: str,
    report: ValidationReport,
) -> None:
    operand_types = document.get("operand_types")
    known_types = set(operand_types) if isinstance(operand_types, Mapping) else set()
    operands = form.get("operands")
    if not isinstance(operands, list):
        return
    for index, operand in enumerate(operands):
        operand_location = f"{location}.operands[{index}]"
        if not isinstance(operand, Mapping):
            report.error(operand_location, "operand must be an object")
            continue
        operand_type = operand.get("type")
        if operand_type not in known_types:
            report.error(f"{operand_location}.type", f"unknown operand type {operand_type!r}")
        field_name = operand.get("field")
        if field_name is not None and field_name not in fields:
            report.error(f"{operand_location}.field", f"unknown form field {field_name!r}")
        if operand_type in REGISTER_OPERAND_WIDTHS:
            if not isinstance(field_name, str) or field_name not in fields:
                report.error(
                    f"{operand_location}.field",
                    f"{operand_type} operand must reference an encoded field",
                )
                continue
            low, high, field_data = fields[field_name]
            expected_widths = REGISTER_OPERAND_WIDTHS[str(operand_type)]
            if high - low + 1 not in expected_widths:
                width_text = "/".join(str(width) for width in sorted(expected_widths))
                report.error(
                    f"{operand_location}.field",
                    f"{operand_type} requires a {width_text}-bit register-index field",
                )
            if field_data.get("kind") not in {"register", "operand"}:
                report.error(
                    f"{operand_location}.field",
                    "register operand must reference a register/operand field",
                )
            definition = operand_types.get(operand_type) if isinstance(operand_types, Mapping) else None
            if isinstance(definition, Mapping):
                expected_file = REGISTER_FILES[str(operand_type)]
                if definition.get("register_file") != expected_file:
                    report.error(
                        f"$.operand_types.{operand_type}.register_file",
                        f"must be {expected_file}",
                    )


def _validate_address_template(
    form: Mapping[str, Any],
    fields: Mapping[str, tuple[int, int, Mapping[str, Any]]],
    location: str,
    report: ValidationReport,
) -> None:
    template = form.get("address_template")
    if form.get("class") != "MEMORY":
        if template is not None:
            report.error(f"{location}.address_template", "is only legal on MEMORY forms")
        return
    if not isinstance(template, Mapping):
        report.error(f"{location}.address_template", "MEMORY form requires an address template")
        return
    expression = template.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        report.error(f"{location}.address_template.expression", "must be a non-empty string")
    if template.get("offset_unit") != "bytes":
        report.error(f"{location}.address_template.offset_unit", "must be 'bytes'")
    mode = template.get("mode")
    if mode not in {"uniform_base", "lane_address", "sv_mix", "scalar_indexed"}:
        report.error(f"{location}.address_template.mode", f"unknown address mode {mode!r}")
    scale = parse_integer(template.get("scale"))
    if scale is None or not 1 <= scale <= 16:
        report.error(f"{location}.address_template.scale", "must be an integer in 1..16")
    operands = form.get("operands")
    references: set[str] = set(fields)
    if isinstance(operands, list):
        for operand in operands:
            if isinstance(operand, Mapping):
                for key in ("name", "field"):
                    value = operand.get(key)
                    if isinstance(value, str):
                        references.add(value)
    address_operands = template.get("address_operands")
    if not isinstance(address_operands, list) or not address_operands:
        report.error(
            f"{location}.address_template.address_operands",
            "must be a non-empty list",
        )
        return
    for index, name in enumerate(address_operands):
        if name not in references:
            report.error(
                f"{location}.address_template.address_operands[{index}]",
                f"unknown operand or field {name!r}",
            )
    if form.get("encoding_format") == "VMEM":
        expected_fields = {
            "uniform_base": ["sbase", "simm16"],
            "lane_address": ["vaddr", "simm16"],
            "sv_mix": ["sbase", "vaddr", "simm16"],
        }
        expected = expected_fields.get(str(mode))
        if expected is None:
            report.error(
                f"{location}.address_template.mode",
                "VMEM mode must be uniform_base, lane_address, or sv_mix",
            )
        elif address_operands != expected:
            report.error(
                f"{location}.address_template.address_operands",
                f"VMEM {mode} requires {expected}",
            )
        operand_by_name = {
            operand.get("name"): operand
            for operand in (operands if isinstance(operands, list) else [])
            if isinstance(operand, Mapping)
        }
        space = template.get("space")
        address_name = "address" if mode == "lane_address" else "base"
        address = operand_by_name.get(address_name)
        suffix = "lane" if mode == "lane_address" else "uniform"
        expected_type = f"address_{space}_{suffix}"
        if not isinstance(address, Mapping) or address.get("type") != expected_type:
            report.error(
                f"{location}.operands",
                f"VMEM {mode} requires {address_name} operand type {expected_type!r}",
            )
        if mode == "sv_mix":
            index = operand_by_name.get("index")
            if not isinstance(index, Mapping) or (
                index.get("type") != "vgpr_index" or index.get("field") != "vaddr"
            ):
                report.error(
                    f"{location}.operands",
                    "VMEM sv_mix requires a vgpr_index operand in vaddr",
                )
    if (
        mode == "sv_mix"
        and form.get("encoding_format") in {"VMEM", "VSHMEM"}
        and isinstance(expression, str)
        and "zero_extend(vaddr)" not in expression
    ):
        report.error(
            f"{location}.address_template.expression",
            "SV mixed address must zero_extend(vaddr)",
        )


def _validate_matrix_contract(
    form: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    contract = form.get("matrix_contract")
    if form.get("class") != "MATRIX":
        if contract is not None:
            report.error(f"{location}.matrix_contract", "is only legal on MATRIX forms")
        return
    if not isinstance(contract, Mapping):
        report.error(f"{location}.matrix_contract", "MATRIX form requires matrix_contract")
        return
    if form.get("execution_domain") != "warp_matrix":
        report.error(f"{location}.execution_domain", "MATRIX form requires warp_matrix")
    if form.get("guard_policy") != "required_pt":
        report.error(f"{location}.guard_policy", "MATRIX form requires required_pt")
    shape = contract.get("shape")
    if shape != {"m": 16, "n": 8, "k": 16}:
        report.error(
            f"{location}.matrix_contract.shape",
            "the unique MMA contract must use shape m16n8k16",
        )
    if contract.get("element_types") != {"A": "F16", "B": "F16", "C": "F32", "D": "F32"}:
        report.error(
            f"{location}.matrix_contract.element_types",
            "the unique MMA contract requires F16/F16/F32/F32",
        )
    fragments = contract.get("fragments")
    if not isinstance(fragments, Mapping) or set(fragments) != {"A", "B", "C", "D"}:
        report.error(
            f"{location}.matrix_contract.fragments",
            "must define exactly A, B, C, and D",
        )
    else:
        for fragment, data in fragments.items():
            if not isinstance(data, Mapping):
                report.error(
                    f"{location}.matrix_contract.fragments.{fragment}",
                    "must be an object",
                )
                continue
            registers = parse_integer(data.get("registers_per_lane"))
            alignment = parse_integer(data.get("base_alignment"))
            if registers is None or not 1 <= registers <= 4:
                report.error(
                    f"{location}.matrix_contract.fragments.{fragment}.registers_per_lane",
                    "must be in 1..4",
                )
            if alignment not in {2, 4}:
                report.error(
                    f"{location}.matrix_contract.fragments.{fragment}.base_alignment",
                    "must be 2 or 4",
                )
            if not isinstance(data.get("mapping"), str) or not data["mapping"].strip():
                report.error(
                    f"{location}.matrix_contract.fragments.{fragment}.mapping",
                    "must be non-empty",
                )
    participation = contract.get("participation")
    if not isinstance(participation, Mapping):
        report.error(f"{location}.matrix_contract.participation", "must be an object")
    else:
        if participation.get("required_live_lanes") != 32:
            report.error(
                f"{location}.matrix_contract.participation.required_live_lanes",
                "must equal 32",
            )
        if participation.get("required_exec_equals_live") is not True:
            report.error(
                f"{location}.matrix_contract.participation.required_exec_equals_live",
                "must be true",
            )
    operands = form.get("operands")
    fragment_operands = {
        operand.get("name"): operand
        for operand in (operands if isinstance(operands, list) else [])
        if isinstance(operand, Mapping) and operand.get("type") == "mma_fragment"
    }
    if set(fragment_operands) != {"dst", "a", "b", "c"}:
        report.error(
            f"{location}.operands",
            "MMA contract requires dst/a/b/c mma_fragment operands",
        )


def _validate_atomic_form(
    family: Mapping[str, Any],
    form: Mapping[str, Any],
    fields: Mapping[str, tuple[int, int, Mapping[str, Any]]],
    location: str,
    report: ValidationReport,
) -> None:
    if family.get("semantic_group") != "atomic":
        return
    mnemonic = form.get("mnemonic")
    if not isinstance(mnemonic, str):
        return
    segments = mnemonic.split(".")
    operation = next(
        (name for name in ("LOAD", "STORE", "ADD", "XCHG", "AND", "OR", "XOR", "MIN", "MAX", "CAS") if name in segments),
        None,
    )
    legal_orders = form.get("legal_orders")
    expected_orders = (
        {"RELAXED", "ACQUIRE"}
        if operation == "LOAD"
        else {"RELAXED", "RELEASE"}
        if operation == "STORE"
        else ATOMIC_ORDERS
    )
    legal_order_names = (
        [name for name in legal_orders if isinstance(name, str)]
        if isinstance(legal_orders, list)
        else []
    )
    if (
        not isinstance(legal_orders, list)
        or len(legal_order_names) != len(legal_orders)
        or len(legal_order_names) != len(set(legal_order_names))
        or set(legal_order_names) != expected_orders
    ):
        report.error(
            f"{location}.legal_orders",
            f"{operation} atomic requires legal orders {sorted(expected_orders)}",
        )
    legal_scopes = form.get("legal_scopes")
    expected_scopes = {"CTA"} if "SHARED" in segments else ATOMIC_SCOPES
    legal_scope_names = (
        [name for name in legal_scopes if isinstance(name, str)]
        if isinstance(legal_scopes, list)
        else []
    )
    if (
        not isinstance(legal_scopes, list)
        or len(legal_scope_names) != len(legal_scopes)
        or len(legal_scope_names) != len(set(legal_scope_names))
        or set(legal_scope_names) != expected_scopes
    ):
        report.error(
            f"{location}.legal_scopes",
            f"atomic address space requires legal scopes {sorted(expected_scopes)}",
        )
    syntax = form.get("syntax")
    if not isinstance(syntax, str) or ".{order}.{scope} " not in syntax:
        report.error(
            f"{location}.syntax",
            "atomic syntax must expose .{order}.{scope} modifiers",
        )
    operand_list = form.get("operands")
    operands = {
        operand.get("name"): operand
        for operand in (operand_list if isinstance(operand_list, list) else [])
        if isinstance(operand, Mapping)
    }
    modifier_contracts = {
        "order": ("atomic_order", ATOMIC_ORDERS),
        "scope": ("memory_scope", ATOMIC_SCOPES),
    }
    for name, (operand_type, _) in modifier_contracts.items():
        field = fields.get(name)
        if field is None:
            report.error(f"{location}.fields[{name}]", "atomic modifier field is missing")
        else:
            _, _, data = field
            if _constraint_value(data) is not None:
                report.error(
                    f"{location}.fields[{name}]",
                    "atomic modifier must remain runtime-selectable, not fixed",
                )
            if data.get("kind") != "operand":
                report.error(f"{location}.fields[{name}]", "atomic modifier must be an operand field")
        operand = operands.get(name)
        if not isinstance(operand, Mapping) or (
            operand.get("type") != operand_type or operand.get("field") != name
        ):
            report.error(
                f"{location}.operands",
                f"atomic {name} modifier requires {operand_type} operand in field {name}",
            )
    scope_field = fields.get("scope")
    if scope_field is not None and scope_field[2].get("reserved_values") != [3]:
        report.error(
            f"{location}.fields[scope].reserved_values",
            "2-bit atomic scope must classify encoding 3 as reserved",
        )
    example = form.get("example")
    if isinstance(example, Mapping):
        field_values = example.get("field_values")
        if not isinstance(field_values, Mapping) or set(field_values) != {"order", "scope"}:
            report.error(
                f"{location}.example.field_values",
                "atomic example must provide concrete order and scope values",
            )
        else:
            order_value = parse_integer(field_values.get("order"))
            scope_value = parse_integer(field_values.get("scope"))
            order_name = next(
                (name for name, code in ATOMIC_ORDER_CODES.items() if code == order_value),
                None,
            )
            scope_name = next(
                (name for name, code in ATOMIC_SCOPE_CODES.items() if code == scope_value),
                None,
            )
            if order_name not in expected_orders:
                report.error(
                    f"{location}.example.field_values.order",
                    "example order is outside legal_orders",
                )
            if scope_name not in expected_scopes:
                report.error(
                    f"{location}.example.field_values.scope",
                    "example scope is reserved or outside legal_scopes",
                )
            assembly = example.get("assembly")
            if (
                isinstance(assembly, str)
                and order_name is not None
                and scope_name is not None
                and f".{order_name}.{scope_name} " not in assembly
            ):
                report.error(
                    f"{location}.example.assembly",
                    "atomic example assembly does not match concrete field_values",
                )
            machine_word = example.get("machine_word")
            if isinstance(machine_word, str) and MACHINE_WORD_RE.fullmatch(machine_word):
                word = int(machine_word, 16)
                for name, expected in (("order", order_value), ("scope", scope_value)):
                    field = fields.get(name)
                    if field is not None and expected is not None:
                        low, high, _ = field
                        actual = (word >> low) & ((1 << (high - low + 1)) - 1)
                        if actual != expected:
                            report.error(
                                f"{location}.example.machine_word",
                                f"{name} encodes {actual}, field_values declares {expected}",
                            )
    if "CAS" in segments:
        if not {"compare", "replacement"}.issubset(operands):
            report.error(
                f"{location}.operands",
                "CAS requires distinct compare and replacement operands",
            )


def _validate_named_form_contract(
    form: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    mnemonic = form.get("mnemonic")
    operands = form.get("operands")
    typed = {
        operand.get("name"): operand.get("type")
        for operand in (operands if isinstance(operands, list) else [])
        if isinstance(operand, Mapping)
    }
    if mnemonic == "V_BCAST.B32":
        if (
            form.get("execution_domain") != "vector"
            or form.get("class") != "CROSSLANE"
            or form.get("guard_policy") != "optional"
            or typed.get("dst") != "vgpr32"
            or typed.get("src") != "sgpr32"
        ):
            report.error(
                location,
                "V_BCAST.B32 must be an optionally guarded SGPR-to-VGPR broadcast",
            )
    if mnemonic == "V_BCAST.B64":
        if (
            form.get("execution_domain") != "vector"
            or form.get("class") != "CROSSLANE"
            or form.get("guard_policy") != "optional"
            or typed.get("dst") != "vgpr64"
            or typed.get("src") != "sgpr64"
        ):
            report.error(
                location,
                "V_BCAST.B64 must be an optionally guarded SGPR64-to-VGPR64 broadcast",
            )
    if mnemonic == "S_READFIRST.B64":
        if (
            form.get("execution_domain") != "scalar"
            or form.get("class") != "CROSSLANE"
            or form.get("guard_policy") != "required_pt"
            or form.get("required_state") != "scalar_ready"
            or typed.get("dst") != "sgpr64"
            or typed.get("src") != "vgpr64"
        ):
            report.error(
                location,
                "S_READFIRST.B64 must be a scalar-ready VGPR64-to-SGPR64 transfer",
            )
    if mnemonic == "X_BROADCAST.B32":
        if (
            form.get("execution_domain") != "warp_collective"
            or form.get("class") != "CROSSLANE"
            or form.get("guard_policy") != "required_pt"
            or typed.get("dst") != "vgpr32"
            or typed.get("src") != "vgpr32"
            or typed.get("lane") not in {"sgpr32", "uimm8"}
        ):
            report.error(
                location,
                "X_BROADCAST.B32 must use VGPR source/destination and a uniform lane selector",
            )
    if mnemonic == "V_SHUFFLE.DOWN.B32":
        delta_type = typed.get("lane_or_delta")
        expected_opcode = {"vgpr32": 11, "uimm8": 13}.get(delta_type)
        operand_fields = {
            operand.get("name"): operand.get("field")
            for operand in (operands if isinstance(operands, list) else [])
            if isinstance(operand, Mapping)
        }
        if (
            form.get("execution_domain") != "warp_collective"
            or form.get("class") != "CROSSLANE"
            or form.get("format") != 0
            or form.get("guard_policy") != "required_pt"
            or typed.get("dst") != "vgpr32"
            or typed.get("src") != "vgpr32"
            or expected_opcode is None
            or form.get("opcode") != expected_opcode
            or operand_fields.get("lane_or_delta") != "vb"
            or typed.get("width") != "uimm8"
            or operand_fields.get("width") != "imm8"
        ):
            report.error(
                location,
                "V_SHUFFLE.DOWN.B32 must use opcode 11 for VGPR delta or opcode 13 for immediate delta",
            )


def _validate_pair_syntax(
    form: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    syntax = form.get("syntax")
    if not isinstance(syntax, str):
        return
    matches = list(REGISTER_PAIR_RE.finditer(syntax))
    for match in matches:
        first_prefix, first_text, second_prefix, second_text = match.groups()
        first, second = int(first_text), int(second_text)
        if first_prefix != second_prefix or first % 2 or second != first + 1:
            report.error(
                f"{location}.syntax",
                f"register pair {match.group(0)!r} must use an even base and adjacent registers",
            )
    operands = form.get("operands")
    explicit_types = {
        operand.get("type")
        for operand in (operands if isinstance(operands, list) else [])
        if isinstance(operand, Mapping) and not operand.get("implicit")
    }
    required_prefixes = {
        prefix
        for operand_type, prefix in (("sgpr64", "s"), ("vgpr64", "v"))
        if operand_type in explicit_types
    }
    found_prefixes = {match.group(1) for match in matches}
    missing = sorted(required_prefixes - found_prefixes)
    if missing:
        report.error(
            f"{location}.syntax",
            f"64-bit register operands require explicit adjacent pair syntax for {missing}",
        )


def _validate_call_form(
    form: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    mnemonic = form.get("mnemonic")
    operands = form.get("operands")
    operand_by_name = {
        operand.get("name"): operand
        for operand in (operands if isinstance(operands, list) else [])
        if isinstance(operand, Mapping)
    }
    if mnemonic == "SSY":
        stack = operand_by_name.get("call_stack")
        if not isinstance(stack, Mapping) or (
            stack.get("type") != "call_stack"
            or stack.get("access") != "read"
            or stack.get("implicit") is not True
        ):
            report.error(
                f"{location}.operands",
                "SSY requires an implicit read-only call_stack operand",
            )
        return
    if mnemonic not in SCALAR_CONTROL_MNEMONICS:
        return
    if mnemonic in {"CALL", "CALL.IND", "RET"}:
        stack = operand_by_name.get("call_stack")
        if not isinstance(stack, Mapping) or (
            stack.get("type") != "call_stack"
            or stack.get("access") != "read_write"
            or stack.get("implicit") is not True
        ):
            report.error(
                f"{location}.operands",
                f"{mnemonic} requires an implicit read_write call_stack operand",
            )
    elif "call_stack" in operand_by_name:
        report.error(f"{location}.operands", "JUMP.IND must not modify the call stack")
    if mnemonic == "CALL":
        target_type = "disp30"
    elif mnemonic in {"CALL.IND", "JUMP.IND"}:
        target_type = "sgpr64"
    else:
        target_type = None
    if target_type is not None:
        target = operand_by_name.get("target")
        if not isinstance(target, Mapping) or target.get("type") != target_type:
            report.error(
                f"{location}.operands",
                f"{mnemonic} target must use operand type {target_type}",
            )
    if mnemonic in {"CALL", "CALL.IND"}:
        constraints = form.get("constraints")
        if not isinstance(constraints, list) or not any(
            "descriptor.call_stack_depth" in item
            for item in constraints
            if isinstance(item, str)
        ):
            report.error(
                f"{location}.constraints",
                f"{mnemonic} must state the descriptor.call_stack_depth rule",
            )


def _validate_state_rules(
    form: Mapping[str, Any],
    location: str,
    report: ValidationReport,
) -> None:
    domain = form.get("execution_domain")
    state = form.get("required_state")
    mnemonic = form.get("mnemonic")
    if domain == "scalar" and state != "scalar_ready":
        report.error(f"{location}.required_state", "all scalar forms require scalar_ready")
    if mnemonic in SCALAR_CONTROL_MNEMONICS and state != "scalar_ready":
        report.error(
            f"{location}.required_state",
            f"{mnemonic} control form requires scalar_ready",
        )


def _writes_vgpr(document: Mapping[str, Any], form: Mapping[str, Any]) -> bool:
    operand_types = document.get("operand_types")
    if not isinstance(operand_types, Mapping):
        return False
    operands = form.get("operands")
    return any(
        isinstance(operand, Mapping)
        and operand.get("access") in {"write", "read_write"}
        and isinstance(operand_types.get(operand.get("type")), Mapping)
        and operand_types[operand["type"]].get("register_file") == "VGPR"
        for operand in (operands if isinstance(operands, list) else [])
    )


def enumerate_vgpr_tag_effects(document: Mapping[str, Any]) -> dict[str, str]:
    """Derive the tag effect for every form that writes any VGPR32 slot."""
    contract = document.get("barrier_contract")
    policy = contract.get("vgpr_tag_write_policy") if isinstance(contract, Mapping) else None
    default_action = policy.get("default_action") if isinstance(policy, Mapping) else None
    exceptions = policy.get("exceptions") if isinstance(policy, Mapping) else None
    exception_actions = {
        (item.get("family_id"), item.get("form_id"), item.get("mnemonic")): item.get("action")
        for item in (exceptions if isinstance(exceptions, list) else [])
        if isinstance(item, Mapping)
    }
    effects: dict[str, str] = {}
    for family in get_families(document) or []:
        if not isinstance(family, Mapping):
            continue
        for form in get_forms(family) or []:
            if not isinstance(form, Mapping) or not _writes_vgpr(document, form):
                continue
            exception_key = (family.get("id"), form.get("id"), form.get("mnemonic"))
            action = exception_actions.get(exception_key, default_action)
            if isinstance(action, str):
                effects[form_key(family, form)] = action
    return effects


def _validate_barrier_contract(
    document: Mapping[str, Any],
    families_by_id: Mapping[str, Mapping[str, Any]],
    report: ValidationReport,
) -> None:
    limits = document.get("architectural_limits")
    if not isinstance(limits, Mapping) or limits.get("named_barrier_slots_per_cta") != 8:
        report.error(
            "$.architectural_limits.named_barrier_slots_per_cta",
            "named barriers require exactly 8 slots per CTA",
        )
    operand_types = document.get("operand_types")
    barrier_id = operand_types.get("barrier_id") if isinstance(operand_types, Mapping) else None
    if not isinstance(barrier_id, Mapping) or any(
        barrier_id.get(name) != value
        for name, value in {"kind": "barrier", "bits": 3, "range": "0..7"}.items()
    ):
        report.error(
            "$.operand_types.barrier_id",
            "must define the canonical 3-bit named-barrier id range 0..7",
        )
    barrier_token = (
        operand_types.get("barrier_token") if isinstance(operand_types, Mapping) else None
    )
    expected_token = {
        "kind": "barrier_token",
        "bits": 32,
        "element_bits": 32,
        "register_file": "VGPR",
        "range": "v0..v255",
    }
    if not isinstance(barrier_token, Mapping) or any(
        barrier_token.get(name) != value for name, value in expected_token.items()
    ):
        report.error(
            "$.operand_types.barrier_token",
            "barrier_token must be a 32-bit VGPR token type",
        )

    contract = document.get("barrier_contract")
    if not isinstance(contract, Mapping):
        report.error("$.barrier_contract", "root named-barrier contract is missing")
        return
    expected_root = {
        "owner_identity": {
            "name": "linear_tid",
            "formula": "linear_tid = warp_id * 32 + lane_id",
            "equivalent_tuple": ["warp_id", "lane_id"],
        },
        "generation_domain": {
            "type": "mathematical_nonnegative_integer",
            "notation": "N",
            "initial": 0,
            "retire_step": 1,
            "monotonic": True,
            "wraps": False,
            "finite_implementation_rule": "as_if_non_wrapping",
            "observable_identity_reuse": "forbidden",
            "stale_token_revival": "forbidden",
            "example_mechanisms": ["wider_epoch", "capability_id", "safe_reclamation"],
        },
        "token_tag_fields": ["cta_identity", "linear_tid", "slot", "generation"],
        "wait_record_fields": ["warp_id", "owner_snapshot", "resume_pc"],
        "max_blocked_records_per_warp": 1,
        "idle_slot": {
            "mode": "EMPTY",
            "arrived_set_empty": True,
            "consumed_set_empty": True,
            "waiters_empty": True,
            "completed": False,
            "generation_ignored": True,
        },
    }
    for name, expected in expected_root.items():
        if contract.get(name) != expected:
            report.error(
                f"$.barrier_contract.{name}",
                f"must equal the canonical named-barrier contract {expected!r}",
            )

    policy = contract.get("vgpr_tag_write_policy")
    expected_selection = {
        "register_file": "VGPR",
        "accesses": ["write", "read_write"],
        "granularity": "each_vgpr32_slot",
    }
    if not isinstance(policy, Mapping):
        report.error(
            "$.barrier_contract.vgpr_tag_write_policy",
            "VGPR tag-write policy is missing",
        )
    else:
        if policy.get("target_selection") != expected_selection:
            report.error(
                "$.barrier_contract.vgpr_tag_write_policy.target_selection",
                "must select every written/read_write VGPR32 slot",
            )
        if policy.get("default_action") != "clear":
            report.error(
                "$.barrier_contract.vgpr_tag_write_policy.default_action",
                "every VGPR write must clear the tag by default",
            )
        exceptions = policy.get("exceptions")
        normalized_exceptions = {
            (item.get("family_id"), item.get("form_id"), item.get("mnemonic")): item.get(
                "action"
            )
            for item in (exceptions if isinstance(exceptions, list) else [])
            if isinstance(item, Mapping)
        }
        if (
            not isinstance(exceptions, list)
            or len(exceptions) != 2
            or normalized_exceptions != VGPR_TAG_EXCEPTIONS
        ):
            report.error(
                "$.barrier_contract.vgpr_tag_write_policy.exceptions",
                "must contain only V_MOV.B32 register-copy and BAR.ARRIVE.CTA create exceptions",
            )

    writer_keys = {
        form_key(family, form)
        for family in get_families(document) or []
        if isinstance(family, Mapping)
        for form in get_forms(family) or []
        if isinstance(form, Mapping) and _writes_vgpr(document, form)
    }
    tag_effects = enumerate_vgpr_tag_effects(document)
    if set(tag_effects) != writer_keys:
        report.error(
            "$.barrier_contract.vgpr_tag_write_policy",
            "tag_effect metadata does not cover every VGPR-writing form",
        )
    if any(
        action not in {"clear", "copy_source_tag", "create_tag"}
        for action in tag_effects.values()
    ):
        report.error(
            "$.barrier_contract.vgpr_tag_write_policy",
            "tag_effect contains an unknown action",
        )

    required_text = {
        "F061": (
            "linear_tid",
            "BarrierWaitRecord",
            "SYNC mode",
            "current generation",
            "shared CTA release",
            "shared CTA acquire",
            "EXIT never shrinks",
        ),
        "F062": (
            "linear_tid",
            "SPLIT mode",
            "current generation",
            "shared CTA release",
            "create_tag",
        ),
        "F063": (
            "linear_tid",
            "BarrierWaitRecord",
            "current generation",
            "shared CTA acquire",
            "explicit id",
        ),
    }
    for family_id, expected in BARRIER_FORMS.items():
        family = families_by_id.get(family_id)
        location = f"$.families[{family_id}]"
        if not isinstance(family, Mapping):
            report.error(location, "canonical named-barrier family is missing")
            continue
        if (
            family.get("mnemonic") != expected["family_mnemonic"]
            or family.get("semantic_group") != "barrier"
        ):
            report.error(
                location,
                f"must be canonical family {expected['family_mnemonic']}",
            )
        forms = get_forms(family)
        if not isinstance(forms, list) or len(forms) != 1 or not isinstance(forms[0], Mapping):
            report.error(f"{location}.forms", "must contain exactly the canonical CTA form")
            continue
        form = forms[0]
        form_location = f"{location}.forms[cta]"
        triple = (
            form.get("class"),
            parse_integer(form.get("format")),
            parse_integer(form.get("opcode")),
        )
        if (
            form.get("id") != "cta"
            or form.get("mnemonic") != expected["form_mnemonic"]
            or triple != expected["triple"]
            or form.get("execution_domain") != "cta_sync"
            or form.get("encoding_format") != "SYNC"
            or form.get("guard_policy") != "required_pt"
            or form.get("required_state") != "none"
        ):
            report.error(
                form_location,
                f"must use canonical {expected['form_mnemonic']} identity and triple",
            )
        if form.get("syntax") != expected["syntax"]:
            report.error(
                f"{form_location}.syntax",
                "must expose the explicit barrier slot id",
            )
        actual_operands = [
            (
                operand.get("name"),
                operand.get("type"),
                operand.get("access"),
                operand.get("field"),
            )
            for operand in form.get("operands", [])
            if isinstance(operand, Mapping)
        ]
        if actual_operands != expected["operands"]:
            report.error(
                f"{form_location}.operands",
                f"must equal canonical operands {expected['operands']!r}",
            )
        fields = {name: data for name, data in normalize_fields(form)}
        slot = fields.get("slot3")
        if (
            not isinstance(slot, Mapping)
            or slot.get("lsb") != 51
            or slot.get("width") != 3
            or slot.get("kind") != "operand"
            or "fixed" in slot
            or slot.get("must_zero")
        ):
            report.error(
                f"{form_location}.fields[slot3]",
                "must be the explicit variable 3-bit slot field at bits 53:51",
            )
        example = form.get("example")
        if not isinstance(example, Mapping) or (
            example.get("assembly") != expected["assembly"]
            or example.get("machine_word") != expected["machine_word"]
        ):
            report.error(
                f"{form_location}.example",
                "must contain the canonical slot-3 assembly and machine word",
            )
        prose_parts = [form.get("semantics", "")]
        prose_parts.extend(form.get("constraints", []))
        prose = " ".join(item for item in prose_parts if isinstance(item, str))
        for fragment in required_text[family_id]:
            if fragment not in prose:
                report.error(
                    form_location,
                    f"named-barrier contract must state {fragment!r}",
                )


def _validate_global_contracts(
    document: Mapping[str, Any],
    class_codes: Mapping[str, int],
    report: ValidationReport,
) -> None:
    if set(class_codes) != ISA_CLASSES:
        report.error(
            "$.class_registry",
            f"must define the eight ISA classes {sorted(ISA_CLASSES)}",
        )
    operand_types = document.get("operand_types")
    vpred = operand_types.get("vpred") if isinstance(operand_types, Mapping) else None
    if not isinstance(vpred, Mapping):
        report.error("$.operand_types.vpred", "VP operand type is missing")
    else:
        expected = {
            "kind": "predicate_register",
            "bits": 32,
            "element_bits": 1,
            "register_file": "VPRED",
        }
        for name, value in expected.items():
            if vpred.get(name) != value:
                report.error(f"$.operand_types.vpred.{name}", f"must equal {value!r}")
    memory_scope = (
        operand_types.get("memory_scope") if isinstance(operand_types, Mapping) else None
    )
    expected_scope = {
        "kind": "enum_modifier",
        "bits": 2,
        "values": ["CTA", "DEVICE", "SYSTEM"],
        "reserved_values": [3],
    }
    if not isinstance(memory_scope, Mapping):
        report.error("$.operand_types.memory_scope", "memory scope operand type is missing")
    else:
        for name, value in expected_scope.items():
            if memory_scope.get(name) != value:
                report.error(
                    f"$.operand_types.memory_scope.{name}",
                    f"must equal {value!r}",
                )

    faults = document.get("faults")
    fault_names = {
        item.get("name")
        for item in (faults if isinstance(faults, list) else [])
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    priority = document.get("fault_priority")
    if not isinstance(priority, list) or not priority:
        report.error("$.fault_priority", "must be a non-empty ordered list")
    else:
        priority_names = [name for name in priority if isinstance(name, str)]
        if len(priority_names) != len(priority):
            report.error("$.fault_priority", "every entry must be a fault name")
        if len(priority_names) != len(set(priority_names)):
            report.error("$.fault_priority", "fault names must be unique")
        unknown = sorted(set(priority_names) - fault_names)
        if unknown:
            report.error("$.fault_priority", f"references unknown faults {unknown}")
        expected_priority = fault_names - {"DEADLOCK"}
        if set(priority_names) != expected_priority:
            report.error(
                "$.fault_priority",
                "must rank every synchronous fault exactly once (DEADLOCK is excluded)",
            )

    architectural_limits = document.get("architectural_limits")
    architectural_depth = (
        parse_integer(architectural_limits.get("call_stack_depth"))
        if isinstance(architectural_limits, Mapping)
        else None
    )
    descriptor = document.get("descriptor_contract")
    depth = descriptor.get("call_stack_depth") if isinstance(descriptor, Mapping) else None
    if not isinstance(depth, Mapping):
        report.error(
            "$.descriptor_contract.call_stack_depth",
            "descriptor call-stack contract is missing",
        )
    else:
        minimum = parse_integer(depth.get("minimum"))
        maximum = parse_integer(depth.get("maximum"))
        if minimum != 0:
            report.error(
                "$.descriptor_contract.call_stack_depth.minimum",
                "must equal 0",
            )
        if maximum != architectural_depth or maximum != 16:
            report.error(
                "$.descriptor_contract.call_stack_depth.maximum",
                "must equal architectural call_stack_depth (16)",
            )


def _validate_machine_word(
    form: Mapping[str, Any],
    machine_word: Any,
    location: str,
    report: ValidationReport,
) -> None:
    if not isinstance(machine_word, str) or not MACHINE_WORD_RE.fullmatch(machine_word):
        report.error(location, "machine_word must be 0x followed by 16 uppercase hex digits")
        return
    word = int(machine_word, 16)
    for name, data in normalize_fields(form):
        bit_range = _range(data)
        expected = _constraint_value(data)
        if bit_range is None or expected is None:
            continue
        low, high = bit_range
        actual = (word >> low) & ((1 << (high - low + 1)) - 1)
        if actual != expected:
            report.error(location, f"field {name!r} encodes {actual}, expected {expected}")
    for name, data in normalize_fields(form):
        bit_range = _range(data)
        reserved_values = data.get("reserved_values")
        if bit_range is None or not isinstance(reserved_values, list):
            continue
        low, high = bit_range
        actual = (word >> low) & ((1 << (high - low + 1)) - 1)
        parsed_reserved = {
            value
            for item in reserved_values
            if (value := parse_integer(item)) is not None
        }
        if actual in parsed_reserved:
            report.error(location, f"field {name!r} uses reserved encoding {actual}")
    fields = {name: data for name, data in normalize_fields(form)}
    guard = fields.get("guard")
    if guard is not None:
        low, high = _range(guard) or (13, 18)
        guard_value = (word >> low) & ((1 << (high - low + 1)) - 1)
        if form.get("guard_policy") == "optional" and guard_value > 33:
            report.error(location, f"guard code {guard_value} is reserved")


def _validate_form(
    document: Mapping[str, Any],
    family: Mapping[str, Any],
    form: Any,
    location: str,
    class_codes: Mapping[str, int],
    formats_by_class: Mapping[str, Mapping[int, Mapping[str, Any]]],
    report: ValidationReport,
) -> tuple[str | None, tuple[Any, Any, Any] | None]:
    if not isinstance(form, Mapping):
        report.error(location, "form must be an object")
        return None, None
    form_id = form.get("id")
    if not isinstance(form_id, str) or not form_id:
        report.error(f"{location}.id", "must be a non-empty string")
        return None, None
    location = f"{location}[{form_id}]"
    fields = _validate_fields(form, f"{location}.fields", get_word_bits(document), report)
    _validate_header(document, form, fields, class_codes, location, report)
    _validate_format_registration(form, fields, formats_by_class, location, report)
    _validate_operands(document, form, fields, location, report)
    _validate_address_template(form, fields, location, report)
    _validate_matrix_contract(form, location, report)
    _validate_atomic_form(family, form, fields, location, report)
    _validate_named_form_contract(form, location, report)
    _validate_pair_syntax(form, location, report)
    _validate_call_form(form, location, report)
    _validate_state_rules(form, location, report)
    domain = form.get("execution_domain")
    if domain not in EXECUTION_DOMAINS:
        report.error(f"{location}.execution_domain", f"unknown execution domain {domain!r}")
    class_name = form.get("class")
    if class_name not in ISA_CLASSES:
        report.error(f"{location}.class", f"unknown ISA class {class_name!r}")

    if form.get("guard_policy") == "explicit_condition":
        operands = form.get("operands", [])
        if not any(
            isinstance(operand, Mapping) and operand.get("type") == "pred_cond"
            for operand in operands
        ):
            report.error(
                f"{location}.operands",
                "explicit_condition form must have a pred_cond operand",
            )
    semantic_groups = document.get("semantic_groups")
    if isinstance(semantic_groups, list) and family.get("semantic_group") not in semantic_groups:
        report.error(
            f"{location}.semantic_group",
            f"unknown family semantic group {family.get('semantic_group')!r}",
        )
    example = form.get("example")
    if isinstance(example, Mapping):
        _validate_machine_word(
            form,
            example.get("machine_word"),
            f"{location}.example.machine_word",
            report,
        )
    triple = (
        form.get("class"),
        parse_integer(form.get("format")),
        parse_integer(form.get("opcode")),
    )
    return form_id, triple


def validate_document(document: Any, schema: Any | None = None) -> ValidationReport:
    report = ValidationReport()
    if schema is not None:
        validate_schema(document, schema, report)
    if not isinstance(document, Mapping):
        report.error("$", "YAML root must be an object")
        return report
    families = get_families(document)
    if not isinstance(families, list):
        report.error("$.families", "must be a list")
        return report
    word_bits = get_word_bits(document)
    if word_bits != 64:
        report.error("$.word_bits", f"must be 64, got {word_bits}")

    class_codes, formats_by_class = _registry_maps(document, report)
    _validate_global_contracts(document, class_codes, report)
    report.family_count = len(families)
    family_ids: set[str] = set()
    families_by_id: dict[str, Mapping[str, Any]] = {}
    triples: dict[tuple[Any, Any, Any], str] = {}
    control_mnemonics: set[str] = set()
    matrix_form_count = 0
    for family_index, family in enumerate(families):
        family_location = f"$.families[{family_index}]"
        if not isinstance(family, Mapping):
            report.error(family_location, "family must be an object")
            continue
        family_id = family.get("id")
        if not isinstance(family_id, str) or not family_id:
            report.error(f"{family_location}.id", "must be a non-empty string")
            family_id = f"#{family_index + 1}"
        elif family_id in family_ids:
            report.error(f"{family_location}.id", f"duplicate family id {family_id!r}")
        family_ids.add(str(family_id))
        families_by_id[str(family_id)] = family
        family_location = f"$.families[{family_id}]"
        forms = get_forms(family)
        if not isinstance(forms, list) or not forms:
            report.error(f"{family_location}.forms", "must be a non-empty list")
            continue
        report.form_count += len(forms)
        local_ids: set[str] = set()
        atomic_names: set[tuple[Any, Any]] = set()
        for form_index, form in enumerate(forms):
            form_id, triple = _validate_form(
                document,
                family,
                form,
                f"{family_location}.forms",
                class_codes,
                formats_by_class,
                report,
            )
            if form_id is not None:
                if form_id in local_ids:
                    report.error(
                        f"{family_location}.forms[{form_index}].id",
                        f"duplicate family-local form id {form_id!r}",
                    )
                local_ids.add(form_id)
            if triple is not None:
                key = form_key(family, form) if isinstance(form, Mapping) else str(form_index)
                if triple in triples:
                    report.error(
                        f"{family_location}.forms[{form_id}].encoding",
                        f"(class,format,opcode) {triple} duplicates {triples[triple]}",
                    )
                else:
                    triples[triple] = key
            if isinstance(form, Mapping):
                domain = form.get("execution_domain")
                if domain == "warp_control" and isinstance(form.get("mnemonic"), str):
                    control_mnemonics.add(form["mnemonic"])
                if form.get("class") == "MATRIX":
                    matrix_form_count += 1
                if family.get("semantic_group") == "atomic":
                    atomic_key = (form.get("mnemonic"), form.get("encoding_format"))
                    if atomic_key in atomic_names:
                        report.error(
                            f"{family_location}.forms[{form_index}].mnemonic",
                            f"duplicate atomic mnemonic/format {atomic_key!r}",
                        )
                    atomic_names.add(atomic_key)

    counts = document.get("counts")
    if not isinstance(counts, Mapping):
        report.error("$.counts", "must be an object")
    else:
        for name, actual in (("families", report.family_count), ("forms", report.form_count)):
            declared = parse_integer(counts.get(name))
            if declared != actual:
                report.error(f"$.counts.{name}", f"declares {declared}, found {actual}")
    if control_mnemonics != REQUIRED_CONTROL_MNEMONICS:
        report.error(
            "$.families",
            f"warp-control forms must be {sorted(REQUIRED_CONTROL_MNEMONICS)}; "
            f"found {sorted(control_mnemonics)}",
        )
    if matrix_form_count != 1:
        report.error(
            "$.families",
            f"exactly one canonical MATRIX/MMA form is required; found {matrix_form_count}",
        )
    _validate_barrier_contract(document, families_by_id, report)
    return report


def validate_vectors(
    document: Mapping[str, Any],
    vectors: Any,
    report: ValidationReport,
) -> None:
    if not isinstance(vectors, Mapping):
        report.error("$vectors", "vector file root must be an object")
        return
    if vectors.get("word_bits") != 64:
        report.error("$vectors.word_bits", "must equal 64")
    entries = vectors.get("forms")
    if not isinstance(entries, list):
        report.error("$vectors.forms", "must be a list")
        return
    indexed: dict[str, Mapping[str, Any]] = {}
    family_by_key: dict[str, Mapping[str, Any]] = {}
    for family in get_families(document) or []:
        if not isinstance(family, Mapping):
            continue
        for form in get_forms(family) or []:
            if isinstance(form, Mapping):
                key = form_key(family, form)
                indexed[key] = form
                family_by_key[key] = family
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        location = f"$vectors.forms[{index}]"
        if not isinstance(entry, Mapping):
            report.error(location, "must be an object")
            continue
        key = entry.get("key")
        if not isinstance(key, str) or key not in indexed:
            report.error(f"{location}.key", f"unknown form key {key!r}")
            continue
        if key in seen:
            report.error(f"{location}.key", f"duplicate vector for {key!r}")
        seen.add(key)
        form = indexed[key]
        expected_metadata = {
            "family_id": family_by_key[key].get("id"),
            "form_id": form.get("id"),
            "class": form.get("class"),
            "format": form.get("format"),
            "opcode": form.get("opcode"),
        }
        for name, expected in expected_metadata.items():
            if entry.get(name) != expected:
                report.error(f"{location}.{name}", f"expected {expected!r}")
        machine_word = entry.get("machine_word")
        _validate_machine_word(form, machine_word, f"{location}.machine_word", report)
        example = form.get("example")
        if isinstance(example, Mapping) and machine_word != example.get("machine_word"):
            report.error(
                f"{location}.machine_word",
                "does not match the YAML example machine_word",
            )
    report.vector_count = len(seen)
    missing = sorted(set(indexed) - seen)
    extra = sorted(seen - set(indexed))
    if missing or extra:
        report.error("$vectors.forms", f"all-form coverage mismatch; missing={missing}, extra={extra}")
    declared = vectors.get("counts")
    if isinstance(declared, Mapping) and parse_integer(declared.get("forms")) != len(entries):
        report.error("$vectors.counts.forms", "does not match vector entry count")


def validate_all(
    document: Any,
    schema: Any,
    vectors: Any | None = None,
) -> ValidationReport:
    report = validate_document(document, schema)
    if isinstance(document, Mapping) and vectors is not None:
        validate_vectors(document, vectors, report)
    return report


def format_report(report: ValidationReport, source: Path) -> str:
    status = "PASS" if report.ok else "FAIL"
    lines = [
        f"ISA validation: {status}",
        f"Source: {source}",
        f"Counts: families={report.family_count}, forms={report.form_count}, "
        f"vectors={report.vector_count}",
    ]
    if report.errors:
        lines.append(f"Errors ({len(report.errors)}):")
        lines.extend(f"  ERROR: {message}" for message in report.errors)
    if report.warnings:
        lines.append(f"Warnings ({len(report.warnings)}):")
        lines.extend(f"  WARNING: {message}" for message in report.warnings)
    if not report.errors and not report.warnings:
        lines.append("No validation issues found.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("yaml_path", nargs="?", type=Path, default=DEFAULT_ISA)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    parser.add_argument("--no-vectors", action="store_true", help="skip all-form vectors")
    args = parser.parse_args(argv)
    source = args.yaml_path.expanduser().resolve()
    try:
        document = load_isa(source)
        schema = load_json(args.schema.expanduser().resolve())
        vectors = None if args.no_vectors else load_json(args.vectors.expanduser().resolve())
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        print(f"ISA validation: FAIL\nERROR: {exc}", file=sys.stderr)
        return 2
    report = validate_all(document, schema, vectors)
    print(format_report(report, source), file=sys.stdout if report.ok else sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
