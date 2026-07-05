import asyncio

from sqlalchemy import text

from deploysense.database import async_session_factory


async def fix():
    async with async_session_factory() as db:
        await db.execute(
            text("UPDATE deployments SET risk_level = 'MODERATE' WHERE risk_level = 'MEDIUM'")
        )
        await db.execute(
            text("UPDATE risk_assessments SET risk_level = 'MODERATE' WHERE risk_level = 'MEDIUM'")
        )
        await db.commit()
        print("Fixed!")


if __name__ == "__main__":
    asyncio.run(fix())
