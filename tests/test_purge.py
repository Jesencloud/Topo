from unittest.mock import patch

from src.clean.project import PurgeManager, Scanner, _merge_artifact_roots


def test_merge_artifact_roots_deduplicates_and_removes_nested_paths(test_env):
    parent = (test_env / "project/node_modules").resolve()
    child = (parent / "nested/target").resolve()
    sibling_prefix = (test_env / "project/node_modules-extra").resolve()

    merged = _merge_artifact_roots([child, parent, child, sibling_prefix])

    assert merged == [parent, sibling_prefix]


def test_merge_artifact_roots_handles_child_before_parent(test_env):
    parent = (test_env / "z/project/target").resolve()
    child = (parent / "nested/build").resolve()

    assert _merge_artifact_roots([child, parent]) == [parent]


def test_merge_artifact_roots_collapses_a_multi_level_chain(test_env):
    """Three nesting levels at once, so an intermediate root must be dropped.

    Only the outermost path survives, which pins the ancestor test to "any
    retained ancestor" rather than "the immediate parent": the deepest entry's
    parent is the intermediate one, not the root that ends up being kept.
    Passing them deepest-first also proves discovery order is irrelevant.
    """
    outer = (test_env / "repo/node_modules").resolve()
    middle = (outer / "pkg/target").resolve()
    inner = (middle / "debug/build").resolve()
    unrelated = (test_env / "repo/dist").resolve()

    assert _merge_artifact_roots([inner, middle, outer, unrelated]) == [unrelated, outer]


def test_scan_artifacts(test_env):
    scanner = Scanner([])
    project_dir = test_env / "my_project"
    project_dir.mkdir()
    (project_dir / "node_modules").mkdir()
    (project_dir / "target").mkdir()
    (project_dir / "src").mkdir()

    artifacts = scanner.scan_artifacts(project_dir)
    artifact_names = [p.name for p in artifacts]

    assert "node_modules" in artifact_names
    assert "target" in artifact_names
    assert "src" not in artifact_names


def test_recursive_scan(test_env):
    # Setup: Projects at different depths
    p1 = test_env / "Projects/p1"
    p1.mkdir(parents=True)
    (p1 / "Cargo.toml").touch()
    (p1 / "target").mkdir()

    p2 = test_env / "Projects/subdir/p2"
    p2.mkdir(parents=True)
    (p2 / "package.json").touch()

    scanner = Scanner([str(test_env / "Projects")])
    projects = list(scanner.scan_for_projects())

    assert p1 in projects
    assert p2 in projects


def test_scan_artifacts_bin_requires_dotnet_project(test_env):
    """L4: a bare 'bin' dir is purged only when a .NET project file sits beside
    it; otherwise it may be a script/binary dir and must be left alone."""
    scanner = Scanner([])

    # Non-.NET project: bin/ must be ignored, other artifacts still collected.
    plain = test_env / "plain_project"
    plain.mkdir()
    (plain / "package.json").touch()
    (plain / "bin").mkdir()
    (plain / "node_modules").mkdir()
    plain_names = [p.name for p in scanner.scan_artifacts(plain)]
    assert "bin" not in plain_names
    assert "node_modules" in plain_names

    # .NET project: bin/ is a genuine build artifact.
    dotnet = test_env / "dotnet_project"
    dotnet.mkdir()
    (dotnet / "App.csproj").touch()
    (dotnet / "bin").mkdir()
    dotnet_names = [p.name for p in scanner.scan_artifacts(dotnet)]
    assert "bin" in dotnet_names


def test_run_scan_limits_size_workers_and_drops_nested_artifacts(test_env):
    parent = test_env / "project/node_modules"
    child = parent / "nested/target"
    child.mkdir(parents=True)
    manager = PurgeManager()
    manager.scanner.scan_for_projects = lambda: iter([test_env / "project"])
    manager.scanner.scan_artifacts = lambda _project: [child, parent]

    with (
        patch("src.clean.project.get_size_fast", return_value=10),
        patch(
            "src.clean.project.ThreadPoolExecutor",
            wraps=__import__(
                "concurrent.futures", fromlist=["ThreadPoolExecutor"]
            ).ThreadPoolExecutor,
        ) as executor,
    ):
        results = manager.run_scan()

    executor.assert_called_once_with(max_workers=2)
    assert [item["path"] for item in results] == [parent.resolve()]
