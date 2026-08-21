def test_indexing_config_present():
    from app.core.config import settings

    assert settings.MAX_FILE_SIZE_MB == 30
    assert settings.CHUNK_MIN_SIZE == 5
    assert "pdf" in settings.allowed_extensions
    assert "exe" not in settings.allowed_extensions
    assert settings.MINERU_BASE_URL.startswith("https://")


def test_allowed_extensions_normalized():
    from app.core.config import settings

    assert settings.allowed_extensions == {"txt", "md", "pdf", "docx"}
