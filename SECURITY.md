# Security

QUIDZ is a portfolio reference implementation, not a supported product. It is still a repository
about signature verification, so the following is worth stating plainly.

## Every secret here is a placeholder

The simulator signs with `whsec_synthetic_primary`, `whsec_synthetic_previous` and an Adyen HMAC
key of `"00" * 32`. These are literals in `src/quidz/sim.py`, they are valid nowhere, and they
exist so the demo can sign and verify its own deliveries. No code path in this repository reaches
the network, and no test does either. Do not point this at a real provider without supplying your
own keys through your own configuration, and never commit those.

## No PAN and no PII enters the ledger, the inbox or the logs

The stored delivery row keeps the raw request bytes, which is what makes replay and
after-the-fact verification possible, so what the simulator generates matters. It generates no
card-like data of any kind: identifiers are letter-seeded tokens with every fourth character
forced to a letter, which caps any digit run at three.
`tests/test_sim.py::test_no_generated_payload_carries_anything_card_shaped` sweeps every payload
from five seeds across both scenarios for runs of 13 to 19 digits and fails on any hit.

In a real deployment the same rule holds and is harder: a webhook body can carry a shopper email,
a billing address or a partial card number, so retention on the delivery table is a decision that
has to be made deliberately rather than inherited from a demo.

## HMAC verification is necessary and not sufficient

A valid signature says the body was produced by somebody holding the endpoint secret. It does not
say the request is recent, unless a timestamp is inside the signed content, and it does not say
the caller is who they claim to be at the network level. Use TLS. Use IP allowlisting where the
provider publishes ranges, which Stripe does. Rotate endpoint secrets, which is why
`verify_stripe` accepts a sequence of active secrets rather than one.

Note the asymmetry this repo is built to make visible: Stripe signs a timestamp, so a tolerance
window is available and is set to 300 seconds by default. Adyen signs no timestamp, so no
equivalent window exists there, and identity dedup plus TLS plus IP allowlisting is the whole
replay defence on that path.

## The framework traps

Two ways to build a verifier that passes its unit tests and fails in production:

- The webhook route must receive the raw request bytes, before any body parser runs. `express.json()`
  mounted ahead of the route, Next.js `bodyParser`, and AWS API Gateway without an explicit
  `rawBody` body-mapping template all hand the handler a re-encoded body, and a re-encoded body
  does not verify.
- The webhook route must be exempted from CSRF middleware in Django and Rails style frameworks.
  A provider cannot present a CSRF token, so the request is rejected before verification ever
  runs, and the provider sees a failure it will retry.

## Reporting

Open a GitHub issue at <https://github.com/PNX89/QUIDZ/issues>. There is no private disclosure
process and no security support commitment, because this is a portfolio reference rather than
something anybody should be running.
