# Reference: Gherkin projection of the stapel-auth flows

The committed reference output of `stapel_core.flows.gherkin`
(flow-system.md §3 — the flow is the source, the `.feature` is a
projection): the three stapel-auth flows rendered as bilingual, executable
Gherkin bundles.

```
source/                      # committed snapshot (input)
  flows.json                 #   stapel-auth docs/flows/flows.json
  translations/flows.*.json  #   stapel-auth per-app catalogs (en, ru)
en/ ru/                      # generated bundles (output)
  <flow_id>.feature          #   localized Gherkin, happy path
  steps/flows.steps.ts       #   playwright-bdd step library
  steps/fixtures.ts          #   the `stapel` world (codegen typed client)
```

- Runner: **playwright-bdd**. HTTP steps drive the codegen typed client
  (`@stapel/core` `createStapelClient`); human/UI steps are honest
  `TODO(testid)` stubs until a testid plan is attached to the flow
  (system-design §7.20); comm-effect steps are pending side-effect
  assertions.
- Drift gate: `tests/test_flow_feature_reference.py` regenerates `en/`/`ru/`
  from `source/` and asserts byte-for-byte equality. Regenerate with
  `STAPEL_REGEN_FLOW_FEATURES=1 pytest tests/test_flow_feature_reference.py`
  and commit.
- The `source/` snapshot is lifted from stapel-auth; refresh it manually when
  the auth flows change. In a real module the bundles live next to the flows
  (like stapel-auth's `docs/flows/` SA-doc trees) and regenerate from the live
  registry via `manage.py generate_flow_features`.
