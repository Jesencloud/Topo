#!/usr/bin/env bash
#
# The one producer of the engine binaries committed under src/core/bin/, and the
# one verifier of the stamp that ties them to the Rust source they were built
# from.
#
# Why a stamp and not `cmp` against a fresh CI build: a musl release build bakes
# in the builder's cargo registry path and the exact rustc it used, so the same
# source gives different bytes on a different machine. The bundled engines carry
# /home/<developer>/.cargo/..., the CI artifact for the very same commit carries
# /home/runner/.cargo/... and is 4 KiB larger. Byte comparison is only
# meaningful between two builds by the same producer -- which is what a rebuild
# through this script is -- so the cross-machine check is the stamp: the hash of
# the sources the bundled bytes came from, plus the hash of those bytes.
#
# Nothing here runs on a user's machine. install.sh replaces the host-arch
# engine with a signed release download and deletes topo-core/ entirely, stamp
# included.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CRATE_DIR="$REPO_ROOT/topo-core"
BIN_DIR="$REPO_ROOT/src/core/bin"
STAMP_FILE="$CRATE_DIR/engine.stamp"
ENGINE_ARCHES=(x86_64 aarch64)

# The channel is a channel and not a pinned version on purpose: bytes are
# allowed to change when the toolchain does, because the stamp is refreshed in
# the same commit that ships the new bytes.
TOOLCHAIN="${TOPO_ENGINE_TOOLCHAIN:-stable}"

# Sort object keys and drop the one field two runs over the same tree may
# legitimately disagree on: reading a directory updates its atime, and --stats
# folds atime into newest_activity_secs.
NORMALIZE_JSON='
import json, sys
data = json.load(sys.stdin)
if isinstance(data, dict):
    data.pop("newest_activity_secs", None)
json.dump(data, sys.stdout, sort_keys=True)
'

usage() {
    cat <<'EOF'
Usage: packaging/build-engine.sh [--verify | --compare BINARY ARCH]
                                 [--check-elf BINARY ARCH]

Rebuild the engines committed under src/core/bin/ and refresh
topo-core/engine.stamp. Run this after any change to topo-core/, and commit the
binaries together with the stamp.

Options:
  --verify              Check the bundled engines against the stamp and against
                        the current Rust source. Builds nothing.
  --compare BINARY ARCH Scan a fixture tree with BINARY and with the bundled
                        engine for ARCH, and diff the JSON they emit.
  --check-elf BINARY ARCH
                        Check that BINARY is a statically linked ELF for ARCH.
  -h, --help            Show this help
EOF
}

die() {
    printf '%s\n' "❌ $*" >&2
    exit 1
}

rebuild_hint() {
    printf '%s\n' "   请重新构建并提交引擎 (rebuild and commit the engine):" >&2
    printf '%s\n' "     packaging/build-engine.sh" >&2
    printf '%s\n' "   then commit src/core/bin/topo-core-* with topo-core/engine.stamp." >&2
}

# Resolve the real binaries rather than trusting PATH or `rustup run`: rustup
# only exports RUSTUP_TOOLCHAIN and leaves PATH alone, so on a box whose distro
# rust comes before ~/.cargo/bin the cargo it starts still spawns the distro
# rustc -- which has no musl std and fails with "can't find crate for `std`".
engine_bin() {
    local tool="$1" path
    if command -v rustup >/dev/null 2>&1; then
        path="$(rustup which --toolchain "$TOOLCHAIN" "$tool" 2>/dev/null || true)"
    else
        path="$(command -v "$tool" || true)"
    fi
    [ -n "$path" ] || die "Cannot find $tool for toolchain $TOOLCHAIN."
    printf '%s' "$path"
}

engine_cargo() {
    local cargo rustc
    cargo="$(engine_bin cargo)"
    rustc="$(engine_bin rustc)"
    RUSTC="$rustc" "$cargo" "$@"
}

engine_rustc() {
    local rustc
    rustc="$(engine_bin rustc)"
    "$rustc" "$@"
}

# Everything the binary is compiled from: the manifest (which carries the
# release profile), the locked dependency versions, and the crate sources.
# tests/ is deliberately out -- an integration test never reaches the binary,
# and demanding a rebuild for one would train the developer to ignore the gate.
engine_source_hash() {
    (
        cd "$CRATE_DIR"
        {
            printf '%s\n' Cargo.toml Cargo.lock
            find src -type f -name '*.rs' | LC_ALL=C sort
        } | xargs sha256sum | sha256sum | cut -d' ' -f1
    )
}

