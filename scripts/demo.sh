#!/usr/bin/env bash
# The demo: minilake builds every layer on a fresh SYNTHETIC delivery, prints what each layer did,
# then runs the seven gates, and exits 1 unless every gate passes. Standard library only; runs in
# seconds. An independent stand-in written only from the platform's public write-up; not the
# platform's code.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m minilake
