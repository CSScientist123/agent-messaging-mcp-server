#!/usr/bin/env bash
# Re-render every Mermaid source to the PNGs that people actually look at.
#
# TWO destinations, and that is the point. The repo's visualizations/*.png is
# what docs/ links to. The Obsidian vault's Attachments/ is where these are
# actually READ. Rendering one and not the other is exactly how the vault came
# to show a diagram full of [IDLE] and interrupt_partner a day after both were
# deleted from the code -- so this script does both, every time, and
# tests/doc_consistency.py fails if either falls behind its source.
#
# Run it after editing anything in visualizations/.
set -euo pipefail
cd "$(dirname "$0")"

VAULT_ATTACHMENTS="/mnt/c/Data/Books/Brains/polling-mechanism/Attachments"

render() {   # render <src.mmd> <dest.png> <scale>
    npx --no-install @mermaid-js/mermaid-cli \
        -i "$1" -o "$2" -w 2400 -s "$3" --backgroundColor white >/dev/null
}

for f in visualizations/*.mmd; do
    render "$f" "${f%.mmd}.png" 1
    echo "rendered ${f%.mmd}.png"
    # The vault is read at full zoom in Obsidian, so it gets the high-res render.
    # Skipped silently when the vault is not mounted -- same reasoning as the
    # vault checks in tests/doc_consistency.py.
    if [ -d "$VAULT_ATTACHMENTS" ]; then
        render "$f" "$VAULT_ATTACHMENTS/$(basename "${f%.mmd}").png" 3
        echo "rendered $VAULT_ATTACHMENTS/$(basename "${f%.mmd}").png"
    fi
done
