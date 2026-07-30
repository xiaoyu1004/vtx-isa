from __future__ import annotations

import copy
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_spec  # noqa: E402
import isa_model  # noqa: E402
import validate_isa  # noqa: E402


FAMILY_COUNT = 66
FORM_COUNT = 379


class IsaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = validate_isa.load_isa(validate_isa.DEFAULT_ISA)
        cls.expanded = isa_model.expand_document(copy.deepcopy(cls.document))
        cls.schema = validate_isa.load_json(validate_isa.DEFAULT_SCHEMA)
        cls.vectors = validate_isa.load_json(validate_isa.DEFAULT_VECTORS)
        cls.report = validate_isa.validate_all(cls.document, cls.schema, cls.vectors)

    def forms(self, document=None):
        source = self.expanded if document is None else document
        return [form for _, form in isa_model.iter_forms(source)]

    def find_form(self, document, **match):
        for _, form in isa_model.iter_forms(document):
            if all(form.get(name) == value for name, value in match.items()):
                return form
        raise AssertionError(f"no form matches {match}")

    def test_repository_isa_passes_schema_and_semantic_validation(self) -> None:
        self.assertTrue(self.report.ok, "\n".join(self.report.errors))

    def test_counts_are_read_from_yaml_and_match_actual_inventory(self) -> None:
        declared = self.document["counts"]
        self.assertEqual(
            (declared["families"], declared["forms"]),
            (FAMILY_COUNT, FORM_COUNT),
        )
        self.assertEqual(
            (self.report.family_count, self.report.form_count),
            (declared["families"], declared["forms"]),
        )

    def test_every_form_has_a_unique_golden_vector(self) -> None:
        form_keys = {
            validate_isa.form_key(family, form)
            for family, form in isa_model.iter_forms(self.document)
        }
        vector_keys = {entry["key"] for entry in self.vectors["forms"]}
        self.assertEqual(vector_keys, form_keys)
        self.assertEqual(len(vector_keys), FORM_COUNT)
        self.assertEqual(self.report.vector_count, FORM_COUNT)
        for entry in self.vectors["forms"]:
            self.assertRegex(entry["machine_word"], r"^0x[0-9A-F]{16}$")

    def test_every_form_has_unique_encoding_triple(self) -> None:
        triples = [
            (form["class"], form["format"], form["opcode"]) for form in self.forms()
        ]
        self.assertEqual(len(triples), len(set(triples)))

    def test_seven_execution_domains_eight_classes_and_memory_formats(self) -> None:
        domains = {
            form["execution_domain"]
            for family in self.document["families"]
            for form in family["forms"]
        }
        classes = {entry["name"] for entry in self.document["class_registry"]}
        self.assertEqual(domains, validate_isa.EXECUTION_DOMAINS)
        self.assertEqual(classes, validate_isa.ISA_CLASSES)
        memory_formats = {
            entry["name"]: entry["code"]
            for entry in self.document["format_registry"]["MEMORY"]
        }
        self.assertEqual(memory_formats["SMEMX"], 6)
        self.assertEqual(memory_formats["VATOMX"], 7)

    def test_fault_priority_and_vp_shape(self) -> None:
        fault_names = {fault["name"] for fault in self.document["faults"]}
        priority = self.document["fault_priority"]
        self.assertEqual(set(priority), fault_names - {"DEADLOCK"})
        self.assertEqual(len(priority), len(set(priority)))
        vp = self.document["operand_types"]["vpred"]
        self.assertEqual(
            (vp["bits"], vp["element_bits"], vp["register_file"]),
            (32, 1, "VPRED"),
        )

        broken_priority = copy.deepcopy(self.document)
        broken_priority["fault_priority"].append(broken_priority["fault_priority"][0])
        report = validate_isa.validate_document(broken_priority)
        self.assertTrue(any("fault names must be unique" in error for error in report.errors))

        broken_vp = copy.deepcopy(self.document)
        broken_vp["operand_types"]["vpred"]["element_bits"] = 2
        report = validate_isa.validate_document(broken_vp)
        self.assertTrue(any("operand_types.vpred.element_bits" in error for error in report.errors))

    def test_v_mov_reg_forms_take_a_mixed_source(self) -> None:
        """V_MOV replaces the deleted V_BCAST: ssrc picks the source file."""
        forms = {
            (form["mnemonic"], form["encoding_format"]): form
            for form in self.forms()
            if form["mnemonic"] in {"V_MOV.B32", "V_MOV.B64"}
        }
        for mnemonic, source_type in (("V_MOV.B32", "vsrc32"), ("V_MOV.B64", "vsrc64")):
            form = forms[(mnemonic, "V1")]
            operand_types = {
                operand["name"]: operand["type"] for operand in form["operands"]
            }
            self.assertEqual(form["execution_domain"], "vector")
            self.assertEqual(form["guard_policy"], "optional")
            self.assertEqual(operand_types["src"], source_type)
            selector = next(
                field for field in form["fields"] if field["name"] == "ssrc"
            )
            self.assertEqual(selector["kind"], isa_model.SELECTOR_KIND)
            self.assertNotIn("fixed", selector)
            self.assertNotIn("must_zero", selector)

        for mnemonic in ("V_BCAST.B32", "V_BCAST.B64"):
            self.assertNotIn(
                mnemonic, {form["mnemonic"] for form in self.forms()}
            )

    def test_b64_cross_domain_transfer_forms(self) -> None:
        forms = {
            form["mnemonic"]: form
            for form in self.forms()
            if form["mnemonic"] == "S_READFIRST.B64"
        }
        readfirst_types = {
            operand["name"]: operand["type"] for operand in forms["S_READFIRST.B64"]["operands"]
        }
        self.assertEqual(
            (
                forms["S_READFIRST.B64"]["execution_domain"],
                forms["S_READFIRST.B64"]["required_state"],
                readfirst_types["src"],
                readfirst_types["dst"],
            ),
            ("scalar", "scalar_ready", "vgpr64", "sgpr64"),
        )

        broken = copy.deepcopy(self.document)
        form = self.find_form(broken, mnemonic="S_READFIRST.B64")
        next(operand for operand in form["operands"] if operand["name"] == "src")[
            "type"
        ] = "sgpr64"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("VGPR64-to-SGPR64" in error for error in report.errors))

    def test_mixed_source_selector_model(self) -> None:
        """Each vector format exposes one selector that names at most one SGPR."""
        mixed = [form for form in self.forms() if isa_model.mixed_source_operands(form)]
        self.assertEqual(len(mixed), 90)
        self.assertEqual(
            {form["encoding_format"] for form in mixed},
            set(isa_model.SELECTOR_LAYOUT),
        )
        for form in mixed:
            selector_name, choices = isa_model.SELECTOR_LAYOUT[form["encoding_format"]]
            selector = next(
                field for field in form["fields"] if field["name"] == selector_name
            )
            self.assertEqual(selector["width"], 1 if form["encoding_format"] == "V1" else 2)
            self.assertNotIn("fixed", selector)
            self.assertNotIn("must_zero", selector)
            bound = {
                operand["field"] for operand in isa_model.mixed_source_operands(form)
            }
            self.assertTrue(bound.issubset(set(choices.values())))

        for form in self.forms():
            if isa_model.mixed_source_operands(form):
                continue
            layout = isa_model.SELECTOR_LAYOUT.get(form["encoding_format"])
            if layout is None:
                continue
            selector = next(
                field for field in form["fields"] if field["name"] == layout[0]
            )
            self.assertTrue(selector.get("must_zero") or selector.get("fixed") == 0)

    def test_mixed_source_selector_negatives(self) -> None:
        no_selector_format = copy.deepcopy(self.document)
        form = self.find_form(no_selector_format, mnemonic="S_READFIRST.B32")
        next(operand for operand in form["operands"] if operand["name"] == "src")[
            "type"
        ] = "vsrc32"
        report = validate_isa.validate_document(no_selector_format)
        self.assertTrue(
            any("has no source selector" in error for error in report.errors)
        )

        write_access = copy.deepcopy(self.document)
        form = self.find_form(write_access, mnemonic="V_MOV.B32", encoding_format="V1")
        next(operand for operand in form["operands"] if operand["name"] == "src")[
            "access"
        ] = "read_write"
        report = validate_isa.validate_document(write_access)
        self.assertTrue(any("must be read-only" in error for error in report.errors))

    def test_mixed_source_vectors_cover_every_selector_code(self) -> None:
        entries = self.vectors["mixed_source"]
        self.assertEqual(len(entries), self.vectors["counts"]["mixed_source"])
        expected: set[str] = set()
        for family, form in isa_model.iter_forms(self.expanded):
            layout = isa_model.SELECTOR_LAYOUT.get(form["encoding_format"])
            operands = isa_model.mixed_source_operands(form)
            if not operands or layout is None:
                continue
            selector_name, choices = layout
            bound = {operand["field"] for operand in operands}
            key = validate_isa.form_key(family, form)
            for code, field_name in choices.items():
                if field_name in bound:
                    expected.add(f"{key}#{selector_name}={code}")
        self.assertEqual({entry["key"] for entry in entries}, expected)
        for entry in entries:
            self.assertGreater(entry["selector_code"], 0)
            self.assertRegex(entry["machine_word"], r"^0x[0-9A-F]{16}$")

        broken = copy.deepcopy(self.vectors)
        broken["mixed_source"][0]["selector_code"] = 0
        report = validate_isa.ValidationReport()
        validate_isa.validate_vectors(self.expanded, broken, report)
        self.assertTrue(
            any("selector_code" in error for error in report.errors),
            "\n".join(report.errors),
        )

    def test_bar_sync_is_the_only_barrier_instruction(self) -> None:
        barrier_families = [
            family
            for family in self.expanded["families"]
            if family["semantic_group"] == "barrier"
        ]
        self.assertEqual(len(barrier_families), 1)
        family = barrier_families[0]
        expected = validate_isa.BARRIER_FORM
        self.assertEqual(family["id"], expected["family_id"])
        self.assertEqual(family["mnemonic"], expected["family_mnemonic"])
        self.assertEqual(len(family["forms"]), 1)

        form = family["forms"][0]
        self.assertEqual(form["mnemonic"], expected["form_mnemonic"])
        self.assertEqual(
            (form["class"], form["format"], form["opcode"]), expected["triple"]
        )
        self.assertEqual(
            (form["example"]["assembly"], form["example"]["machine_word"]),
            (expected["assembly"], expected["machine_word"]),
        )
        self.assertEqual(
            [
                (o["name"], o["type"], o["access"], o["field"])
                for o in form["operands"]
            ],
            expected["operands"],
        )
        covered_bits = {
            bit
            for field in form["fields"]
            for bit in range(field["lsb"], field["lsb"] + field["width"])
        }
        self.assertEqual(covered_bits, set(range(64)))

        base_word = int(form["example"]["machine_word"], 16) & ~(0x7 << 51)
        for slot in (0, 7):
            word = f"0x{base_word | (slot << 51):016X}"
            report = validate_isa.ValidationReport()
            validate_isa._validate_machine_word(form, word, "$slot", report)
            self.assertTrue(report.ok, "\n".join(report.errors))

        mnemonics = {form["mnemonic"] for form in self.forms()}
        self.assertTrue(mnemonics.isdisjoint({"BAR.ARRIVE.CTA", "BAR.WAIT.CTA"}))
        self.assertNotIn("barrier_token", self.document["operand_types"])

    def test_barrier_contract_is_the_simplified_model(self) -> None:
        contract = self.document["barrier_contract"]
        self.assertEqual(set(contract), {
            "owner_identity",
            "live_owner_set",
            "wait_record_fields",
            "max_blocked_records_per_warp",
            "idle_slot",
        })
        self.assertEqual(contract["owner_identity"]["name"], "linear_tid")
        self.assertEqual(contract["live_owner_set"]["shrinks_on"], "EXIT")
        self.assertFalse(contract["live_owner_set"]["exit_contributes_release"])
        self.assertEqual(
            contract["wait_record_fields"],
            ["warp_id", "owner_snapshot", "resume_pc"],
        )
        self.assertEqual(contract["max_blocked_records_per_warp"], 1)
        self.assertEqual(
            contract["idle_slot"], {"arrived_set_empty": True, "waiters_empty": True}
        )

        form = self.find_form(self.expanded, mnemonic="BAR.SYNC.CTA")
        text = " ".join([form["semantics"], *form["constraints"]])
        for fragment in validate_isa.BARRIER_REQUIRED_TEXT:
            self.assertIn(fragment, text)

    def test_barrier_negative_contracts(self) -> None:
        renamed = copy.deepcopy(self.document)
        family = next(
            family for family in renamed["families"] if family["id"] == "bar-sync"
        )
        family["mnemonic"] = "BARRIER.SYNC"
        family["forms"][0]["mnemonic"] = "BARRIER.SYNC.CTA"
        report = validate_isa.validate_document(renamed)
        self.assertTrue(
            any("BAR.SYNC" in error for error in report.errors),
            "\n".join(report.errors),
        )

        wrong_opcode = copy.deepcopy(self.document)
        self.find_form(wrong_opcode, mnemonic="BAR.SYNC.CTA")["opcode"] = 2
        report = validate_isa.validate_document(wrong_opcode)
        self.assertFalse(report.ok)

        wrong_owner = copy.deepcopy(self.document)
        wrong_owner["barrier_contract"]["owner_identity"]["name"] = "lane_id"
        report = validate_isa.validate_document(wrong_owner)
        self.assertTrue(
            any("barrier_contract.owner_identity" in error for error in report.errors)
        )

        exit_releases = copy.deepcopy(self.document)
        exit_releases["barrier_contract"]["live_owner_set"][
            "exit_contributes_release"
        ] = True
        report = validate_isa.validate_document(exit_releases)
        self.assertTrue(
            any("barrier_contract.live_owner_set" in error for error in report.errors)
        )

    def test_faults_drop_barrier_and_rename_divergence(self) -> None:
        names = [fault["name"] for fault in self.document["faults"]]
        self.assertIn("DIVERGENCE_FAULT", names)
        self.assertNotIn("SCALAR_STATE_FAULT", names)
        self.assertNotIn("BARRIER_FAULT", names)
        self.assertEqual(names, sorted(names, key=lambda name: names.index(name)))
        self.assertEqual(
            [fault["code"] for fault in self.document["faults"]],
            list(range(1, len(names) + 1)),
        )
        self.assertEqual(set(self.document["fault_priority"]), set(names) - {"DEADLOCK"})

    def test_docs08_barrier_static_gate(self) -> None:
        text = (ROOT / "docs" / "08-conformance.md").read_text(encoding="utf-8")
        for required in (
            "BAR.SYNC.CTA id",
            "live_owner_set",
            "DIVERGENCE_FAULT",
            "(class=5, format=0, opcode=4) 未分配",
            "(class=5, format=0, opcode=5) 未分配",
        ):
            self.assertIn(required, text)

        # The deleted names may only survive as things the suite must reject or
        # must prove absent, never as behaviour the suite still exercises.
        for line in text.splitlines():
            if "BAR.ARRIVE" in line or "BAR.WAIT" in line:
                self.assertIn("拒绝", line)
            if "generation" in line or "consumed_set" in line or "SPLIT" in line:
                self.assertIn("不存在", line)

    def test_prose_names_the_memory_ordering_family_as_the_manifest_does(self) -> None:
        """Prose drifted to MEMBAR once while the manifest said FENCE."""
        mnemonics = {form["mnemonic"] for form in self.forms()}
        self.assertTrue(
            {"FENCE.CTA", "FENCE.DEVICE", "FENCE.SYSTEM"}.issubset(mnemonics)
        )

        sources = sorted((ROOT / "docs").glob("*.md"))
        sources += [ROOT / "README.md", ROOT / "REVIEW.md", ROOT / "CHANGELOG.md"]
        for source in sources:
            for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "MEMBAR" in line:
                    self.assertIn("拒绝", line, f"{source.name}:{number}")

    def test_x_broadcast_register_and_immediate_lane_forms(self) -> None:
        forms = [
            form for form in self.forms() if form["mnemonic"] == "X_BROADCAST.B32"
        ]
        self.assertEqual(len(forms), 2)
        lane_types = {
            next(operand["type"] for operand in form["operands"] if operand["name"] == "lane")
            for form in forms
        }
        self.assertEqual(lane_types, {"sgpr32", "uimm8"})
        self.assertTrue(
            all(
                form["execution_domain"] == "warp_collective"
                and form["guard_policy"] == "required_pt"
                for form in forms
            )
        )

    def test_shuffle_down_register_and_immediate_delta_forms(self) -> None:
        forms = [
            form for form in self.forms() if form["mnemonic"] == "V_SHUFFLE.DOWN.B32"
        ]
        self.assertEqual(len(forms), 2)
        by_delta_type = {
            next(
                operand["type"]
                for operand in form["operands"]
                if operand["name"] == "lane_or_delta"
            ): form
            for form in forms
        }
        self.assertEqual(set(by_delta_type), {"vgpr32", "uimm8"})
        self.assertEqual(by_delta_type["vgpr32"]["opcode"], 11)
        self.assertEqual(by_delta_type["uimm8"]["opcode"], 13)
        self.assertEqual(
            next(
                operand["field"]
                for operand in by_delta_type["uimm8"]["operands"]
                if operand["name"] == "lane_or_delta"
            ),
            "vb",
        )
        self.assertEqual(
            by_delta_type["uimm8"]["example"],
            {
                "assembly": "V_SHUFFLE.DOWN.B32 v0, v0, 0, 32",
                "machine_word": "0x0100000000000686",
            },
        )
        self.assertTrue(
            all(
                form["execution_domain"] == "warp_collective"
                and form["guard_policy"] == "required_pt"
                for form in forms
            )
        )

        broken = copy.deepcopy(self.document)
        immediate = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if family["id"] == "v-shuffle" and form["id"] == "down_imm"
        )
        next(
            operand
            for operand in immediate["operands"]
            if operand["name"] == "lane_or_delta"
        )["field"] = "smask"
        report = validate_isa.validate_document(broken)
        self.assertTrue(
            any(
                "V_SHUFFLE.DOWN.B32 must use opcode 11" in error
                for error in report.errors
            )
        )

    def test_schema_validation_is_executed(self) -> None:
        broken = copy.deepcopy(self.document)
        del broken["status"]
        report = validate_isa.validate_document(broken, self.schema)
        self.assertTrue(any("schema:" in error and "status" in error for error in report.errors))

    def test_duplicate_encoding_triple_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        forms = [form for _, form in isa_model.iter_forms(broken)]
        first, second = forms[:2]
        second["encoding_format"] = first["encoding_format"]
        second["opcode"] = first["opcode"]
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("duplicates" in error and "class,format,opcode" in error for error in report.errors))

    def test_unknown_encoding_format_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        broken["families"][1]["forms"][0]["encoding_format"] = "NOPE"
        report = validate_isa.validate_document(broken)
        self.assertTrue(
            any("encoding_format" in error for error in report.errors),
            "\n".join(report.errors),
        )

    def test_derived_form_keys_must_not_be_authored(self) -> None:
        for key, value in (("class", "SALU"), ("format", 0), ("fields", [])):
            broken = copy.deepcopy(self.document)
            broken["families"][0]["forms"][0][key] = value
            report = validate_isa.validate_document(broken)
            self.assertTrue(
                any(
                    "derived from format_registry" in error and key in error
                    for error in report.errors
                ),
                key,
            )

    def test_mixed_memory_formats_use_registered_fields(self) -> None:
        registry = {
            entry["name"]: [field["name"] for field in entry["fields"]]
            for entry in self.document["format_registry"]["MEMORY"]
        }
        mixed = [
            form
            for form in self.forms()
            if form["encoding_format"] in {"SMEMX", "VATOMX"}
        ]
        self.assertEqual(
            {form["encoding_format"] for form in mixed},
            {"SMEMX", "VATOMX"},
        )
        self.assertEqual(len(mixed), 22)
        for form in mixed:
            payload = [field["name"] for field in form["fields"] if field["lsb"] >= 19]
            self.assertEqual(payload, registry[form["encoding_format"]])

        broken = copy.deepcopy(self.document)
        entry = next(
            entry
            for entry in broken["format_registry"]["MEMORY"]
            if entry["name"] == "SMEMX"
        )
        next(field for field in entry["fields"] if field["name"] == "sindex")[
            "name"
        ] = "vindex"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("unknown form field" in error for error in report.errors))

    def test_vmem_address_mode_contracts(self) -> None:
        forms = [form for form in self.forms() if form["encoding_format"] == "VMEM"]
        self.assertEqual(len(forms), 24)
        expected_fields = {
            "uniform_base": ["sbase", "simm16"],
            "lane_address": ["vaddr", "simm16"],
            "sv_mix": ["sbase", "vaddr", "simm16"],
        }
        self.assertEqual(
            {form["address_template"]["mode"] for form in forms},
            set(expected_fields),
        )
        for form in forms:
            template = form["address_template"]
            self.assertEqual(template["address_operands"], expected_fields[template["mode"]])
            self.assertEqual(template["offset_unit"], "bytes")
            self.assertEqual(template["scale"], 1)
            if template["mode"] == "sv_mix":
                self.assertIn("zero_extend(vaddr)", template["expression"])

        def sv_mix(document):
            return next(
                form
                for _, form in isa_model.iter_forms(document)
                if form["encoding_format"] == "VMEM"
                and form.get("address_template", {}).get("mode") == "sv_mix"
            )

        broken = copy.deepcopy(self.document)
        form = sv_mix(broken)
        form["address_template"]["address_operands"] = ["sbase", "simm16"]
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("VMEM sv_mix requires" in error for error in report.errors))

        broken_extension = copy.deepcopy(self.document)
        form = sv_mix(broken_extension)
        form["address_template"]["expression"] = form["address_template"][
            "expression"
        ].replace("zero_extend(vaddr)", "sign_extend(vaddr)")
        report = validate_isa.validate_document(broken_extension)
        self.assertTrue(any("must zero_extend(vaddr)" in error for error in report.errors))

    def test_register_pair_syntax_is_adjacent_and_even(self) -> None:
        pair_forms = [
            form
            for form in self.forms()
            if validate_isa.REGISTER_PAIR_RE.search(form["syntax"])
        ]
        self.assertGreater(len(pair_forms), 100)
        for form in pair_forms:
            for match in validate_isa.REGISTER_PAIR_RE.finditer(form["syntax"]):
                first_prefix, first_text, second_prefix, second_text = match.groups()
                self.assertEqual(first_prefix, second_prefix)
                self.assertEqual(int(first_text) % 2, 0)
                self.assertEqual(int(second_text), int(first_text) + 1)

        broken = copy.deepcopy(self.document)
        form = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if "s0:s1" in form["syntax"]
        )
        form["syntax"] = form["syntax"].replace("s0:s1", "s0:s2", 1)
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("even base and adjacent registers" in error for error in report.errors))

    def test_atomic_modifier_sets_and_cas_contract(self) -> None:
        memory_scope = self.document["operand_types"]["memory_scope"]
        self.assertEqual(memory_scope["values"], ["CTA", "DEVICE", "SYSTEM"])
        self.assertEqual(memory_scope["reserved_values"], [3])
        atomic_forms = [
            form
            for family, form in isa_model.iter_forms(self.expanded)
            if family["semantic_group"] == "atomic"
        ]
        self.assertEqual(len(atomic_forms), 100)
        for form in atomic_forms:
            segments = form["mnemonic"].split(".")
            expected_orders = (
                {"RELAXED", "ACQUIRE"}
                if "LOAD" in segments
                else {"RELAXED", "RELEASE"}
                if "STORE" in segments
                else validate_isa.ATOMIC_ORDERS
            )
            expected_scopes = {"CTA"} if "SHARED" in segments else validate_isa.ATOMIC_SCOPES
            self.assertEqual(set(form["legal_orders"]), expected_orders)
            self.assertEqual(set(form["legal_scopes"]), expected_scopes)
            self.assertIn(".{order}.{scope} ", form["syntax"])
            order_field = next(field for field in form["fields"] if field["name"] == "order")
            scope_field = next(field for field in form["fields"] if field["name"] == "scope")
            self.assertNotIn("fixed", order_field)
            self.assertNotIn("fixed", scope_field)
            self.assertEqual(scope_field["reserved_values"], [3])
            field_values = form["example"]["field_values"]
            order_name = next(
                name
                for name, code in validate_isa.ATOMIC_ORDER_CODES.items()
                if code == field_values["order"]
            )
            scope_name = next(
                name
                for name, code in validate_isa.ATOMIC_SCOPE_CODES.items()
                if code == field_values["scope"]
            )
            self.assertIn(order_name, expected_orders)
            self.assertIn(scope_name, expected_scopes)
            self.assertIn(f".{order_name}.{scope_name} ", form["example"]["assembly"])
            if "CAS" in segments:
                names = {operand["name"] for operand in form["operands"]}
                self.assertTrue({"compare", "replacement"}.issubset(names))

        def first_atomic(document):
            return next(
                form
                for family, form in isa_model.iter_forms(document)
                if family["semantic_group"] == "atomic"
            )

        def registry_field(document, format_name, field_name):
            for entries in document["format_registry"].values():
                for entry in entries:
                    if entry.get("name") != format_name:
                        continue
                    for field in entry["fields"]:
                        if field["name"] == field_name:
                            return field
            raise AssertionError(f"{format_name}.{field_name} not registered")

        broken_order = copy.deepcopy(self.document)
        first_atomic(broken_order)["legal_orders"] = ["RELAXED", "BOGUS"]
        report = validate_isa.validate_document(broken_order)
        self.assertTrue(any(".legal_orders" in error for error in report.errors))

        # Dropping the operand binding turns `order` into a must-zero hole, which
        # is the only way the derived layout can pin an atomic modifier.
        broken_modifier = copy.deepcopy(self.document)
        atomic = first_atomic(broken_modifier)
        atomic["operands"] = [
            operand for operand in atomic["operands"] if operand["name"] != "order"
        ]
        report = validate_isa.validate_document(broken_modifier)
        self.assertTrue(any("runtime-selectable, not fixed" in error for error in report.errors))

        broken_scope = copy.deepcopy(self.document)
        atomic = first_atomic(broken_scope)
        registry_field(broken_scope, atomic["encoding_format"], "scope")[
            "reserved_values"
        ] = [2]
        report = validate_isa.validate_document(broken_scope)
        self.assertTrue(any("classify encoding 3 as reserved" in error for error in report.errors))

        broken_example = copy.deepcopy(self.document)
        first_atomic(broken_example)["example"]["field_values"]["scope"] = 3
        report = validate_isa.validate_document(broken_example)
        self.assertTrue(any("reserved or outside legal_scopes" in error for error in report.errors))

        broken_cas = copy.deepcopy(self.document)
        cas = next(
            form
            for _, form in isa_model.iter_forms(broken_cas)
            if ".CAS." in form["mnemonic"]
        )
        cas["operands"] = [
            operand for operand in cas["operands"] if operand["name"] != "replacement"
        ]
        report = validate_isa.validate_document(broken_cas)
        self.assertTrue(any("CAS requires distinct compare" in error for error in report.errors))

    def test_unique_mma_contract_and_negative_participation(self) -> None:
        forms = [form for form in self.forms() if form["class"] == "MATRIX"]
        self.assertEqual(len(forms), 1)
        form = forms[0]
        contract = form["matrix_contract"]
        self.assertEqual(form["execution_domain"], "warp_matrix")
        self.assertEqual(contract["shape"], {"m": 16, "n": 8, "k": 16})
        self.assertEqual(
            contract["element_types"],
            {"A": "F16", "B": "F16", "C": "F32", "D": "F32"},
        )
        self.assertEqual(set(contract["fragments"]), {"A", "B", "C", "D"})
        self.assertEqual(contract["participation"]["required_live_lanes"], 32)
        self.assertTrue(contract["participation"]["required_exec_equals_live"])

        broken = copy.deepcopy(self.document)
        matrix = self.find_form(broken, encoding_format="MMA")
        matrix["matrix_contract"]["participation"]["required_live_lanes"] = 16
        report = validate_isa.validate_document(broken)
        self.assertTrue(
            any("required_live_lanes" in error and "must equal 32" in error for error in report.errors)
        )

        duplicate = copy.deepcopy(self.document)
        matrix_family = next(
            family
            for family in duplicate["families"]
            if any(form["encoding_format"] == "MMA" for form in family["forms"])
        )
        clone = copy.deepcopy(matrix_family["forms"][0])
        clone["id"] += ".duplicate"
        clone["opcode"] = 1
        matrix_family["forms"].append(clone)
        duplicate["counts"]["forms"] += 1
        report = validate_isa.validate_document(duplicate)
        self.assertTrue(any("exactly one canonical MATRIX/MMA" in error for error in report.errors))

    def test_full_64_bit_coverage_negative(self) -> None:
        """A registry layout that leaves a hole must be rejected for every form."""
        broken = copy.deepcopy(self.document)
        entry = next(
            entry
            for entry in broken["format_registry"]["SYS"]
            if entry["name"] == "SYS"
        )
        entry["fields"][-1]["width"] -= 1
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("does not cover all 64 bits" in error for error in report.errors))

    def test_required_pt_guard_is_derived_from_guard_policy(self) -> None:
        form = next(
            form for form in self.forms() if form["guard_policy"] == "required_pt"
        )
        guard = next(field for field in form["fields"] if field["name"] == "guard")
        self.assertEqual(guard["fixed"], 0)
        optional = next(
            form for form in self.forms() if form["guard_policy"] == "optional"
        )
        optional_guard = next(
            field for field in optional["fields"] if field["name"] == "guard"
        )
        self.assertNotIn("fixed", optional_guard)

    def test_scalar_ready_rule_negative(self) -> None:
        broken_state = copy.deepcopy(self.document)
        scalar = self.find_form(broken_state, execution_domain="scalar")
        scalar["required_state"] = "none"
        report = validate_isa.validate_document(broken_state)
        self.assertTrue(any("all scalar forms require scalar_ready" in error for error in report.errors))

    def test_control_inventory_and_scalar_state_requirements(self) -> None:
        forms = {
            form["mnemonic"]: form
            for form in self.forms()
            if form["execution_domain"] == "warp_control"
        }
        self.assertEqual(set(forms), validate_isa.REQUIRED_CONTROL_MNEMONICS)
        self.assertTrue(
            all(
                forms[mnemonic]["required_state"] == "scalar_ready"
                for mnemonic in validate_isa.SCALAR_CONTROL_MNEMONICS
            )
        )

        broken = copy.deepcopy(self.document)
        call = self.find_form(broken, mnemonic="CALL")
        call["required_state"] = "none"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("CALL control form requires scalar_ready" in error for error in report.errors))

    def test_call_descriptor_and_stack_operand_rules(self) -> None:
        architectural_depth = self.document["architectural_limits"]["call_stack_depth"]
        descriptor = self.document["descriptor_contract"]["call_stack_depth"]
        self.assertEqual(descriptor, {"minimum": 0, "maximum": architectural_depth})
        self.assertEqual(architectural_depth, 16)

        forms = {
            form["mnemonic"]: form
            for form in self.forms()
            if form["mnemonic"] in validate_isa.SCALAR_CONTROL_MNEMONICS
        }
        for mnemonic in ("CALL", "CALL.IND", "RET"):
            stack = next(
                operand for operand in forms[mnemonic]["operands"] if operand["name"] == "call_stack"
            )
            self.assertEqual(
                (stack["type"], stack["access"], stack["implicit"]),
                ("call_stack", "read_write", True),
            )
        self.assertFalse(
            any(operand["name"] == "call_stack" for operand in forms["JUMP.IND"]["operands"])
        )
        ssy = self.find_form(self.expanded, mnemonic="SSY")
        ssy_stack = next(
            operand for operand in ssy["operands"] if operand["name"] == "call_stack"
        )
        self.assertEqual(
            (ssy_stack["type"], ssy_stack["access"], ssy_stack["implicit"]),
            ("call_stack", "read", True),
        )

        broken_descriptor = copy.deepcopy(self.document)
        broken_descriptor["descriptor_contract"]["call_stack_depth"]["maximum"] = 15
        report = validate_isa.validate_document(broken_descriptor)
        self.assertTrue(any("must equal architectural call_stack_depth" in error for error in report.errors))

        broken_call = copy.deepcopy(self.document)
        call = self.find_form(broken_call, mnemonic="CALL")
        call["operands"] = [
            operand for operand in call["operands"] if operand["name"] != "call_stack"
        ]
        report = validate_isa.validate_document(broken_call)
        self.assertTrue(any("implicit read_write call_stack" in error for error in report.errors))

        broken_ssy = copy.deepcopy(self.document)
        ssy = self.find_form(broken_ssy, mnemonic="SSY")
        next(operand for operand in ssy["operands"] if operand["name"] == "call_stack")[
            "access"
        ] = "read_write"
        report = validate_isa.validate_document(broken_ssy)
        self.assertTrue(any("SSY requires an implicit read-only" in error for error in report.errors))

    def test_sgpr_vgpr_and_vpred_field_reference_negative(self) -> None:
        for operand_type in ("sgpr32", "vgpr32", "vpred", "vsrc32"):
            broken = copy.deepcopy(self.document)
            operand = next(
                operand
                for _, form in isa_model.iter_forms(broken)
                for operand in form["operands"]
                if operand["type"] == operand_type
            )
            operand["field"] = "missing_field"
            report = validate_isa.validate_document(broken)
            self.assertTrue(
                any("unknown form field" in error for error in report.errors),
                operand_type,
            )

    def test_machine_word_fixed_and_must_zero_negative(self) -> None:
        broken_vectors = copy.deepcopy(self.vectors)
        broken_vectors["forms"][0]["machine_word"] = "0xFFFFFFFFFFFFFFFF"
        report = validate_isa.ValidationReport()
        validate_isa.validate_vectors(self.expanded, broken_vectors, report)
        self.assertTrue(any("encodes" in error and "expected" in error for error in report.errors))

    def test_appendix_contains_new_form_metadata(self) -> None:
        appendix = build_spec.render_instruction_reference(self.expanded, self.vectors)
        for text in (
            "执行域",
            "编码格式",
            "语义组",
            "(class, format, opcode)",
            "Guard policy",
            "Required state",
            "Operands",
            "Semantics",
            "Faults",
            "64 位机器字",
        ):
            self.assertIn(text, appendix)
        self.assertIn("Address template", appendix)
        self.assertIn("Matrix contract", appendix)
        self.assertIn("Atomic modifiers", appendix)
        self.assertIn("Descriptor contract", appendix)
        self.assertIn("Barrier contract", appendix)
        self.assertIn("Scalar source selector", appendix)
        self.assertIn("`ssrc_sel`", appendix)
        self.assertNotIn("VGPR tag effect", appendix)
        self.assertIn("示例字段值", appendix)
        self.assertIn("保留值", appendix)
        self.assertGreaterEqual(
            len(re.findall(r"`0x[0-9A-F]{16}`", appendix)), FORM_COUNT
        )

    def test_markdown_table_preserves_code_span_pipe(self) -> None:
        row = "| name | `a|b` | escaped \\| pipe |"
        self.assertEqual(
            build_spec.split_markdown_table_row(row),
            ["name", "`a|b`", "escaped | pipe"],
        )
        rendered = build_spec.markdown_to_html(
            "| A | B |\n|---|---|\n| x | `left|right` |\n",
            "table",
        )
        self.assertEqual(rendered.count("<td>"), 2)
        self.assertIn("<code>left|right</code>", rendered)

    def test_html_toc_ignores_fenced_hash_comments_and_links_existing_ids(self) -> None:
        source = "\n".join(
            (
                "# Real chapter",
                "",
                "```python",
                "# not a heading",
                "## also not a heading",
                "```",
                "",
                "~~~shell",
                "# still not a heading",
                "~~~",
                "",
                "## Real section",
                "",
            )
        )
        rendered = build_spec.markdown_to_html(source, "TOC regression")
        toc_html = rendered.split("<main>", 1)[0]
        hrefs = set(re.findall(r'<a href="#([^"]+)">', toc_html))
        ids = set(re.findall(r'\bid="([^"]+)"', rendered))
        self.assertEqual(hrefs, {"real-chapter", "real-section"})
        self.assertTrue(hrefs.issubset(ids))
        self.assertNotIn("not a heading</a>", toc_html)
        self.assertNotIn("still not a heading</a>", toc_html)
        self.assertIn("# not a heading", rendered)

    def test_build_writes_only_new_draft_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs"
            dist = root / "dist"
            docs.mkdir()
            dist.mkdir()
            (docs / "00.md").write_text("# 测试章节\n\nSGPR + VGPR", encoding="utf-8")
            md = dist / "VTX-ISA-Reference-1.0-Draft.md"
            html = dist / "VTX-ISA-Reference-1.0-Draft.html"
            pdf = dist / "VTX-ISA-Reference-1.0-Draft.pdf"
            stale = [dist / name for name in build_spec.STALE_ARTIFACT_NAMES]
            for path in stale:
                path.write_text("obsolete", encoding="utf-8")
            unrelated = dist / "keep-this-unrelated-file.txt"
            unrelated.write_text("keep", encoding="utf-8")

            def fake_pdf(markdown: str, destination: Path, title: str, cover_summary: str = "") -> Path:
                self.assertIn("SGPR + VGPR", markdown)
                self.assertIn(f"{FAMILY_COUNT} 指令家族", cover_summary)
                self.assertIn(f"{FORM_COUNT} 指令形式", cover_summary)
                destination.write_bytes(b"%PDF-test")
                return Path("test-font.ttf")

            with (
                patch.object(build_spec, "DOCS_DIR", docs),
                patch.object(build_spec, "DIST_DIR", dist),
                patch.object(build_spec, "MERGED_MARKDOWN", md),
                patch.object(build_spec, "HTML_REFERENCE", html),
                patch.object(build_spec, "PDF_REFERENCE", pdf),
                patch.object(build_spec, "markdown_to_pdf", side_effect=fake_pdf),
            ):
                outputs = build_spec.build(validate_isa.DEFAULT_ISA)
            self.assertEqual(outputs, [md, html, pdf])
            self.assertTrue(all(path.is_file() for path in outputs))
            self.assertTrue(all(not path.exists() for path in stale))
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_pdf_has_visible_toc_and_normalized_glyphs(self) -> None:
        source = "# 第一章\n\n## 子节\n\nA ⊆ B，C ⊂ D，⋃S ↔ T。\n"
        normalized = build_spec._normalize_pdf_text(source)
        self.assertNotRegex(normalized, r"[⊆⊂⋃↔]")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test.pdf"
            build_spec.markdown_to_pdf(source, output, "VTX-1 测试", "SGPR + VGPR · 1/1")
            self.assertGreater(output.stat().st_size, 1000)
            pdf_bytes = output.read_bytes()
            objects = {
                int(match.group(1)): match.group(2)
                for match in re.finditer(
                    rb"(?m)^(\d+) 0 obj\s*(.*?)\s*endobj",
                    pdf_bytes,
                    re.DOTALL,
                )
            }
            outline_root = next(
                body for body in objects.values() if b"/Type /Outlines" in body
            )
            declared_count = int(
                re.search(rb"/Count\s+(\d+)", outline_root).group(1)
            )
            actual_entries = sum(
                b"/Title " in body and re.search(rb"/Parent\s+\d+\s+0\s+R", body)
                is not None
                for body in objects.values()
            )
            self.assertEqual(declared_count, actual_entries)


if __name__ == "__main__":
    unittest.main()
