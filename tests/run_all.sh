#!/usr/bin/env bash
# Run every check and report a single verdict.
#
#   ./tests/run_all.sh            all checks (needs the RSP1 attached)
#   ./tests/run_all.sh --offline  skip anything that needs the hardware
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p artifacts

OFFLINE=0
[ "${1:-}" = "--offline" ] && OFFLINE=1

PASS=0
FAIL=0
SKIP=0
declare -a FAILED

run() {
    local name="$1"; shift
    printf '\n=== %s ===\n' "$name"
    if "$@"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED+=("$name")
    fi
}

skip() {
    printf '\n=== %s ===\nSKIPPED (%s)\n' "$1" "$2"
    SKIP=$((SKIP + 1))
}

# Filter the ubiquitous Qt/FFmpeg noise but keep the exit status of the command
# under test -- a pipe that succeeds must not mask a failing test.
quiet() { grep -viE 'vulkan|ffmpeg|vdpau|MESA|multimedia.plugin|nullptr|Gtk-Message|propagateSizeHints'; }

dsp()      { python3 tests/test_dsp.py | quiet; }
memory()   { python3 tests/test_memory.py | quiet; }
units()    { python3 tests/test_units.py | quiet; }
chain()    { python3 tests/test_chain.py | quiet; }
cli()      { python3 -m rspsdr --memories | quiet; }
# A packaged build has to be able to say which build it is, and the version is
# written down in three places that are easy to let drift: the module, what
# --version prints, and the Debian changelog the package is named after.
version()  { python3 - <<'PY' | quiet
import re, subprocess, sys
sys.path.insert(0, ".")
from rspsdr import __version__
out = subprocess.run([sys.executable, "-m", "rspsdr", "--version"],
                     capture_output=True, text=True).stdout.strip()
changelog = re.search(r"^rspsdr \((\S+?)\)", open("debian/changelog").read(),
                      re.M).group(1)
want = f"rspsdr {__version__}"
ok = out == want and changelog == __version__
print(f"  {'PASS' if ok else 'FAIL'}  the version agrees everywhere :: "
      f"module {__version__}, --version “{out}”, changelog {changelog}")
print("version agreement " + ("passed" if ok else "FAILED"))
sys.exit(0 if ok else 1)
PY
}
compile()  { python3 -m compileall -q rspsdr tests >/dev/null; }
check()    { python3 -m rspsdr --check | quiet; }
receive()  { python3 -m rspsdr --selftest --duration 8 \
                 --freq 9805000 --mode am --bw 6000 --gain 50 | quiet; }
wav()      { python3 -m rspsdr --selftest --wav artifacts/verify.wav \
                 --duration 8 --freq 9805000 --mode am --gain 50 >/dev/null 2>&1 \
             && python3 tests/check_wav.py artifacts/verify.wav; }
gui()      { QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py \
                 --shot artifacts/verify_gui.png --seconds 10 | quiet; }
# Needs a real display as well as the radio; skips itself without either.
real()     { python3 tests/real_display_check.py --shot-dir artifacts | quiet; }

run "compile"            compile
run "unit tests"         units
run "receive chain"      chain
run "dsp unit tests"     dsp
run "memory store tests" memory
run "cli memories"       cli
run "version agreement"  version

if [ "$OFFLINE" = "1" ]; then
    skip "hardware checks" "--offline"
else
    if python3 -c "
import sys
sys.path.insert(0,'.')
from rspsdr.radio import list_devices
sys.exit(0 if list_devices() else 1)" >/dev/null 2>&1; then
        run "device diagnostics"   check
        run "headless receive"     receive
        run "recorded audio"       wav
        run "gui smoke test"       gui
        run "real display"         real
    else
        skip "hardware checks" "no SoapySDR device found"
    fi
fi

printf '\n========================================\n'
printf 'passed %d   failed %d   skipped %d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
    printf 'failures: %s\n' "${FAILED[*]}"
    exit 1
fi
printf 'ALL CHECKS PASSED\n'