file_hash() {
    sha256sum "$1" | cut -d' ' -f1
}

stamp_value() {
    [ -f "$STAMP_FILE" ] || return 1
    awk -v key="$1" '$1 == key { print $2; found = 1 } END { exit !found }' "$STAMP_FILE"
}

# The properties that make a bundled engine usable on someone else's machine.
# This is the check that catches the accident this script exists to prevent: a
# host `cargo build --release` copied over the tracked file leaves a
# glibc-dynamic binary behind, which dies with an exec format error anywhere
# else -- and get_rust_scan_data() then silently falls back to pure Python.
verify_static_elf() {
    local path="$1" arch="$2" machine
    case "$arch" in
        x86_64) machine='X86-64' ;;
        aarch64) machine='AArch64' ;;
        *) die "Unknown engine architecture: $arch" ;;
    esac
    # LC_ALL=C because readelf translates its field names, and a developer with a
    # localised shell would otherwise see every binary rejected as "not x86_64".
    if ! LC_ALL=C readelf -h "$path" | grep -q "Machine:.*$machine"; then
        die "$path is not an $arch ELF binary."
    fi
    if LC_ALL=C readelf -l "$path" | grep -qi INTERP; then
        die "$path requests a dynamic linker; it is not statically linked."
    fi
    if LC_ALL=C readelf -d "$path" | grep -qi NEEDED; then
        die "$path has dynamic library dependencies; it is not statically linked."
    fi
}

write_stamp() {
    local source_hash arch
    source_hash="$(engine_source_hash)"
    {
        printf '%s\n' "# Ties the engines committed under src/core/bin/ to the Rust source they"
        printf '%s\n' "# were built from. Refresh with packaging/build-engine.sh; never by hand."
        printf '%s\n' "# built with $(engine_rustc --version)"
        printf 'source  %s\n' "$source_hash"
        for arch in "${ENGINE_ARCHES[@]}"; do
            printf 'topo-core-%s  %s\n' "$arch" "$(file_hash "$BIN_DIR/topo-core-$arch")"
        done
    } > "$STAMP_FILE"
}

verify_engines() {
    local recorded actual arch engine
    if ! recorded="$(stamp_value source)"; then
        die "$STAMP_FILE is missing or has no source hash."
    fi
    actual="$(engine_source_hash)"
    if [ "$recorded" != "$actual" ]; then
        printf '%s\n' "❌ topo-core/ has changed since the bundled engines were built." >&2
        printf '%s\n' "   stamp: $recorded" >&2
        printf '%s\n' "   源码:  $actual" >&2
        rebuild_hint
        exit 1
    fi
    for arch in "${ENGINE_ARCHES[@]}"; do
        engine="$BIN_DIR/topo-core-$arch"
        [ -f "$engine" ] || die "$engine is missing."
        if ! recorded="$(stamp_value "topo-core-$arch")"; then
            die "$STAMP_FILE records no hash for topo-core-$arch."
        fi
        actual="$(file_hash "$engine")"
        if [ "$recorded" != "$actual" ]; then
            printf '%s\n' "❌ $engine is not the binary the stamp was written for." >&2
            printf '%s\n' "   stamp: $recorded" >&2
            printf '%s\n' "   实际:  $actual" >&2
            rebuild_hint
            exit 1
        fi
        verify_static_elf "$engine" "$arch"
    done
    printf '%s\n' "✅ Bundled engines match topo-core/engine.stamp and the current Rust source."
}

engine_json() {
    local binary="$1" root="$2" mode="$3" output
    local -a argv=("$binary")
    [ "$mode" = single ] || argv+=("--$mode")
    argv+=("$root")
    if ! output="$("${argv[@]}" 2>/dev/null)" || [ -z "$output" ]; then
        die "$binary produced no output for $mode mode."
    fi
    printf '%s' "$output" | python3 -c "$NORMALIZE_JSON"
}

# What a stamp cannot prove: that the recorded bytes really behave like the
# current source. Only the runner's own architecture can be asked -- the other
# engine would need emulation, and the stamp already covers staleness for both.
#
# The scratch tree's path is a global because the trap that removes it runs
# after the function has returned: as a `local` it would be unbound by then, and
# under set -u the handler would fail instead of cleaning up.
FIXTURE_DIR=""

