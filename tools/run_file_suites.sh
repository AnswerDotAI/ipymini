#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

joblog=${1:-/tmp/pytest-joblog.tsv}

ls pytests/test_*.py | parallel -j 4 --joblog "$joblog" 'pytest -q -o addopts="" {}'

echo "Per-file durations (seconds):"
awk 'NR>1 {print $4 "\t" $NF}' "$joblog" | sort -nr
