"""Adversarial test suite for Charter.

Covers five categories of attacks against the v0.x trust model:

  - Prompt injection via Charter clause text
  - Prompt injection via calling-agent task description
  - Confidence-threshold manipulation (low-confidence flood)
  - Charter chain attenuation bypass attempts
  - Cryptographic attacks (signature replay, kid swap, JWKS/pin/log tampering)

These tests assert what the protocol DOES catch today. Failures that are
known-broken are marked `xfail` and link to follow-up issues; see
`docs/threat-model.md` for the curated catalogue.

The suite is non-blocking on CI by design — adversarial regressions are
surfaced as warnings rather than failing the main pipeline. See
`.github/workflows/ci.yml`.
"""
