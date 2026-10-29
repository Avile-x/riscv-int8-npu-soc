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
mkdir -p "$mdir"
{
    printf '%s\n' '#!/bin/sh'
    printf '%s\n' 'printf "%b\n" "$FAKE_SIM_OUTPUT"'
    printf '%s\n' 'exit 0'
} > "$mdir/$out"
chmod +x "$mdir/$out"
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
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("runner negative paths PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
