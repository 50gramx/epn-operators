# epn-operators

Catalog of EP&N Deployment-Operator bundles. Operators are pure YAML — no code lives here. The `epn-daemon` loads these at install time.

## Scope

Each operator directory (`operators/<name>/v<N>/`) contains exactly four files:
1. `operator.yaml` — metadata, resource requirements, persona ref, bridge capabilities
2. `deployment.yaml` — k8s Deployment (+ PVC, ConfigMap if needed)
3. `service.yaml` — k8s Service
4. `persona.yaml` — AI agent persona spec for this deployment

## Adding an operator

1. Create `operators/<name>/v1/` with all four files.
2. Use `operators/filesystem/v1/` as the canonical template.
3. `operator.yaml` id must be globally unique: `epn.<name>.v<N>`.
4. Resource requests must be realistic — the scheduler uses them directly.
5. Open a PR. The daemon loads operators by scanning this catalog at startup.

## Versioning

- Non-breaking changes (new env vars, tweaked limits): bump `version` in `operator.yaml`.
- Breaking changes (renamed ports, changed PVC structure): create `v2/` directory.
- Old versions stay forever — nodes may still be running them.

## Operator catalog (current)

| id | status | description |
|----|--------|-------------|
| `epn.filesystem.v1` | WU-A.4 | Local persistent volume |
| `epn.ollama.v1` | WU-C.1 | Local LLM inference (Ollama) |
| `epn.remote-inference.v1` | WU-E.5 | GPU-required inference, federates to peer |
| `epn.sigma.v1` | (persona) | Filesystem agent persona |
| `epn.rho.v1` | WU-E.6 | GPU renter agent persona |
| `epn.pi.v1` | WU-F.8 | Payment treasury agent persona |

## Design docs

- `eapp-live/docs/EPN_AGENT_DEPIN_DESIGN.md` §6
- `eapp-live/docs/EPN_AGENT_IMPLEMENTATION_PLAN.md` WU-A.3, WU-A.4
