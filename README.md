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
