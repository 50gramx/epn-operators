# epn-operators

Catalog of EP&N Deployment-Operator bundles. Each operator is a directory containing:

- `operator.yaml` — declarative metadata (id, version, resource requirements, persona ref)
- `deployment.yaml` — k8s Deployment manifest
- `service.yaml` — k8s Service manifest
- `persona.yaml` — AI agent persona spec (see `epn-personas` for schema)

## Operator directory layout

```
operators/
  <name>/
    v<N>/
      operator.yaml
      deployment.yaml
      service.yaml
      persona.yaml
```

## Adding a new operator

1. Create the versioned directory under `operators/`.
2. Fill all four YAML files (see `operators/filesystem/v1/` as canonical example).
3. Bump `version` in `operator.yaml` for any change; create `v2/` for breaking changes.
4. Open a PR — the EP&N daemon loads operators from this catalog at install time.

## Reference

- Design: `eapp-live/docs/EPN_AGENT_DEPIN_DESIGN.md` §6
- Implementation plan: `eapp-live/docs/EPN_AGENT_IMPLEMENTATION_PLAN.md` WU-A.3, WU-A.4

## Engine and policy bundles (D67–D71, 2026-09-21)

`operators/engine-*`, `operators/policy-enforce` and `operators/priority` are
the templated bundles the daemon renders (Go `text/template`, `missingkey=error`)
for its inference engines, the PriorityClass ladder and the Kyverno admission
policies. The daemon embeds a copy under
`epn-daemon/internal/operators/store/bundles/` so a gram needs no network to
render them; a gram overrides any bundle by placing it under
`EPN_HOME/operators/<id>/v<N>/`. This catalog is the published source: the
embedded copy is synced from here (`operators/<id>/v1` ⇄
`store/bundles/<id>/v1`, byte-identical) and the daemon's golden tests hold
the rendered output.

| id | what |
|----|------|
| `priority` | `epn-live` > `epn-interactive` > `epn-background` PriorityClasses |
| `engine-speech` | speech pack (ASR/TTS) pods, `epn-live`, memory request = limit |
| `engine-voice-llm` | voice talker Deployment (ollama), `epn-live` |
| `engine-voice-llm-kserve` | the same talker as a KServe InferenceService (scale-to-zero, preload) |
| `engine-ollama` | catalog engine, `epn-interactive` |
| `engine-generation` | generation jobs, `epn-background` |
| `policy-enforce` | Kyverno ClusterPolicies + ResourceQuota for governed namespaces |
