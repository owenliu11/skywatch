from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.db import check_db, connect_db, disconnect_db
from app.redis_client import check_redis, connect_redis, disconnect_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    await connect_redis()

    yield

    await disconnect_redis()
    await disconnect_db()


app = FastAPI(
    title="SkyWatch API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    database_ok = await check_db()
    redis_ok = await check_redis()

    healthy = database_ok and redis_ok

    body = {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if database_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }

    if not healthy:
        return JSONResponse(
            status_code=503,
            content=body,
        )

    return body