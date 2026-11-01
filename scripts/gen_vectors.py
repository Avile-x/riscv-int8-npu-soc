#!/usr/bin/env python3
"""Generate deterministic V1 4x4 INT8 GEMM test vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.golden import (  # noqa: E402
    INT8_MAX,
    INT8_MIN,
    MATRIX_SIZE,
    GemmCase,
    Matrix,
    gemm4x4,
)

SCHEMA_VERSION: Final[int] = 1
DEFAULT_SEED: Final[int] = 20261025
DEFAULT_RANDOM_COUNT: Final[int] = 1000
RESULT_MIN: Final[int] = MATRIX_SIZE * INT8_MIN * INT8_MAX
RESULT_MAX: Final[int] = MATRIX_SIZE * INT8_MIN * INT8_MIN

def _matrix(rows: list[list[int]]) -> Matrix:
    return tuple(tuple(row) for row in rows)

def _random_matrix(rng: random.Random) -> Matrix:
    return tuple(
        tuple(rng.randint(INT8_MIN, INT8_MAX) for _ in range(MATRIX_SIZE))
        for _ in range(MATRIX_SIZE)
    )

_A_REF = _matrix(
    [
        [1, 2, 3, 4],
        [-1, 2, -3, 4],
        [5, -6, 7, -8],
        [-2, -3, 4, 5],
    ]
)
_B_REF = _matrix(
    [
        [1, -1, 2, 0],
        [2, 1, -2, 3],
        [-1, 2, 0, 1],
        [3, -2, 1, -1],
    ]
)
_C_REF = _matrix(
    [
        [14, -1, 2, 5],
        [18, -11, -2, -1],
        [-38, 19, 14, -3],
        [3, -3, 7, -10],
    ]
)
_ZERO = _matrix([[0, 0, 0, 0] for _ in range(MATRIX_SIZE)])
_IDENTITY = _matrix(
    [
        [1 if row == column else 0 for column in range(MATRIX_SIZE)]
        for row in range(MATRIX_SIZE)
    ]
)
_ALL_NEG128 = _matrix([[-128] * MATRIX_SIZE for _ in range(MATRIX_SIZE)])
_ALL_POS127 = _matrix([[127] * MATRIX_SIZE for _ in range(MATRIX_SIZE)])
_ALL_65536 = _matrix([[65536] * MATRIX_SIZE for _ in range(MATRIX_SIZE)])
_ALL_NEG65024 = _matrix([[-65024] * MATRIX_SIZE for _ in range(MATRIX_SIZE)])
_ALL_64516 = _matrix([[64516] * MATRIX_SIZE for _ in range(MATRIX_SIZE)])
_A_MIX = _matrix(
    [
        [127, 127, 127, 127],
        [127, 127, 127, 127],
        [-128, -128, -128, -128],
        [-128, -128, -128, -128],
    ]
)
_B_MIX = _matrix(
    [
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
        [-128, -128, 127, 127],
    ]
)
_C_MIX = _matrix(
    [
        [-65024, -65024, 64516, 64516],
        [-65024, -65024, 64516, 64516],
        [65536, 65536, -65024, -65024],
        [65536, 65536, -65024, -65024],
    ]
)

_FIXED_CASES: Final[tuple[GemmCase, ...]] = (
    GemmCase("reference", _A_REF, _B_REF, _C_REF),
    GemmCase("zero_left", _ZERO, _B_REF, _ZERO),
    GemmCase("zero_right", _A_REF, _ZERO, _ZERO),
    GemmCase("identity_left", _IDENTITY, _B_REF, _B_REF),
    GemmCase("identity_right", _A_REF, _IDENTITY, _A_REF),
    GemmCase("all_neg128", _ALL_NEG128, _ALL_NEG128, _ALL_65536),
    GemmCase(
        "all_neg128_x_pos127",
        _ALL_NEG128,
        _ALL_POS127,
        _ALL_NEG65024,
    ),
    GemmCase("all_pos127", _ALL_POS127, _ALL_POS127, _ALL_64516),
    GemmCase("mix_boundary", _A_MIX, _B_MIX, _C_MIX),
)

def make_cases(seed: int, random_count: int) -> list[GemmCase]:
    if type(seed) is not int:
        raise TypeError("seed must be int")
    if type(random_count) is not int:
        raise TypeError("random_count must be int")
    if random_count < 0:
        raise ValueError("random_count must be >= 0")

    cases = list(_FIXED_CASES)
    rng = random.Random(seed)
    for index in range(random_count):
        a = _random_matrix(rng)
        b = _random_matrix(rng)
        cases.append(
            GemmCase(f"random_{index:04d}", a, b, gemm4x4(a, b))
        )
    return cases

def _flatten_matrix(
    matrix: Matrix,
    label: str,
    minimum: int,
    maximum: int,
) -> list[int]:
    if type(matrix) is not tuple:
        raise TypeError(f"{label} must be a tuple of 4 tuples")
    if len(matrix) != MATRIX_SIZE:
        raise ValueError(f"{label} must have exactly 4 rows")

    values: list[int] = []
    for row_index, row in enumerate(matrix):
        if type(row) is not tuple:
            raise TypeError(f"{label}[{row_index}] must be a tuple")
        if len(row) != MATRIX_SIZE:
            raise ValueError(f"{label}[{row_index}] must have exactly 4 columns")
        for column_index, value in enumerate(row):
            if type(value) is not int:
                raise TypeError(
                    f"{label}[{row_index}][{column_index}] must be int"
                )
            if value < minimum or value > maximum:
                raise ValueError(
                    f"{label}[{row_index}][{column_index}]={value} "
                    f"outside [{minimum}, {maximum}]"
                )
            values.append(value)
    return values

def _render_vectors(
    cases: list[GemmCase], seed: int, random_count: int
) -> str:
    lines = [
        "{",
        f'  "schema_version": {SCHEMA_VERSION},',
        f'  "seed": {seed},',
        f'  "random_count": {random_count},',
        '  "cases": [',
    ]
    for index, case in enumerate(cases):
        a = _flatten_matrix(case.a, f"cases[{index}].a", INT8_MIN, INT8_MAX)
        b = _flatten_matrix(case.b, f"cases[{index}].b", INT8_MIN, INT8_MAX)
        expected = _flatten_matrix(
            case.expected,
            f"cases[{index}].expected",
            RESULT_MIN,
            RESULT_MAX,
        )
        suffix = "," if index + 1 < len(cases) else ""
        lines.extend(
            [
                "    {",
                f'      "name": {json.dumps(case.name, ensure_ascii=True)},',
                f'      "a": {json.dumps(a, separators=(",", ":"))},',
                f'      "b": {json.dumps(b, separators=(",", ":"))},',
                f'      "expected": '
                f'{json.dumps(expected, separators=(",", ":"))}',
                f"    }}{suffix}",
            ]
        )
    lines.extend(["  ]", "}"])
    return "\n".join(lines) + "\n"

def write_vectors(
    path: Path,
    cases: list[GemmCase],
    seed: int,
    random_count: int,
) -> None:
    if type(seed) is not int:
        raise TypeError("seed must be int")
    if type(random_count) is not int:
        raise TypeError("random_count must be int")
    if random_count < 0:
        raise ValueError("random_count must be >= 0")
    if len(cases) != len(_FIXED_CASES) + random_count:
        raise ValueError(
            "cases count must equal fixed cases plus random_count"
        )

    names: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, GemmCase):
            raise TypeError(f"cases[{index}] must be GemmCase")
        if type(case.name) is not str or not case.name:
            raise ValueError(f"cases[{index}].name must be a non-empty str")
        if case.name in names:
            raise ValueError(f"duplicate case name: {case.name!r}")
        names.add(case.name)

        actual = gemm4x4(case.a, case.b)
        if case.expected != actual:
            raise ValueError(
                f"case {case.name!r} expected does not match golden"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_vectors(cases, seed, random_count),
        encoding="utf-8",
        newline="\n",
    )

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic 4x4 INT8 GEMM vectors."
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--random-count",
        type=int,
        default=DEFAULT_RANDOM_COUNT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "vectors" / "v1_gemm4x4.json",
    )
    args = parser.parse_args()

    try:
        cases = make_cases(args.seed, args.random_count)
        write_vectors(args.output, cases, args.seed, args.random_count)
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"generated {len(cases)} cases "
        f"({len(_FIXED_CASES)} fixed + {args.random_count} random)"
    )
    print(f"seed={args.seed} random_count={args.random_count}")
    print(f"output={args.output}")
    print(f"sha256={digest}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
