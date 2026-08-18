from __future__ import annotations

import re

from quidz.sim import BreakMode, Simulator

# A run of 13 to 19 digits is the shape of a card number. Nothing this simulator emits may
# match it, in any payload, under any seed.
CARD_LIKE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def test_the_simulator_exposes_exactly_six_break_modes() -> None:
    assert [mode.value for mode in BreakMode] == [
        "duplicate",
        "replay",
        "reorder",
        "drop",
        "amount-mismatch",
        "tamper",
    ]


def test_the_same_seed_produces_byte_identical_deliveries() -> None:
    first = Simulator(seed=7).deliveries("adversarial")
    second = Simulator(seed=7).deliveries("adversarial")
    assert [(d.identity, d.raw, d.headers) for d in first] == [
        (d.identity, d.raw, d.headers) for d in second
    ]


def test_no_generated_payload_carries_anything_card_shaped() -> None:
    offenders: list[str] = []
    for seed in range(5):
        for scenario in ("happy", "adversarial"):
            for delivery in Simulator(seed=seed).deliveries(scenario):
                body = delivery.raw.decode("utf-8")
                offenders.extend(CARD_LIKE.findall(body))
    assert offenders == []
