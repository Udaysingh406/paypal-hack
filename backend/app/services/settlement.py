from dataclasses import dataclass


@dataclass(frozen=True)
class Transfer:
    from_user: str
    to_user: str
    amount_minor: int
    currency: str


def split_equal(amount_minor: int, users: list[str]) -> dict[str, int]:
    if amount_minor < 0 or not users:
        raise ValueError("amount_minor must be non-negative and users must be non-empty")
    base, remainder = divmod(amount_minor, len(users))
    return {user: base + (index < remainder) for index, user in enumerate(users)}


def calculate_balances(
    contributions: dict[str, int], shares: dict[str, int]
) -> dict[str, int]:
    if any(value < 0 for value in contributions.values()) or any(value < 0 for value in shares.values()):
        raise ValueError("monetary values cannot be negative")
    users = set(contributions) | set(shares)
    balances = {user: contributions.get(user, 0) - shares.get(user, 0) for user in users}
    if sum(balances.values()) != 0:
        raise ValueError("contributions and shares must balance")
    return balances


def optimize_settlement(
    balances: dict[str, int], currency: str = "INR"
) -> list[Transfer]:
    if any(value < 0 and not isinstance(value, int) for value in balances.values()):
        raise ValueError("balances must be integers")
    debtors = [[user, -amount] for user, amount in balances.items() if amount < 0]
    creditors = [[user, amount] for user, amount in balances.items() if amount > 0]
    debtors.sort(key=lambda item: item[1], reverse=True)
    creditors.sort(key=lambda item: item[1], reverse=True)
    if sum(item[1] for item in debtors) != sum(item[1] for item in creditors):
        raise ValueError("balances must sum to zero")
    transfers: list[Transfer] = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor, owed = debtors[debtor_index]
        creditor, due = creditors[creditor_index]
        amount = min(owed, due)
        if amount <= 0 or debtor == creditor:
            raise ValueError("invalid settlement state")
        transfers.append(Transfer(debtor, creditor, amount, currency))
        debtors[debtor_index][1] -= amount
        creditors[creditor_index][1] -= amount
        if debtors[debtor_index][1] == 0:
            debtor_index += 1
        if creditors[creditor_index][1] == 0:
            creditor_index += 1
    return transfers


def recover_after_failure(
    balances: dict[str, int], failed: Transfer, paid_minor: int = 0
) -> list[Transfer]:
    if paid_minor < 0 or paid_minor > failed.amount_minor:
        raise ValueError("paid_minor must be within the failed transfer amount")
    updated = dict(balances)
    outstanding = failed.amount_minor - paid_minor
    updated[failed.from_user] = updated.get(failed.from_user, 0) - paid_minor
    updated[failed.to_user] = updated.get(failed.to_user, 0) + paid_minor
    if outstanding:
        updated[failed.from_user] = updated.get(failed.from_user, 0) + outstanding
        updated[failed.to_user] = updated.get(failed.to_user, 0) - outstanding
    return optimize_settlement(updated, failed.currency)
