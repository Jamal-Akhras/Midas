# Running Midas behind Tollgate

[Tollgate](https://github.com/Jamal-Akhras/tollgate) is a policy and audit
gateway for LLM traffic: per-tenant budgets reserved before every call,
a field allowlist, PII redaction, and an audit trail hashed into
Merkle trees and signed with Ed25519. Midas can run every OpenAI- and
Anthropic-format model call through it. Nothing in Midas requires it:
the defaults talk to the vendors directly, and the demo path and CI never
touch a network.

## Why

Midas already caps AI spend (`DAILY_SPEND_LIMIT_USD`) and prices every
call in its write-ahead cost log from `routing.yaml`. Both live inside the
Midas process, so a Midas bug can defeat both: the July 2026 ledger race
showed in-process accounting going wrong, and the price table was stale
for months without anyone noticing. Tollgate puts the budget outside the
process (a reservation is taken before the vendor is called, from a price
catalog Midas does not own) and returns what it charged in a header, which
Midas stores next to its own estimate. Two independent ledgers; a
`cost.divergence` audit event when they disagree by more than 25%.

It does nothing for returns. It is a safety and accountability layer.

## Setup

1. **Run Tollgate** (see its README; this assumes the quickstart layout
   and a build from the `main` branch, which is pre-release and may
   change). Give Midas its own tenant with a daily budget equal to the
   Midas cap and cost reporting on:

   ```yaml
   providers:
     openai:    { format: openai,    baseUrl: "https://api.openai.com/v1" }
     anthropic: { format: anthropic, baseUrl: "https://api.anthropic.com" }

   models:
     gpt-4o:            { provider: openai,    upstream: "gpt-4o" }
     gpt-4o-mini:       { provider: openai,    upstream: "gpt-4o-mini" }
     claude-opus-5-5:   { provider: anthropic, upstream: "claude-opus-5-5" }
     claude-sonnet-5-5: { provider: anthropic, upstream: "claude-sonnet-5-5" }
     claude-haiku-5-5:  { provider: anthropic, upstream: "claude-haiku-5-5" }

   tenants:
     midas:
       models: [gpt-4o, gpt-4o-mini, claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5]
       dataClass: internal
       budget: { limit: 10.00, period: daily }   # match DAILY_SPEND_LIMIT_USD
       showCosts: true
   ```

   Model names must match `midas/providers/routing.yaml`, because Midas
   sends the routing name as the `model` field. Prices come from
   Tollgate's catalog unless you set them per model.

2. **Point Midas at the gateway** in `.env`. The vendor keys move into
   Tollgate's own configuration; Midas holds only the tenant key:

   ```env
   OPENAI_BASE_URL=http://127.0.0.1:8080/v1
   OPENAI_API_KEY=tg_...            # the midas tenant key, printed by `tollgate init`
   ANTHROPIC_BASE_URL=http://127.0.0.1:8080/anthropic
   ANTHROPIC_API_KEY=tg_...         # the same tenant key
   ```

   Midas sends OpenAI calls to `{OPENAI_BASE_URL}/chat/completions` and
   Anthropic calls to `{ANTHROPIC_BASE_URL}/v1/messages`. Every field it
   sends (`model`, `max_tokens`, `messages`, `tools`, `response_format`,
   `system`) is on Tollgate's allowlist; Midas does not stream.

3. **Gemini** stays direct. Tollgate speaks the OpenAI and Anthropic wire
   formats only. Either leave `GEMINI_API_KEY` empty (the router falls
   through to the other providers, which is what happened in practice in
   July 2026 anyway) or accept that Gemini calls bypass the gateway.

## What Midas records

- `cost_events.gateway_cost_usd`: the `X-Tollgate-Cost` value for the
  call, `NULL` for direct vendor calls. `midas stats` reports
  `gateway_spend_30d` beside `ai_spend_30d`.
- `cost.divergence` audit events when the gateway's charge and the
  `routing.yaml` estimate differ by more than 25%: one of the two price
  tables is wrong, and the trail says which call exposed it.
- Over-budget refusals arrive as HTTP 429 in the vendor's own error
  shape. The router treats them as a `ProviderError`, falls through to
  the next candidate (which, behind the same tenant, is also refused),
  and the stage fails loudly: no silent degradation to another provider.

## Verifying

```bash
midas run ACME            # with real keys and the gateway configured
midas audit --limit 20    # look for cost.divergence; absence means the ledgers agree
tollgate audit verify     # Tollgate's own trail still verifies
```

Compare the gateway's record of the run against `midas audit`: the two
were produced by different processes and should tell the same story.

## Limits

- Tollgate is pre-release (milestone M1 in progress). Its config keys
  above follow its README at the time of writing; check the current
  README before copying them.
- Latency adds milliseconds per call, irrelevant at Midas's cadence.
- Redaction and data-class controls add nothing for market-data prompts;
  the budget, the independent price catalog and the signed trail are the
  point.
