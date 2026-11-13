#!/usr/bin/env python3
"""Isolated negative-path checks for scripts/check_smoke.py."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "check_smoke.py"
PE_RUNNER = ROOT / "scripts" / "check_pe.py"
ARRAY_RUNNER = ROOT / "scripts" / "check_array.py"
V1_RUNNER = ROOT / "scripts" / "check_v1.py"

ARRAY_PASS_LINE = (
    "ARRAY_PASS checks=16144 formal_cases=1009 "
    "formal_elements=16144 mapping_cases=64"
)
ARRAY_SUMMARY_LINE = (
    "ARRAY_SUMMARY formal_cases=1009 formal_elements=16144 "
    "mapping_cases=64 transactions=1700 checks=20000 "
    "protocol_checks=500 alignment_checks=500 pe_checks=1400000 "
    "capture_checks=100 reset_cases=6 stale_cases=3"
)

FAKE_VERILATOR = r"""#!/bin/sh
set -eu
mdir=""
out="sim_smoke"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --Mdir) mdir="$2"; shift 2 ;;
        -o) out="$2"; shift 2 ;;
        *) shift ;;
    esac
done
mkdir -p "$mdir"
{
    printf '%s\n' '#!/bin/sh'
    if [ -n "${FAKE_SIM_SLEEP:-}" ]; then
        printf 'sleep %s\n' "$FAKE_SIM_SLEEP"
    fi
    printf '%s\n' 'printf "%b\n" "$FAKE_SIM_OUTPUT"'
    printf '%s\n' 'exit 0'
} > "$mdir/$out"
chmod +x "$mdir/$out"
exit 0
"""


FAKE_YOSYS = r"""#!/bin/sh
set -eu
if [ "${1:-}" = "-V" ]; then
    echo "Yosys 0.52 (fake)"
    exit 0
fi
script=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -s) script="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [ -n "${FAKE_YOSYS_SLEEP:-}" ]; then
    sleep "$FAKE_YOSYS_SLEEP"
fi
target=pe
module=pe_ws
case "$script" in
    *array*) target=array; module=systolic_array_4x4 ;;
esac
echo "Yosys 0.52 (fake)"
echo "Top module:  \\$module"
echo "PREMAP_BEGIN"
if [ "$target" = "array" ]; then
    echo "=== systolic_array_4x4 ==="
    echo "   Number of cells: 133"
    echo "=== design hierarchy ==="
    echo "   systolic_array_4x4 1"
    echo "     pe_ws 16"
    echo "   Number of cells: 117"
    echo "     \$mul 16"
    echo "     \$sdffe 117"
else
    echo "=== pe_ws ==="
    echo "   Number of cells: 6"
    echo "     \$mul 1"
    echo "     \$sdffe 5"
fi
echo "PREMAP_END"
echo "FINAL_BEGIN"
mode="${FAKE_YOSYS_MODE:-valid}"
if [ "$mode" = "zero_cells" ] && [ "$target" = "array" ]; then
    echo "   Number of wire bits: 0"
    echo "   Number of cells: 0"
elif [ "$mode" = "pe_bad_cells" ] && [ "$target" = "pe" ]; then
    echo "   Number of wire bits: 1198"
    echo "   Number of cells: 10"
    echo "     \$_AND_ 10"
elif [ "$mode" = "array_bad_wire" ] && [ "$target" = "array" ]; then
    echo "   Number of wire bits: 10"
    echo "   Number of cells: 18246"
    echo "     pe_ws 16"
    echo "     \$_AND_ 18230"
elif [ "$target" = "array" ]; then
    echo "   Number of wire bits: 20378"
    echo "   Number of cells: 18246"
    echo "     pe_ws 16"
    echo "     \$_AND_ 18230"
else
    echo "   Number of wire bits: 1198"
    echo "   Number of cells: 1144"
    echo "     \$_AND_ 1143"
fi
echo "FINAL_END"
case "$mode" in
    no_marker)
        ;;
    dup_marker)
        echo "SYNTH_DONE top=$module"
        echo "SYNTH_DONE top=$module"
        ;;
    array_no_marker)
        if [ "$target" = "array" ]; then
            :
        else
            echo "SYNTH_DONE top=$module"
        fi
        ;;
    *)
        echo "SYNTH_DONE top=$module"
        ;;
