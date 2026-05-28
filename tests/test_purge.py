from src.clean.project import Scanner


def test_is_project_root(test_env):
    scanner = Scanner([])

    project_dir = test_env / "my_project"
    project_dir.mkdir()
    (project_dir / "package.json").touch()

    assert scanner.is_project_root(project_dir) is True

    regular_dir = test_env / "docs"
    regular_dir.mkdir()
    assert scanner.is_project_root(regular_dir) is False


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
