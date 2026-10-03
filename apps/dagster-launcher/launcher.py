"""A constrained launcher for Dagster runs.

Callers never talk to Dagster. They can only:

  POST /launch        start one of a fixed set of targets, in a fixed code
                      location, with parameters validated against a schema
  GET  /runs/{id}     read the status and step timings of a run this
                      launcher started
  GET  /runs/{id}/usage
                      per-attempt memory and CPU observed for that run's
                      pods, from cAdvisor metrics (when "usage" is configured)
  GET  /active        list this launcher's runs that are not finished yet
  GET  /healthz       liveness

Everything that decides *what* runs and *where* comes from the operator's
config file, not the request: the code location, repository, job, asset
selection, run config template and run tags. The request supplies a target
name, a candidate identity (commit SHA and image digests, recorded as run
tags) and a few typed parameters.

Only one run owned by this launcher is active at a time:

- a request for the candidate that is already running returns that run
  (retries never start a second run);
- a request for a different candidate is refused with 409 while an earlier
  run is active; with "supersede": true the earlier run is asked to
  terminate, and requests keep being refused until Dagster reports it
  finished. A newer candidate therefore never starts while an older run
  can still write.

Standard library only. Configuration: see README.md.
"""

import hmac
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TERMINAL = {"SUCCESS", "FAILURE", "CANCELED"}
ACTIVE = ["QUEUED", "NOT_STARTED", "MANAGED", "STARTING", "STARTED", "CANCELING"]
MAX_BODY = 64 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
RUN_ID_RE = re.compile(r"^[0-9a-f-]{8,64}$")
RELEASE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
# A run_config string that is exactly "${...}": a param name, or a value taken
# from the verified candidate (candidate.sha, candidate.release,
# candidate.digests.<name>) so the caller cannot choose it separately.
PLACEHOLDER_RE = re.compile(r"\$\{(candidate\.(?:sha|release|digests\.[a-z][a-z0-9-]{0,30})|[a-z_][a-z0-9_]*)\}")
SCALAR_TYPES = {"string": str, "integer": int, "boolean": bool}
# The same references inside a longer string ("${cpu}m", or a JSON tag value)
# are replaced with their text. Only values whose text can't break out of the
# surrounding string are allowed there: integer and boolean params and the
# validated candidate fields. A free-form string param never is.
EMBEDDED_RE = re.compile(r"\$\{(candidate\.(?:sha|release|digests\.[a-z][a-z0-9-]{0,30})|[a-z_][a-z0-9_]*)\}")
EMBEDDABLE_TYPES = ("integer", "boolean")


def iso(ts):
    """Epoch seconds to a UTC ISO-8601 instant, or None."""
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def text(v):
    """A typed value's text for embedding: JSON spelling for booleans."""
    return json.dumps(v) if isinstance(v, bool) else str(v)


def params_digest(params):
    """Stable identity of a launch's params, after defaults are filled in."""
    return "sha256:" + hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Runs started before params were tagged all had empty params.
EMPTY_PARAMS_DIGEST = params_digest({})

LAUNCH = """
mutation Launch($p: ExecutionParams!) {
  launchRun(executionParams: $p) {
    __typename
    ... on LaunchRunSuccess { run { runId status } }
    ... on Error { message }
  }
}"""
ACTIVE_RUNS = """
query Active($tags: [ExecutionTag!], $statuses: [RunStatus!]) {
  runsOrError(filter: {tags: $tags, statuses: $statuses}) {
    __typename
    ... on Runs { results { runId status tags { key value } } }
    ... on Error { message }
  }
}"""
RUN = """
query Run($id: ID!) {
  runOrError(runId: $id) {
    __typename
    ... on Run { runId status startTime endTime tags { key value }
                 stepStats { stepKey status startTime endTime attempts { startTime endTime } } }
    ... on Error { message }
  }
}"""
TERMINATE = """
mutation Terminate($id: String!) {
  terminateRun(runId: $id) {
    __typename
    ... on TerminateRunFailure { message }
    ... on Error { message }
  }
}"""


