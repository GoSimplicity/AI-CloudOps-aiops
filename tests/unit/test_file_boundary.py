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


@pytest.mark.parametrize("name", ["../outside.md", "/tmp/outside.md", "a/../../outside.txt", "..\\outside.txt", "bad\x00.md"])
def test_knowledge_upload_rejects_paths(tmp_path, name):
    from app.common.exceptions import ValidationError
    from app.services.assistant_service import OptimizedAssistantService

    service = OptimizedAssistantService()
    with pytest.raises(ValidationError):
        service._resolve_filename_conflict(str(tmp_path), name)
