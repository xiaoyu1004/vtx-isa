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
import validate_isa  # noqa: E402


class IsaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = validate_isa.load_isa(validate_isa.DEFAULT_ISA)
        cls.schema = validate_isa.load_json(validate_isa.DEFAULT_SCHEMA)
        cls.vectors = validate_isa.load_json(validate_isa.DEFAULT_VECTORS)
        cls.report = validate_isa.validate_all(cls.document, cls.schema, cls.vectors)

    def test_repository_isa_passes_schema_and_semantic_validation(self) -> None:
        self.assertTrue(self.report.ok, "\n".join(self.report.errors))

    def test_counts_are_read_from_yaml_and_match_actual_inventory(self) -> None:
        declared = self.document["counts"]
        self.assertEqual((declared["families"], declared["forms"]), (69, 391))
        self.assertEqual(
            (self.report.family_count, self.report.form_count),
            (declared["families"], declared["forms"]),
        )

    def test_all_391_forms_have_unique_vectors(self) -> None:
        form_keys = {
            validate_isa.form_key(family, form)
            for family in self.document["families"]
            for form in family["forms"]
        }
        vector_keys = {entry["key"] for entry in self.vectors["forms"]}
        self.assertEqual(vector_keys, form_keys)
        self.assertEqual(len(vector_keys), 391)
        self.assertEqual(self.report.vector_count, 391)
        for entry in self.vectors["forms"]:
            self.assertRegex(entry["machine_word"], r"^0x[0-9A-F]{16}$")

    def test_every_form_has_unique_encoding_triple(self) -> None:
        triples = [
            (form["class"], form["format"], form["opcode"])
            for family in self.document["families"]
            for form in family["forms"]
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

    def test_v_bcast_is_true_sgpr_to_vgpr_optional_broadcast(self) -> None:
        form = next(
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["mnemonic"] == "V_BCAST.B32"
        )
        operand_types = {operand["name"]: operand["type"] for operand in form["operands"]}
        self.assertEqual(form["execution_domain"], "vector")
        self.assertEqual(form["guard_policy"], "optional")
        self.assertEqual((operand_types["src"], operand_types["dst"]), ("sgpr32", "vgpr32"))
        guard = next(field for field in form["fields"] if field["name"] == "guard")
        self.assertNotIn("fixed", guard)

        broken = copy.deepcopy(self.document)
        candidate = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["mnemonic"] == "V_BCAST.B32"
        )
        next(operand for operand in candidate["operands"] if operand["name"] == "src")["type"] = "vgpr32"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("SGPR-to-VGPR broadcast" in error for error in report.errors))

    def test_b64_cross_domain_transfer_forms(self) -> None:
        forms = {
            form["mnemonic"]: form
            for family in self.document["families"]
            for form in family["forms"]
            if form["mnemonic"] in {"V_BCAST.B64", "S_READFIRST.B64"}
        }
        self.assertEqual(set(forms), {"V_BCAST.B64", "S_READFIRST.B64"})
        broadcast_types = {
            operand["name"]: operand["type"] for operand in forms["V_BCAST.B64"]["operands"]
        }
        readfirst_types = {
            operand["name"]: operand["type"] for operand in forms["S_READFIRST.B64"]["operands"]
        }
        self.assertEqual(
            (
                forms["V_BCAST.B64"]["execution_domain"],
                forms["V_BCAST.B64"]["guard_policy"],
                broadcast_types["src"],
                broadcast_types["dst"],
            ),
            ("vector", "optional", "sgpr64", "vgpr64"),
        )
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
        form = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["mnemonic"] == "S_READFIRST.B64"
        )
        next(operand for operand in form["operands"] if operand["name"] == "src")[
            "type"
        ] = "sgpr64"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("VGPR64-to-SGPR64" in error for error in report.errors))

    def test_named_barrier_inventory_examples_slots_and_structure(self) -> None:
        families = {
            family["id"]: family
            for family in self.document["families"]
            if family["id"] in validate_isa.BARRIER_FORMS
        }
        self.assertEqual(set(families), set(validate_isa.BARRIER_FORMS))
        expected_examples = {
            "F061": ("BAR.SYNC.CTA 3", "0x0018000000000185"),
            "F062": ("BAR.ARRIVE.CTA v5, 3", "0x0018000000280205"),
            "F063": ("BAR.WAIT.CTA 3, v5", "0x0018000000280285"),
        }
        for family_id, expected in validate_isa.BARRIER_FORMS.items():
            family = families[family_id]
            self.assertEqual(family["mnemonic"], expected["family_mnemonic"])
            self.assertEqual(family["semantic_group"], "barrier")
            self.assertEqual(len(family["forms"]), 1)
            form = family["forms"][0]
            self.assertEqual(form["mnemonic"], expected["form_mnemonic"])
            self.assertEqual((form["class"], form["format"], form["opcode"]), expected["triple"])
            self.assertEqual(
                (form["example"]["assembly"], form["example"]["machine_word"]),
                expected_examples[family_id],
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
                self.assertEqual((int(word, 16) >> 51) & 0x7, slot)

        contract = self.document["barrier_contract"]
        self.assertEqual(contract["owner_identity"]["name"], "linear_tid")
        self.assertEqual(
            contract["token_tag_fields"],
            ["cta_identity", "linear_tid", "slot", "generation"],
        )
        self.assertEqual(
            contract["wait_record_fields"],
            ["warp_id", "owner_snapshot", "resume_pc"],
        )
        self.assertEqual(contract["idle_slot"]["mode"], "EMPTY")
        self.assertTrue(contract["idle_slot"]["generation_ignored"])
        generation = contract["generation_domain"]
        self.assertEqual(generation["type"], "mathematical_nonnegative_integer")
        self.assertEqual((generation["initial"], generation["retire_step"]), (0, 1))
        self.assertTrue(generation["monotonic"])
        self.assertFalse(generation["wraps"])
        self.assertEqual(generation["finite_implementation_rule"], "as_if_non_wrapping")
        self.assertEqual(generation["observable_identity_reuse"], "forbidden")
        self.assertEqual(generation["stale_token_revival"], "forbidden")
        flattened_text = " ".join(
            item
            for family in families.values()
            for form in family["forms"]
            for item in [form["semantics"], *form["constraints"]]
        )
        for fragment in (
            "SYNC mode",
            "SPLIT mode",
            "generation",
            "linear_tid",
            "BarrierWaitRecord",
            "EXIT never shrinks",
            "shared CTA release",
            "shared CTA acquire",
        ):
            self.assertIn(fragment, flattened_text)

    def test_named_barrier_negative_contracts(self) -> None:
        old_name = copy.deepcopy(self.document)
        family = next(family for family in old_name["families"] if family["id"] == "F061")
        family["mnemonic"] = "BARRIER.SYNC"
        family["forms"][0]["mnemonic"] = "BARRIER.SYNC.CTA"
        report = validate_isa.validate_document(old_name)
        self.assertTrue(any("canonical family BAR.SYNC" in error for error in report.errors))

        sgpr_token = copy.deepcopy(self.document)
        arrive = next(family for family in sgpr_token["families"] if family["id"] == "F062")[
            "forms"
        ][0]
        next(operand for operand in arrive["operands"] if operand["name"] == "token")[
            "type"
        ] = "sgpr32"
        report = validate_isa.validate_document(sgpr_token)
        self.assertTrue(any("canonical operands" in error for error in report.errors))

        sgpr_token_type = copy.deepcopy(self.document)
        sgpr_token_type["operand_types"]["barrier_token"]["register_file"] = "SGPR"
        report = validate_isa.validate_document(sgpr_token_type)
        self.assertTrue(any("barrier_token must be a 32-bit VGPR" in error for error in report.errors))

        missing_wait_id = copy.deepcopy(self.document)
        wait = next(
            family for family in missing_wait_id["families"] if family["id"] == "F063"
        )["forms"][0]
        wait["operands"] = [
            operand for operand in wait["operands"] if operand["name"] != "barrier"
        ]
        report = validate_isa.validate_document(missing_wait_id)
        self.assertTrue(any("canonical operands" in error for error in report.errors))

        wrong_triple = copy.deepcopy(self.document)
        sync = next(family for family in wrong_triple["families"] if family["id"] == "F061")[
            "forms"
        ][0]
        sync["opcode"] = 2
        report = validate_isa.validate_document(wrong_triple)
        self.assertTrue(any("canonical BAR.SYNC.CTA identity and triple" in error for error in report.errors))

        generation_mutations = (
            ("type", "u64"),
            ("retire_step", 2),
            ("wraps", True),
            ("stale_token_revival", "allowed"),
        )
        for name, value in generation_mutations:
            broken_generation = copy.deepcopy(self.document)
            broken_generation["barrier_contract"]["generation_domain"][name] = value
            report = validate_isa.validate_document(broken_generation)
            self.assertTrue(
                any("barrier_contract.generation_domain" in error for error in report.errors),
                name,
            )

    def test_docs08_named_barrier_generation_static_gate(self) -> None:
        text = (ROOT / "docs" / "08-conformance.md").read_text(encoding="utf-8")
        for required in (
            "BAR.SYNC.CTA id",
            "BAR.ARRIVE.CTA vd,id",
            "BAR.WAIT.CTA id,vs",
            "generation oracle 必须使用数学非负整数",
            "逻辑 generation 仍严格 `old+1`、不回绕",
            "绝不复活",
            "assert logical_generation == previous_logical_generation + 1",
            "capability ID 或安全回收",
        ):
            self.assertIn(required, text)

    def test_vgpr_tag_effect_policy_is_closed(self) -> None:
        operand_types = self.document["operand_types"]
        writers = {
            validate_isa.form_key(family, form)
            for family in self.document["families"]
            for form in family["forms"]
            if any(
                operand["access"] in {"write", "read_write"}
                and operand_types[operand["type"]].get("register_file") == "VGPR"
                for operand in form["operands"]
            )
        }
        effects = validate_isa.enumerate_vgpr_tag_effects(self.document)
        self.assertEqual(set(effects), writers)
        self.assertEqual(effects["F025/b32.reg"], "copy_source_tag")
        self.assertEqual(effects["F062/cta"], "create_tag")
        self.assertTrue(
            all(
                action == "clear"
                for key, action in effects.items()
                if key not in {"F025/b32.reg", "F062/cta"}
            )
        )

        broken = copy.deepcopy(self.document)
        broken["barrier_contract"]["vgpr_tag_write_policy"]["exceptions"].pop()
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("must contain only V_MOV.B32" in error for error in report.errors))

        broken_owner = copy.deepcopy(self.document)
        broken_owner["barrier_contract"]["owner_identity"]["name"] = "lane_id"
        report = validate_isa.validate_document(broken_owner)
        self.assertTrue(any("barrier_contract.owner_identity" in error for error in report.errors))

    def test_x_broadcast_register_and_immediate_lane_forms(self) -> None:
        forms = [
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["mnemonic"] == "X_BROADCAST.B32"
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

    def test_schema_validation_is_executed(self) -> None:
        broken = copy.deepcopy(self.document)
        del broken["status"]
        report = validate_isa.validate_document(broken, self.schema)
        self.assertTrue(any("schema:" in error and "status" in error for error in report.errors))

    def test_duplicate_encoding_triple_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        forms = [form for family in broken["families"] for form in family["forms"]]
        first, second = forms[:2]
        second["class"], second["format"], second["opcode"] = (
            first["class"],
            first["format"],
            first["opcode"],
        )
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("duplicates" in error and "class,format,opcode" in error for error in report.errors))

    def test_class_specific_format_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        form = broken["families"][1]["forms"][0]
        form["encoding_format"] = "V1"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("encoding_format" in error and "must be" in error for error in report.errors))

    def test_mixed_memory_formats_use_registered_fields(self) -> None:
        registry = {
            entry["name"]: [field["name"] for field in entry["fields"]]
            for entry in self.document["format_registry"]["MEMORY"]
        }
        mixed = [
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["encoding_format"] in {"SMEMX", "VATOMX"}
        ]
        self.assertEqual(
            {form["encoding_format"] for form in mixed},
            {"SMEMX", "VATOMX"},
        )
        self.assertEqual(len(mixed), 24)
        for form in mixed:
            payload = [field["name"] for field in form["fields"] if field["lsb"] >= 19]
            self.assertEqual(payload, registry[form["encoding_format"]])

        broken = copy.deepcopy(self.document)
        form = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["encoding_format"] == "SMEMX"
        )
        next(field for field in form["fields"] if field["name"] == "sindex")["name"] = "vindex"
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("does not exactly match" in error for error in report.errors))

    def test_vmem_address_mode_contracts(self) -> None:
        forms = [
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["encoding_format"] == "VMEM"
        ]
        self.assertEqual(len(forms), 36)
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

        broken = copy.deepcopy(self.document)
        form = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["encoding_format"] == "VMEM"
            and form["address_template"]["mode"] == "sv_mix"
        )
        form["address_template"]["address_operands"] = ["sbase", "simm16"]
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("VMEM sv_mix requires" in error for error in report.errors))

        broken_extension = copy.deepcopy(self.document)
        form = next(
            form
            for family in broken_extension["families"]
            for form in family["forms"]
            if form["encoding_format"] == "VMEM"
            and form["address_template"]["mode"] == "sv_mix"
        )
        form["address_template"]["expression"] = form["address_template"][
            "expression"
        ].replace("zero_extend(vaddr)", "sign_extend(vaddr)")
        report = validate_isa.validate_document(broken_extension)
        self.assertTrue(any("must zero_extend(vaddr)" in error for error in report.errors))

    def test_register_pair_syntax_is_adjacent_and_even(self) -> None:
        pair_forms = [
            form
            for family in self.document["families"]
            for form in family["forms"]
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
            for family in self.document["families"]
            if family["semantic_group"] == "atomic"
            for form in family["forms"]
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

        broken_order = copy.deepcopy(self.document)
        atomic = next(
            form
            for family in broken_order["families"]
            if family["semantic_group"] == "atomic"
            for form in family["forms"]
        )
        atomic["legal_orders"] = ["RELAXED", "BOGUS"]
        report = validate_isa.validate_document(broken_order)
        self.assertTrue(any(".legal_orders" in error for error in report.errors))

        broken_modifier = copy.deepcopy(self.document)
        atomic = next(
            form
            for family in broken_modifier["families"]
            if family["semantic_group"] == "atomic"
            for form in family["forms"]
        )
        next(field for field in atomic["fields"] if field["name"] == "order")["fixed"] = 0
        report = validate_isa.validate_document(broken_modifier)
        self.assertTrue(any("runtime-selectable, not fixed" in error for error in report.errors))

        broken_scope = copy.deepcopy(self.document)
        atomic = next(
            form
            for family in broken_scope["families"]
            if family["semantic_group"] == "atomic"
            for form in family["forms"]
        )
        next(field for field in atomic["fields"] if field["name"] == "scope")[
            "reserved_values"
        ] = [2]
        report = validate_isa.validate_document(broken_scope)
        self.assertTrue(any("classify encoding 3 as reserved" in error for error in report.errors))

        broken_example = copy.deepcopy(self.document)
        atomic = next(
            form
            for family in broken_example["families"]
            if family["semantic_group"] == "atomic"
            for form in family["forms"]
        )
        atomic["example"]["field_values"]["scope"] = 3
        report = validate_isa.validate_document(broken_example)
        self.assertTrue(any("reserved or outside legal_scopes" in error for error in report.errors))

        broken_cas = copy.deepcopy(self.document)
        cas = next(
            form
            for family in broken_cas["families"]
            for form in family["forms"]
            if ".CAS." in form["mnemonic"]
        )
        cas["operands"] = [
            operand for operand in cas["operands"] if operand["name"] != "replacement"
        ]
        report = validate_isa.validate_document(broken_cas)
        self.assertTrue(any("CAS requires distinct compare" in error for error in report.errors))

    def test_unique_mma_contract_and_negative_participation(self) -> None:
        forms = [
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["class"] == "MATRIX"
        ]
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
        matrix = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["class"] == "MATRIX"
        )
        matrix["matrix_contract"]["participation"]["required_live_lanes"] = 16
        report = validate_isa.validate_document(broken)
        self.assertTrue(
            any("required_live_lanes" in error and "must equal 32" in error for error in report.errors)
        )

        duplicate = copy.deepcopy(self.document)
        matrix_family = next(
            family
            for family in duplicate["families"]
            if any(form["class"] == "MATRIX" for form in family["forms"])
        )
        clone = copy.deepcopy(matrix_family["forms"][0])
        clone["id"] += ".duplicate"
        clone["opcode"] = 1
        next(field for field in clone["fields"] if field["name"] == "opcode")["fixed"] = 1
        matrix_family["forms"].append(clone)
        duplicate["counts"]["forms"] += 1
        report = validate_isa.validate_document(duplicate)
        self.assertTrue(any("exactly one canonical MATRIX/MMA" in error for error in report.errors))

    def test_header_fixed_value_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        form = broken["families"][0]["forms"][0]
        opcode = next(field for field in form["fields"] if field["name"] == "opcode")
        opcode["fixed"] = 63
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("fixed value must equal form opcode" in error for error in report.errors))

    def test_full_64_bit_coverage_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        form = broken["families"][0]["forms"][0]
        form["fields"][-1]["width"] -= 1
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("does not cover all 64 bits" in error for error in report.errors))

    def test_required_pt_guard_negative(self) -> None:
        broken = copy.deepcopy(self.document)
        form = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["guard_policy"] == "required_pt"
        )
        guard = next(field for field in form["fields"] if field["name"] == "guard")
        guard.pop("fixed")
        report = validate_isa.validate_document(broken)
        self.assertTrue(any("requires fixed PT" in error for error in report.errors))

    def test_scalar_ready_rule_negative(self) -> None:
        broken_state = copy.deepcopy(self.document)
        scalar = next(
            form
            for family in broken_state["families"]
            for form in family["forms"]
            if form["execution_domain"] == "scalar"
        )
        scalar["required_state"] = "none"
        report = validate_isa.validate_document(broken_state)
        self.assertTrue(any("all scalar forms require scalar_ready" in error for error in report.errors))

    def test_control_inventory_and_scalar_state_requirements(self) -> None:
        forms = {
            form["mnemonic"]: form
            for family in self.document["families"]
            for form in family["forms"]
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
        call = next(
            form
            for family in broken["families"]
            for form in family["forms"]
            if form["mnemonic"] == "CALL"
        )
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
            for family in self.document["families"]
            for form in family["forms"]
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
        ssy = next(
            form
            for family in self.document["families"]
            for form in family["forms"]
            if form["mnemonic"] == "SSY"
        )
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
        call = next(
            form
            for family in broken_call["families"]
            for form in family["forms"]
            if form["mnemonic"] == "CALL"
        )
        call["operands"] = [
            operand for operand in call["operands"] if operand["name"] != "call_stack"
        ]
        report = validate_isa.validate_document(broken_call)
        self.assertTrue(any("implicit read_write call_stack" in error for error in report.errors))

        broken_ssy = copy.deepcopy(self.document)
        ssy = next(
            form
            for family in broken_ssy["families"]
            for form in family["forms"]
            if form["mnemonic"] == "SSY"
        )
        next(operand for operand in ssy["operands"] if operand["name"] == "call_stack")[
            "access"
        ] = "read_write"
        report = validate_isa.validate_document(broken_ssy)
        self.assertTrue(any("SSY requires an implicit read-only" in error for error in report.errors))

    def test_sgpr_vgpr_and_vpred_field_reference_negative(self) -> None:
        for operand_type in ("sgpr32", "vgpr32", "vpred"):
            broken = copy.deepcopy(self.document)
            operand = next(
                operand
                for family in broken["families"]
                for form in family["forms"]
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
        report = validate_isa.validate_document(self.document)
        validate_isa.validate_vectors(self.document, broken_vectors, report)
        self.assertTrue(any("encodes" in error and "expected" in error for error in report.errors))

    def test_appendix_contains_new_form_metadata(self) -> None:
        appendix = build_spec.render_instruction_reference(self.document, self.vectors)
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
        self.assertIn("VGPR tag effect", appendix)
        self.assertIn("`create_tag`", appendix)
        self.assertIn("`copy_source_tag`", appendix)
        self.assertIn("示例字段值", appendix)
        self.assertIn("保留值", appendix)
        self.assertGreaterEqual(len(re.findall(r"`0x[0-9A-F]{16}`", appendix)), 391)

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
                self.assertIn("69 指令家族", cover_summary)
                self.assertIn("391 指令形式", cover_summary)
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
