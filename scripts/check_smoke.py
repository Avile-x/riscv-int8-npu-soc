#!/usr/bin/env python3
"""S0 smoke build and verification entry."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build"

CheckKind = Literal["sim", "sim4", "synth"]

SIM_TOP = "tb_smoke_counter"
SIM_COMPILE_TIMEOUT_S = 120.0
SIM_RUN_TIMEOUT_S = 60.0
FOUR_STATE_COMPILE_TIMEOUT_S = 120.0
FOUR_STATE_RUN_TIMEOUT_S = 60.0
SYNTH_TIMEOUT_S = 120.0

SIM_COMPILE_FORBIDDEN = (r"%Error", r"%Warning", r"SMOKE_FAIL")
SIM_RUN_FORBIDDEN = (r"%Error", r"SMOKE_FAIL")
FOUR_STATE_COMPILE_FORBIDDEN = (
    r"error:",
    r"syntax error",
    r"warning:",
    r"SMOKE_FAIL",
)
FOUR_STATE_RUN_FORBIDDEN = (r"ERROR:", r"FATAL:", r"SMOKE_FAIL")
SYNTH_FORBIDDEN = (
    r"ERROR:",
    r"Warning:",
    r"\blatch\b",
    r"undriven",
    r"has no driver",
    r"not driven",
    r"conflicting driver",
)

class SmokeFailure(RuntimeError):
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

def _write_log(
    path: Path,
    argv: list[str],
    cp: CompletedProcess[str],
) -> None:
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
    path: Path,
    argv: list[str],
    exc: subprocess.TimeoutExpired,
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
        raise SmokeFailure(
            f"refusing to clean unexpected build directory: {resolved}"
        )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)

def _fail_on_patterns(text: str, patterns: tuple[str, ...], label: str) -> None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise SmokeFailure(
                f"{label} emitted forbidden pattern {pattern!r}; see build log"
            )

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
        raise SmokeFailure(
            "wslpath not found; cannot invoke Windows Icarus"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure("wslpath timed out while converting a path") from exc
    converted = result.stdout.strip()
    if result.returncode != 0 or not converted:
        raise SmokeFailure(
            f"wslpath failed for {path}: {result.stderr.strip()}"
        )
    return converted

def check_simulation(build_dir: Path) -> None:
    _prepare_build_dir(build_dir)

    obj_dir = build_dir / "obj"
    compile_log = build_dir / "compile.log"
    sim_log = build_dir / "sim.log"
    vcd_path = build_dir / "smoke.vcd"
    binary = obj_dir / "sim_smoke"

    compile_argv = [
        "verilator",
        "--binary",
        "--timing",
        "--trace",
        "-Wall",
        "-Wno-fatal",
        "--top-module",
        SIM_TOP,
        "--Mdir",
        str(obj_dir),
        "-o",
        "sim_smoke",
        str(ROOT / "rtl/smoke/smoke_counter.sv"),
        str(ROOT / "tb/smoke/tb_smoke_counter.sv"),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, SIM_COMPILE_TIMEOUT_S, compile_log
    )
    if compile_result.returncode != 0:
        raise SmokeFailure(
            f"Verilator compile failed with exit "
            f"{compile_result.returncode}; see {compile_log}"
        )
    _fail_on_patterns(
        _combined(compile_result), SIM_COMPILE_FORBIDDEN, "Verilator compile"
    )

    if not binary.is_file():
        raise SmokeFailure(f"simulation binary missing: {binary}")

    run_result = _run_and_log(
        [str(binary)], build_dir, SIM_RUN_TIMEOUT_S, sim_log
    )
    if run_result.returncode != 0:
        raise SmokeFailure(
            f"simulation failed with exit {run_result.returncode}; see {sim_log}"
        )
    run_text = _combined(run_result)
    _fail_on_patterns(run_text, SIM_RUN_FORBIDDEN, "simulation")

    pass_lines = [
        line.strip()
        for line in run_text.splitlines()
        if line.strip().startswith("SMOKE_PASS")
    ]
    if len(pass_lines) != 1 or re.fullmatch(
        r"SMOKE_PASS checks=\d+", pass_lines[0]
    ) is None:
        raise SmokeFailure(
            f"expected exactly one valid SMOKE_PASS marker, got {pass_lines!r}"
        )

    if not vcd_path.is_file() or vcd_path.stat().st_size == 0:
        raise SmokeFailure(f"VCD trace missing or empty: {vcd_path}")

    print(f"simulation PASS: {pass_lines[0]}")

def check_synthesis(build_dir: Path) -> None:
    _prepare_build_dir(build_dir)

    log_path = build_dir / "yosys.log"
    source = ROOT / "rtl/smoke/smoke_counter.sv"
    yosys_script = (
        f"read_verilog -sv {source}; "
        "hierarchy -check -top smoke_counter; "
        "proc; opt; check -assert; "
        "synth -top smoke_counter; "
        "check -assert; "
        "select -assert-none t:$dlatch t:$_DLATCH_*; "
        "stat"
    )
    argv = ["yosys", "-p", yosys_script]
    result = _run_and_log(argv, ROOT, SYNTH_TIMEOUT_S, log_path)
    text = log_path.read_text(encoding="utf-8", errors="replace")

    if result.returncode != 0:
        raise SmokeFailure(
            f"Yosys synthesis failed with exit {result.returncode}; "
            f"see {log_path}"
        )
    _fail_on_patterns(text, SYNTH_FORBIDDEN, "Yosys")

    if re.search(r"Top module:\s+\\?smoke_counter\b", text) is None:
        raise SmokeFailure("synthesis report does not identify smoke_counter top")
    if re.search(r"Number of wire bits:\s+\d+", text) is None:
        raise SmokeFailure("synthesis report is missing wire-bit statistics")
    cells = re.search(r"Number of cells:\s+(\d+)", text)
    if cells is None or int(cells.group(1)) <= 0:
        raise SmokeFailure("synthesis report is missing non-zero cell statistics")

    print("synthesis PASS: top=smoke_counter")

def check_four_state(build_dir: Path) -> None:
    compiler, vvp, windows_tool = _find_four_state_tool()
    origin = "Windows Icarus via WSL interop" if windows_tool else "native Icarus"
    print(f"four-state tool: {compiler} ({origin})")

    _prepare_build_dir(build_dir)

    compile_log = build_dir / "compile.log"
    binary = build_dir / "sim_smoke4.vvp"
    rtl = ROOT / "rtl/smoke/smoke_counter.sv"
    tb = ROOT / "tb/smoke/tb_smoke_counter.sv"

    def tool_path(path: Path) -> str:
        return _to_windows_path(path) if windows_tool else str(path)

    compile_argv = [
        str(compiler),
        "-g2012",
        "-Wall",
        "-o",
        tool_path(binary),
        tool_path(rtl),
        tool_path(tb),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, FOUR_STATE_COMPILE_TIMEOUT_S, compile_log
    )
    compile_text = _combined(compile_result)
    if compile_result.returncode != 0:
        raise SmokeFailure(
            f"Icarus compile failed with exit {compile_result.returncode}; "
            f"see {compile_log}"
        )
    _fail_on_patterns(
        compile_text, FOUR_STATE_COMPILE_FORBIDDEN, "Icarus compile"
    )
    if not binary.is_file():
        raise SmokeFailure(f"four-state simulation binary missing: {binary}")

    for label, plusarg, expected_got in (
        ("X", "+inject_x", "xx"),
        ("Z", "+inject_z", "zz"),
    ):
        log_path = build_dir / f"run_{label.lower()}.log"
        result = _run_and_log(
            [str(vvp), tool_path(binary), plusarg],
            build_dir,
            FOUR_STATE_RUN_TIMEOUT_S,
            log_path,
        )
        text = _combined(result)
        if result.returncode == 0:
            raise SmokeFailure(
                f"{label} injection unexpectedly passed (exit 0); see {log_path}"
            )
        if "SMOKE_PASS" in text:
            raise SmokeFailure(
                f"{label} injection emitted SMOKE_PASS; see {log_path}"
            )
        if "SMOKE_FAIL" not in text:
            raise SmokeFailure(
                f"{label} injection did not emit SMOKE_FAIL; see {log_path}"
            )
        if f"got={expected_got}" not in text.lower():
            raise SmokeFailure(
                f"{label} injection did not report got={expected_got}; "
                f"see {log_path}"
            )
        print(
            f"four-state {label} injection PASS: "
            f"exit={result.returncode}, got={expected_got}"
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
        raise SmokeFailure(
            f"four-state normal simulation failed with exit "
            f"{normal_result.returncode}; see {normal_log}"
        )
    _fail_on_patterns(
        normal_text, FOUR_STATE_RUN_FORBIDDEN, "Icarus normal run"
    )
    pass_lines = [
        line.strip()
        for line in normal_text.splitlines()
        if line.strip().startswith("SMOKE_PASS")
    ]
    if len(pass_lines) != 1 or re.fullmatch(
        r"SMOKE_PASS checks=\d+", pass_lines[0]
    ) is None:
        raise SmokeFailure(
            f"expected exactly one valid SMOKE_PASS marker, got {pass_lines!r}"
        )
    vcd_path = build_dir / "smoke.vcd"
    if not vcd_path.is_file() or vcd_path.stat().st_size == 0:
        raise SmokeFailure(f"four-state VCD trace missing or empty: {vcd_path}")
    print(f"four-state normal PASS: {pass_lines[0]}")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the S0 smoke simulation, four-state, or synthesis check."
    )
    parser.add_argument("kind", choices=("sim", "sim4", "synth"))
    args = parser.parse_args()

    build_dir = BUILD_ROOT / args.kind

    try:
        if args.kind == "sim":
            check_simulation(build_dir)
        elif args.kind == "sim4":
            check_four_state(build_dir)
        else:
            check_synthesis(build_dir)
    except subprocess.TimeoutExpired as exc:
        print(
            f"FAIL: '{args.kind}' timed out after {exc.timeout}s",
            file=sys.stderr,
        )
        return 2
    except FileNotFoundError as exc:
        print(f"FAIL: required tool not found: {exc.filename}", file=sys.stderr)
        return 3
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"PASS: {args.kind} (build directory: {build_dir})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
