#!/usr/bin/env python3
"""Tests for the V1 integer golden and deterministic vectors."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.golden import GemmCase, Matrix, gemm4x4, validate_int8_matrix
from scripts.gen_vectors import make_cases, write_vectors

def _matrix(rows: list[list[int]]) -> Matrix:
    return tuple(tuple(row) for row in rows)

def _single(row: int, column: int, value: int) -> Matrix:
    rows = [[0, 0, 0, 0] for _ in range(4)]
    rows[row][column] = value
    return _matrix(rows)

def _transpose(matrix: Matrix) -> Matrix:
    return tuple(
        tuple(matrix[row][column] for row in range(4))
        for column in range(4)
    )

def _flatten(matrix: Matrix) -> list[int]:
    return [value for row in matrix for value in row]

def _unflatten(values: list[int]) -> Matrix:
    return tuple(
        tuple(values[4 * row : 4 * row + 4]) for row in range(4)
    )

ZERO = _matrix([[0, 0, 0, 0] for _ in range(4)])
IDENTITY = _matrix(
    [[1 if row == column else 0 for column in range(4)] for row in range(4)]
)
A_REF = _matrix(
    [
        [1, 2, 3, 4],
        [-1, 2, -3, 4],
        [5, -6, 7, -8],
        [-2, -3, 4, 5],
    ]
)
B_REF = _matrix(
    [
        [1, -1, 2, 0],
        [2, 1, -2, 3],
        [-1, 2, 0, 1],
        [3, -2, 1, -1],
    ]
)
C_REF = _matrix(
    [
        [14, -1, 2, 5],
        [18, -11, -2, -1],
        [-38, 19, 14, -3],
        [3, -3, 7, -10],
    ]
)
ALL_NEG128 = _matrix([[-128] * 4 for _ in range(4)])
ALL_POS127 = _matrix([[127] * 4 for _ in range(4)])
ALL_65536 = _matrix([[65536] * 4 for _ in range(4)])
ALL_NEG65024 = _matrix([[-65024] * 4 for _ in range(4)])
ALL_64516 = _matrix([[64516] * 4 for _ in range(4)])
MIX_A = _matrix(
    [
        [127, 127, 127, 127],
        [127, 127, 127, 127],
        [-128, -128, -128, -128],
        [-128, -128, -128, -128],
    ]
)
MIX_B = _matrix(
    [
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
    ]
)
MIX_C = _matrix(
    [
        [-65024, -65024, 64516, 64516],
        [-65024, -65024, 64516, 64516],
        [65536, 65536, -65024, -65024],
        [65536, 65536, -65024, -65024],
    ]
)

FIXED_CASES = (
    ("reference", A_REF, B_REF, C_REF),
    ("zero_left", ZERO, B_REF, ZERO),
    ("zero_right", A_REF, ZERO, ZERO),
    ("identity_left", IDENTITY, B_REF, B_REF),
    ("identity_right", A_REF, IDENTITY, A_REF),
    ("all_neg128", ALL_NEG128, ALL_NEG128, ALL_65536),
    ("all_neg128_x_pos127", ALL_NEG128, ALL_POS127, ALL_NEG65024),
    ("all_pos127", ALL_POS127, ALL_POS127, ALL_64516),
    ("mix_boundary", MIX_A, MIX_B, MIX_C),
)

class FixedCaseTests(unittest.TestCase):
    def test_nine_cases_match_reviewed_values(self) -> None:
        self.assertEqual(len(FIXED_CASES), 9)
        for name, a, b, expected in FIXED_CASES:
            with self.subTest(name=name):
                actual = gemm4x4(a, b)
                self.assertEqual(len(actual), 4)
                for row in range(4):
                    self.assertEqual(len(actual[row]), 4)
                    for column in range(4):
                        self.assertEqual(
                            actual[row][column],
                            expected[row][column],
                            f"{name} C[{row}][{column}]",
                        )

    def test_generator_fixed_cases_match_reviewed_values(self) -> None:
        generated = make_cases(20261025, 0)
        self.assertEqual(len(generated), len(FIXED_CASES))
        for case, (name, a, b, expected) in zip(generated, FIXED_CASES):
            self.assertEqual(case.name, name)
            self.assertEqual(case.a, a)
            self.assertEqual(case.b, b)
            self.assertEqual(case.expected, expected)

class ExhaustiveOperandTests(unittest.TestCase):
    def test_all_256_by_256_operand_pairs(self) -> None:
        zero_tail = ZERO[1:]
        for a_value in range(-128, 128):
            for b_value in range(-128, 128):
                a = ((a_value, 0, 0, 0),) + zero_tail
                b = ((b_value, 0, 0, 0),) + zero_tail
                expected = ((a_value * b_value, 0, 0, 0),) + zero_tail
                self.assertEqual(
                    gemm4x4(a, b),
                    expected,
                    f"A[0][0]={a_value}, B[0][0]={b_value}",
                )

class SinglePointMappingTests(unittest.TestCase):
    def test_all_i_k_j_positions(self) -> None:
        for i in range(4):
            for k in range(4):
                for j in range(4):
                    a_value = i * 16 + k + 1
                    b_value = -(k * 16 + j + 1)
                    a = _single(i, k, a_value)
                    b = _single(k, j, b_value)
                    expected = _single(i, j, a_value * b_value)
                    self.assertEqual(
                        gemm4x4(a, b),
                        expected,
                        f"i={i}, k={k}, j={j}",
                    )

class RandomCaseTests(unittest.TestCase):
    def test_fixed_seed_count_and_determinism(self) -> None:
        cases = make_cases(20261025, 1000)
        self.assertEqual(len(cases), len(FIXED_CASES) + 1000)
        self.assertEqual(len({case.name for case in cases}), len(cases))
        self.assertEqual(
            [case.name for case in cases[: len(FIXED_CASES)]],
            [name for name, _, _, _ in FIXED_CASES],
        )
        self.assertTrue(
            all(case.name.startswith("random_") for case in cases[len(FIXED_CASES) :])
        )
        self.assertEqual(cases, make_cases(20261025, 1000))

    def test_random_result_range_and_properties(self) -> None:
        cases = make_cases(20261025, 1000)
        for case in cases[len(FIXED_CASES) :]:
            self.assertEqual(gemm4x4(case.a, case.b), case.expected, case.name)

            for row in case.expected:
                for value in row:
                    self.assertIs(type(value), int, case.name)
                    self.assertGreaterEqual(value, -65024, case.name)
                    self.assertLessEqual(value, 65536, case.name)

            self.assertEqual(gemm4x4(ZERO, case.b), ZERO, case.name)
            self.assertEqual(gemm4x4(case.a, ZERO), ZERO, case.name)
            self.assertEqual(gemm4x4(IDENTITY, case.b), case.b, case.name)
            self.assertEqual(gemm4x4(case.a, IDENTITY), case.a, case.name)
            self.assertEqual(
                _transpose(case.expected),
                gemm4x4(_transpose(case.b), _transpose(case.a)),
                case.name,
            )

    def test_invalid_generator_arguments(self) -> None:
        with self.assertRaises(ValueError):
            make_cases(20261025, -1)
        with self.assertRaises(TypeError):
            make_cases(True, 0)
        with self.assertRaises(TypeError):
            make_cases(20261025, True)

class GeneratorCliTests(unittest.TestCase):
    def test_invalid_random_count_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="golden-cli-") as tmp:
            path = Path(tmp) / "vectors.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "gen_vectors.py"),
                    "--random-count",
                    "-1",
                    "--output",
                    str(path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1, output)
            self.assertIn("ERROR:", output)
            self.assertFalse(path.exists())

class ValidationTests(unittest.TestCase):
    def test_accepts_int8_boundaries(self) -> None:
        matrix = _matrix(
            [
                [-128, 127, 0, -1],
                [1, -2, 2, -3],
                [3, -4, 4, -5],
                [5, -6, 6, -7],
            ]
        )
        self.assertIsNone(validate_int8_matrix(matrix))

    def test_rejects_non_tuple_containers(self) -> None:
        with self.assertRaises(TypeError):
            validate_int8_matrix(list(IDENTITY))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            validate_int8_matrix((list(IDENTITY[0]),) + IDENTITY[1:])  # type: ignore[arg-type]

    def test_rejects_non_int_elements(self) -> None:
        for value in (True, 1.0, "1", None):
            with self.subTest(value=value):
                matrix = ((value, 0, 0, 0),) + ZERO[1:]
                with self.assertRaises(TypeError):
                    validate_int8_matrix(matrix)  # type: ignore[arg-type]

    def test_rejects_wrong_dimensions(self) -> None:
        for matrix in ((), IDENTITY[:3], IDENTITY + (IDENTITY[0],)):
            with self.subTest(rows=len(matrix)):
                with self.assertRaises(ValueError):
                    validate_int8_matrix(matrix)
        with self.assertRaises(ValueError):
            validate_int8_matrix(((1, 2, 3),) + ZERO[1:])
        with self.assertRaises(ValueError):
            validate_int8_matrix(((1, 2, 3, 4, 5),) + ZERO[1:])

    def test_rejects_out_of_range_values(self) -> None:
        for value in (-129, 128):
            with self.subTest(value=value):
                matrix = ((value, 0, 0, 0),) + ZERO[1:]
                with self.assertRaises(ValueError):
                    validate_int8_matrix(matrix)

    def test_gemm_validates_both_operands(self) -> None:
        invalid = ((128, 0, 0, 0),) + ZERO[1:]
        with self.assertRaises(ValueError):
            gemm4x4(invalid, ZERO)
        with self.assertRaises(ValueError):
            gemm4x4(ZERO, invalid)

class VectorSerializationTests(unittest.TestCase):
    def test_json_schema_roundtrip_and_layout(self) -> None:
        seed = 20261025
        random_count = 3
        cases = make_cases(seed, random_count)
        with tempfile.TemporaryDirectory(prefix="golden-vectors-") as tmp:
            path = Path(tmp) / "vectors" / "v1_gemm4x4.json"
            write_vectors(path, cases, seed, random_count)
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            text = raw.decode("utf-8")
            self.assertNotIn(str(ROOT), text)
            payload = json.loads(text)

            self.assertEqual(
                list(payload),
                ["schema_version", "seed", "random_count", "cases"],
            )
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["seed"], seed)
            self.assertEqual(payload["random_count"], random_count)
            self.assertEqual(len(payload["cases"]), len(cases))

            for json_case, case in zip(payload["cases"], cases):
                self.assertEqual(
                    list(json_case),
                    ["name", "a", "b", "expected"],
                )
                self.assertEqual(json_case["name"], case.name)
                for key in ("a", "b", "expected"):
                    self.assertEqual(len(json_case[key]), 16)
                    for value in json_case[key]:
                        self.assertIs(type(value), int)
                self.assertEqual(_unflatten(json_case["a"]), case.a)
                self.assertEqual(_unflatten(json_case["b"]), case.b)
                self.assertEqual(
                    _unflatten(json_case["expected"]), case.expected
                )
                for row in range(4):
                    for column in range(4):
                        self.assertEqual(
                            json_case["a"][4 * row + column],
                            case.a[row][column],
                        )
                        self.assertEqual(
                            json_case["expected"][4 * row + column],
                            case.expected[row][column],
                        )

            reference = payload["cases"][0]
            self.assertEqual(reference["name"], "reference")
            self.assertLess(min(reference["a"]), 0)
            self.assertLess(min(reference["expected"]), 0)

    def test_same_parameters_produce_identical_bytes(self) -> None:
        seed = 20261025
        random_count = 5
        cases_one = make_cases(seed, random_count)
        cases_two = make_cases(seed, random_count)

        with tempfile.TemporaryDirectory(prefix="golden-determinism-") as tmp:
            root = Path(tmp)
            first = root / "first.json"
            second = root / "second.json"
            write_vectors(first, cases_one, seed, random_count)
            write_vectors(second, cases_two, seed, random_count)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).digest(),
                hashlib.sha256(second.read_bytes()).digest(),
            )

            changed = root / "changed.json"
            write_vectors(
                changed,
                make_cases(seed + 1, random_count),
                seed + 1,
                random_count,
            )
            self.assertNotEqual(first.read_bytes(), changed.read_bytes())

    def test_tampered_expected_rejected_by_writer(self) -> None:
        cases = make_cases(20261025, 0)
        expected_rows = [list(row) for row in cases[0].expected]
        expected_rows[0][0] += 1
        tampered = list(cases)
        tampered[0] = GemmCase(
            cases[0].name,
            cases[0].a,
            cases[0].b,
            _matrix(expected_rows),
        )

        with tempfile.TemporaryDirectory(prefix="golden-reject-") as tmp:
            path = Path(tmp) / "bad.json"
            with self.assertRaises(ValueError):
                write_vectors(path, tampered, 20261025, 0)
            self.assertFalse(path.exists())

class TamperedExpectationTests(unittest.TestCase):
    def test_tampered_fixed_expectation_exits_nonzero(self) -> None:
        formal = ROOT / "vectors" / "v1_gemm4x4.json"
        before = (
            hashlib.sha256(formal.read_bytes()).hexdigest()
            if formal.exists()
            else None
        )

        with tempfile.TemporaryDirectory(prefix="golden-tamper-") as tmp:
            temp_root = Path(tmp)
            test_file = temp_root / "test_tampered_expectation.py"
            test_file.write_text(
                "import unittest\n"
                "from model.golden import gemm4x4\n"
                "A = ((1, 0, 0, 0), (0, 1, 0, 0), "
                "(0, 0, 1, 0), (0, 0, 0, 1))\n"
                "B = ((1, 0, 0, 0), (0, 1, 0, 0), "
                "(0, 0, 1, 0), (0, 0, 0, 1))\n"
                "EXPECTED = ((2, 0, 0, 0), (0, 1, 0, 0), "
                "(0, 0, 1, 0), (0, 0, 0, 1))\n"
                "class TamperedExpectation(unittest.TestCase):\n"
                "    def test_mismatch(self):\n"
                "        self.assertEqual(gemm4x4(A, B), EXPECTED)\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (
                os.pathsep + env["PYTHONPATH"]
                if env.get("PYTHONPATH")
                else ""
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "-q",
                    "test_tampered_expectation",
                ],
                cwd=temp_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1, output)
            self.assertIn("FAILED", output)

        after = (
            hashlib.sha256(formal.read_bytes()).hexdigest()
            if formal.exists()
            else None
        )
        self.assertEqual(before, after)

if __name__ == "__main__":
    unittest.main()
