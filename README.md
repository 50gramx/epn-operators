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
render them (the cold seed; `TestEmbeddedBundlesAreTheCatalogByteForByte`
pins it to this tree). At runtime the catalog reaches a gram as SIGNED
PACKAGES (D77, 2026-09-22): a foundation gram packs each bundle here into a
content-addressed package, signs an index id → version → CID with its node
key and its foundation membership credential, and beacons it on the DHT;
every gram verifies both offline, fetches from any peer, and renders from
its verified package cache — so a bundle moves without a daemon release and
nothing unsigned on a disk is ever rendered. The three copies (this git
tree, every binary, every gram's cache) mean a network that goes fully dark
loses nothing. Born operators (Evo/foundation-authored) travel in the same
shape: `operator.yaml` beside `spec.json`, with substrate and priority
derived from the body rung (D79).

| id | what |
|----|------|
| `priority` | `epn-live` > `epn-interactive` > `epn-background` PriorityClasses |
| `engine-speech` | speech pack (ASR/TTS) pods, `epn-live`, memory request = limit |
| `engine-voice-llm` | voice talker Deployment (ollama), `epn-live` |
| `engine-voice-llm-kserve` | the same talker as a KServe InferenceService (scale-to-zero, preload) |
| `engine-ollama` | catalog engine, `epn-interactive` |
| `engine-generation` | generation jobs, `epn-background` |
| `engine-vllm` | vLLM OpenAI server, GPU (nvidia RuntimeClass) or CPU, learned memory ceiling |
| `engine-llamacpp` | llama.cpp server for one GGUF, learned memory ceiling |
| `runner` | the pod a born script/built specialist runs in (zero until asked, `epn-background`) |
| `region-scene-bake` | on-demand region scene bake job, program carried as `main.py` |
| `policy-enforce` | Kyverno ClusterPolicies + ResourceQuota for governed namespaces |
