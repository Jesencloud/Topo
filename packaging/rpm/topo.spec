Name:           topo
Version:        0.5.0
Release:        1%{?dist}
Summary:        High-performance Linux System Optimizer (Inspired by Mole)

License:        MIT
URL:            https://github.com/Jesencloud/Topo
Source0:        https://github.com/Jesencloud/Topo/archive/refs/heads/main.tar.gz

BuildArch:      x86_64 aarch64
Requires:       python3 >= 3.10
Requires:       git
Requires:       procps-ng

%description
Topo is a high-performance system optimization and cleanup tool for Linux,
featuring a hybrid Python logic and a custom Rust scanning engine.

%prep
%setup -q -n Topo-main

%build
# No build step needed for Python, and we use pre-compiled Rust binaries in this version
# Future: Build rust core here if needed

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{_datadir}/topo
mkdir -p %{buildroot}%{_bindir}

# Copy source files
cp -r src %{buildroot}%{_datadir}/topo/
cp topo %{buildroot}%{_datadir}/topo/
cp LICENSE %{buildroot}%{_datadir}/topo/

# Create symlink in /usr/bin
ln -s %{_datadir}/topo/topo %{buildroot}%{_bindir}/topo

%files
%{_datadir}/topo
%{_bindir}/topo
%license LICENSE

%changelog
* Tue May 26 2026 Jesencloud <jesen@example.com> - 0.5.0-1
- Initial RPM release with multi-arch support
