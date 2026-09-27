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


def test_semantic_cache_defaults_to_off():
    """语义缓存默认关闭（安全默认）。

    缓存命中会**短路整个图（含检索）**，缓存内容是 graphrag 全链路的完整回答 ——
    documents.status 过滤器够不着它，停用文档的内容最长可泄漏 REDIS_CACHE_EXPIRE。
    默认 False 让"忘记配 .env"的部署失败方向从"静默泄漏"变成"只是不缓存"。

    ⚠️ 断言的是【代码默认值】而非运行时值：运行时被 .env 覆盖是预期的。
    写成 `settings.SEMANTIC_CACHE_ENABLED is False` 会假通过 —— 本机 .env 恰好
    设了 false，换个环境就挂。
    """
    from app.core.config import Settings

    assert Settings.model_fields["SEMANTIC_CACHE_ENABLED"].default is False
