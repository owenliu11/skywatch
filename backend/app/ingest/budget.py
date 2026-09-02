from datetime import UTC, datetime


def bbox_cost(
    lamin: float,
    lomin: float,
    lamax: float,
    lomax: float,
) -> int:
    lat_range = abs(lamax - lamin)
    lon_range = abs(lomax - lomin)

    area = lat_range * lon_range

    if area <= 25:
        return 1
    if area <= 100:
        return 2
    if area <= 400:
        return 3

    return 4


class CreditBudget:
    def __init__(self, daily_budget: int) -> None:
        self.daily_budget = daily_budget
        self._remaining = daily_budget
        self._reset_date = datetime.now(UTC).date()

    def _reset_if_needed(self) -> None:
        today = datetime.now(UTC).date()

        if today != self._reset_date:
            self._remaining = self.daily_budget
            self._reset_date = today

    def remaining(self) -> int:
        self._reset_if_needed()
        return self._remaining

    def can_afford(self, cost: int) -> bool:
        self._reset_if_needed()
        return self._remaining >= cost

    def record_local_spend(self, cost: int) -> None:
        self._reset_if_needed()
        self._remaining = max(self._remaining - cost, 0)

    def sync_with_server(self, remaining: int) -> None:
        self._reset_if_needed()
        self._remaining = remaining