#!/usr/bin/env python3
"""epn.diag.v1 -- eyes on the cluster, held by the network.

Every diagnosis this fleet needed was gathered by hand: SSH to bootstrap,
kubectl through a distro, a person reading pod events at a keyboard. That does
not scale to a billion grams, and a shell on somebody else's machine is the
largest blast radius there is.

This is the same questions, asked by a workload. It runs in the sandbox every
specialist runs in, with one thing added: the perimeter's read-only diagnostic
identity (epn-diag-reader), which can get/list/watch the cluster objects that
explain why something is not running -- and nothing else. No secrets, no exec,
no writes, no host.

Adding a check is adding a handler here and a capability in spec.json. Nothing
in the daemon changes. That is the whole point of holding it as data.

Standard library only: the runner image installs nothing.
"""
import json
import os
import ssl
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://%s:%s" % (
    os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
    os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
)

# The vendor device plugins the daemon installs (cluster/gpu.go
# gpuPluginDaemonSets). Named here so diag.gpu can ask about them by name.
GPU_PLUGINS = {
    "nvidia": ("kube-system", "nvidia-device-plugin-daemonset"),
    "amd": ("kube-system", "amdgpu-device-plugin-daemonset"),
}
PLATFORM_NS = "epn-system"


def _token():
    try:
        with open(os.path.join(SA, "token")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _ctx():
    ctx = ssl.create_default_context()
    ca = os.path.join(SA, "ca.crt")
    if os.path.exists(ca):
        ctx.load_verify_locations(ca)
    return ctx


def k8s(path):
    """One GET against the API. Errors come back as data, never as a crash:
    a diagnosis that dies on the first unreadable object diagnoses nothing."""
    tok = _token()
    if not tok:
        return None, "no service account token mounted -- was reads_cluster set on the spec?"
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=8, context=_ctx()) as r:
            return json.load(r), None
    except HTTPError as e:
        return None, "HTTP %d on %s" % (e.code, path)
    except URLError as e:
        return None, "api unreachable: %s" % e.reason
    except Exception as e:  # noqa: BLE001 -- report, never crash
        return None, "%s: %s" % (type(e).__name__, e)


def _pod_summary(p):
    ps = p.get("status", {})
    cs = ps.get("containerStatuses", [])
    conds = ps.get("conditions", [])
    return {
        "name": p["metadata"]["name"],
        "phase": ps.get("phase"),
        "scheduled": [c.get("reason") or c.get("status") for c in conds if c.get("type") == "PodScheduled"],
        "scheduled_message": [c.get("message") for c in conds if c.get("type") == "PodScheduled" and c.get("message")],
        "waiting": [c.get("state", {}).get("waiting", {}).get("reason") for c in cs if c.get("state", {}).get("waiting")],
        "last_terminated": [c.get("lastState", {}).get("terminated", {}).get("reason") for c in cs if c.get("lastState", {}).get("terminated")],
        "restarts": sum(c.get("restartCount", 0) for c in cs),
    }


def _pods(ns, match_labels):
    q = ",".join("%s=%s" % kv for kv in sorted(match_labels.items()))
    pods, err = k8s("/api/v1/namespaces/%s/pods?labelSelector=%s" % (ns, q))
    if err:
        return [], err
    return [_pod_summary(p) for p in pods.get("items", [])], None


# ---------------------------------------------------------------- checks --

def health():
    return {"ok": True, "status": "healthy", "api": API}


def gpu():
    """Why does the cluster advertise no GPU? The same chain the daemon's
    device-plugin warn categorises, read from the objects themselves."""
    out = {"ok": True, "nodes": [], "plugins": {}}
    nodes, err = k8s("/api/v1/nodes")
    if err:
        return {"ok": False, "error": err}
    for n in nodes.get("items", []):
        st = n.get("status", {})
        alloc = st.get("allocatable", {})
        cap = st.get("capacity", {})
        labels = n["metadata"].get("labels", {})
        out["nodes"].append({
            "name": n["metadata"]["name"],
            "allocatable_gpu": {k: v for k, v in alloc.items() if "gpu" in k},
            "capacity_gpu": {k: v for k, v in cap.items() if "gpu" in k},
            "labels_gpu": {k: v for k, v in labels.items() if "gpu" in k or "nvidia" in k},
            "conditions": [c["type"] + "=" + c["status"] for c in st.get("conditions", []) if c["status"] != "False"],
        })
    for vendor, (ns, name) in GPU_PLUGINS.items():
        ds, err = k8s("/apis/apps/v1/namespaces/%s/daemonsets/%s" % (ns, name))
        if err:
            out["plugins"][vendor] = {"present": False, "detail": err}
            continue
        s = ds.get("status", {})
        rec = {
            "present": True,
            "desired": s.get("desiredNumberScheduled", 0),
            "ready": s.get("numberReady", 0),
            "available": s.get("numberAvailable", 0),
        }
        sel = ds.get("spec", {}).get("selector", {}).get("matchLabels", {})
        rec["pods"], perr = _pods(ns, sel)
        if perr:
            rec["pods_error"] = perr
        out["plugins"][vendor] = rec
    return out


