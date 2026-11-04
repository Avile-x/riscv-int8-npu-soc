#!/usr/bin/env python3
"""V1-T03 single-PE build and verification entry."""

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

SIM_TOP = "tb_pe_ws"
SIM_COMPILE_TIMEOUT_S = 300.0
SIM_RUN_TIMEOUT_S = 120.0
FOUR_STATE_COMPILE_TIMEOUT_S = 120.0
FOUR_STATE_RUN_TIMEOUT_S = 120.0
SYNTH_TIMEOUT_S = 120.0

SIM_COMPILE_FORBIDDEN = (
    r"%Error",
    r"%Warning",
    r"PE_FAIL",
    r"PE_PROTOCOL_FAIL",
)
SIM_RUN_FORBIDDEN = (r"%Error", r"PE_FAIL", r"PE_PROTOCOL_FAIL")
FOUR_STATE_COMPILE_FORBIDDEN = (
    r"error:",
    r"syntax error",
    r"warning:",
    r"sorry:",
    r"preprocessor failed",
    r"PE_FAIL",
    r"PE_PROTOCOL_FAIL",
)
FOUR_STATE_NORMAL_FORBIDDEN = (
    r"ERROR:",
    r"FATAL:",
    r"PE_FAIL",
    r"PE_PROTOCOL_FAIL",
)
SYNTH_FORBIDDEN = (
    r"ERROR:",
    r"Warning:",
    r"\blatch\b",
    r"undriven",
    r"has no driver",
    r"not driven",
    r"conflicting driver",
)

FOUR_STATE_INJECTIONS = (
    (
        "x",
        "+inject_x",
        (r"PE_FAIL", r"got_psum=xxxxxxxx"),
    ),
    (
        "z",
        "+inject_z",
        (r"PE_FAIL", r"got_psum=zzzzzzzz"),
    ),
    (
        "act_only",
        "+inject_act_only",
        (r"PE_PROTOCOL_FAIL", r"reason=act_valid_without_psum"),
    ),
    (
        "psum_only",
        "+inject_psum_only",
        (r"PE_PROTOCOL_FAIL", r"reason=psum_valid_without_act"),
    ),
    (
        "illegal_control",
        "+inject_illegal_control",
        (r"PE_PROTOCOL_FAIL", r"reason=weight_we_and_pipe_en"),
    ),
)


