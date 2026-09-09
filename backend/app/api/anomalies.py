"""Phase 4 API placeholder; no anomaly detection runs in Phase 3."""

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/anomalies", summary="Phase 4 anomaly stub")
async def get_anomalies(
    since: str | None = Query(None, description="Reserved Phase 4 time filter; ignored."),
    anomaly_type: str | None = Query(None, alias="type", description="Reserved filter; ignored."),
) -> list[dict[str, object]]:
    """Always return an empty list. Filters are accepted but not implemented yet."""
    return []
