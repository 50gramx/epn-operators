# epn.diag.v1 — eyes on the cluster, held by the network

A diagnostic specialist. It runs in the same sandbox as every other specialist
and asks the Kubernetes API the questions that used to need a person at a
keyboard: is the device plugin scheduled, what did the scheduler say, which
engine pod is not ready and why, what does the quota hold.

**Adding a check is adding a handler in `main.py` and a capability in
`spec.json`. Nothing in the daemon changes.** That is why it lives here and not
in the binary.

## What it can see, and what it cannot

`reads_cluster: true` mounts the perimeter's one read-only identity,
`epn-diag-reader` — get/list/watch on pods, events, nodes, services, quotas,
deployments, daemonsets. No secrets, no exec, no writes. The daemon creates
that identity; a spec cannot name any other, and the install policy refuses one
that tries.

It **cannot** see the host: WSL, `/dev/dxg`, the NVIDIA driver, launchd, Lima,
host disk. Those are compiled, signed, read-only probes shipped by daemon
release — because network-held data that runs on the host is precisely the
supply-chain attack the sandbox exists to stop. Cluster-side questions iterate
here; host-side questions iterate in the daemon. That line is the reason this
is safe to hand to a billion grams.

## Install

Pack the two files into the daemon's install shape and hand it in on loopback:

```sh
python3 pack.py > /tmp/epn.diag.v1.json
curl -sS -X POST \
  "http://127.0.0.1:53581/v1/operators/install?request=install+the+cluster+diagnostic+reader" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/epn.diag.v1.json
```

`request` is required: it is what you, the owner, are consenting to, in your
words. The file goes through the **same** install chain Evo's own authoring
uses — perimeter ensured, sandbox and image policy, registration, spin-up.
There is one install function and this is a second caller of it.

The runner is `python@sha256:…` pinned by digest, 50m/64Mi requested, 500m/256Mi
limited, asleep at `replicas: 0` until asked, with a readiness probe generated
from `diag.health`.

## Ask it

Through Evo (it is a specialist and appears in the consult roster), or over the
fabric from another gram on `/epn/tunnel/1.1.0` by operator id, or directly from
inside the cluster:

```
GET /diag/gpu
GET /diag/engines
GET /diag/events?namespace=epn-system
GET /diag/quota
GET /diag/health
```

Every answer is JSON. Errors are data (`{"ok": false, "error": …}`), never a
crash: a diagnosis that dies on the first unreadable object diagnoses nothing.

## Iterate

Change `main.py`, bump nothing, re-run install. The operator id is the same;
the spec replaces the installed one; the ConfigMap carries the new program.

When a question turns out to need the host, it does not go here. It goes into
the daemon as a probe, and the answer travels as telemetry.
