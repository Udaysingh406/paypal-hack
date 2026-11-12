import pytest
from app.services.settlement import calculate_balances, optimize_settlement, split_equal


def test_equal_split_rounds_minor_units() -> None:
    assert split_equal(100, ["a", "b", "c"]) == {"a": 34, "b": 33, "c": 33}


def test_goa_demo_balances_and_transfers() -> None:
    balances = calculate_balances(
        {"Udai": 800000, "Rahul": 400000, "Aman": 200000, "Karan": 0},
        {"Udai": 350000, "Rahul": 350000, "Aman": 350000, "Karan": 350000},
    )
    transfers = optimize_settlement(balances, "INR")
    assert [(item.from_user, item.to_user, item.amount_minor) for item in transfers] == [
        ("Karan", "Udai", 350000),
        ("Aman", "Udai", 100000),
        ("Aman", "Rahul", 50000),
    ]


def test_unbalanced_records_are_rejected() -> None:
    with pytest.raises(ValueError):
        calculate_balances({"a": 100}, {"a": 0})