class PeFailure(RuntimeError):
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
        raise PeFailure(
            f"refusing to clean unexpected build directory: {resolved}"
        )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _fail_on_patterns(text: str, patterns: tuple[str, ...], label: str) -> None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise PeFailure(
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
        raise PeFailure(
            "wslpath not found; cannot invoke Windows Icarus"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PeFailure("wslpath timed out while converting a path") from exc
    converted = result.stdout.strip()
    if result.returncode != 0 or not converted:
        raise PeFailure(
            f"wslpath failed for {path}: {result.stderr.strip()}"
        )
    return converted


def _single_pass_marker(text: str) -> str:
    pass_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("PE_PASS")
    ]
    if len(pass_lines) != 1 or re.fullmatch(
        r"PE_PASS checks=\d+", pass_lines[0]
    ) is None:
        raise PeFailure(
            f"expected exactly one valid PE_PASS marker, got {pass_lines!r}"
        )
    return pass_lines[0]


def _require_nonempty_vcd(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise PeFailure(f"{label} VCD trace missing or empty: {path}")


def check_simulation(build_dir: Path) -> None:
    _prepare_build_dir(build_dir)

    obj_dir = build_dir / "obj"
    compile_log = build_dir / "compile.log"
    sim_log = build_dir / "sim.log"
    vcd_path = build_dir / "pe.vcd"
    binary = obj_dir / "sim_pe"

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
        "sim_pe",
        str(ROOT / "rtl/core/pe_ws.sv"),
        str(ROOT / "tb/core/tb_pe_ws.sv"),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, SIM_COMPILE_TIMEOUT_S, compile_log
    )
    if compile_result.returncode != 0:
        raise PeFailure(
            f"Verilator compile failed with exit "
            f"{compile_result.returncode}; see {compile_log}"
        )
    _fail_on_patterns(
        _combined(compile_result), SIM_COMPILE_FORBIDDEN, "Verilator compile"
    )

    if not binary.is_file():
        raise PeFailure(f"simulation binary missing: {binary}")

    run_result = _run_and_log(
        [str(binary)], build_dir, SIM_RUN_TIMEOUT_S, sim_log
    )
    if run_result.returncode != 0:
        raise PeFailure(
            f"simulation failed with exit {run_result.returncode}; see {sim_log}"
        )
    run_text = _combined(run_result)
    _fail_on_patterns(run_text, SIM_RUN_FORBIDDEN, "simulation")

    pass_line = _single_pass_marker(run_text)
    _require_nonempty_vcd(vcd_path, "simulation")

    print(f"simulation PASS: {pass_line}")


def check_synthesis(build_dir: Path) -> None:
    _prepare_build_dir(build_dir)

    log_path = build_dir / "yosys.log"
    source = ROOT / "rtl/core/pe_ws.sv"
    include_dir = ROOT / "rtl/core"
    yosys_script = (
        f"read_verilog -sv -I{include_dir} {source}; "
        "hierarchy -check -top pe_ws; "
        "proc; opt; check -assert; "
        "select -assert-none t:$dlatch t:$_DLATCH_*; "
        "synth -top pe_ws; "
        "check -assert; "
        "select -assert-none t:$dlatch t:$_DLATCH_*; "
        "stat"
    )
    argv = ["yosys", "-p", yosys_script]
    result = _run_and_log(argv, ROOT, SYNTH_TIMEOUT_S, log_path)
    text = log_path.read_text(encoding="utf-8", errors="replace")

    if result.returncode != 0:
        raise PeFailure(
            f"Yosys synthesis failed with exit {result.returncode}; "
            f"see {log_path}"
        )
    _fail_on_patterns(text, SYNTH_FORBIDDEN, "Yosys")

    if re.search(r"Top module:\s+\\?pe_ws\b", text) is None:
        raise PeFailure("synthesis report does not identify pe_ws top")
    if re.search(r"Number of wire bits:\s+\d+", text) is None:
        raise PeFailure("synthesis report is missing wire-bit statistics")
    cells = re.search(r"Number of cells:\s+(\d+)", text)
    if cells is None or int(cells.group(1)) <= 0:
        raise PeFailure("synthesis report is missing non-zero cell statistics")

    print("synthesis PASS: top=pe_ws")


def check_four_state(build_dir: Path) -> None:
    compiler, vvp, windows_tool = _find_four_state_tool()
    origin = "Windows Icarus via WSL interop" if windows_tool else "native Icarus"
    print(f"four-state tool: {compiler} ({origin})")

    _prepare_build_dir(build_dir)

    compile_log = build_dir / "compile.log"
    binary = build_dir / "sim_pe4.vvp"
    rtl = ROOT / "rtl/core/pe_ws.sv"
    tb = ROOT / "tb/core/tb_pe_ws.sv"
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
        tool_path(rtl),
        tool_path(tb),
    ]
    compile_result = _run_and_log(
        compile_argv, ROOT, FOUR_STATE_COMPILE_TIMEOUT_S, compile_log
    )
    compile_text = _combined(compile_result)
    if compile_result.returncode != 0:
        raise PeFailure(
            f"Icarus compile failed with exit {compile_result.returncode}; "
            f"see {compile_log}"
        )
    _fail_on_patterns(
        compile_text, FOUR_STATE_COMPILE_FORBIDDEN, "Icarus compile"
    )
    if not binary.is_file():
        raise PeFailure(f"four-state simulation binary missing: {binary}")

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
            raise PeFailure(
                f"{name} injection unexpectedly passed (exit 0); see {log_path}"
            )
        if "PE_PASS" in text:
            raise PeFailure(f"{name} injection emitted PE_PASS; see {log_path}")
        for marker in markers:
            if marker not in text:
                raise PeFailure(
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
        raise PeFailure(
            f"four-state normal simulation failed with exit "
            f"{normal_result.returncode}; see {normal_log}"
        )
    _fail_on_patterns(
        normal_text, FOUR_STATE_NORMAL_FORBIDDEN, "Icarus normal run"
    )
    pass_line = _single_pass_marker(normal_text)
    vcd_path = build_dir / "pe.vcd"
    _require_nonempty_vcd(vcd_path, "four-state")
    print(f"four-state normal PASS: {pass_line}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the V1-T03 single-PE simulation, four-state, or synthesis check."
    )
    parser.add_argument("kind", choices=("sim", "sim4", "synth"))
    args = parser.parse_args()

    build_dir = BUILD_ROOT / f"pe_{args.kind}"

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
    except PeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"PASS: {args.kind} (build directory: {build_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
