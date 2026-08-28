#!/usr/bin/env bash
# Re-render every Mermaid source to the PNG that docs/ links to.
#
# Run this after editing anything in visualizations/. tests/doc_consistency.py
# fails if a .mmd is newer than its .png, because an image that disagrees with
# its source is worse than no image: it reads as authoritative and is wrong.
set -euo pipefail
cd "$(dirname "$0")"
for f in visualizations/*.mmd; do
    npx --no-install @mermaid-js/mermaid-cli \
        -i "$f" -o "${f%.mmd}.png" -w 2400 --backgroundColor white >/dev/null
    echo "rendered ${f%.mmd}.png"
done
