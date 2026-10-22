import pytest

from app.utils.file_boundary import resolve_tool_path


def test_confines_relative_absolute_and_symlink_paths(monkeypatch, tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    monkeypatch.setenv("MCP_FILE_ROOT", str(root))
    assert resolve_tool_path("guide.md") == str(root / "guide.md")
    assert resolve_tool_path(str(root / "guide.md")) == str(root / "guide.md")
    (root / "escape").symlink_to(tmp_path)
    for value in ["../secret", str(tmp_path / "secret"), "escape/secret"]:
        with pytest.raises(ValueError, match="outside MCP_FILE_ROOT"):
            resolve_tool_path(value)
