#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
OUTPUT_DIR="$REPO_ROOT/dist/packages"
ENGINE_X86_64=""
ENGINE_AARCH64=""

usage() {
    cat <<'EOF'
Usage: packaging/build-linux-packages.sh [options]

Build Topo .deb and .rpm packages from the current checkout.

Options:
  --x86_64-engine PATH     Path to topo-core-x86_64
  --aarch64-engine PATH    Path to topo-core-aarch64
  --output-dir DIR         Directory for generated packages
  --version VERSION        Override package version
  -h, --help               Show this help

If an engine path is not provided, the script looks for:
  ./topo-core-$ARCH
  ./src/core/bin/topo-core-$ARCH
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --x86_64-engine)
            ENGINE_X86_64="${2:?--x86_64-engine requires a path}"
            shift 2
            ;;
        --aarch64-engine)
            ENGINE_AARCH64="${2:?--aarch64-engine requires a path}"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="${2:?--output-dir requires a directory}"
            shift 2
            ;;
        --version)
            VERSION="${2:?--version requires a value}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Error: required command '$1' was not found." >&2
        exit 1
    fi
}

find_engine() {
    local arch="$1"
    local explicit="$2"
    local candidate

    if [[ -n "$explicit" ]]; then
        if [[ ! -f "$explicit" ]]; then
            echo "Error: engine binary not found: $explicit" >&2
            exit 1
        fi
        printf '%s\n' "$explicit"
        return
    fi

    for candidate in \
        "$REPO_ROOT/topo-core-$arch" \
        "$REPO_ROOT/src/core/bin/topo-core-$arch"
    do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
}

copy_runtime_tree() {
    local root="$1"
    local engine_arch="$2"
    local engine_path="$3"
    local app_dir="$root/usr/lib/topo"
    local doc_dir="$root/usr/share/doc/topo"

    mkdir -p "$app_dir/src/core/bin" "$root/usr/bin" "$doc_dir"

    install -m 755 "$REPO_ROOT/topo" "$app_dir/topo"
    install -m 644 "$REPO_ROOT/VERSION" "$app_dir/VERSION"
    printf 'package\n' > "$app_dir/.topo-install-source"
    chmod 644 "$app_dir/.topo-install-source"

    cp -a "$REPO_ROOT/src/." "$app_dir/src/"
    find "$app_dir/src" -type d -name __pycache__ -prune -exec rm -rf '{}' +
    # shellcheck disable=SC2016
    find "$app_dir/src" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*$py.class' \) -delete

    rm -f "$app_dir/src/core/bin"/topo-core-* 2>/dev/null || true
    install -m 755 "$engine_path" "$app_dir/src/core/bin/topo-core-$engine_arch"

    if [[ -d "$REPO_ROOT/assets" ]]; then
        mkdir -p "$app_dir/assets"
        find "$REPO_ROOT/assets" -maxdepth 1 -type f -name '*.wav' \
            -exec install -m 644 '{}' "$app_dir/assets/" ';'
    fi

    install -m 644 "$REPO_ROOT/LICENSE" "$doc_dir/LICENSE"
    if [[ -f "$REPO_ROOT/README.md" ]]; then
        install -m 644 "$REPO_ROOT/README.md" "$doc_dir/README.md"
    fi

    ln -s ../lib/topo/topo "$root/usr/bin/topo"
}