def engines():
    """Every engine deployment in the platform namespace: replicas, readiness,
    and for anything not ready, the pod-level reason."""
    deps, err = k8s("/apis/apps/v1/namespaces/%s/deployments" % PLATFORM_NS)
    if err:
        return {"ok": False, "error": err}
    out = {"ok": True, "deployments": []}
    for d in deps.get("items", []):
        s = d.get("status", {})
        containers = d["spec"]["template"]["spec"].get("containers", [])
        rec = {
            "name": d["metadata"]["name"],
            "replicas": d.get("spec", {}).get("replicas", 0),
            "ready": s.get("readyReplicas", 0),
            "available": s.get("availableReplicas", 0),
            "images": [c["image"] for c in containers],
            "gpu_limits": [c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu") for c in containers if c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")],
        }
        if rec["replicas"] and rec["ready"] < rec["replicas"]:
            rec["pods"], perr = _pods(PLATFORM_NS, d["spec"]["selector"]["matchLabels"])
            if perr:
                rec["pods_error"] = perr
        out["deployments"].append(rec)
    return out


def events(ns=None):
    """Warning events, newest first. The scheduler and kubelet write the
    reason for most failures here, and nobody reads it."""
    if ns:
        path = "/api/v1/namespaces/%s/events?fieldSelector=type=Warning" % ns
    else:
        path = "/api/v1/events?fieldSelector=type=Warning"
    ev, err = k8s(path)
    if err:
        return {"ok": False, "error": err}
    items = sorted(ev.get("items", []), key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)[:40]
    return {"ok": True, "events": [{
        "at": e.get("lastTimestamp") or e.get("eventTime"),
        "ns": e.get("metadata", {}).get("namespace"),
        "object": "%s/%s" % (e.get("involvedObject", {}).get("kind"), e.get("involvedObject", {}).get("name")),
        "reason": e.get("reason"),
        "message": (e.get("message") or "")[:300],
        "count": e.get("count", 1),
    } for e in items]}


def quota():
    """Every ResourceQuota the reader can see: hard vs used. A pod that requests
    nothing counts as zero against the cap, which is how a cap stops meaning
    anything -- so pods without requests are listed beside the numbers."""
    rq, err = k8s("/api/v1/resourcequotas")
    if err:
        return {"ok": False, "error": err}
    out = {"ok": True, "quotas": [{
        "ns": q["metadata"]["namespace"],
        "name": q["metadata"]["name"],
        "hard": q.get("status", {}).get("hard", {}),
        "used": q.get("status", {}).get("used", {}),
    } for q in rq.get("items", [])]}
    pods, perr = k8s("/api/v1/pods")
    if not perr:
        out["pods_without_requests"] = [
            "%s/%s" % (p["metadata"]["namespace"], p["metadata"]["name"])
            for p in pods.get("items", [])
            if any(not c.get("resources", {}).get("requests") for c in p["spec"].get("containers", []))
        ]
    return out


ROUTES = {
    "/diag/health": lambda q: health(),
    "/diag/gpu": lambda q: gpu(),
    "/diag/engines": lambda q: engines(),
    "/diag/events": lambda q: events(q.get("namespace")),
    "/diag/quota": lambda q: quota(),
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        fn = ROUTES.get(u.path)
        if fn is None:
            self._send(404, {"ok": False, "error": "no such diagnostic", "routes": sorted(ROUTES)})
            return
        try:
            self._send(200, fn(q))
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def _send(self, code, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quiet: the daemon meters, this need not chatter
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), H).serve_forever()