class HTTPError(Exception):
    def __init__(self, status, body):
        super().__init__(body)
        self.status, self.body = status, body


class Launcher:
    def __init__(self, config, keys, graphql_url):
        self.cfg = config
        self.keys = [k.encode() for k in keys if k]
        self.url = graphql_url
        self.owner = config["owner"]
        self.prefix = config.get("tag_prefix", "launcher")
        self.lock = threading.Lock()
        if not self.keys:
            raise SystemExit("no launcher keys configured")
        for chk in config.get("image_checks", []):
            if chk["digest"] not in config.get("required_digests", []):
                raise SystemExit(f"image_checks digest {chk['digest']!r} is not in required_digests")
        for name, target in config["targets"].items():
            self.check_target_config(name, target)

    def check_target_config(self, name, target):
        """Refuse at startup a target whose params or run_config can't work."""
        schema = target.get("params", {})
        for pname, spec in schema.items():
            if spec.get("type") not in (*SCALAR_TYPES, "array"):
                raise SystemExit(f"target {name!r} param {pname!r}: unknown type {spec.get('type')!r}")
            if spec["type"] == "array" and spec.get("items", {}).get("type") not in SCALAR_TYPES:
                raise SystemExit(f"target {name!r} param {pname!r}: array items need a scalar type")
            if "default" in spec:
                try:
                    self.check_param(pname, spec, spec["default"])
                except HTTPError as e:
                    raise SystemExit(f"target {name!r} param {pname!r}: default is invalid: {e.body}")

        def check_ref(where, ref, embedded):
            if ref.startswith("candidate.digests."):
                if ref.split(".", 2)[2] not in self.cfg.get("required_digests", []):
                    raise SystemExit(f"target {name!r}: {where} names a digest not in required_digests")
            elif ref.startswith("candidate."):
                return
            elif ref not in schema:
                raise SystemExit(f"target {name!r}: {where} names an undeclared param")
            elif embedded and schema[ref]["type"] not in EMBEDDABLE_TYPES:
                raise SystemExit(f"target {name!r}: {where} embeds {ref!r}; only integer and boolean params "
                                 "can appear inside a longer string")

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                if m := PLACEHOLDER_RE.fullmatch(v):
                    check_ref(v, m.group(1), embedded=False)
                else:
                    for m in EMBEDDED_RE.finditer(v):
                        check_ref(v, m.group(1), embedded=True)
        walk(target.get("run_config", {}))
        # Tag values are always strings, so every reference in them is embedded.
        for k, v in target.get("tags", {}).items():
            for m in EMBEDDED_RE.finditer(v):
                check_ref(f"tag {k}", m.group(1), embedded=True)
        if self.cfg.get("usage") and not self.cfg["usage"].get("metrics_url"):
            raise SystemExit("usage needs metrics_url")

    # --- Kubernetes --------------------------------------------------------
    def kube_get(self, path):
        try:
            base = os.environ.get("KUBE_API_URL")
            headers, ctx = {}, None
            if not base:
                sa = "/var/run/secrets/kubernetes.io/serviceaccount"
                base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
                headers["Authorization"] = "Bearer " + open(f"{sa}/token").read().strip()
                ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=15, context=ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # A checked object that doesn't exist fails the check.
                raise HTTPError(409, {"error": "not found in kubernetes", "path": path.split("?")[0]})
            raise HTTPError(502, {"error": "kubernetes refused", "status": e.code})
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError) as e:
            raise HTTPError(502, {"error": "kubernetes unavailable", "detail": str(e)[:200]})

    def check_writers(self):
        """Refuse while any pod matching a writer check is still Pending or
        Running. A run's status can be terminal while pods it started (step
        Jobs under a k8s executor) are still writing; the status alone is not
        proof that the previous writer stopped."""
        for chk in self.cfg.get("writer_checks", []):
            path = f"/api/v1/namespaces/{chk['namespace']}/pods"
            if chk.get("label_selector"):
                path += "?" + urllib.parse.urlencode({"labelSelector": chk["label_selector"]})
            pods = self.kube_get(path)["items"]
            # service_account: matched on the pod spec, which admission can pin,
            # rather than on labels, which whoever creates the pod chooses.
            if chk.get("service_account"):
                pods = [p for p in pods if p.get("spec", {}).get("serviceAccountName") == chk["service_account"]]
            # A pod being deleted is still Running until its processes exit.
            live = sorted(p["metadata"]["name"] for p in pods
                          if p.get("status", {}).get("phase") in ("Pending", "Running"))
            if live:
                raise HTTPError(409, {"error": "writers still running",
                                      "detail": "pods from an earlier run have not stopped; retry",
                                      "pods": live[:20]})

    def check_images(self, digests):
        """Refuse unless each checked Deployment is fully rolled out on the
        candidate's digest. Otherwise the run could use an older image while
        its tags claim the candidate's."""
        for chk in self.cfg.get("image_checks", []):
            want = digests[chk["digest"]]
            name = f"{chk['namespace']}/{chk['deployment']}"
            d = self.kube_get(f"/apis/apps/v1/namespaces/{chk['namespace']}/deployments/{chk['deployment']}")
            pod = d["spec"]["template"]["spec"]
            images = [c["image"] for c in pod["containers"] if c["name"] in chk.get("containers", [c["name"]])]
            images += [e.get("value", "") for c in pod["containers"] for e in c.get("env", [])
                       if e["name"] in chk.get("env", [])]
            if not images or any(not i.endswith("@" + want) for i in images):
                raise HTTPError(409, {"error": "deployed image is not the candidate", "deployment": name,
                                      "digest": chk["digest"]})
            st, want_n = d.get("status", {}), d["spec"].get("replicas", 1)
            if not (st.get("observedGeneration", 0) >= d["metadata"]["generation"]
                    and st.get("replicas", 0) == st.get("updatedReplicas", 0) == st.get("availableReplicas", 0) == want_n):
                raise HTTPError(409, {"error": "deployment is still rolling out", "deployment": name})

    # --- Dagster -----------------------------------------------------------
    def gql(self, query, variables):
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise HTTPError(502, {"error": "dagster unavailable", "detail": str(e)[:200]})
        if body.get("errors"):
            raise HTTPError(502, {"error": "dagster error", "detail": str(body["errors"])[:300]})
        return body["data"]

    def owner_tag(self):
        return {"key": f"{self.prefix}/owner", "value": self.owner}

    def active_runs(self):
        d = self.gql(ACTIVE_RUNS, {"tags": [self.owner_tag()], "statuses": ACTIVE})["runsOrError"]
        if d["__typename"] != "Runs":
            raise HTTPError(502, {"error": "dagster error", "detail": d.get("message", d["__typename"])})
        return [self.summary(r) for r in d["results"]]

    def summary(self, run):
        tags = {t["key"]: t["value"] for t in run.get("tags", [])}
        p = f"{self.prefix}/"
        out = {
            "run_id": run["runId"],
            "status": run["status"],
            "terminal": run["status"] in TERMINAL,
            "success": run["status"] == "SUCCESS",
            "target": tags.get(p + "target"),
            "params_digest": tags.get(p + "params", EMPTY_PARAMS_DIGEST),
            "candidate": {
                "sha": tags.get(p + "candidate-sha"),
                "release": tags.get(p + "release"),
                "digests": {k[len(p + "digest-"):]: v for k, v in tags.items() if k.startswith(p + "digest-")},
            },
        }
        for k in ("startTime", "endTime"):
            if k in run:
                out[k] = run[k]
        out["started_at"], out["completed_at"] = iso(run.get("startTime")), iso(run.get("endTime"))
        return out

    # --- validation --------------------------------------------------------
    def validate_launch(self, req):
        allowed_fields = {"target", "candidate", "params", "supersede"}
        extra = set(req) - allowed_fields
        if extra:
            raise HTTPError(400, {"error": "unknown fields", "fields": sorted(extra)})
        target_name = req.get("target")
        target = self.cfg["targets"].get(target_name) if isinstance(target_name, str) else None
        if target is None:
            raise HTTPError(403, {"error": "target not allowed", "target": target_name})
        cand = req.get("candidate")
        if not isinstance(cand, dict) or set(cand) - {"sha", "digests", "release"}:
            raise HTTPError(400, {"error": "candidate must be {sha, digests, release?}"})
        release = cand.get("release")
        if release is not None and (not isinstance(release, str) or not RELEASE_RE.match(release)):
            raise HTTPError(400, {"error": "candidate.release must match " + RELEASE_RE.pattern})
        if not isinstance(cand.get("sha"), str) or not SHA_RE.match(cand["sha"]):
            raise HTTPError(400, {"error": "candidate.sha must be a 40-hex commit SHA"})
        digests = cand.get("digests") or {}
        required = set(self.cfg.get("required_digests", []))
        if not isinstance(digests, dict) or set(digests) != required:
            raise HTTPError(400, {"error": "candidate.digests must name exactly", "names": sorted(required)})
        for k, v in digests.items():
            if not DIGEST_NAME_RE.match(k) or not isinstance(v, str) or not DIGEST_RE.match(v):
                raise HTTPError(400, {"error": "bad digest", "name": k})
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPError(400, {"error": "params must be an object"})
        schema = target.get("params", {})
        unknown = set(params) - set(schema)
        if unknown:
            raise HTTPError(400, {"error": "params not allowed", "params": sorted(unknown)})
        for name, spec in schema.items():
            if name not in params:
                if spec.get("required"):
                    raise HTTPError(400, {"error": "missing param", "param": name})
                if "default" in spec:
                    params[name] = spec["default"]
                continue
            self.check_param(name, spec, params[name])
        supersede = req.get("supersede", False)
        if not isinstance(supersede, bool):
            raise HTTPError(400, {"error": "supersede must be boolean"})
        return target_name, target, cand["sha"], release, digests, params, supersede

    def check_param(self, name, spec, v):
        """Check one value against its spec; arrays check every item."""
        t = spec["type"]
        if t == "array":
            if not isinstance(v, list):
                raise HTTPError(400, {"error": "bad param type", "param": name, "expected": "array"})
            if not spec.get("min_items", 0) <= len(v) <= spec.get("max_items", len(v)):
                raise HTTPError(400, {"error": "param has wrong number of items", "param": name})
            if len({json.dumps(x, sort_keys=True) for x in v}) != len(v):
                raise HTTPError(400, {"error": "param has duplicate items", "param": name})
            for item in v:
                self.check_param(name, spec["items"], item)
            return
        ok = isinstance(v, SCALAR_TYPES[t]) and not (t == "integer" and isinstance(v, bool))
        if not ok:
            raise HTTPError(400, {"error": "bad param type", "param": name, "expected": t})
        if "enum" in spec and v not in spec["enum"]:
            raise HTTPError(400, {"error": "param not in enum", "param": name})
        if t == "string" and "pattern" in spec and not re.fullmatch(spec["pattern"], v):
            raise HTTPError(400, {"error": "param does not match pattern", "param": name})
        if t == "integer" and not (spec.get("min", v) <= v <= spec.get("max", v)):
            raise HTTPError(400, {"error": "param out of range", "param": name})

    @staticmethod
    def resolve(ref, params, candidate):
        if ref.startswith("candidate.digests."):
            return candidate["digests"].get(ref.split(".", 2)[2])
        if ref.startswith("candidate."):
            return candidate.get(ref.split(".", 1)[1])
        return params.get(ref, None)

    def render(self, template, params, candidate):
        """Substitute "${...}" placeholders: a whole-string placeholder takes the
        typed value, one inside a longer string is replaced with its text."""
        if isinstance(template, dict):
            return {k: self.render(v, params, candidate) for k, v in template.items()}
        if isinstance(template, list):
            return [self.render(v, params, candidate) for v in template]
        if isinstance(template, str):
            m = PLACEHOLDER_RE.fullmatch(template)
            if m:
                return self.resolve(m.group(1), params, candidate)
            return EMBEDDED_RE.sub(lambda m: text(self.resolve(m.group(1), params, candidate)), template)
        return template

    # --- operations --------------------------------------------------------
    def launch(self, req):
        target_name, target, sha, release, digests, params, supersede = self.validate_launch(req)
        pdigest = params_digest(params)
        with self.lock:
            self.check_images(digests)
            active = self.active_runs()
            if active:
                same = all(r["candidate"]["sha"] == sha and r["candidate"]["release"] == release
                           and r["candidate"]["digests"] == digests and r["target"] == target_name
                           and r["params_digest"] == pdigest for r in active)
                if same:
                    return 200, {"reused": True, **active[0]}
                if supersede:
                    for r in active:
                        if r["status"] != "CANCELING":
                            self.gql(TERMINATE, {"id": r["run_id"]})
                    return 409, {"error": "superseding", "detail": "earlier run asked to terminate; retry until it is finished",
                                 "active": active}
                return 409, {"error": "busy", "detail": "an earlier run is still active", "active": active}
            self.check_writers()
            p = f"{self.prefix}/"
            tags = [self.owner_tag(), {"key": p + "target", "value": target_name}, {"key": p + "candidate-sha", "value": sha},
                    {"key": p + "params", "value": pdigest}]
            if release:
                tags.append({"key": p + "release", "value": release})
            tags += [{"key": p + "digest-" + k, "value": v} for k, v in sorted(digests.items())]
            cand = {"sha": sha, "release": release, "digests": digests}
            tags += [{"key": k, "value": self.render(v, params, cand)} for k, v in sorted(target.get("tags", {}).items())]
            selector = {
                "repositoryLocationName": self.cfg["location"],
                "repositoryName": self.cfg.get("repository", "__repository__"),
                "jobName": target["job"],
            }
            if target.get("asset_selection"):
                selector["assetSelection"] = [{"path": k.split("/")} for k in target["asset_selection"]]
            execution = {
                "selector": selector,
                "runConfigData": self.render(target.get("run_config", {}), params, cand),
                "executionMetadata": {"tags": tags},
            }
            d = self.gql(LAUNCH, {"p": execution})["launchRun"]
            if d["__typename"] != "LaunchRunSuccess":
                return 502, {"error": "launch refused by dagster", "type": d["__typename"], "detail": d.get("message")}
            return 201, {"reused": False, "run_id": d["run"]["runId"], "status": d["run"]["status"]}

    def own_run(self, run_id):
        if not RUN_ID_RE.match(run_id):
            raise HTTPError(404, {"error": "not found"})
        d = self.gql(RUN, {"id": run_id})["runOrError"]
        if d["__typename"] != "Run":
            raise HTTPError(404, {"error": "not found"})
        tags = {t["key"]: t["value"] for t in d["tags"]}
        # Runs this launcher didn't start look exactly like runs that don't exist.
        if tags.get(f"{self.prefix}/owner") != self.owner:
            raise HTTPError(404, {"error": "not found"})
        return d

    def run(self, run_id):
        d = self.own_run(run_id)
        out = self.summary(d)
        # Only what Dagster recorded; a missing time stays null, never guessed.
        out["step_timings"] = {
            s["stepKey"]: {
                "status": s.get("status"),
                "started_at": iso(s.get("startTime")),
                "completed_at": iso(s.get("endTime")),
                "attempts": [{"started_at": iso(a.get("startTime")), "completed_at": iso(a.get("endTime"))}
                             for a in s.get("attempts") or []],
            }
            for s in d.get("stepStats") or []
        }
        return 200, out

    # --- usage -------------------------------------------------------------
    def attempt_pods(self, d):
        """Every pod a run's attempts could have used, by the names the Dagster
        Kubernetes run launcher and step executor give them: the run worker
        (and resumed workers), and one Job per step attempt. Names, not labels,
        so pods that are already deleted are still found in the metrics."""
        rid = d["runId"]
        out = [{"kind": "run_worker", "step_key": None, "attempt": None,
                "started_at": d.get("startTime"), "completed_at": d.get("endTime"),
                "pod_re": rf"dagster-run-{rid}(-[0-9]+)?-[a-z0-9]{{5}}"}]
        for s in d.get("stepStats") or []:
            attempts = s.get("attempts") or [{"startTime": s.get("startTime"), "endTime": s.get("endTime")}]
            base = "dagster-step-" + hashlib.md5((rid + s["stepKey"]).encode()).hexdigest()
            for i, a in enumerate(attempts):
                job = base if i == 0 else f"{base}-{i}"
                out.append({"kind": "step", "step_key": s["stepKey"], "attempt": i + 1,
                            "started_at": a.get("startTime"), "completed_at": a.get("endTime"),
                            "pod_re": rf"{job}-[a-z0-9]{{5}}"})
        return out

    def metrics(self, query, at):
        url = self.cfg["usage"]["metrics_url"].rstrip("/") + "/api/v1/query"
        data = urllib.parse.urlencode({"query": query, "time": f"{at:.3f}"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=30) as r:
                body = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise HTTPError(502, {"error": "metrics unavailable", "detail": str(e)[:200]})
        if body.get("status") != "success":
            raise HTTPError(502, {"error": "metrics error", "detail": str(body.get("error"))[:200]})
        return {(r["metric"].get("pod"), r["metric"].get("container")): float(r["value"][1])
                for r in body["data"]["result"]}

    def usage(self, run_id):
        u = self.cfg.get("usage")
        if not u:
            raise HTTPError(404, {"error": "usage is not configured"})
        d = self.own_run(run_id)
        attempts = self.attempt_pods(d)
        step = int(u.get("scrape_interval_seconds", 30))
        now = time.time()
        start = (d.get("startTime") or now - 3600) - 2 * step
        end = min(now, (d.get("endTime") or now) + 2 * step)
        rng = f"{max(int(end - start), step)}s"
        sel = '{namespace="%s",container!="",container!="POD",pod=~"%s"}' % (
            u.get("namespace", "default"), "|".join(a["pod_re"] for a in attempts))
        ws, cpu = "container_memory_working_set_bytes" + sel, "container_cpu_usage_seconds_total" + sel
        q = {
            "samples": f"sum by (pod, container) (count_over_time({ws}[{rng}]))",
            "first_sample": f"min by (pod, container) (tfirst_over_time({ws}[{rng}]))",
            "last_sample": f"max by (pod, container) (tlast_over_time({ws}[{rng}]))",
            "memory_working_set_max_observed_bytes": f"max by (pod, container) (max_over_time({ws}[{rng}]))",
            "memory_high_water_bytes": f"max by (pod, container) (max_over_time(container_memory_max_usage_bytes{sel}[{rng}]))",
            # cumulative per container instance; a restarted container is a new series, so sum them
            "cpu_seconds_observed": f"sum by (pod, container) (max_over_time({cpu}[{rng}]))",
            "cpu_cores_max_observed": f"max by (pod, container) (max_over_time(rate({cpu}[{max(2 * step, 60)}s])[{rng}:{step}s]))",
        }
        res = {k: self.metrics(v, end) for k, v in q.items()}
        pods = {}
        for key in res["samples"]:
            pods.setdefault(key[0], {})[key[1]] = {
                k: (int(res[k][key]) if k in ("samples", "memory_working_set_max_observed_bytes",
                                              "memory_high_water_bytes") else res[k][key])
                if key in res[k] else None for k in q}
        out = []
        for a in attempts:
            rx = re.compile(a["pod_re"])
            matched = {p: c for p, c in pods.items() if rx.fullmatch(p)}
            dur = (a["completed_at"] or end) - a["started_at"] if a["started_at"] else None
            obs = [c for cs in matched.values() for c in cs.values()
                   if c["samples"] and c["first_sample"] is not None and c["last_sample"] is not None]
            observed = (max(c["last_sample"] for c in obs) - min(c["first_sample"] for c in obs) + step
                        if obs else 0)
            for c in (c for cs in matched.values() for c in cs.values()):
                for k in ("first_sample", "last_sample"):
                    c[k] = iso(c[k])
            out.append({
                "kind": a["kind"], "step_key": a["step_key"], "attempt": a["attempt"],
                "started_at": iso(a["started_at"]), "completed_at": iso(a["completed_at"]),
                "measured": bool(obs),
                "observed_seconds": round(observed, 1) if obs else 0,
                "coverage": round(min(1.0, observed / dur), 3) if obs and dur and dur > 0 else None,
                "pods": [{"pod": p, "containers": cs} for p, cs in sorted(matched.items())],
            })
        return 200, {
            "run_id": d["runId"], "status": d["status"], "terminal": d["status"] in TERMINAL,
            "source": "cAdvisor container metrics via a Prometheus-compatible API (MetricsQL)",
            "sampling_interval_seconds": step,
            "window": {"start": iso(start), "end": iso(end)},
            "notes": [
                "Values are the maximum OBSERVED at the sampling interval: spikes shorter than the interval, "
                "or a whole pod shorter than it, can be missed.",
                "memory_high_water_bytes is the kernel's cgroup high-water mark (memory.peak) as of the "
                "container's last sample; usage after that sample is not seen, so it is a lower bound.",
                "An attempt with measured=false has no samples: treat it as unmeasured, never as zero.",
                "coverage is observed sample span / attempt duration; null when the duration is unknown.",
            ],
            "attempts": out,
        }

    def authorized(self, header):
        if not header or not header.startswith("Bearer "):
            return False
        presented = header[len("Bearer "):].encode()
        return any(hmac.compare_digest(presented, k) for k in self.keys)


def make_handler(launcher):
    class Handler(BaseHTTPRequestHandler):
        server_version = "dagster-launcher"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self, fn):
            try:
                if self.path != "/healthz" and not launcher.authorized(self.headers.get("Authorization")):
                    return self.send(401, {"error": "unauthorized"})
                self.send(*fn())
            except HTTPError as e:
                self.send(e.status, e.body)
            except Exception as e:  # never leak a stack trace
                sys.stderr.write(f"internal error: {e!r}\n")
                self.send(500, {"error": "internal error"})

        def do_GET(self):
            if self.path == "/healthz":
                return self.handle_request(lambda: (200, {"ok": True}))
            if self.path == "/active":
                return self.handle_request(lambda: (200, {"active": launcher.active_runs()}))
            if self.path.startswith("/runs/") and self.path.endswith("/usage"):
                return self.handle_request(lambda: launcher.usage(self.path[len("/runs/"):-len("/usage")]))
            if self.path.startswith("/runs/"):
                return self.handle_request(lambda: launcher.run(self.path[len("/runs/"):]))
            self.handle_request(lambda: (404, {"error": "not found"}))

        def do_POST(self):
            def body():
                if self.path != "/launch":
                    return 404, {"error": "not found"}
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_BODY:
                    raise HTTPError(413 if n > MAX_BODY else 400, {"error": "body required, max 64 KiB"})
                if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                    raise HTTPError(415, {"error": "application/json required"})
                try:
                    req = json.loads(self.rfile.read(n))
                except ValueError:
                    raise HTTPError(400, {"error": "invalid JSON"})
                if not isinstance(req, dict):
                    raise HTTPError(400, {"error": "JSON object required"})
                return launcher.launch(req)
            self.handle_request(body)

    return Handler


def load_keys():
    path = os.environ.get("LAUNCHER_KEYS_FILE")
    raw = open(path).read() if path else os.environ.get("LAUNCHER_KEYS", "")
    return [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]


def main():
    config = json.load(open(os.environ.get("LAUNCHER_CONFIG", "/config/launcher.json")))
    launcher = Launcher(config, load_keys(), os.environ["DAGSTER_GRAPHQL_URL"])
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(launcher))
    sys.stderr.write(f"dagster-launcher on :{port}, location={config['location']}, targets={sorted(config['targets'])}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
