#!/usr/bin/env bash
#
# run_tests.sh — Unified test runner for Reality Check.
#
# Runs all unit and integration test suites:
#   1. agent core unittests (evidence store, s3 docs, local agent, web handler, canary)
#   2. cost_lambda tests (Aurora latency tracker, billing projection, web portal)
#   3. cdc pipeline tests (WAL parser, lease manager, duckdb applier, reader, mcp server)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
fi

echo "=========================================================="
echo "Running Reality Check test suites with $PYTHON"
echo "=========================================================="

echo "==> 1. Running core agent tests..."
"$PYTHON" -m unittest discover -s agent -p 'test_*.py'

echo "==> 2. Running cost lambda web tests..."
"$PYTHON" agent/cost_lambda/test_web.py

echo "==> 3. Running cost lambda billing tests..."
"$PYTHON" agent/cost_lambda/test_billing.py

echo "==> 4. Running serverless DuckDB CDC tests..."
"$PYTHON" agent/test_cdc.py

echo "=========================================================="
echo "SUCCESS: All test suites passed cleanly."
echo "=========================================================="
