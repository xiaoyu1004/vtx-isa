#!/usr/bin/env python3
"""Regenerate the conformance encoding vectors from the machine description.

The ``forms`` section holds one golden word per form, taken from the form's
authored example.  The ``mixed_source`` section additionally covers every
non-zero scalar-source selector code, which the ``forms`` words never exercise
because each example encodes the all-VGPR case.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import isa_model
from isa_model import SELECTOR_LAYOUT, mixed_source_operands

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ISA = REPO_ROOT / "isa" / "vtx1" / "isa.yaml"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "conformance" / "encoding_vectors.json"
SCHEMA_VERSION = "1.0-draft"


def _field_map(form: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(field["name"]): field
        for field in form.get("fields") or []
        if isinstance(field, Mapping) and field.get("name")
    }


def _place(word: int, field: Mapping[str, Any], value: int) -> int:
    lsb = int(field["lsb"])
    mask = ((1 << int(field["width"])) - 1) << lsb
    return (word & ~mask) | ((value << lsb) & mask)


def _hex(word: int) -> str:
    return "0x%016X" % word


def form_entry(family: Mapping[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
    example = form.get("example") or {}
    return {
        "key": f"{family.get('id')}/{form.get('id')}",
        "family_id": family.get("id"),
        "form_id": form.get("id"),
        "class": form.get("class"),
        "format": form.get("format"),
        "opcode": form.get("opcode"),
        "machine_word": example.get("machine_word"),
    }


def selector_entries(
    family: Mapping[str, Any],
    form: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """One vector per reachable non-zero selector code of a mixed-source form."""
    mixed = mixed_source_operands(form)
    layout = SELECTOR_LAYOUT.get(str(form.get("encoding_format")))
    if not mixed or layout is None:
        return []
    selector_name, choices = layout
    fields = _field_map(form)
    selector = fields.get(selector_name)
    example = form.get("example") or {}
    base = example.get("machine_word")
    if selector is None or not isinstance(base, str):
        return []
    mixed_fields = {str(operand.get("field")) for operand in mixed}
    base_word = int(base, 16)
    entries: list[dict[str, Any]] = []
    for code in sorted(choices):
        field_name = choices[code]
        if field_name not in mixed_fields:
            continue
        entries.append(
            {
                "key": f"{family.get('id')}/{form.get('id')}#{selector_name}={code}",
                "family_id": family.get("id"),
                "form_id": form.get("id"),
                "selector_field": selector_name,
                "selector_code": code,
                "scalar_field": field_name,
                "machine_word": _hex(_place(base_word, selector, code)),
            }
        )
    return entries


def build(document: Mapping[str, Any]) -> dict[str, Any]:
    forms: list[dict[str, Any]] = []
    mixed: list[dict[str, Any]] = []
    for family, form in isa_model.iter_forms(document):
        forms.append(form_entry(family, form))
        mixed.extend(selector_entries(family, form))
    return {
        "schema_version": SCHEMA_VERSION,
        "word_bits": 64,
        "counts": {"forms": len(forms), "mixed_source": len(mixed)},
        "forms": forms,
        "mixed_source": mixed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isa", type=Path, default=DEFAULT_ISA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    document = isa_model.load_isa(args.isa)
    vectors = build(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(vectors, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        "wrote %s: %d forms, %d mixed-source vectors"
        % (args.output, vectors["counts"]["forms"], vectors["counts"]["mixed_source"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
