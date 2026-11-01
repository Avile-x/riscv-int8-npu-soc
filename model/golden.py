#!/usr/bin/env python3
"""Pure-integer reference model for the V1 4x4 signed INT8 GEMM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

Matrix = tuple[tuple[int, ...], ...]

MATRIX_SIZE: Final[int] = 4
INT8_MIN: Final[int] = -128
INT8_MAX: Final[int] = 127

@dataclass(frozen=True)
class GemmCase:
    name: str
    a: Matrix
    b: Matrix
    expected: Matrix

def _validate_matrix(
    matrix: object, name: str, minimum: int, maximum: int
) -> None:
    if type(matrix) is not tuple:
        raise TypeError(f"{name} must be a tuple of 4 tuples")
    if len(matrix) != MATRIX_SIZE:
        raise ValueError(f"{name} must have exactly 4 rows")

    for row_index, row in enumerate(matrix):
        if type(row) is not tuple:
            raise TypeError(f"{name}[{row_index}] must be a tuple")
        if len(row) != MATRIX_SIZE:
            raise ValueError(f"{name}[{row_index}] must have exactly 4 columns")
        for column_index, value in enumerate(row):
            if type(value) is not int:
                raise TypeError(
                    f"{name}[{row_index}][{column_index}] must be int"
                )
            if value < minimum or value > maximum:
                raise ValueError(
                    f"{name}[{row_index}][{column_index}]={value} "
                    f"outside [{minimum}, {maximum}]"
                )

def validate_int8_matrix(matrix: Matrix) -> None:
    _validate_matrix(matrix, "matrix", INT8_MIN, INT8_MAX)

def gemm4x4(a: Matrix, b: Matrix) -> Matrix:
    _validate_matrix(a, "a", INT8_MIN, INT8_MAX)
    _validate_matrix(b, "b", INT8_MIN, INT8_MAX)

    return tuple(
        tuple(
            sum(a[i][k] * b[k][j] for k in range(MATRIX_SIZE))
            for j in range(MATRIX_SIZE)
        )
        for i in range(MATRIX_SIZE)
    )
