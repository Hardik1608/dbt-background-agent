# Copied verbatim from 05_sales-analytics-platform/src/sales_analytics/ingestion/synthetic.py on 2026-10-14
# (provenance: fixtures/sales_warehouse/PROVENANCE.md). Only the import path was changed.
"""Deterministic synthetic generator with the UCI Online Retail II schema.

Used by unit/integration tests and CI so nothing depends on a 45 MB download. It is not
a random table: it reproduces every data-quality quirk the silver layer must handle in
the real dataset, plus a behavioural signal so the churn model has something to learn.

Quirks reproduced (all measured in the real data first, see docs/DATA_MODEL.md):
* cancellations: invoices prefixed ``C`` with negative quantities
* bad-debt adjustments: invoices prefixed ``A`` (stock code ``B``, negative price)
* stock write-offs: negative quantity, price 0, no customer ("damaged", "lost", ...)
* guest checkouts: missing ``customer_id``
* non-product stock codes: POST, M, D, BANK CHARGES, gift vouchers
* exact duplicate lines, lowercase stock-code suffixes, untrimmed descriptions
* invoices whose lines straddle a minute boundary (two timestamps per invoice)
* a bulk-quantity outlier order cancelled minutes later (like the real 74,215 units)
* customers that change country (exercises the SCD Type 2 dimension)

Time is simulated as float "days since start" and converted to timestamps once at the
end, which keeps generation of ~70k lines to a couple of seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from dbt_agent.fixtures.sales_source.landing import LANDING_COLUMNS

COUNTRY_WEIGHTS: dict[str, float] = {
    "United Kingdom": 0.80,
    "Germany": 0.04,
    "France": 0.035,
    "EIRE": 0.03,
    "Netherlands": 0.015,
    "Spain": 0.012,
    "Belgium": 0.012,
    "Switzerland": 0.01,
    "Portugal": 0.01,
    "Australia": 0.008,
    "Norway": 0.008,
    "Italy": 0.008,
    "Sweden": 0.006,
    "Denmark": 0.006,
}
_COUNTRIES = list(COUNTRY_WEIGHTS)
_COUNTRY_P = np.array(list(COUNTRY_WEIGHTS.values())) / sum(COUNTRY_WEIGHTS.values())

SEASONALITY = np.array([0.75, 0.75, 1.0, 0.9, 1.0, 1.0, 0.95, 0.95, 1.25, 1.4, 1.6, 0.9])
MAX_SEASONALITY = float(SEASONALITY.max())

_ADJECTIVES = [
    "WHITE",
    "RED",
    "PINK",
    "BLUE",
    "VINTAGE",
    "RETRO",
    "WOODEN",
    "CERAMIC",
    "GLASS",
    "PAISLEY",
    "SPOTTY",
    "HEART",
    "STAR",
    "FLORAL",
    "JUMBO",
    "MINI",
]
_ITEMS = [
    "T-LIGHT HOLDER",
    "LANTERN",
    "COAT HANGER",
    "LUNCH BAG",
    "CAKE CASES",
    "MUG",
    "PLATE",
    "BUNTING",
    "DOORSTOP",
    "CUSHION COVER",
    "PHOTO FRAME",
    "NOTEBOOK",
    "TEA TOWEL",
    "STORAGE JAR",
    "PARTY BAG",
    "WATER BOTTLE",
    "CANDLE",
    "CLOCK",
]
_WRITE_OFF_REASONS = ["damaged", "lost", "check", "?", "wet damaged", "smashed"]
_QUANTITIES = np.array([1, 2, 3, 4, 6, 8, 12, 24, 48])
_QUANTITY_CDF = np.cumsum([0.18, 0.14, 0.08, 0.10, 0.14, 0.06, 0.18, 0.08, 0.04])

FIRST_INVOICE_NUMBER = 489434
PRE_CHURN_FADE_DAYS = 75.0


@dataclass(frozen=True)
class SyntheticConfig:
    n_customers: int = 500
    n_products: int = 200
    seed: int = 42
    start: str = "2009-12-01"
    end: str = "2011-12-09 18:00"
    guest_order_share: float = 0.15
    cancellation_rate: float = 0.04
    duplicate_line_rate: float = 0.01
    country_change_rate: float = 0.04
    write_off_rate: float = 0.003


@dataclass
class _Invoice:
    kind: str  # sale | cancel | adjust
    day: float  # days since start (fractional = time of day)
    customer_id: str | None
    country: str
    lines: list[tuple[str, str, int, float, int]] = field(default_factory=list)
    # line tuple: (stock_code, description, quantity, price, minute_offset)


class _Calendar:
    """Day-index helpers (month for seasonality, weekday to skip Saturdays)."""

    def __init__(self, start: pd.Timestamp, end: pd.Timestamp) -> None:
        self.start = start
        self.n_days = (end - start).days + 2
        days = pd.date_range(start.normalize(), periods=self.n_days + 400, freq="D")
        self.month = days.month.to_numpy() - 1
        self.weekday = days.dayofweek.to_numpy()

    def seasonality(self, day: float) -> float:
        return float(SEASONALITY[self.month[int(day)]])

    def business_time(self, rng: np.random.Generator, day: float) -> float:
        """Snap to a time of day between 07:30 and 19:00, moving Saturdays to Sunday."""
        base = float(np.floor(day))
        if self.weekday[int(base)] == 5:
            base += 1.0
        minutes = int(rng.integers(7 * 60 + 30, 19 * 60))
        return base + minutes / 1440.0


def _sample_without_replacement(rng: np.random.Generator, log_w: np.ndarray, k: int) -> np.ndarray:
    """Weighted sampling without replacement via the Gumbel top-k trick (vectorised)."""
    keys = log_w + rng.gumbel(size=log_w.shape[0])
    return np.argpartition(-keys, k - 1)[:k] if k < log_w.shape[0] else np.argsort(-keys)


def generate(cfg: SyntheticConfig | None = None) -> pd.DataFrame:
    """Return a landing-schema frame sorted by invoice date (deterministic for a seed)."""
    cfg = cfg or SyntheticConfig()
    rng = np.random.default_rng(cfg.seed)
    start, end = pd.Timestamp(cfg.start), pd.Timestamp(cfg.end)
    end_day = (end - start).total_seconds() / 86400.0
    cal = _Calendar(start, end)

    # ---------------------------------------------------------------- products
    n_p = cfg.n_products
    p_codes = [
        str(20000 + i * 7) + (str(rng.choice(list("ABCPW"))) if rng.random() < 0.15 else "")
        for i in range(n_p)
    ]
    p_names = [f"{rng.choice(_ADJECTIVES)} {rng.choice(_ITEMS)}" for _ in range(n_p)]
    p_prices = np.clip(np.round(np.exp(rng.normal(np.log(2.5), 0.7, n_p)), 2), 0.29, 25.0)
    p_log_w = -0.9 * np.log(rng.permutation(n_p) + 1.0)

    # ---------------------------------------------------------------- customers
    n_c = cfg.n_customers
    c_ids = np.sort(12000 + rng.choice(6000, size=n_c, replace=False)).astype(str)
    c_country = rng.choice(_COUNTRIES, size=n_c, p=_COUNTRY_P)
    existing = rng.random(n_c) < 0.45  # already customers when the data starts
    acquired = np.where(existing, 0.0, rng.uniform(0, end_day - 60, n_c))
    lifetime = rng.exponential(420.0, n_c)
    loyal = rng.random(n_c) < 0.2  # never churn inside the window
    churn_day = np.where(loyal, np.inf, acquired + lifetime)
    wholesaler = rng.random(n_c) < 0.08
    rate_per_day = np.exp(rng.normal(np.log(0.9), 0.6, n_c)) * np.where(wholesaler, 3, 1) / 30
    basket = np.exp(rng.normal(0.0, 0.35, n_c)) * np.where(wholesaler, 1.8, 1.0)
    new_country = rng.choice(_COUNTRIES, size=n_c, p=_COUNTRY_P)
    changes_country = (rng.random(n_c) < cfg.country_change_rate) & (new_country != c_country)
    change_fraction = rng.uniform(0.3, 0.7, n_c)

    # ---------------------------------------------------------------- orders
    # (day, customer_index or -1 for guests, country)
    orders: list[tuple[float, int, str]] = []
    for i in range(n_c):
        active_until = min(churn_day[i], end_day)
        switch_day = (
            acquired[i] + (active_until - acquired[i]) * change_fraction[i]
            if changes_country[i]
            else np.inf
        )
        lam_max = rate_per_day[i] * MAX_SEASONALITY
        t = acquired[i]
        while True:  # thinned Poisson process with seasonality + pre-churn fade-out
            t += rng.exponential(1.0 / lam_max)
            if t >= active_until:
                break
            accept = cal.seasonality(t) / MAX_SEASONALITY
            if churn_day[i] - t < PRE_CHURN_FADE_DAYS:
                accept *= 0.45
            if rng.random() >= accept:
                continue
            day = cal.business_time(rng, t)
            if day > end_day:
                break
            orders.append((day, i, str(new_country[i] if day >= switch_day else c_country[i])))

    n_guest = int(len(orders) * cfg.guest_order_share / (1 - cfg.guest_order_share))
    guest_days = rng.uniform(0, end_day, n_guest)
    guest_country = rng.choice(_COUNTRIES, size=n_guest, p=_COUNTRY_P)
    for d, country in zip(guest_days, guest_country, strict=True):
        orders.append((min(cal.business_time(rng, d), end_day), -1, str(country)))
    orders.sort(key=lambda o: (o[0], o[1]))

    invoices: list[_Invoice] = []
    for day, ci, country in orders:
        cust = None if ci < 0 else str(c_ids[ci])
        bsk = 1.0 if ci < 0 else float(basket[ci])
        is_wholesale = ci >= 0 and bool(wholesaler[ci])
        straddles = rng.random() < 0.01
        n_lines = int(min(40, 1 + rng.poisson(7 * bsk**0.5)))
        lines: list[tuple[str, str, int, float, int]] = []
        for j, p in enumerate(_sample_without_replacement(rng, p_log_w, min(n_lines, n_p))):
            qty = int(_QUANTITIES[np.searchsorted(_QUANTITY_CDF, rng.random() * _QUANTITY_CDF[-1])])
            if is_wholesale:
                qty *= int(rng.choice([1, 2, 4]))
            price = float(p_prices[p])
            if qty >= 48:
                price = round(price * 0.85, 2)  # bulk discount, like the real price list
            if cust is not None and rng.random() < 0.002:
                price = 0.0  # free gift line
            code = p_codes[p]
            if code[-1].isalpha() and rng.random() < 0.05:
                code = code[:-1] + code[-1].lower()  # lowercase suffix quirk (e.g. 85123a)
            desc = p_names[p] + (" " if rng.random() < 0.2 else "")  # untrimmed quirk
            lines.append((code, desc, qty, price, 1 if (straddles and j >= 3) else 0))
        if cust is not None and country != "United Kingdom" and rng.random() < 0.6:
            lines.append(("POST", "POSTAGE", int(rng.integers(1, 4)), 18.0, 0))
        if rng.random() < 0.004:
            lines.append(("M", "Manual", 1, round(float(rng.uniform(0.5, 50)), 2), 0))
        if cust is not None and rng.random() < 0.003:
            lines.append(("gift_0001_20", "Dotcomgiftshop Gift Voucher £20.00", 1, 17.02, 0))
        invoices.append(_Invoice("sale", day, cust, country, lines))

        cancel_p = cfg.cancellation_rate if cust else cfg.cancellation_rate / 2
        if rng.random() < cancel_p:
            k = int(rng.integers(1, len(lines) + 1))
            picked = sorted(rng.choice(len(lines), size=k, replace=False))
            c_lines = [
                (lines[x][0], lines[x][1], -abs(lines[x][2]), lines[x][3], 0) for x in picked
            ]
            if rng.random() < 0.1:
                c_lines.append(("D", "Discount", -1, round(float(rng.uniform(5, 50)), 2), 0))
            c_day = min(day + float(rng.uniform(0.01, 10)), end_day)
            invoices.append(_Invoice("cancel", c_day, cust, country, c_lines))

    # Stock write-offs: negative quantity, zero price, no customer, free-text description.
    n_write_offs = max(1, int(sum(len(inv.lines) for inv in invoices) * cfg.write_off_rate))
    for _ in range(n_write_offs):
        p = int(rng.integers(n_p))
        day = min(cal.business_time(rng, rng.uniform(0, end_day)), end_day)
        reason = str(rng.choice(_WRITE_OFF_REASONS))
        line = (p_codes[p], reason, -int(rng.integers(1, 100)), 0.0, 0)
        invoices.append(_Invoice("sale", day, None, "United Kingdom", [line]))

    # Bank charges (booked on cancellation invoices) and bad-debt adjustments ("A" invoices).
    for _ in range(8):
        day = min(cal.business_time(rng, rng.uniform(0, end_day)), end_day)
        line = ("BANK CHARGES", "Bank Charges", -1, 15.0, 0)
        invoices.append(_Invoice("cancel", day, None, "United Kingdom", [line]))
    for _ in range(2):
        day = min(cal.business_time(rng, rng.uniform(0, end_day)), end_day)
        line = ("B", "Adjust bad debt", 1, -round(float(rng.uniform(500, 5000)), 2), 0)
        invoices.append(_Invoice("adjust", day, None, "United Kingdom", [line]))

    # A bulk outlier order cancelled 16 minutes later (mirrors the real 74,215-unit order).
    whale = int(rng.integers(n_c))
    outlier_day = max(
        (pd.Timestamp("2011-01-18 10:01") - start).total_seconds() / 86400.0,
        float(np.ceil(acquired[whale])) + 10 / 24,
    )
    big = (p_codes[0], p_names[0], 50_000, 1.04, 0)
    whale_id, whale_country = str(c_ids[whale]), str(c_country[whale])
    invoices.append(_Invoice("sale", outlier_day, whale_id, whale_country, [big]))
    invoices.append(
        _Invoice(
            "cancel",
            outlier_day + 16 / 1440,
            whale_id,
            whale_country,
            [(big[0], big[1], -big[2], big[3], 0)],
        )
    )

    # ---------------------------------------------------------------- emit rows
    # Invoice numbers follow one chronological sequence; prefixes mark the invoice kind.
    invoices.sort(key=lambda inv: (inv.day, inv.customer_id or "", inv.kind))
    prefix = {"sale": "", "cancel": "C", "adjust": "A"}
    cols: dict[str, list] = {
        c: []
        for c in (
            "invoice",
            "stock_code",
            "description",
            "quantity",
            "day",
            "price",
            "customer_id",
            "country",
        )
    }
    for number, inv in enumerate(invoices, start=FIRST_INVOICE_NUMBER):
        invoice_id = f"{prefix[inv.kind]}{number}"
        minute = round(inv.day * 1440.0) / 1440.0  # whole minutes, like the source system
        for code, desc, qty, price, offset in inv.lines:
            copies = 2 if rng.random() < cfg.duplicate_line_rate else 1  # exact duplicates
            for _ in range(copies):
                cols["invoice"].append(invoice_id)
                cols["stock_code"].append(code)
                cols["description"].append(desc)
                cols["quantity"].append(qty)
                cols["day"].append(minute + offset / 1440.0)
                cols["price"].append(price)
                cols["customer_id"].append(inv.customer_id)
                cols["country"].append(inv.country)

    frame = pd.DataFrame(cols)
    frame["invoice_date"] = (start + pd.to_timedelta(frame.pop("day"), unit="D")).dt.round("min")
    frame = frame.sort_values(["invoice_date", "invoice"], kind="mergesort").reset_index(drop=True)
    frame["_source_sheet"] = "synthetic"
    frame["_source_row_number"] = np.arange(len(frame), dtype="int64")
    return frame[LANDING_COLUMNS]
