# testing token was recieved


import asyncio

from app.ingest.opensky import fetch_token


async def main() -> None:
    token = await fetch_token()

    print(f"Token received successfully. Length: {len(token)}")


if __name__ == "__main__":
    asyncio.run(main())