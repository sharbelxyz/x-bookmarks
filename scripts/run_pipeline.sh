#!/bin/bash
# Run the full merge → categorize → report pipeline
set -e
cd /Users/mshrmnsr/claude1/x-bookmarks

echo "=== Phase 3: Merge & Deduplicate ==="
python3 scripts/merge_and_dedupe.py

echo ""
echo "=== Phase 4: Categorize ==="
python3 scripts/categorize.py

echo ""
echo "=== Phase 5: Generate Report ==="
python3 scripts/generate_report.py

echo ""
echo "=== Done! ==="
echo "Report: output/bookmark_report.md"
echo "Stats: output/stats.json"
echo "CSVs: output/csv/"
