#!/usr/bin/env bash
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/pkg"
mkdir -p "$stage/DEBIAN" "$stage/usr/lib/image-harvester" "$stage/usr/bin" \
         "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
         "$stage/usr/share/doc/image-harvester"
cp -r "$project/src/image_harvester" "$stage/usr/lib/image-harvester/"
find "$stage" -type d -name __pycache__ -prune -exec rm -r {} +
find "$stage" -type f -name '*.pyc' -delete
cp "$project/packaging/image-harvester.desktop" "$stage/usr/share/applications/"
cp "$project/packaging/image-harvester.svg" "$stage/usr/share/icons/hicolor/scalable/apps/"
cp "$project/README.md" "$project/docs/SPECIFICATION-AR.md" "$project/LICENSE" "$stage/usr/share/doc/image-harvester/"
cat > "$stage/usr/bin/image-harvester" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/image-harvester${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m image_harvester "$@"
EOF
chmod 755 "$stage/usr/bin/image-harvester"
cat > "$stage/DEBIAN/control" <<'EOF'
Package: image-harvester
Version: 0.1.0-1
Section: graphics
Priority: optional
Architecture: all
Depends: python3 (>= 3.12), python3-tk
Maintainer: Image Harvester contributors <noreply@example.invalid>
Description: Lightweight website image downloader with Arabic graphical interface
 Extract images from one page or a bounded website crawl, honor robots.txt,
 deduplicate downloaded files and write JSON and CSV reports.
EOF
output="${1:-$project/../image-harvester_0.1.0-1_all.deb}"
dpkg-deb --build --root-owner-group "$stage" "$output"
echo "Created: $output"