esac
exit 0
"""


def _make_root(*, strip_four_state_defaults: bool = False) -> Path:
    root = Path(tempfile.mkdtemp(prefix="smoke-runner-negative-"))
    (root / "scripts").mkdir()
    runner_copy = root / "scripts" / "check_smoke.py"
    shutil.copy2(RUNNER, runner_copy)
    if strip_four_state_defaults:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            'Path("/mnt/d/Software/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        text = text.replace(
            'Path("/mnt/c/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        runner_copy.write_text(text, encoding="utf-8")
    (root / "rtl" / "smoke").mkdir(parents=True)
    (root / "tb" / "smoke").mkdir(parents=True)
    (root / "rtl" / "smoke" / "smoke_counter.sv").write_text(
        "", encoding="utf-8"
    )
    (root / "tb" / "smoke" / "tb_smoke_counter.sv").write_text(
        "", encoding="utf-8"
    )
    return root


def _run_case(
    name: str,
    kind: str,
    fake_output: str | None,
    expected_code: int,
    expected_text: str,
    *,
    strip_four_state_defaults: bool = False,
) -> None:
    root = _make_root(strip_four_state_defaults=strip_four_state_defaults)
    try:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(bin_dir)
        env.pop("FOUR_STATE_IVERILOG", None)

        if fake_output is not None:
            fake = bin_dir / "verilator"
            fake.write_text(FAKE_VERILATOR, encoding="utf-8")
            fake.chmod(0o755)
            for tool in ("mkdir", "chmod"):
                target = shutil.which(tool)
                if target is None:
                    raise RuntimeError(f"required host tool not found: {tool}")
                os.symlink(target, bin_dir / tool)
            env["FAKE_SIM_OUTPUT"] = fake_output

        if strip_four_state_defaults:
            env["FOUR_STATE_IVERILOG"] = "/nonexistent/iverilog"

        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_smoke.py"), kind],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != expected_code:
            raise AssertionError(
                f"{name}: expected exit {expected_code}, "
                f"got {result.returncode}\n{output}"
            )
        if expected_text not in output:
            raise AssertionError(
                f"{name}: expected text {expected_text!r} not found\n{output}"
            )
        print(f"PASS {name}: exit={result.returncode}, message matched")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_pe_root(
    *,
    strip_four_state_defaults: bool = False,
    patch_timeout: bool = False,
) -> Path:
    root = Path(tempfile.mkdtemp(prefix="pe-runner-negative-"))
    (root / "scripts").mkdir()
    runner_copy = root / "scripts" / "check_pe.py"
    shutil.copy2(PE_RUNNER, runner_copy)
    if strip_four_state_defaults:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            'Path("/mnt/d/Software/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        text = text.replace(
            'Path("/mnt/c/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        runner_copy.write_text(text, encoding="utf-8")
    if patch_timeout:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            "SIM_RUN_TIMEOUT_S = 120.0",
            "SIM_RUN_TIMEOUT_S = 1.0",
        )
        runner_copy.write_text(text, encoding="utf-8")
    (root / "rtl" / "core").mkdir(parents=True)
    (root / "tb" / "core").mkdir(parents=True)
    (root / "rtl" / "core" / "npu_types.sv").write_text("", encoding="utf-8")
    (root / "rtl" / "core" / "pe_ws.sv").write_text("", encoding="utf-8")
    (root / "tb" / "core" / "tb_pe_ws.sv").write_text("", encoding="utf-8")
    return root


def _run_pe_case(
    name: str,
    kind: str,
    fake_output: str | None,
    expected_code: int,
    expected_text: str,
    *,
    strip_four_state_defaults: bool = False,
    patch_timeout: bool = False,
    fake_sleep: bool = False,
) -> None:
    root = _make_pe_root(
        strip_four_state_defaults=strip_four_state_defaults,
        patch_timeout=patch_timeout,
    )
    try:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(bin_dir)
        env.pop("FOUR_STATE_IVERILOG", None)

        if fake_output is not None:
            fake = bin_dir / "verilator"
            fake.write_text(FAKE_VERILATOR, encoding="utf-8")
            fake.chmod(0o755)
            targets = ["mkdir", "chmod"]
            if fake_sleep:
                targets.append("sleep")
            for tool in targets:
                target = shutil.which(tool)
                if target is None:
                    raise RuntimeError(f"required host tool not found: {tool}")
                os.symlink(target, bin_dir / tool)
            env["FAKE_SIM_OUTPUT"] = fake_output
            if fake_sleep:
                env["FAKE_SIM_SLEEP"] = "2"

        if strip_four_state_defaults:
            env["FOUR_STATE_IVERILOG"] = "/nonexistent/iverilog"

        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_pe.py"), kind],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != expected_code:
            raise AssertionError(
                f"{name}: expected exit {expected_code}, "
                f"got {result.returncode}\n{output}"
            )
        if expected_text not in output:
            raise AssertionError(
                f"{name}: expected text {expected_text!r} not found\n{output}"
            )
        print(f"PASS {name}: exit={result.returncode}, message matched")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_array_root(
    *,
    strip_four_state_defaults: bool = False,
    patch_timeout: bool = False,
) -> Path:
    root = Path(tempfile.mkdtemp(prefix="array-runner-negative-"))
    (root / "scripts").mkdir()
    runner_copy = root / "scripts" / "check_array.py"
    shutil.copy2(ARRAY_RUNNER, runner_copy)
    if strip_four_state_defaults:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            'Path("/mnt/d/Software/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        text = text.replace(
            'Path("/mnt/c/iverilog/bin/iverilog.exe")',
            'Path("/nonexistent/iverilog.exe")',
        )
        runner_copy.write_text(text, encoding="utf-8")
    if patch_timeout:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            "SIM_RUN_TIMEOUT_S = 300.0",
            "SIM_RUN_TIMEOUT_S = 1.0",
        )
        runner_copy.write_text(text, encoding="utf-8")
    (root / "vectors").mkdir()
    shutil.copy2(
        ROOT / "vectors" / "v1_gemm4x4.json",
        root / "vectors" / "v1_gemm4x4.json",
    )
    (root / "model").mkdir()
    shutil.copy2(ROOT / "model" / "golden.py", root / "model" / "golden.py")
    (root / "rtl" / "core").mkdir(parents=True)
    (root / "tb" / "core").mkdir(parents=True)
    (root / "rtl" / "core" / "npu_types.sv").write_text("", encoding="utf-8")
    (root / "rtl" / "core" / "pe_ws.sv").write_text("", encoding="utf-8")
    (root / "rtl" / "core" / "systolic_array_4x4.sv").write_text(
        "", encoding="utf-8"
    )
    (root / "tb" / "core" / "tb_systolic_array_4x4.sv").write_text(
        "", encoding="utf-8"
    )
    return root


def _run_array_case(
    name: str,
    kind: str,
    fake_output: str | None,
    expected_code: int,
    expected_text: str,
    *,
    strip_four_state_defaults: bool = False,
    patch_timeout: bool = False,
    fake_sleep: bool = False,
) -> None:
    root = _make_array_root(
        strip_four_state_defaults=strip_four_state_defaults,
        patch_timeout=patch_timeout,
    )
    try:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(bin_dir)
        env.pop("FOUR_STATE_IVERILOG", None)

        if fake_output is not None:
            fake = bin_dir / "verilator"
            fake.write_text(FAKE_VERILATOR, encoding="utf-8")
            fake.chmod(0o755)
            targets = ["mkdir", "chmod"]
            if fake_sleep:
                targets.append("sleep")
            for tool in targets:
                target = shutil.which(tool)
                if target is None:
                    raise RuntimeError(f"required host tool not found: {tool}")
                os.symlink(target, bin_dir / tool)
            env["FAKE_SIM_OUTPUT"] = fake_output
            if fake_sleep:
                env["FAKE_SIM_SLEEP"] = "2"

        if strip_four_state_defaults:
            env["FOUR_STATE_IVERILOG"] = "/nonexistent/iverilog"

        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_array.py"), kind],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != expected_code:
            raise AssertionError(
                f"{name}: expected exit {expected_code}, "
                f"got {result.returncode}\n{output}"
            )
        if expected_text not in output:
            raise AssertionError(
                f"{name}: expected text {expected_text!r} not found\n{output}"
            )
        print(f"PASS {name}: exit={result.returncode}, message matched")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_v1_root(*, patch_timeout: bool = False) -> Path:
    root = Path(tempfile.mkdtemp(prefix="v1-runner-negative-"))
    (root / "scripts").mkdir()
    runner_copy = root / "scripts" / "check_v1.py"
    shutil.copy2(V1_RUNNER, runner_copy)
    if patch_timeout:
        text = runner_copy.read_text(encoding="utf-8")
        text = text.replace(
            "SYNTH_TIMEOUT_S = 180.0",
            "SYNTH_TIMEOUT_S = 1.0",
        )
        runner_copy.write_text(text, encoding="utf-8")
    for relative in (
        "rtl/core/npu_types.sv",
        "rtl/core/pe_ws.sv",
        "rtl/core/systolic_array_4x4.sv",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def _run_v1_case(
    name: str,
    fake_mode: str | None,
    expected_code: int,
    expected_text: str,
    *,
    patch_timeout: bool = False,
    fake_sleep: str | None = None,
) -> None:
    root = _make_v1_root(patch_timeout=patch_timeout)
    try:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(bin_dir)

        if fake_mode is not None:
            fake = bin_dir / "yosys"
            fake.write_text(FAKE_YOSYS, encoding="utf-8")
            fake.chmod(0o755)
            sleep_tool = shutil.which("sleep")
            if sleep_tool is None:
                raise RuntimeError("required host tool not found: sleep")
            os.symlink(sleep_tool, bin_dir / "sleep")
            env["FAKE_YOSYS_MODE"] = fake_mode
            if fake_sleep is not None:
                env["FAKE_YOSYS_SLEEP"] = fake_sleep

        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_v1.py"), "synth"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != expected_code:
            raise AssertionError(
                f"{name}: expected exit {expected_code}, "
                f"got {result.returncode}\n{output}"
            )
        if expected_text not in output:
            raise AssertionError(
                f"{name}: expected text {expected_text!r} not found\n{output}"
            )
        print(f"PASS {name}: exit={result.returncode}, message matched")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    try:
        _run_case(
            "sim-tool-missing",
            "sim",
            None,
            3,
            "required tool not found",
        )
        _run_case(
            "sim4-tool-missing",
            "sim4",
            None,
            3,
            "required tool not found",
            strip_four_state_defaults=True,
        )
        _run_case(
            "missing-marker",
            "sim",
            "no marker here",
            1,
            "expected exactly one valid SMOKE_PASS marker",
        )
        _run_case(
            "duplicate-marker",
            "sim",
            "SMOKE_PASS checks=1\nSMOKE_PASS checks=1",
            1,
            "expected exactly one valid SMOKE_PASS marker",
        )
        _run_pe_case(
            "pe-sim-tool-missing",
            "sim",
            None,
            3,
            "required tool not found",
        )
        _run_pe_case(
            "pe-sim4-tool-missing",
            "sim4",
            None,
            3,
            "required tool not found",
            strip_four_state_defaults=True,
        )
        _run_pe_case(
            "pe-synth-tool-missing",
            "synth",
            None,
            3,
            "required tool not found",
        )
        _run_pe_case(
            "pe-sim-timeout",
            "sim",
            "PE_PASS checks=1",
            2,
            "timed out after",
            patch_timeout=True,
            fake_sleep=True,
        )
        _run_pe_case(
            "pe-sim-missing-marker",
            "sim",
            "no marker here",
            1,
            "expected exactly one valid PE_PASS marker",
        )
        _run_pe_case(
            "pe-sim-duplicate-marker",
            "sim",
            "PE_PASS checks=1\nPE_PASS checks=1",
            1,
            "expected exactly one valid PE_PASS marker",
        )
        _run_array_case(
            "array-sim-tool-missing",
            "sim",
            None,
            3,
            "required tool not found",
        )
        _run_array_case(
            "array-sim4-tool-missing",
            "sim4",
            None,
            3,
            "required tool not found",
            strip_four_state_defaults=True,
        )
        _run_array_case(
            "array-sim-timeout",
            "sim",
            ARRAY_PASS_LINE + "\n" + ARRAY_SUMMARY_LINE,
            2,
            "timed out after",
            patch_timeout=True,
            fake_sleep=True,
        )
        _run_array_case(
            "array-sim-missing-marker",
            "sim",
            "no marker here",
            1,
            "expected exactly one valid ARRAY_PASS marker",
        )
        _run_array_case(
            "array-sim-duplicate-marker",
            "sim",
            ARRAY_PASS_LINE + "\n" + ARRAY_PASS_LINE,
            1,
            "expected exactly one valid ARRAY_PASS marker",
        )
        _run_array_case(
            "array-sim-missing-summary",
            "sim",
            ARRAY_PASS_LINE,
            1,
            "expected exactly one ARRAY_SUMMARY line",
        )
        _run_array_case(
            "array-sim-summary-mismatch",
            "sim",
            ARRAY_PASS_LINE
            + "\n"
            + ARRAY_SUMMARY_LINE.replace(
                "formal_cases=1009", "formal_cases=1008", 1
            ),
            1,
            "formal case count 1008 != 1009",
        )
        _run_v1_case(
            "v1-synth-tool-missing",
            None,
            3,
            "required tool not found",
        )
        _run_v1_case(
            "v1-synth-timeout",
            "valid",
            2,
            "timed out after",
            patch_timeout=True,
            fake_sleep="5",
        )
        _run_v1_case(
            "v1-synth-missing-marker",
            "no_marker",
            1,
            "expected exactly one 'SYNTH_DONE top=pe_ws' completion marker",
        )
        _run_v1_case(
            "v1-synth-duplicate-marker",
            "dup_marker",
            1,
            "expected exactly one 'SYNTH_DONE top=pe_ws' completion marker",
        )
        _run_v1_case(
            "v1-synth-zero-cell-report",
            "zero_cells",
            1,
            "zero cell or wire statistics",
        )
        _run_v1_case(
            "v1-synth-array-missing-marker",
            "array_no_marker",
            1,
            "expected exactly one 'SYNTH_DONE top=systolic_array_4x4'",
        )
        _run_v1_case(
            "v1-synth-pe-cell-mismatch",
            "pe_bad_cells",
            1,
            "pe_ws final cell count 10 != 1144",
        )
        _run_v1_case(
            "v1-synth-array-wire-mismatch",
            "array_bad_wire",
            1,
            "systolic_array_4x4 final wire-bit count 10 != 20378",
        )
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("runner negative paths PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
