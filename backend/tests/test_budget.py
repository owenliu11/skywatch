from app.ingest.budget import CreditBudget, bbox_cost


def test_bbox_cost_tier_1() -> None:
    assert bbox_cost(0, 0, 5, 5) == 1


def test_bbox_cost_tier_2() -> None:
    assert bbox_cost(0, 0, 5, 10) == 2


def test_bbox_cost_tier_3() -> None:
    assert bbox_cost(0, 0, 10, 20) == 3


def test_bbox_cost_tier_4() -> None:
    assert bbox_cost(0, 0, 20, 25) == 4


def test_bay_area_is_one_credit() -> None:
    cost = bbox_cost(
        lamin=37.0,
        lomin=-123.0,
        lamax=38.5,
        lomax=-121.5,
    )

    assert cost == 1


def test_budget_starts_full() -> None:
    budget = CreditBudget(4000)

    assert budget.remaining() == 4000


def test_local_spend_reduces_budget() -> None:
    budget = CreditBudget(4000)

    budget.record_local_spend(1)

    assert budget.remaining() == 3999


def test_budget_cannot_go_below_zero() -> None:
    budget = CreditBudget(2)

    budget.record_local_spend(5)

    assert budget.remaining() == 0


def test_can_afford() -> None:
    budget = CreditBudget(3)

    assert budget.can_afford(3)
    assert not budget.can_afford(4)


def test_server_remaining_overrides_local_estimate() -> None:
    budget = CreditBudget(4000)

    budget.record_local_spend(1)
    assert budget.remaining() == 3999

    budget.sync_with_server(3975)

    assert budget.remaining() == 3975