build_one() {
    local package_type="$1"
    local package_arch="$2"
    local engine_arch="$3"
    local engine_path="$4"
    local package_path="$5"
    local root="$WORK_DIR/root-$package_type-$package_arch"
    local python_depends
    local -a fpm_args

    rm -rf "$root"
    copy_runtime_tree "$root" "$engine_arch" "$engine_path"

    # The launcher refuses to run on anything older than 3.10 (see MIN_PYTHON in
    # ./topo), and src/ is full of PEP 604 annotations, so an older interpreter
    # cannot load it at all. Debian 11 ships 3.9 and Ubuntu 20.04 ships 3.8:
    # unversioned, the dependency lets apt install the package there and leaves
    # the user to discover the problem on first run.
    #
    # The rpm deliberately keeps no lower bound. openSUSE Leap 15.6's python3 is
    # 3.6, and the release workflow installs python311 beside it and symlinks
    # /usr/local/bin/python3, which a versioned dependency cannot see -- zypper
    # would refuse the package on the very distro it was built for. Every deb leg
    # (ubuntu-latest, debian:stable) already ships 3.11 or newer, so the bound
    # costs nothing there. fpm passes a dependency that is already in Debian's
    # `name (>= version)` form through untouched.
    if [[ "$package_type" == deb ]]; then
        python_depends='python3 (>= 3.10)'
    else
        python_depends='python3'
    fi

    fpm_args=(
        -s dir
        -t "$package_type"
        -n topo
        -v "$VERSION"
        --iteration 1
        --architecture "$package_arch"
        --package "$package_path"
        --license MIT
        --maintainer "Jesencloud"
        --vendor "Jesencloud"
        --url "https://github.com/Jesencloud/Topo"
        --description "Linux cleanup, app removal, disk analysis, and status checks."
        --category utils
        --depends curl
        --depends "$python_depends"
        --depends python3-packaging
        -C "$root"
    )

    rm -f "$package_path"
    fpm "${fpm_args[@]}" .
    echo "Built $package_path"
}

if [[ -z "$VERSION" ]]; then
    echo "Error: package version is empty." >&2
    exit 1
fi

require_command fpm
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
rm -f \
    "$OUTPUT_DIR"/topo_*.deb \
    "$OUTPUT_DIR"/topo_*.deb.sha256 \
    "$OUTPUT_DIR"/topo_*.deb.asc \
    "$OUTPUT_DIR"/topo_*.deb.sha256.asc \
    "$OUTPUT_DIR"/topo-*.rpm \
    "$OUTPUT_DIR"/topo-*.rpm.sha256 \
    "$OUTPUT_DIR"/topo-*.rpm.asc \
    "$OUTPUT_DIR"/topo-*.rpm.sha256.asc

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/topo-packaging.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

declare -A ENGINE_BY_ARCH=(
    [x86_64]="$(find_engine x86_64 "$ENGINE_X86_64")"
    [aarch64]="$(find_engine aarch64 "$ENGINE_AARCH64")"
)
declare -A DEB_ARCH_BY_ENGINE=(
    [x86_64]=amd64
    [aarch64]=arm64
)
declare -A RPM_ARCH_BY_ENGINE=(
    [x86_64]=x86_64
    [aarch64]=aarch64
)

built_any=false
for engine_arch in x86_64 aarch64; do
    engine_path="${ENGINE_BY_ARCH[$engine_arch]}"
    if [[ -z "$engine_path" ]]; then
        echo "Skipping $engine_arch: no topo-core-$engine_arch binary found." >&2
        continue
    fi

    deb_arch="${DEB_ARCH_BY_ENGINE[$engine_arch]}"
    rpm_arch="${RPM_ARCH_BY_ENGINE[$engine_arch]}"
    # Both names carry the `--iteration 1` revision, because both packages do:
    # dpkg-deb -f reports Version 1.1.2-1 either way, and a file named
    # topo_1.1.2_amd64.deb claims to be a native package with no revision at all.
    # Debian's convention is name_upstream-revision_arch.deb; rpm's is
    # name-upstream-release.arch.rpm. get_package_asset_name() in
    # src/core/install_source.py has to spell out the same two names.
    build_one deb "$deb_arch" "$engine_arch" "$engine_path" \
        "$OUTPUT_DIR/topo_${VERSION}-1_${deb_arch}.deb"
    build_one rpm "$rpm_arch" "$engine_arch" "$engine_path" \
        "$OUTPUT_DIR/topo-${VERSION}-1.${rpm_arch}.rpm"
    built_any=true
done

if [[ "$built_any" != true ]]; then
    echo "Error: no packages were built because no engine binaries were found." >&2
    exit 1
fi