cleanup_fixture() {
    [ -z "$FIXTURE_DIR" ] || rm -rf "$FIXTURE_DIR"
}

compare_behaviour() {
    local fresh="$1" arch="$2" mode
    local bundled="$BIN_DIR/topo-core-$arch"
    [ -x "$fresh" ] || die "$fresh is not an executable file."
    # An absolute path, because a bare name like "topo-core-x86_64" -- which is
    # what the CI step passes -- would otherwise be looked up on PATH.
    fresh="$(cd "$(dirname "$fresh")" && pwd)/$(basename "$fresh")"
    [ -f "$bundled" ] || die "$bundled is missing."
    verify_static_elf "$fresh" "$arch"
    verify_static_elf "$bundled" "$arch"

    FIXTURE_DIR="$(mktemp -d)"
    trap cleanup_fixture EXIT
    mkdir -p "$FIXTURE_DIR/nested/deep" "$FIXTURE_DIR/empty"
    # Sparse: the scanner sizes files by st_size, so a 3 MiB file costs no disk
    # and still clears the 1 MiB threshold that puts it in top_files.
    truncate -s 3M "$FIXTURE_DIR/big.bin"
    truncate -s 2M "$FIXTURE_DIR/nested/medium.bin"
    printf 'small\n' > "$FIXTURE_DIR/nested/deep/small.txt"

    for mode in single tree stats; do
        if [ "$(engine_json "$fresh" "$FIXTURE_DIR" "$mode")" \
            != "$(engine_json "$bundled" "$FIXTURE_DIR" "$mode")" ]; then
            printf '%s\n' "❌ The bundled $arch engine answers differently than a fresh build" >&2
            printf '%s\n' "   of the current source ($mode mode)." >&2
            rebuild_hint
            exit 1
        fi
    done
    printf '%s\n' "✅ The bundled $arch engine matches a fresh build of the current source."
}

# The same three assertions as a standalone mode, so the CI job that builds a
# release engine from scratch can make them instead of carrying its own copy of
# the readelf incantations -- which is where a translated field name or a
# forgotten check would go unnoticed the longest.
check_elf() {
    local path="$1" arch="$2"
    [ -f "$path" ] || die "$path is missing."
    verify_static_elf "$path" "$arch"
    printf '%s\n' "✅ $path is a statically linked $arch ELF binary."
}

build_engines() {
    local arch target libdir built flags
    for arch in "${ENGINE_ARCHES[@]}"; do
        target="$arch-unknown-linux-musl"
        # The host cc links x86_64 musl statically, but cannot target aarch64 --
        # Fedora's ld.bfd rejects the aarch64 erratum flag rustc passes. The
        # rust-lld shipped with the toolchain cross-links without a system
        # cross-gcc, so aarch64 asks for it and x86_64 deliberately does not:
        # changing the linker changes the bytes, and these are the bytes the
        # committed engines were built with.
        flags=""
        if [ "$arch" = aarch64 ]; then
            flags="-Clinker=rust-lld"
        fi
        libdir="$(engine_rustc --print target-libdir --target "$target" 2>/dev/null || true)"
        if [ -z "$libdir" ] || [ ! -d "$libdir" ]; then
            die "Rust target $target is not installed: rustup target add $target"
        fi
        printf '%s\n' "☉ Building $target ..."
        RUSTFLAGS="$flags" engine_cargo build --quiet --release \
            --manifest-path "$CRATE_DIR/Cargo.toml" --target "$target"
        built="$CRATE_DIR/target/$target/release/topo-core"
        verify_static_elf "$built" "$arch"
        install -m 755 "$built" "$BIN_DIR/topo-core-$arch"
        printf '%s\n' "  ✓ src/core/bin/topo-core-$arch"
    done
    write_stamp
    printf '%s\n' "  ✓ topo-core/engine.stamp"
}

case "${1:-}" in
    "")
        build_engines
        ;;
    --verify)
        [ $# -eq 1 ] || die "--verify takes no arguments."
        verify_engines
        ;;
    --compare)
        [ $# -eq 3 ] || die "--compare needs a binary and an architecture."
        compare_behaviour "$2" "$3"
        ;;
    --check-elf)
        [ $# -eq 3 ] || die "--check-elf needs a binary and an architecture."
        check_elf "$2" "$3"
        ;;
    -h | --help)
        usage
        ;;
    *)
        printf '%s\n' "Unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
esac
