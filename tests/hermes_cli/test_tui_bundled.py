def test_tui_finds_bundled_entry_js(tmp_path):
    """_find_bundled_tui finds entry.js bundled in the package."""
    tui_dist = tmp_path / "hermes_cli" / "tui_dist"
    tui_dist.mkdir(parents=True)
    entry = tui_dist / "entry.js"
    entry.write_text("// bundled TUI", encoding="utf-8")

    from hermes_cli.main import _find_bundled_tui

    result = _find_bundled_tui(hermes_cli_dir=tmp_path / "hermes_cli")
    assert result is not None
    assert result.name == "entry.js"


def test_tui_returns_none_when_no_bundle(tmp_path):
    """_find_bundled_tui returns None when no bundle exists."""
    from hermes_cli.main import _find_bundled_tui

    result = _find_bundled_tui(hermes_cli_dir=tmp_path / "hermes_cli")
    assert result is None


def test_tui_finds_node_beside_venv_python(tmp_path, monkeypatch):
    from hermes_cli import main

    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.touch()
    node = python.with_name("node.exe")
    node.touch()
    monkeypatch.setattr(main.sys, "executable", str(python))
    monkeypatch.setattr(main.sys, "platform", "win32")

    assert main._find_venv_node() == str(node)


def test_bundled_tui_preempts_missing_source_workspace(tmp_path, monkeypatch):
    from hermes_cli import main

    entry = tmp_path / "hermes_cli" / "tui_dist" / "entry.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr(main, "_ensure_tui_node", lambda: None)
    monkeypatch.setattr(main, "_find_bundled_tui", lambda: entry)
    monkeypatch.setattr(main, "_find_venv_node", lambda: main.sys.executable)

    argv, cwd = main._make_tui_argv(tmp_path / "missing-ui-tui", tui_dev=False)

    assert argv[-1] == str(entry)
    assert cwd == entry.parent
