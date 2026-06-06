#!/bin/sh

set -eu

app_dir="${TOPO_PACKAGE_APP_DIR:-/usr/lib/topo}"
if [ -d "$app_dir" ]; then
    find "$app_dir" -depth -type d -empty -exec rmdir {} \; 2>/dev/null || true
fi

target_user="${SUDO_USER:-}"
if [ -z "$target_user" ] && [ "${SUDO_UID:-0}" != "0" ]; then
    target_user="$(getent passwd "$SUDO_UID" 2>/dev/null | cut -d: -f1)"
fi
if [ -z "$target_user" ] || [ "$target_user" = "root" ]; then
    exit 0
fi

home_dir="$(getent passwd "$target_user" 2>/dev/null | cut -d: -f6)"
if [ -z "$home_dir" ] || [ ! -d "$home_dir" ]; then
    exit 0
fi

launcher="$home_dir/.local/bin/topo"

if [ -x "$home_dir/.topo/topo" ]; then
    exit 0
fi

if [ -L "$launcher" ]; then
    target="$(readlink "$launcher" 2>/dev/null || true)"
    if [ "$target" = "/usr/bin/topo" ] || [ "$target" = "/usr/lib/topo/topo" ]; then
        rm -f "$launcher"
    fi
elif [ -f "$launcher" ] && grep -q "Managed by topo package compatibility launcher" "$launcher"; then
    rm -f "$launcher"
fi
