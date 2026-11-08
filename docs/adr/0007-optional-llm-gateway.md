# ADR 0007: An optional LLM gateway in front of the vendors

Date: 2026-11-08 · Status: Accepted

## Context

Midas caps spend and prices every call inside its own process. The July
2026 ledger race showed in-process accounting can fail, and `routing.yaml`
carried stale prices for months without detection. Tollgate, the
companion project, reserves a budget before every call from a price
catalog Midas does not own, keeps a signed audit trail, and returns what
it charged in a response header.

## Decision

Support an OpenAI- or Anthropic-format gateway without requiring one:

- `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` default to the vendors. The
  providers build their endpoints from them; the matching API key then
  holds the gateway's tenant key. Gemini has no gateway path and stays
  direct.
- Providers read the `X-Tollgate-Cost` header into
  `ModelResponse.gateway_cost_usd`; the router stores it on `cost_events`
  beside its own estimate. When the two differ by more than 25% the
  router writes a `cost.divergence` audit event: one price table is
  wrong, and the trail says which call exposed it.
- The demo path, the tests and CI never need a gateway or a network.

`docs/tollgate.md` describes the tenant configuration.

## Consequences

Two independent cost ledgers, the same pattern Midas uses for fills
(internal simulator against Alpaca). An external, reservation-before-call
budget that no Midas bug can bypass. One more service to run, and a
dependency on a pre-release project, both of which the operator opts into.

## Alternatives considered

- Hard dependency on Tollgate: would break the zero-key demo path and tie
  CI to a JVM build.
- Gateway cost as the only ledger: loses the cross-check, which is the
  reason to have the header at all.
