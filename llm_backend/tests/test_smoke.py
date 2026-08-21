def test_app_importable():
    import main  # noqa: F401  # FastAPI 入口在 llm_backend/main.py(模块名 main,非 app.main)


async def test_db_reachable():
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        row = await s.execute(text("SELECT 1"))
        assert row.scalar() == 1
