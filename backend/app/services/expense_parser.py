import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedExpense:
    description: str
    amount_minor: int
    currency: str
    payer: str
    participants: list[str]
    split_type: str


def parse_expense(text: str, known_users: list[str]) -> ParsedExpense:
    amount_match = re.search(r"(?:₹|INR|Rs\.?)\s*([\d,]+)", text, re.IGNORECASE)
    if not amount_match:
        raise ValueError("an INR amount is required")
    amount_minor = int(amount_match.group(1).replace(",", "")) * 100
    lowered = text.lower()
    payer = next((user for user in known_users if user.lower() in lowered and f"paid by {user.lower()}" in lowered), None)
    payer = payer or next((user for user in known_users if f"{user.lower()} paid" in lowered), None)
    if payer is None:
        raise ValueError("payer is ambiguous")
    participants = [user for user in known_users if user.lower() in lowered]
    if payer not in participants:
        participants.insert(0, payer)
    split_type = "equal" if "equally" in lowered or "split" in lowered else "custom"
    return ParsedExpense("expense", amount_minor, "INR", payer, participants, split_type)
