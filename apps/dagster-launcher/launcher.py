"""A constrained launcher for Dagster runs.

Callers never talk to Dagster. They can only:

  POST /launch        start one of a fixed set of targets, in a fixed code
                      location, with parameters validated against a schema
  GET  /runs/{id}     read the status of a run this launcher started
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
import json
import os
import re
import ssl
import sys
import threading
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
    ... on Run { runId status startTime endTime tags { key value } }
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
            "candidate": {
                "sha": tags.get(p + "candidate-sha"),
                "release": tags.get(p + "release"),
                "digests": {k[len(p + "digest-"):]: v for k, v in tags.items() if k.startswith(p + "digest-")},
            },
        }
        for k in ("startTime", "endTime"):
            if k in run:
                out[k] = run[k]
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
                continue
            v = params[name]
            t = spec["type"]
            ok = (t == "string" and isinstance(v, str)) or (t == "integer" and isinstance(v, int) and not isinstance(v, bool)) \
                or (t == "boolean" and isinstance(v, bool))
            if not ok:
                raise HTTPError(400, {"error": "bad param type", "param": name, "expected": t})
            if "enum" in spec and v not in spec["enum"]:
                raise HTTPError(400, {"error": "param not in enum", "param": name})
            if t == "string" and "pattern" in spec and not re.fullmatch(spec["pattern"], v):
                raise HTTPError(400, {"error": "param does not match pattern", "param": name})
            if t == "integer" and not (spec.get("min", v) <= v <= spec.get("max", v)):
                raise HTTPError(400, {"error": "param out of range", "param": name})
        supersede = req.get("supersede", False)
        if not isinstance(supersede, bool):
            raise HTTPError(400, {"error": "supersede must be boolean"})
        return target_name, target, cand["sha"], release, digests, params, supersede

    def render(self, template, params):
        """Substitute whole-string "${name}" placeholders with typed values."""
        if isinstance(template, dict):
            return {k: self.render(v, params) for k, v in template.items()}
        if isinstance(template, list):
            return [self.render(v, params) for v in template]
        if isinstance(template, str):
            m = re.fullmatch(r"\$\{([a-z_][a-z0-9_]*)\}", template)
            if m:
                return params.get(m.group(1), None)
        return template

    # --- operations --------------------------------------------------------
    def launch(self, req):
        target_name, target, sha, release, digests, params, supersede = self.validate_launch(req)
        with self.lock:
            self.check_images(digests)
            active = self.active_runs()
            if active:
                same = all(r["candidate"]["sha"] == sha and r["candidate"]["release"] == release
                           and r["candidate"]["digests"] == digests and r["target"] == target_name for r in active)
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
            tags = [self.owner_tag(), {"key": p + "target", "value": target_name}, {"key": p + "candidate-sha", "value": sha}]
            if release:
                tags.append({"key": p + "release", "value": release})
            tags += [{"key": p + "digest-" + k, "value": v} for k, v in sorted(digests.items())]
            tags += [{"key": k, "value": v} for k, v in sorted(target.get("tags", {}).items())]
            selector = {
                "repositoryLocationName": self.cfg["location"],
                "repositoryName": self.cfg.get("repository", "__repository__"),
                "jobName": target["job"],
            }
            if target.get("asset_selection"):
                selector["assetSelection"] = [{"path": k.split("/")} for k in target["asset_selection"]]
            execution = {
                "selector": selector,
                "runConfigData": self.render(target.get("run_config", {}), params),
                "executionMetadata": {"tags": tags},
            }
            d = self.gql(LAUNCH, {"p": execution})["launchRun"]
            if d["__typename"] != "LaunchRunSuccess":
                return 502, {"error": "launch refused by dagster", "type": d["__typename"], "detail": d.get("message")}
            return 201, {"reused": False, "run_id": d["run"]["runId"], "status": d["run"]["status"]}

    def run(self, run_id):
        if not RUN_ID_RE.match(run_id):
            raise HTTPError(404, {"error": "not found"})
        d = self.gql(RUN, {"id": run_id})["runOrError"]
        if d["__typename"] != "Run":
            raise HTTPError(404, {"error": "not found"})
        tags = {t["key"]: t["value"] for t in d["tags"]}
        # Runs this launcher didn't start look exactly like runs that don't exist.
        if tags.get(f"{self.prefix}/owner") != self.owner:
            raise HTTPError(404, {"error": "not found"})
        return 200, self.summary(d)

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
