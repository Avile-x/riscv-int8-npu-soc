#!/usr/bin/env python3
"""V1-T05 synthesis, full regression and V1 stage-acceptance gate."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build"
SYNTH_BUILD = BUILD_ROOT / "v1_synth"
REGRESSION_BUILD = BUILD_ROOT / "v1_regression"

PE_TOP = "pe_ws"
ARRAY_TOP = "systolic_array_4x4"
PE_COUNT = 16
EXPECTED_YOSYS_PREFIX = "Yosys 0.52"
SYNTH_TIMEOUT_S = 180.0
REGRESSION_TIMEOUT_S = 1200.0

FORMAL_VECTOR = ROOT / "vectors/v1_gemm4x4.json"
EXPECTED_VECTOR_SHA256 = (
    "a05cbe733e3445cac4b9c7ac125340a53758bdca0a7af3e6b94bb68dc5b7fd0d"
)

EXPECTED_PE_CHECKS = 263_368
EXPECTED_PE_PREMARK_MUL = 1
EXPECTED_PE_PREMARK_SDFFE = 5
EXPECTED_ARRAY_PREMARK_MUL = 16
EXPECTED_ARRAY_PREMARK_SDFFE = 117

EXPECTED_ARRAY_PASS = (
    "ARRAY_PASS checks=188131 formal_cases=1009 "
    "formal_elements=16144 mapping_cases=64"
)
EXPECTED_ARRAY_SUMMARY = {
    "formal_cases": 1009,
    "formal_elements": 16144,
    "mapping_cases": 64,
    "transactions": 1092,
    "checks": 188131,
    "protocol_checks": 28364,
    "alignment_checks": 453696,
    "pe_checks": 1391792,
    "capture_checks": 17392,
    "reset_cases": 6,
    "stale_cases": 3,
}
EXPECTED_SMOKE_CHECKS = 274
EXPECTED_UNITTEST_TESTS = 18
EXPECTED_NEGATIVE_CASES = 22

ARRAY_SUMMARY_STDOUT_RE = re.compile(
    r"^summary: formal_cases=(\d+) formal_elements=(\d+) "
    r"mapping_cases=(\d+) transactions=(\d+) checks=(\d+) "
    r"protocol_checks=(\d+) alignment_checks=(\d+) pe_checks=(\d+) "
    r"capture_checks=(\d+)$",
    re.MULTILINE,
)
ARRAY_SUMMARY_LOG_RE = re.compile(
    r"^ARRAY_SUMMARY formal_cases=(\d+) formal_elements=(\d+) "
    r"mapping_cases=(\d+) transactions=(\d+) checks=(\d+) "
    r"protocol_checks=(\d+) alignment_checks=(\d+) pe_checks=(\d+) "
    r"capture_checks=(\d+) reset_cases=(\d+) stale_cases=(\d+)$",
    re.MULTILINE,
)
ARRAY_PASS_RE = re.compile(
    r"ARRAY_PASS checks=(\d+) formal_cases=(\d+) "
    r"formal_elements=(\d+) mapping_cases=(\d+)"
)
PE_PASS_RE = re.compile(r"PE_PASS checks=(\d+)")
SMOKE_PASS_RE = re.compile(r"SMOKE_PASS checks=(\d+)")

ALLOWED_MEMORY_WARNING_RE = re.compile(
    r"Warning: Replacing memory \\\w+ with list of registers\. "
    r"See (?:.*/)?rtl/core/systolic_array_4x4\.sv:\d+"
)
LATCH_CELL_RE = re.compile(
    r"^\s+(\$[^\s]*[Ll][Aa][Tt][Cc][Hh][^\s]*)\s+(\d+)\s*$", re.MULTILINE
)

REQUIRED_DOCS = (
    ROOT / "docs/architecture/v1-array-spec.md",
    ROOT / "docs/decisions/ADR-0002-v1-array-contract.md",
    ROOT / "env/tool_versions.txt",
    ROOT / "README.md",
)



class V1GateFailure(RuntimeError):
    pass


class ToolMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthesisReport:
    top: str
    wire_bits: int
    cell_count: int
    latch_count: int
    warning_count: int
    error_count: int


@dataclass(frozen=True)
class RegressionResult:
    command: str
    exit_code: int
    marker: str


class _SynthStats(NamedTuple):
    wire_bits: int
    cell_count: int
    latch_count: int
    total_warnings: int
    allowed_warnings: int
    unexpected_warnings: int
    error_count: int
    premap_mul: int
    premap_sdffe: int
    pe_instances: int
    marker_count: int


class _RegressionSpec(NamedTuple):
    name: str
    argv: tuple[str, ...]
    timeout_s: float
    expected_marker: str
    injections: tuple[str, ...] = ()


REGRESSION_SPECS = (
    _RegressionSpec(
        "unittest",
        ("python3", "-m", "unittest", "discover", "-s", "tests", "-v"),
        180.0,
        f"UNITTEST_OK tests={EXPECTED_UNITTEST_TESTS}",
    ),
    _RegressionSpec(
        "pe-sim",
        ("python3", "scripts/check_pe.py", "sim"),
        REGRESSION_TIMEOUT_S,
        f"PE_PASS checks={EXPECTED_PE_CHECKS}",
    ),
    _RegressionSpec(
        "pe-sim4",
        ("python3", "scripts/check_pe.py", "sim4"),
        REGRESSION_TIMEOUT_S,
        f"PE_PASS checks={EXPECTED_PE_CHECKS}",
        ("x", "z", "act_only", "psum_only", "illegal_control"),
    ),
    _RegressionSpec(
        "pe-synth",
        ("python3", "scripts/check_pe.py", "synth"),
        180.0,
        f"SYNTH_PASS top={PE_TOP}",
    ),
    _RegressionSpec(
        "array-sim",
        ("python3", "scripts/check_array.py", "sim"),
        REGRESSION_TIMEOUT_S,
        EXPECTED_ARRAY_PASS,
    ),
    _RegressionSpec(
        "array-sim4",
        ("python3", "scripts/check_array.py", "sim4"),
        REGRESSION_TIMEOUT_S,
        EXPECTED_ARRAY_PASS,
        ("x", "z", "mismatch", "watchdog", "pe_timing", "pe_weight"),
    ),
    _RegressionSpec(
        "smoke-sim",
        ("python3", "scripts/check_smoke.py", "sim"),
        180.0,
        f"SMOKE_PASS checks={EXPECTED_SMOKE_CHECKS}",
    ),
    _RegressionSpec(
        "smoke-sim4",
        ("python3", "scripts/check_smoke.py", "sim4"),
        180.0,
        f"SMOKE_PASS checks={EXPECTED_SMOKE_CHECKS}",
        ("X", "Z"),
    ),
    _RegressionSpec(
        "smoke-synth",
        ("python3", "scripts/check_smoke.py", "synth"),
        180.0,
        "SYNTH_PASS top=smoke_counter",
    ),
    _RegressionSpec(
        "runner-negative",
        ("python3", "scripts/test_runner_negative.py"),
        300.0,
        f"RUNNER_NEGATIVE_PASS cases={EXPECTED_NEGATIVE_CASES}",
    ),
    _RegressionSpec(
        "diff-check",
        ("git", "diff", "--check"),
        60.0,
        "GIT_DIFF_CHECK_CLEAN",
    ),
)


def _combined(cp: CompletedProcess[str]) -> str:
    return (cp.stdout or "") + (cp.stderr or "")


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _write_log(
    path: Path, argv: list[str], cp: CompletedProcess[str]
) -> None:
    path.write_text(
        f"$ {' '.join(argv)}\n"
        f"exit_code: {cp.returncode}\n"
        "--- stdout ---\n"
        f"{cp.stdout or ''}\n"
        "--- stderr ---\n"
        f"{cp.stderr or ''}\n",
        encoding="utf-8",
    )


def _write_timeout_log(
    path: Path, argv: list[str], exc: subprocess.TimeoutExpired
) -> None:
    path.write_text(
        f"$ {' '.join(argv)}\n"
        f"TIMEOUT after {exc.timeout}s\n"
        "--- stdout ---\n"
        f"{_as_text(exc.stdout)}\n"
        "--- stderr ---\n"
        f"{_as_text(exc.stderr)}\n",
        encoding="utf-8",
    )


def _run_and_log(
    argv: list[str], cwd: Path, timeout_s: float, log_path: Path
) -> CompletedProcess[str]:
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout_s,
            capture_output=True,
            text=True,
            check=False,
        )
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
        raise V1GateFailure(
            f"refusing to clean unexpected build directory: {resolved}"
        )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_nonempty_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise V1GateFailure(f"{label} missing or empty: {path}")


def _single_count(section: str, cell_type: str) -> int:
    matches = re.findall(
        rf"^\s+{re.escape(cell_type)}\s+(\d+)\s*$", section, re.MULTILINE
    )
    if len(matches) != 1:
        raise V1GateFailure(
            f"expected one {cell_type} statistic, got {matches!r}"
        )
    return int(matches[0])


def _parse_synth_stats(
    text: str,
    top: str,
    *,
    allow_memory_warnings: bool,
) -> _SynthStats:
    lines = text.splitlines()
    errors = [line for line in lines if line.startswith("ERROR:")]
    warnings = [line for line in lines if line.startswith("Warning:")]
    if allow_memory_warnings:
        allowed = [
            line for line in warnings if ALLOWED_MEMORY_WARNING_RE.fullmatch(line.strip())
        ]
    else:
        allowed = []
    unexpected = [line for line in warnings if line not in allowed]

    if errors:
        raise V1GateFailure(
            "Yosys reported errors: " + " | ".join(errors[:3])
        )
    if unexpected:
        raise V1GateFailure(
            "Yosys reported non-allowlisted warnings: "
            + " | ".join(unexpected[:3])
        )

    summary = re.search(
        r"Warnings:\s+(\d+) unique messages,\s+(\d+) total", text
    )
    total_warnings = (
        int(summary.group(2)) if summary is not None else len(warnings)
    )
    if total_warnings != len(warnings):
        raise V1GateFailure(
            "Yosys warning summary disagrees with warning lines "
            f"({total_warnings} vs {len(warnings)})"
        )

    marker = f"SYNTH_DONE top={top}"
    marker_count = sum(1 for line in lines if marker in line)
    if marker_count != 1:
        raise V1GateFailure(
            f"expected exactly one {marker!r} completion marker, "
            f"got {marker_count}"
        )

    if re.search(rf"Top module:\s+\\?{re.escape(top)}\b", text) is None:
        raise V1GateFailure(f"Yosys log does not identify top {top}")

    for begin, end, label in (
        ("PREMAP_BEGIN", "PREMAP_END", "pre-map stat"),
        ("FINAL_BEGIN", "FINAL_END", "final stat"),
    ):
        if text.count(begin) != 1 or text.count(end) != 1:
            raise V1GateFailure(
                f"Yosys {label} section markers missing or duplicated"
            )

    premap = text.split("PREMAP_BEGIN", 1)[1].split("PREMAP_END", 1)[0]
    final = text.split("FINAL_BEGIN", 1)[1].split("FINAL_END", 1)[0]

    if top == ARRAY_TOP:
        if "=== design hierarchy ===" not in premap:
            raise V1GateFailure(
                "array pre-map stat is missing the design hierarchy section"
            )
        premap_totals = premap.split("=== design hierarchy ===", 1)[1]
    else:
        premap_totals = premap

    premap_mul = _single_count(premap_totals, "$mul")
    premap_sdffe = _single_count(premap_totals, "$sdffe")

    wire_matches = re.findall(
        r"^\s*Number of wire bits:\s+(\d+)\s*$", final, re.MULTILINE
    )
    cell_matches = re.findall(
        r"^\s*Number of cells:\s+(\d+)\s*$", final, re.MULTILINE
    )
    if not wire_matches or not cell_matches:
        raise V1GateFailure("Yosys final report is missing cell/wire statistics")
    wire_bits = int(wire_matches[-1])
    cell_count = int(cell_matches[-1])
    if wire_bits <= 0 or cell_count <= 0:
        raise V1GateFailure(
            "Yosys final report contains zero cell or wire statistics"
        )

    latch_count = sum(int(count) for _, count in LATCH_CELL_RE.findall(final))
    if latch_count != 0:
        raise V1GateFailure(f"Yosys report contains {latch_count} latch cells")

    pe_instances = 0
    if top == ARRAY_TOP:
        pe_lines = re.findall(
            r"^\s+pe_ws\s+(\d+)\s*$", final, re.MULTILINE
        )
        if not pe_lines:
            raise V1GateFailure(
                "array final report does not contain 16 pe_ws instances"
            )
        if any(int(count) != PE_COUNT for count in pe_lines):
            raise V1GateFailure(
                f"array final report pe_ws counts {pe_lines} != {PE_COUNT}"
            )
        pe_instances = PE_COUNT

    expected_mul = (
        EXPECTED_ARRAY_PREMARK_MUL
        if top == ARRAY_TOP
        else EXPECTED_PE_PREMARK_MUL
    )
    expected_sdffe = (
        EXPECTED_ARRAY_PREMARK_SDFFE
        if top == ARRAY_TOP
        else EXPECTED_PE_PREMARK_SDFFE
    )
    if premap_mul != expected_mul:
        raise V1GateFailure(
            f"{top} pre-map multiplier count {premap_mul} != {expected_mul}"
        )
    if premap_sdffe != expected_sdffe:
        raise V1GateFailure(
            f"{top} pre-map register count {premap_sdffe} != {expected_sdffe}"
        )

    return _SynthStats(
        wire_bits=wire_bits,
        cell_count=cell_count,
        latch_count=latch_count,
        total_warnings=total_warnings,
        allowed_warnings=len(allowed),
        unexpected_warnings=len(unexpected),
        error_count=len(errors),
        premap_mul=premap_mul,
        premap_sdffe=premap_sdffe,
        pe_instances=pe_instances,
        marker_count=marker_count,
    )


PE_YOSYS_SCRIPT = """# V1-T05 fixed PE synthesis flow
read_verilog -sv -Irtl/core rtl/core/pe_ws.sv
hierarchy -check -top pe_ws
proc
opt
check -assert
select -assert-none t:$dlatch t:$_DLATCH_*
log "PREMAP_BEGIN"
stat
log "PREMAP_END"
synth -top pe_ws
check -assert
select -assert-none t:$dlatch t:$_DLATCH_*
log "FINAL_BEGIN"
stat
log "FINAL_END"
log "SYNTH_DONE top=pe_ws"
"""

ARRAY_YOSYS_SCRIPT = """# V1-T05 fixed array synthesis flow
read_verilog -sv -Irtl/core rtl/core/pe_ws.sv rtl/core/systolic_array_4x4.sv
hierarchy -check -top systolic_array_4x4
proc
opt
check -assert
select -assert-count 16 t:pe_ws
select -assert-none t:$dlatch t:$_DLATCH_*
log "PREMAP_BEGIN"
stat
log "PREMAP_END"
synth -top systolic_array_4x4
check -assert
select -assert-none t:$dlatch t:$_DLATCH_*
log "FINAL_BEGIN"
stat
log "FINAL_END"
log "SYNTH_DONE top=systolic_array_4x4"
"""


def run_v1_synthesis(build_dir: Path) -> SynthesisReport:
    _prepare_build_dir(build_dir)

    version_log = build_dir / "yosys_version.log"
    version_cp = _run_and_log(
        ["yosys", "-V"], ROOT, SYNTH_TIMEOUT_S, version_log
    )
    version_text = _combined(version_cp).strip()
    if version_cp.returncode != 0:
        raise V1GateFailure(
            f"yosys -V failed with exit {version_cp.returncode}; "
            f"see {version_log}"
        )
    if not version_text.startswith(EXPECTED_YOSYS_PREFIX):
        raise V1GateFailure(
            f"Yosys version {version_text!r} does not match fixed "
            f"{EXPECTED_YOSYS_PREFIX!r}"
        )

    pe_script = build_dir / "yosys_pe.ys"
    array_script = build_dir / "yosys_array.ys"
    pe_script.write_text(PE_YOSYS_SCRIPT, encoding="utf-8")
    array_script.write_text(ARRAY_YOSYS_SCRIPT, encoding="utf-8")

    pe_log = build_dir / "yosys_pe.log"
    pe_cp = _run_and_log(
        ["yosys", "-s", str(pe_script)], ROOT, SYNTH_TIMEOUT_S, pe_log
    )
    pe_text = _combined(pe_cp)
    if pe_cp.returncode != 0:
        raise V1GateFailure(
            f"pe_ws synthesis failed with exit {pe_cp.returncode}; "
            f"see {pe_log}"
        )
    pe_stats = _parse_synth_stats(
        pe_text, PE_TOP, allow_memory_warnings=False
    )

    array_log = build_dir / "yosys_array.log"
    array_cp = _run_and_log(
        ["yosys", "-s", str(array_script)], ROOT, SYNTH_TIMEOUT_S, array_log
    )
    array_text = _combined(array_cp)
    if array_cp.returncode != 0:
        raise V1GateFailure(
            f"systolic_array_4x4 synthesis failed with exit "
            f"{array_cp.returncode}; see {array_log}"
        )
    array_stats = _parse_synth_stats(
        array_text, ARRAY_TOP, allow_memory_warnings=True
    )

    report = SynthesisReport(
        top=ARRAY_TOP,
        wire_bits=array_stats.wire_bits,
        cell_count=array_stats.cell_count,
        latch_count=array_stats.latch_count,
        warning_count=array_stats.unexpected_warnings,
        error_count=array_stats.error_count,
    )
    meta = {
        "yosys_version": version_text.splitlines()[0],
        "pe": {
            "wire_bits": pe_stats.wire_bits,
            "cell_count": pe_stats.cell_count,
            "premap_mul": pe_stats.premap_mul,
            "premap_sdffe": pe_stats.premap_sdffe,
            "total_warnings": pe_stats.total_warnings,
            "allowed_warnings": pe_stats.allowed_warnings,
            "unexpected_warnings": pe_stats.unexpected_warnings,
        },
        "array": {
            "wire_bits": array_stats.wire_bits,
            "cell_count": array_stats.cell_count,
            "premap_mul": array_stats.premap_mul,
            "premap_sdffe": array_stats.premap_sdffe,
            "pe_instances": array_stats.pe_instances,
            "total_warnings": array_stats.total_warnings,
            "allowed_warnings": array_stats.allowed_warnings,
            "unexpected_warnings": array_stats.unexpected_warnings,
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in (
                ROOT / "rtl/core/pe_ws.sv",
                ROOT / "rtl/core/systolic_array_4x4.sv",
                ROOT / "rtl/core/npu_types.sv",
            )
        },
        "scripts": {
            "pe": str(pe_script.relative_to(ROOT)),
            "array": str(array_script.relative_to(ROOT)),
        },
    }
    (build_dir / "synthesis.json").write_text(
        json.dumps(dataclasses.asdict(report), indent=2) + "\n",
        encoding="utf-8",
    )
    (build_dir / "synthesis_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"SYNTH_PASS top={report.top} wire_bits={report.wire_bits} "
        f"cells={report.cell_count} latch={report.latch_count} "
        f"unexpected_warnings={report.warning_count} errors={report.error_count} "
        f"pe_instances={array_stats.pe_instances} "
        f"premap_mul={array_stats.premap_mul} "
        f"premap_sdffe={array_stats.premap_sdffe} "
        f"total_warnings={array_stats.total_warnings} "
        f"allowed_warnings={array_stats.allowed_warnings} "
        f"pe_cells={pe_stats.cell_count} pe_wire_bits={pe_stats.wire_bits} "
        f"yosys={meta['yosys_version']}"
    )
    return report


def _fail_regression(spec: _RegressionSpec, message: str, log_path: Path) -> None:
    raise V1GateFailure(f"{spec.name}: {message}; see {log_path}")


def _require_injection_lines(
    spec: _RegressionSpec, text: str, log_path: Path
) -> None:
    for injection in spec.injections:
        marker = f"four-state {injection} injection PASS"
        count = text.count(marker)
        if count != 1:
            _fail_regression(
                spec,
                f"expected one {marker!r}, got {count}",
                log_path,
            )


def _parse_regression_output(
    spec: _RegressionSpec, text: str, log_path: Path
) -> str:
    if spec.name == "unittest":
        ran = re.findall(r"^Ran (\d+) tests? in ", text, re.MULTILINE)
        if ran != [str(EXPECTED_UNITTEST_TESTS)]:
            _fail_regression(
                spec,
                f"expected Ran {EXPECTED_UNITTEST_TESTS} tests, got {ran!r}",
                log_path,
            )
        if len(re.findall(r"^OK$", text, re.MULTILINE)) != 1:
            _fail_regression(
                spec, "expected exactly one OK marker", log_path
            )
        if "FAILED" in text:
            _fail_regression(spec, "unittest emitted FAILED", log_path)
        return spec.expected_marker

    if spec.name in ("pe-sim", "pe-sim4"):
        matches = PE_PASS_RE.findall(text)
        if matches != [str(EXPECTED_PE_CHECKS)]:
            _fail_regression(
                spec,
                f"expected one PE_PASS checks={EXPECTED_PE_CHECKS}, "
                f"got {matches!r}",
                log_path,
            )
        if spec.name == "pe-sim4":
            _require_injection_lines(spec, text, log_path)
        return spec.expected_marker

    if spec.name == "pe-synth":
        if text.count(f"synthesis PASS: top={PE_TOP}") != 1:
            _fail_regression(
                spec, f"missing unique top={PE_TOP} synthesis marker", log_path
            )
        return spec.expected_marker

    if spec.name in ("array-sim", "array-sim4"):
        pass_matches = ARRAY_PASS_RE.findall(text)
        if len(pass_matches) != 1:
            _fail_regression(
                spec,
                f"expected one ARRAY_PASS marker, got {len(pass_matches)}",
                log_path,
            )
        pass_values = tuple(int(value) for value in pass_matches[0])
        expected_pass = (
            EXPECTED_ARRAY_SUMMARY["checks"],
            EXPECTED_ARRAY_SUMMARY["formal_cases"],
            EXPECTED_ARRAY_SUMMARY["formal_elements"],
            EXPECTED_ARRAY_SUMMARY["mapping_cases"],
        )
        if pass_values != expected_pass:
            _fail_regression(
                spec,
                f"ARRAY_PASS counters {pass_values} != {expected_pass}",
                log_path,
            )

        summary_matches = ARRAY_SUMMARY_STDOUT_RE.findall(text)
        if len(summary_matches) != 1:
            _fail_regression(
                spec,
                f"expected one summary line, got {len(summary_matches)}",
                log_path,
            )
        summary_values = tuple(int(value) for value in summary_matches[0])
        expected_summary = tuple(EXPECTED_ARRAY_SUMMARY.values())[:9]
        if summary_values != expected_summary:
            _fail_regression(
                spec,
                f"summary counters {summary_values} != {expected_summary}",
                log_path,
            )
        if spec.name == "array-sim4":
            _require_injection_lines(spec, text, log_path)
        return spec.expected_marker

    if spec.name in ("smoke-sim", "smoke-sim4"):
        matches = SMOKE_PASS_RE.findall(text)
        if matches != [str(EXPECTED_SMOKE_CHECKS)]:
            _fail_regression(
                spec,
                f"expected one SMOKE_PASS checks={EXPECTED_SMOKE_CHECKS}, "
                f"got {matches!r}",
                log_path,
            )
        if spec.name == "smoke-sim4":
            _require_injection_lines(spec, text, log_path)
        return spec.expected_marker

    if spec.name == "smoke-synth":
        if text.count("synthesis PASS: top=smoke_counter") != 1:
            _fail_regression(
                spec,
                "missing unique top=smoke_counter synthesis marker",
                log_path,
            )
        return spec.expected_marker

    if spec.name == "runner-negative":
        if text.count("runner negative paths PASS") != 1:
            _fail_regression(
                spec, "missing unique negative-path success marker", log_path
            )
        case_count = len(
            [
                line
                for line in text.splitlines()
                if line.startswith("PASS ")
            ]
        )
        if case_count != EXPECTED_NEGATIVE_CASES:
            _fail_regression(
                spec,
                f"expected {EXPECTED_NEGATIVE_CASES} negative cases, "
                f"got {case_count}",
                log_path,
            )
        if "FAIL:" in text:
            _fail_regression(spec, "negative-path runner emitted FAIL", log_path)
        return f"RUNNER_NEGATIVE_PASS cases={case_count}"

    if spec.name == "diff-check":
        if text.strip():
            _fail_regression(
                spec,
                f"git diff --check emitted output: {text.strip()[:200]!r}",
                log_path,
            )
        return spec.expected_marker

    raise V1GateFailure(f"unknown regression spec: {spec.name}")


def run_v1_regression() -> list[RegressionResult]:
    _prepare_build_dir(REGRESSION_BUILD)
    results: list[RegressionResult] = []
    failures = 0

    for spec in REGRESSION_SPECS:
        log_path = REGRESSION_BUILD / f"{spec.name}.log"
        try:
            cp = subprocess.run(
                list(spec.argv),
                cwd=ROOT,
                timeout=spec.timeout_s,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _write_timeout_log(log_path, list(spec.argv), exc)
            raise
        except FileNotFoundError:
            log_path.write_text(
                f"$ {' '.join(spec.argv)}\nTOOL_NOT_FOUND: {spec.argv[0]}\n",
                encoding="utf-8",
            )
            raise
        _write_log(log_path, list(spec.argv), cp)
        text = _combined(cp)

        if cp.returncode == 3:
            raise ToolMissing(
                f"{spec.name} reported a missing tool; see {log_path}"
            )

        if cp.returncode != 0:
            failures += 1
            result = RegressionResult(
                command=" ".join(spec.argv),
                exit_code=cp.returncode,
                marker=f"FAILED exit={cp.returncode}",
            )
            results.append(result)
            print(
                f"REGRESSION {spec.name}: exit={cp.returncode} "
                f"(see {log_path.relative_to(ROOT)})"
            )
            continue

        marker = _parse_regression_output(spec, text, log_path)
        result = RegressionResult(
            command=" ".join(spec.argv),
            exit_code=cp.returncode,
            marker=marker,
        )
        results.append(result)
        print(f"REGRESSION {spec.name}: exit=0 marker={marker}")

    payload = {
        "results": [dataclasses.asdict(result) for result in results],
        "expected_commands": [list(spec.argv) for spec in REGRESSION_SPECS],
    }
    (REGRESSION_BUILD / "regression.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )

    if failures:
        print(
            f"V1_REGRESSION_FAILED commands={len(results)} failures={failures}"
        )
    else:
        print(f"V1_REGRESSION_PASS commands={len(results)}")
    return results


def _require_text_contains(path: Path, needle: str, label: str) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if needle not in text:
        raise V1GateFailure(f"{label} does not contain {needle!r}: {path}")


def _require_vcd(path: Path, required_signals: tuple[str, ...]) -> None:
    _require_nonempty_file(path, "waveform")
    if path.stat().st_size < 1_000_000:
        raise V1GateFailure(f"waveform is unexpectedly small: {path}")
    head = path.read_text(encoding="latin-1", errors="replace")[:262144]
    for signal in required_signals:
        if signal not in head:
            raise V1GateFailure(
                f"waveform {path} does not contain signal {signal!r}"
            )


def _git_status(paths: tuple[str, ...]) -> str:
    cp = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths],
        cwd=ROOT,
        timeout=60.0,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        raise V1GateFailure(
            f"git status failed with exit {cp.returncode}: {cp.stderr.strip()}"
        )
    return cp.stdout.strip()


def validate_v1_gate(
    report: SynthesisReport, results: list[RegressionResult]
) -> None:
    failures: list[str] = []

    if report.top != ARRAY_TOP:
        failures.append(f"report top {report.top!r} != {ARRAY_TOP!r}")
    if report.cell_count <= 0:
        failures.append("report cell count is not positive")
    if report.wire_bits <= 0:
        failures.append("report wire-bit count is not positive")
    if report.latch_count != 0:
        failures.append(f"report latch count is {report.latch_count}, not 0")
    if report.warning_count != 0:
        failures.append(
            f"report has {report.warning_count} non-allowlisted warnings"
        )
    if report.error_count != 0:
        failures.append(f"report has {report.error_count} errors")

    if len(results) != len(REGRESSION_SPECS):
        failures.append(
            f"regression count {len(results)} != {len(REGRESSION_SPECS)}"
        )
    for spec, result in zip(REGRESSION_SPECS, results):
        expected_command = " ".join(spec.argv)
        if result.command != expected_command:
            failures.append(
                f"regression command {result.command!r} != {expected_command!r}"
            )
        if result.exit_code != 0:
            failures.append(
                f"regression {spec.name} exited {result.exit_code}"
            )
        if result.marker != spec.expected_marker:
            failures.append(
                f"regression {spec.name} marker {result.marker!r} != "
                f"{spec.expected_marker!r}"
            )
        log_path = REGRESSION_BUILD / f"{spec.name}.log"
        if not log_path.is_file() or log_path.stat().st_size == 0:
            failures.append(f"regression log missing or empty: {log_path}")

    vector_sha = _sha256(FORMAL_VECTOR)
    if vector_sha != EXPECTED_VECTOR_SHA256:
        failures.append(
            f"formal vector SHA-256 {vector_sha} != {EXPECTED_VECTOR_SHA256}"
        )

    for path in REQUIRED_DOCS:
        if not path.is_file() or path.stat().st_size == 0:
            failures.append(f"required evidence missing or empty: {path}")

    if not failures:
        try:
            _require_text_contains(
                ROOT / "docs/tasks/V1-T01.md", "R-01", "V1-T01 task card"
            )
            for name in ("V1-T02", "V1-T03", "V1-T04"):
                _require_text_contains(
                    ROOT / f"docs/tasks/{name}.md",
                    "R-02",
                    f"{name} task card",
                )
            _require_text_contains(
                ROOT / "docs/tasks/V1-T05.md", "R-03", "V1-T05 task card"
            )
            _require_text_contains(
                ROOT / "docs/tasks/V1-T05.md", "R-01", "V1-T05 traceability"
            )
            _require_text_contains(
                ROOT / "docs/tasks/V1-T05.md", "R-02", "V1-T05 traceability"
            )
            _require_text_contains(
                ROOT / "docs/results/E-20261025-10.md",
                "返工更正",
                "E-20261025-10",
            )
            _require_text_contains(
                ROOT / "docs/results/E-20261025-11.md",
                "返工",
                "E-20261025-11",
            )

            _require_nonempty_file(
                SYNTH_BUILD / "yosys_pe.log", "PE synthesis log"
            )
            _require_nonempty_file(
                SYNTH_BUILD / "yosys_array.log", "array synthesis log"
            )
            _require_nonempty_file(
                SYNTH_BUILD / "synthesis_meta.json", "synthesis metadata"
            )

            _require_vcd(
                BUILD_ROOT / "array_sim/array.vcd",
                ("weight_q", "act_valid", "psum_valid", "rsp_valid"),
            )
            _require_vcd(
                BUILD_ROOT / "array_sim4/array.vcd",
                ("weight_q", "act_valid", "psum_valid", "rsp_valid"),
            )

            for label, log_path in (
                ("array-sim", BUILD_ROOT / "array_sim/sim.log"),
                ("array-sim4", BUILD_ROOT / "array_sim4/run_normal.log"),
            ):
                _require_nonempty_file(log_path, f"{label} simulation log")
                tb_text = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                summaries = ARRAY_SUMMARY_LOG_RE.findall(tb_text)
                if len(summaries) != 1:
                    failures.append(
                        f"{label} TB log has {len(summaries)} "
                        "ARRAY_SUMMARY lines"
                    )
                    continue
                values = tuple(int(value) for value in summaries[0])
                expected = tuple(EXPECTED_ARRAY_SUMMARY.values())
                if values != expected:
                    failures.append(
                        f"{label} TB ARRAY_SUMMARY {values} != {expected}"
                    )

            dirty = _git_status(("model", "vectors"))
            if dirty:
                failures.append(
                    "golden/formal vectors have uncommitted changes: "
                    + dirty.replace("\n", " | ")
                )
        except V1GateFailure as exc:
            failures.append(str(exc))
        except FileNotFoundError as exc:
            raise ToolMissing(f"required tool not found: {exc.filename}") from exc

    if failures:
        raise V1GateFailure(
            "V1 gate failed:\n- " + "\n- ".join(failures)
        )

    print(
        f"V1_GATE_PASS top={report.top} cells={report.cell_count} "
        f"wire_bits={report.wire_bits} regressions={len(results)} "
        f"vector_sha256={EXPECTED_VECTOR_SHA256}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run V1-T05 synthesis, full regression and the V1 acceptance gate. "
            "With no phase, runs all three."
        )
    )
    parser.add_argument(
        "phase",
        nargs="?",
        choices=("all", "synth", "regress"),
        default="all",
    )
    args = parser.parse_args()

    try:
        report: SynthesisReport | None = None
        results: list[RegressionResult] = []

        if args.phase in ("all", "synth"):
            report = run_v1_synthesis(SYNTH_BUILD)
        if args.phase in ("all", "regress"):
            results = run_v1_regression()

        if args.phase == "all":
            assert report is not None
            validate_v1_gate(report, results)
            print(
                f"V1_PASS top={report.top} cells={report.cell_count} "
                f"wire_bits={report.wire_bits} commands={len(results)}"
            )
        elif args.phase == "regress":
            if any(result.exit_code != 0 for result in results):
                return 1
    except subprocess.TimeoutExpired as exc:
        print(f"FAIL: timed out after {exc.timeout}s", file=sys.stderr)
        return 2
    except (FileNotFoundError, ToolMissing) as exc:
        filename = getattr(exc, "filename", None)
        detail = filename if filename else str(exc)
        print(f"FAIL: required tool not found: {detail}", file=sys.stderr)
        return 3
    except V1GateFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
