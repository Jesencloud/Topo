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

# Both accepted launcher targets need canonicalizing before comparison:
# /usr/bin/topo is itself a symlink in some packagings, and /home is a symlink
# on image-based distributions.
canonical_path() {
    readlink -f "$1" 2>/dev/null || printf '%s' "$1"
}

should_replace=false
if [ ! -e "$launcher" ] && [ ! -L "$launcher" ]; then
    should_replace=true
elif [ -L "$launcher" ]; then
    # Only reclaim a link that already points at a Topo install. A link the user
    # made to their own build is left alone instead of silently replaced.
    link_target="$(canonical_path "$launcher")"
    if [ "$link_target" = "$(canonical_path /usr/bin/topo)" ] ||
        [ "$link_target" = "$(canonical_path "$home_dir/.topo/topo")" ]; then
        should_replace=true
    fi
elif [ -f "$launcher" ] && grep -q "Managed by topo package compatibility launcher" "$launcher"; then
    should_replace=true
fi

if [ "$should_replace" != true ]; then
    exit 0
fi

# ~/.local/bin must be a real directory: a symlink (or any non-directory) there
# would let a prepared home redirect this root-run script into a system
# directory such as /etc/sudoers.d (CWE-59). [ -L ] only tests the final
# component, which is why the write itself is deprivileged below.
if [ -L "$bin_dir" ]; then
    echo "topo: $bin_dir is a symlink; skipping the compatibility launcher." >&2
    exit 0
fi
if [ -e "$bin_dir" ] && [ ! -d "$bin_dir" ]; then
    echo "topo: $bin_dir is not a directory; skipping the compatibility launcher." >&2
    exit 0
fi

# Every file operation happens as the target user, so root never writes through
# a path that user controls; any remaining symlink race can then only reach
# files they could already write themselves.
# The single quotes are deliberate: this is a program for the deprivileged shell
# to expand, not text for this one.
# shellcheck disable=SC2016
write_launcher='
set -eu
bin_dir="$1"
mkdir -p "$bin_dir"
tmp="$bin_dir/topo.topo-tmp.$$"
rm -f "$tmp"
# noclobber gives the redirect O_EXCL semantics, so a name that reappears
# between rm and cat aborts the install instead of being written through.
set -C
cat > "$tmp" <<"LAUNCHER_EOF"
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
LAUNCHER_EOF
set +C
chmod 755 "$tmp"
# rename() replaces the name itself and never follows a symlink standing there.
mv -f "$tmp" "$bin_dir/topo"
'

if [ "$(id -u)" = "0" ]; then
    if ! command -v runuser >/dev/null 2>&1; then
        echo "topo: runuser is unavailable; skipping the compatibility launcher." >&2
        exit 0
    fi
    # A failure here costs only the convenience launcher, so it must not fail
    # the package transaction.
    runuser -u "$target_user" -- sh -c "$write_launcher" sh "$bin_dir" || exit 0
else
    # Already unprivileged: there is nothing to drop, so run the writer directly.
    sh -c "$write_launcher" sh "$bin_dir"
fi
