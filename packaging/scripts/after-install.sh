#!/bin/sh

set -eu

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

bin_dir="$home_dir/.local/bin"
launcher="$bin_dir/topo"
target_group="$(id -gn "$target_user" 2>/dev/null || printf '%s' "$target_user")"

should_replace=false
if [ ! -e "$launcher" ] && [ ! -L "$launcher" ]; then
    should_replace=true
elif [ -L "$launcher" ]; then
    should_replace=true
elif [ -f "$launcher" ] && grep -q "Managed by topo package compatibility launcher" "$launcher"; then
    should_replace=true
fi

if [ "$should_replace" != true ]; then
    exit 0
fi

install -d -m 755 "$bin_dir"
chown "$target_user:$target_group" "$bin_dir" 2>/dev/null || true
tmp_launcher="$launcher.topo-tmp.$$"
cat > "$tmp_launcher" <<'EOF'
#!/bin/sh
# Managed by topo package compatibility launcher.
#
# This keeps `topo` usable in shells that cached an older ~/.local/bin/topo
# command path before a Debian/RPM package install created /usr/bin/topo.

if [ -x /usr/bin/topo ]; then
    exec /usr/bin/topo "$@"
fi

if [ -x "$HOME/.topo/topo" ]; then
    exec "$HOME/.topo/topo" "$@"
fi

echo "topo: launcher target not found. Try reinstalling Topo." >&2
exit 127
EOF

chown "$target_user:$target_group" "$tmp_launcher" 2>/dev/null || true
chmod 755 "$tmp_launcher"
mv -f "$tmp_launcher" "$launcher"
