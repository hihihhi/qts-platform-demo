#!/usr/bin/env bash
# The repository's full check; exit 0 only if every part passes:
#   1. the document checks (scripts/check_docs.py): links, anchors, code and mermaid fences, the
#      README's gate output against a fresh run, and the forbidden-terms scan. Set
#      FORBIDDEN_TERMS_FILE to a private pattern file outside the repository to scan for more terms;
#   2. the same checks' self-test: each must go red on a planted defect and stay green on the
#      untouched copy, so a check that has silently stopped working fails here;
#   3. the tests (tests/), one class per layer, plus the gates;
#   4. the demo, which exits 1 unless every gate passes.
# Standard library only.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/check_docs.py
python3 scripts/check_docs.py --self-test
python3 -m unittest discover -s tests
bash scripts/demo.sh >/dev/null
echo "PASS  demo"
