#!/usr/bin/env python3
"""V1-T04 4x4 systolic-array build and verification entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final, Literal

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build"

sys.path.insert(0, str(ROOT))
from model.golden import gemm4x4  # noqa: E402

CheckKind = Literal["sim", "sim4"]

SIM_TOP = "tb_systolic_array_4x4"
SIM_COMPILE_TIMEOUT_S = 600.0
SIM_RUN_TIMEOUT_S = 300.0
FOUR_STATE_COMPILE_TIMEOUT_S = 300.0
FOUR_STATE_RUN_TIMEOUT_S = 300.0

FORMAL_CASES: Final[int] = 1009
FORMAL_ELEMENTS: Final[int] = FORMAL_CASES * 16
MAPPING_CASES: Final[int] = 64
EXPECTED_VECTOR_SHA256: Final[str] = (
    "a05cbe733e3445cac4b9c7ac125340a53758bdca0a7af3e6b94bb68dc5b7fd0d"
)

SIM_COMPILE_FORBIDDEN = (r"%Error", r"%Warning", r"ARRAY_FAIL")
SIM_RUN_FORBIDDEN = (r"%Error", r"%Warning", r"ARRAY_FAIL")
FOUR_STATE_COMPILE_FORBIDDEN = (
    r"error:",
    r"syntax error",
    r"warning:",
    r"sorry:",
    r"preprocessor failed",
    r"ARRAY_FAIL",
)
FOUR_STATE_NORMAL_FORBIDDEN = (r"ERROR:", r"FATAL:", r"ARRAY_FAIL")

SUMMARY_RE = re.compile(
    r"ARRAY_SUMMARY formal_cases=(\d+) formal_elements=(\d+) "
    r"mapping_cases=(\d+) transactions=(\d+) checks=(\d+) "
    r"protocol_checks=(\d+) alignment_checks=(\d+) pe_checks=(\d+) "
    r"capture_checks=(\d+) reset_cases=(\d+) stale_cases=(\d+)"
)
PASS_RE = re.compile(
    r"ARRAY_PASS checks=(\d+) formal_cases=(\d+) "
    r"formal_elements=(\d+) mapping_cases=(\d+)"
)

FOUR_STATE_INJECTIONS = (
    (
        "x",
        "+inject_x",
        (
            "reason=result_mismatch",
            "field=c[0][0]",
            "got=xxxxxxxx",
        ),
    ),
    (
        "z",
        "+inject_z",
        (
            "reason=result_mismatch",
            "field=c[1][1]",
            "got=zzzzzzzz",
        ),
    ),
    (
        "mismatch",
        "+inject_mismatch",
        (
            "reason=result_mismatch",
            "field=c[2][3]",
            "got=01234567",
        ),
    ),
    (
        "watchdog",
        "+inject_watchdog",
        ("reason=watchdog_timeout",),
    ),
    (
        "pe_timing",
        "+inject_pe_timing",
        ("reason=pe_valid_timing",),
    ),
    (
        "pe_weight",
        "+inject_pe_weight",
        ("reason=pe_weight_mismatch",),
    ),
)


class ArrayFailure(RuntimeError):
    pass


def run_tool(
    argv: list[str], cwd: Path, timeout_s: float
) -> CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout_s,
        capture_output=True,
        text=True,
        check=False,
    )


def _combined(cp: CompletedProcess[str]) -> str:
    return cp.stdout + cp.stderr


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _write_log(path: Path, argv: list[str], cp: CompletedProcess[str]) -> None:
    text = (
        f"$ {' '.join(argv)}\n"
        f"exit_code: {cp.returncode}\n"
        "--- stdout ---\n"
        f"{cp.stdout}\n"
        "--- stderr ---\n"
        f"{cp.stderr}\n"
    )
    path.write_text(text, encoding="utf-8")


def _write_timeout_log(
    path: Path, argv: list[str], exc: subprocess.TimeoutExpired
) -> None:
    text = (
        f"$ {' '.join(argv)}\n"
        f"TIMEOUT after {exc.timeout}s\n"
        "--- stdout ---\n"
        f"{_as_text(exc.stdout)}\n"
        "--- stderr ---\n"
        f"{_as_text(exc.stderr)}\n"
    )
    path.write_text(text, encoding="utf-8")


def _run_and_log(
    argv: list[str], cwd: Path, timeout_s: float, log_path: Path
) -> CompletedProcess[str]:
    try:
        cp = run_tool(argv, cwd, timeout_s)
    except subprocess.TimeoutExpired as exc:
        _write_timeout_log(log_path, argv, exc)
        raise
    except FileNotFoundError:
        log_path.write_text(
            f"$ {' '.join(argv)}\nTOOL_NOT_FOUND: {argv[0]}\n",
            encoding="utf-8",
        )
        raise
    _write_log(log_path, argv, cp)
    return cp


def _prepare_build_dir(build_dir: Path) -> None:
    resolved = build_dir.resolve()
    if resolved.parent != BUILD_ROOT.resolve():
        raise ArrayFailure(
            f"refusing to clean unexpected build directory: {resolved}"
        )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _fail_on_patterns(text: str, patterns: tuple[str, ...], label: str) -> None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise ArrayFailure(
                f"{label} emitted forbidden pattern {pattern!r}; see build log"
            )


def _flat_matrix(case: dict[str, object], key: str, index: int) -> tuple:
    raw = case.get(key)
    if not isinstance(raw, list) or len(raw) != 16:
        raise ArrayFailure(
            f"case {index}: {key} must be a flat list of 16 integers"
        )
    for position, value in enumerate(raw):
        if type(value) is not int:
            raise ArrayFailure(
                f"case {index}: {key}[{position}] is not an integer"
            )
        if key == "expected":
            if value < -(2**31) or value > 2**31 - 1:
                raise ArrayFailure(
                    f"case {index}: expected[{position}] outside int32"
                )
        elif value < -128 or value > 127:
            raise ArrayFailure(
                f"case {index}: {key}[{position}] outside int8"
            )
    return tuple(
        tuple(raw[r * 4 + c] for c in range(4)) for r in range(4)
    )


def _validate_and_encode(source: Path) -> tuple[int, list[int], list[int], list[int]]:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_VECTOR_SHA256:
        raise ArrayFailure(
            f"formal vector SHA-256 mismatch: got {digest}, "
            f"expected {EXPECTED_VECTOR_SHA256}"
        )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ArrayFailure(f"formal vector JSON is invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise ArrayFailure("formal vector JSON must be an object")
    if data.get("schema_version") != 1:
        raise ArrayFailure("formal vector schema_version must be 1")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != FORMAL_CASES:
        raise ArrayFailure(
            f"formal vector must contain exactly {FORMAL_CASES} cases"
        )

    names: set[str] = set()
    flat_a: list[int] = []
    flat_b: list[int] = []
    flat_c: list[int] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ArrayFailure(f"case {index}: must be an object")
        name = case.get("name")
        if not isinstance(name, str) or not name:
            raise ArrayFailure(f"case {index}: name must be a non-empty string")
        if name in names:
            raise ArrayFailure(f"duplicate case name: {name}")
        names.add(name)
        a = _flat_matrix(case, "a", index)
        b = _flat_matrix(case, "b", index)
        expected = _flat_matrix(case, "expected", index)
        computed = gemm4x4(a, b)
        if computed != expected:
            raise ArrayFailure(
                f"case {name}: expected matrix does not match golden model"
            )
        flat_a.extend(value for row in a for value in row)
        flat_b.extend(value for row in b for value in row)
        flat_c.extend(value for row in expected for value in row)

    return len(cases), flat_a, flat_b, flat_c


def prepare_vectors(source: Path, build_dir: Path) -> int:
    count, flat_a, flat_b, flat_c = _validate_and_encode(source)
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "formal_a.hex").write_text(
        "".join(f"{value & 0xFF:02x}\n" for value in flat_a),
        encoding="ascii",
    )
    (build_dir / "formal_b.hex").write_text(
        "".join(f"{value & 0xFF:02x}\n" for value in flat_b),
        encoding="ascii",
    )
    (build_dir / "formal_c.hex").write_text(
        "".join(f"{value & 0xFFFFFFFF:08x}\n" for value in flat_c),
        encoding="ascii",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    print(f"vectors: cases={count} sha256={digest}")
    return count


def _find_four_state_tool() -> tuple[Path, Path, bool]:
    candidates: list[Path] = []
    override = os.environ.get("FOUR_STATE_IVERILOG")
    if override:
        match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", override)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2).replace("\\", "/")
            candidates.append(Path(f"/mnt/{drive}/{rest}"))
        else:
            candidates.append(Path(override))

    native = shutil.which("iverilog")
    if native:
        candidates.append(Path(native))
    candidates.extend(
        [
            Path("/mnt/d/Software/iverilog/bin/iverilog.exe"),
            Path("/mnt/c/iverilog/bin/iverilog.exe"),
        ]
    )

    for compiler in candidates:
        if not compiler.is_file():
            continue
        if compiler.suffix.lower() == ".exe":
            vvp = compiler.with_name("vvp.exe")
            windows_tool = True
        else:
            native_vvp = shutil.which("vvp")
            if native_vvp is None:
                continue
            vvp = Path(native_vvp)
            windows_tool = False
        if vvp.is_file():
            return compiler, vvp, windows_tool

    raise FileNotFoundError("iverilog")


def _to_windows_path(path: Path) -> str:
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except FileNotFoundError as exc:
        raise ArrayFailure(
            "wslpath not found; cannot invoke Windows Icarus"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ArrayFailure("wslpath timed out while converting a path") from exc
    converted = result.stdout.strip()
    if result.returncode != 0 or not converted:
        raise ArrayFailure(f"wslpath failed for {path}: {result.stderr.strip()}")
    return converted


def _single_pass_marker(text: str) -> tuple[str, re.Match[str]]:
    pass_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("ARRAY_PASS")
    ]
    if len(pass_lines) != 1:
        raise ArrayFailure(
            f"expected exactly one valid ARRAY_PASS marker, got {pass_lines!r}"
        )
    match = PASS_RE.fullmatch(pass_lines[0])
    if match is None:
        raise ArrayFailure(
            f"expected exactly one valid ARRAY_PASS marker, got {pass_lines!r}"
        )
    return pass_lines[0], match


def _validate_summary(text: str, pass_match: re.Match[str]) -> str:
    summary_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("ARRAY_SUMMARY")
    ]
    if len(summary_lines) != 1:
        raise ArrayFailure(
            f"expected exactly one ARRAY_SUMMARY line, got {summary_lines!r}"
        )
    summary = SUMMARY_RE.fullmatch(summary_lines[0])
    if summary is None:
        raise ArrayFailure(f"malformed ARRAY_SUMMARY line: {summary_lines[0]!r}")
    (
        formal_cases,
        formal_elements,
        mapping_cases,
        transactions,
        checks,
        protocol_checks,
        alignment_checks,
        pe_checks,
        capture_checks,
        reset_cases,
        stale_cases,
    ) = (int(value) for value in summary.groups())
    if formal_cases != FORMAL_CASES:
        raise ArrayFailure(
            f"formal case count {formal_cases} != {FORMAL_CASES}"
        )
    if formal_elements != FORMAL_ELEMENTS:
        raise ArrayFailure(
            f"formal element count {formal_elements} != {FORMAL_ELEMENTS}"
        )
    if mapping_cases != MAPPING_CASES:
        raise ArrayFailure(
            f"mapping case count {mapping_cases} != {MAPPING_CASES}"
        )
    if reset_cases != 6:
        raise ArrayFailure(f"reset case count {reset_cases} != 6")
    if stale_cases != 3:
        raise ArrayFailure(f"consecutive stale-check count {stale_cases} != 3")
    if transactions < FORMAL_CASES + MAPPING_CASES:
        raise ArrayFailure(
            f"transaction count {transactions} below formal+mapping total"
        )
    if checks < FORMAL_ELEMENTS or protocol_checks <= 0:
        raise ArrayFailure("summary reports insufficient checks")
    if alignment_checks <= 0 or capture_checks <= 0:
        raise ArrayFailure("summary reports no alignment or capture checks")
    if pe_checks < 1_000_000:
        raise ArrayFailure(
            "summary reports insufficient independent per-PE checks "
            f"({pe_checks})"
        )

    pass_checks, pass_formal, pass_elements, pass_mapping = (
        int(value) for value in pass_match.groups()
    )
    if (pass_checks, pass_formal, pass_elements, pass_mapping) != (
        checks,
        formal_cases,
        formal_elements,
        mapping_cases,
    ):
        raise ArrayFailure("ARRAY_PASS counters disagree with ARRAY_SUMMARY")

    return (
        f"formal_cases={formal_cases} formal_elements={formal_elements} "
        f"mapping_cases={mapping_cases} transactions={transactions} "
        f"checks={checks} protocol_checks={protocol_checks} "
        f"alignment_checks={alignment_checks} pe_checks={pe_checks} "
        f"capture_checks={capture_checks}"
    )


def _require_nonempty_vcd(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ArrayFailure(f"{label} VCD trace missing or empty: {path}")


def check_simulation(build_dir: Path) -> None:
    _prepare_build_dir(build_dir)
    count = prepare_vectors(ROOT / "vectors/v1_gemm4x4.json", build_dir)
    if count != FORMAL_CASES:
        raise ArrayFailure(f"prepared {count} formal cases, expected {FORMAL_CASES}")

    obj_dir = build_dir / "obj"
    compile_log = build_dir / "compile.log"
    sim_log = build_dir / "sim.log"
    vcd_path = build_dir / "array.vcd"
    binary = obj_dir / "sim_array"

    compile_argv = [
        "verilator",
        "--binary",
        "--timing",
        "--trace",
        "-Wall",
        "-Wno-fatal",
        f"-I{ROOT / 'rtl/core'}",
        "--top-module",
        SIM_TOP,
        "--Mdir",
        str(obj_dir),
        "-o",
        "sim_array",
        str(ROOT / "rtl/core/pe_ws.sv"),
        str(ROOT / "rtl/core/systolic_array_4x4.sv"),
        str(ROOT / "tb/core/tb_systolic_array_4x4.sv"),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, SIM_COMPILE_TIMEOUT_S, compile_log
    )
    if compile_result.returncode != 0:
        raise ArrayFailure(
            f"Verilator compile failed with exit "
            f"{compile_result.returncode}; see {compile_log}"
        )
    _fail_on_patterns(
        _combined(compile_result), SIM_COMPILE_FORBIDDEN, "Verilator compile"
    )
    if not binary.is_file():
        raise ArrayFailure(f"simulation binary missing: {binary}")

    run_result = _run_and_log(
        [str(binary)], build_dir, SIM_RUN_TIMEOUT_S, sim_log
    )
    if run_result.returncode != 0:
        raise ArrayFailure(
            f"simulation failed with exit {run_result.returncode}; see {sim_log}"
        )
    run_text = _combined(run_result)
    _fail_on_patterns(run_text, SIM_RUN_FORBIDDEN, "simulation")
    pass_line, pass_match = _single_pass_marker(run_text)
    summary = _validate_summary(run_text, pass_match)
    _require_nonempty_vcd(vcd_path, "simulation")
    print(f"simulation PASS: {pass_line}")
    print(f"summary: {summary}")


def check_four_state(build_dir: Path) -> None:
    compiler, vvp, windows_tool = _find_four_state_tool()
    origin = "Windows Icarus via WSL interop" if windows_tool else "native Icarus"
    print(f"four-state tool: {compiler} ({origin})")

    _prepare_build_dir(build_dir)
    count = prepare_vectors(ROOT / "vectors/v1_gemm4x4.json", build_dir)
    if count != FORMAL_CASES:
        raise ArrayFailure(f"prepared {count} formal cases, expected {FORMAL_CASES}")

    compile_log = build_dir / "compile.log"
    binary = build_dir / "sim_array4.vvp"
    include_dir = ROOT / "rtl/core"

    def tool_path(path: Path) -> str:
        return _to_windows_path(path) if windows_tool else str(path)

    incdir = _to_windows_path(include_dir) if windows_tool else str(include_dir)
    compile_argv = [
        str(compiler),
        "-g2012",
        "-Wall",
        "-I",
        incdir,
        "-o",
        tool_path(binary),
        tool_path(ROOT / "rtl/core/pe_ws.sv"),
        tool_path(ROOT / "rtl/core/systolic_array_4x4.sv"),
        tool_path(ROOT / "tb/core/tb_systolic_array_4x4.sv"),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, FOUR_STATE_COMPILE_TIMEOUT_S, compile_log
    )
    compile_text = _combined(compile_result)
    if compile_result.returncode != 0:
        raise ArrayFailure(
            f"Icarus compile failed with exit {compile_result.returncode}; "
            f"see {compile_log}"
        )
    _fail_on_patterns(
        compile_text, FOUR_STATE_COMPILE_FORBIDDEN, "Icarus compile"
    )
    if not binary.is_file():
        raise ArrayFailure(f"four-state simulation binary missing: {binary}")

    for name, plusarg, markers in FOUR_STATE_INJECTIONS:
        log_path = build_dir / f"run_{name}.log"
        result = _run_and_log(
            [str(vvp), tool_path(binary), plusarg],
            build_dir,
            FOUR_STATE_RUN_TIMEOUT_S,
            log_path,
        )
        text = _combined(result)
        if result.returncode == 0:
            raise ArrayFailure(
                f"{name} injection unexpectedly passed (exit 0); see {log_path}"
            )
        if "ARRAY_PASS" in text:
            raise ArrayFailure(f"{name} injection emitted ARRAY_PASS; see {log_path}")
        for marker in markers:
            if marker not in text:
                raise ArrayFailure(
                    f"{name} injection did not report {marker!r}; "
                    f"see {log_path}"
                )
        print(
            f"four-state {name} injection PASS: exit={result.returncode}, "
            f"marker={markers[-1]}"
        )

    normal_log = build_dir / "run_normal.log"
    normal_result = _run_and_log(
        [str(vvp), tool_path(binary)],
        build_dir,
        FOUR_STATE_RUN_TIMEOUT_S,
        normal_log,
    )
    normal_text = _combined(normal_result)
    if normal_result.returncode != 0:
        raise ArrayFailure(
            f"four-state normal simulation failed with exit "
            f"{normal_result.returncode}; see {normal_log}"
        )
    _fail_on_patterns(
        normal_text, FOUR_STATE_NORMAL_FORBIDDEN, "Icarus normal run"
    )
    pass_line, pass_match = _single_pass_marker(normal_text)
    summary = _validate_summary(normal_text, pass_match)
    _require_nonempty_vcd(build_dir / "array.vcd", "four-state")
    print(f"four-state normal PASS: {pass_line}")
    print(f"summary: {summary}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the V1-T04 systolic-array simulation or four-state check."
    )
    parser.add_argument("kind", choices=("sim", "sim4"))
    args = parser.parse_args()

    build_dir = BUILD_ROOT / f"array_{args.kind}"

    try:
        if args.kind == "sim":
            check_simulation(build_dir)
        else:
            check_four_state(build_dir)
    except subprocess.TimeoutExpired as exc:
        print(
            f"FAIL: '{args.kind}' timed out after {exc.timeout}s",
            file=sys.stderr,
        )
        return 2
    except FileNotFoundError as exc:
        print(f"FAIL: required tool not found: {exc.filename}", file=sys.stderr)
        return 3
    except ArrayFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"PASS: {args.kind} (build directory: {build_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
