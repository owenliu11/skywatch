import asyncio

from app.ingest.opensky import get_token
from app.redis_client import connect_redis, disconnect_redis


async def main() -> None:
    await connect_redis()

    try:
        token = await get_token()
        print("Token received:", bool(token))
    finally:
        await disconnect_redis()


asyncio.run(main())