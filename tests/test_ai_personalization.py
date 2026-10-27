from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.ai.personalization import clear_user_cache, personalized_greeting
from config_loader import AIConfig
from database.models import AIActivityDaily, AIUserSettings, Base


class AIPersonalizationTests(unittest.TestCase):
    def test_activity_is_used_locally_and_result_is_cached(self) -> None:
        async def test() -> None:
            with tempfile.TemporaryDirectory() as directory:
                engine = create_async_engine(
                    f"sqlite+aiosqlite:///{Path(directory) / 'personalization.db'}"
                )
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                now = datetime(2026, 10, 27, 10, 0)
                async with factory() as session:
                    session.add(
                        AIUserSettings(
                            telegram_id=7,
                            personalization_enabled=True,
                            humor_enabled=True,
                        )
                    )
                    session.add(
                        AIActivityDaily(
                            telegram_id=7,
                            activity_date=date(2026, 10, 27),
                            category="homework",
                            count=3,
                        )
                    )
                    await session.commit()
                    result = await personalized_greeting(
                        session,
                        7,
                        AIConfig(personalization_enabled=True, humor_enabled=True),
                        "Europe/Chisinau",
                        now=now,
                    )
                    self.assertIn("режим продуктивности", result)
                    self.assertEqual(
                        await personalized_greeting(
                            session,
                            7,
                            AIConfig(personalization_enabled=True, humor_enabled=True),
                            "Europe/Chisinau",
                            now=now,
                        ),
                        result,
                    )
                clear_user_cache(7)
                await engine.dispose()

        asyncio.run(test())


if __name__ == "__main__":
    unittest.main()
