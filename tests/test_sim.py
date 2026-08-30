from __future__ import annotations

import re

from quidz.sim import BreakMode, Simulator

# A run of 13 to 19 digits is the shape of a card number. Nothing this simulator emits may
# match it, in any payload, under any seed.
CARD_LIKE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def test_the_adversarial_scenario_applies_every_break_mode() -> None:
    # The point of the scenario, and the reason the flagship demo output is worth reading: a
    # break mode nobody wires into it is a mechanism nothing demonstrates.
    simulator = Simulator(seed=0)
    simulator.deliveries("adversarial")
    assert set(simulator.applied_breaks) == set(BreakMode)


def test_every_break_mode_changes_something_the_happy_path_does_not_produce() -> None:
    """The check above compares applied_breaks to BreakMode, both read from the same enum.

    Adding a member nobody wires into _build_deliveries or remote_payments would still pass
    it. This asks the question that check cannot: run the happy path once per mode, with only
    that one mode on, and require the deliveries or the provider's own payment list to differ
    from an unbroken run. amount-mismatch changes nothing in the deliveries, it books a
    provider total the webhooks never mention, so both surfaces are compared rather than one.
    """
    baseline_deliveries = Simulator(seed=0).deliveries("happy")
    baseline_remote = Simulator(seed=0).remote_payments()
    for mode in BreakMode:
        simulator = Simulator(seed=0)
        broken_deliveries = simulator.deliveries("happy", breaks=[mode])
        broken_remote = simulator.remote_payments()
        assert (broken_deliveries != baseline_deliveries) or (broken_remote != baseline_remote), (
            f"{mode} changed neither the deliveries nor the provider payment list"
        )


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
