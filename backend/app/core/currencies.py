SUPPORTED_CURRENCIES = frozenset({"EUR", "GBP", "INR", "USD"})


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        supported = ", ".join(sorted(SUPPORTED_CURRENCIES))
        raise ValueError(f"Unsupported currency; choose one of: {supported}")
    return currency
