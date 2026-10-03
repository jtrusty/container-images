"""End-to-end checks of the launcher against a fake Dagster GraphQL API.

Runs inside the image (`python /app/selftest.py`) with no network access
beyond loopback. Each case is one of the launcher's promises:

  launch + status        an allowed target starts a run; its status reads back
  retry is idempotent    the same candidate returns the same run
  target refusal         a target outside the config is refused
  override refusal       unknown params, run config or tags are refused
  foreign run refusal    a run this launcher did not start reads as not found
  serialization          a different candidate is refused while a run is active
  supersede              supersede terminates, and nothing starts until terminal
  writer check           nothing launches while an earlier run's pods
                         (run or step) are still Pending or Running, even
                         if Dagster already reports the run finished
  image check            nothing launches unless the code location runs the
                         candidate's digest, fully rolled out
  auth                   no key or a wrong key is refused
  list params            array params check each item, count and duplicates;
                         an omitted param takes its declared default
  candidate values       run config can take the verified candidate's digest,
                         so the caller cannot pass a different one
  params identity        a retry with different params is not the same run
  config refusal         a target whose params or placeholders can't work
                         stops the launcher at startup
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher  # noqa: E402

class FakeDagster:
    def __init__(self):
        self.runs = {}
        self.launches = []
        self.deployment = None
        self.pods = []

    def deploy(self, digest, rolled_out=True):
        image = "registry.example/app@" + digest
        self.deployment = {
            "metadata": {"generation": 2},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": image, "env": [{"name": "CURRENT_IMAGE", "value": image}]}]}}},
            "status": {"observedGeneration": 2, "replicas": 1 if rolled_out else 2,
                       "updatedReplicas": 1, "availableReplicas": 1 if rolled_out else 2},
        }

    def handle(self, query, v):
        if "launchRun" in query:
            p = v["p"]
            self.launches.append(p)
            rid = str(uuid.uuid4())
            self.runs[rid] = {"runId": rid, "status": "QUEUED", "tags": p["executionMetadata"]["tags"]}
            return {"launchRun": {"__typename": "LaunchRunSuccess", "run": {"runId": rid, "status": "QUEUED"}}}
        if "runsOrError" in query:
            want = {(t["key"], t["value"]) for t in v["tags"]}
            res = [r for r in self.runs.values()
                   if want <= {(t["key"], t["value"]) for t in r["tags"]} and r["status"] in v["statuses"]]
            return {"runsOrError": {"__typename": "Runs", "results": res}}
        if "runOrError" in query:
            r = self.runs.get(v["id"])
            return {"runOrError": {"__typename": "Run", **r} if r else {"__typename": "RunNotFoundError", "message": "nope"}}
        if "terminateRun" in query:
            self.runs[v["id"]]["status"] = "CANCELING"
            return {"terminateRun": {"__typename": "TerminateRunSuccess"}}
        raise AssertionError(query)


def serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main():
    fake = FakeDagster()

    class G(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps({"data": fake.handle(body["query"], body["variables"])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/v1/namespaces/ns/pods"):
                if "?" in self.path:
                    assert "labelSelector=role%3Dwriter" in self.path, self.path
                    items = [p for p in fake.pods if p["metadata"].get("labels", {}).get("role") == "writer"]
                else:
                    items = fake.pods
                ok, body = True, {"items": items}
            else:
                ok = self.path == "/apis/apps/v1/namespaces/ns/deployments/code" and fake.deployment
                body = fake.deployment if ok else {"kind": "Status", "code": 404}
            data = json.dumps(body).encode()
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    _, gql = serve(G)
    os.environ["KUBE_API_URL"] = gql
    config = {
        "owner": "selftest",
        "location": "example-location",
        "required_digests": ["app"],
        "writer_checks": [{"namespace": "ns", "label_selector": "role=writer"},
                          {"namespace": "ns", "service_account": "runner-release"}],
        "image_checks": [{"namespace": "ns", "deployment": "code", "digest": "app", "env": ["CURRENT_IMAGE"]}],
        "targets": {
            "slice": {
                "job": "slice_job",
                "run_config": {"ops": {"build": {"config": {"scope": "${scope}", "limit": "${limit}"}}}},
                "params": {
                    "scope": {"type": "string", "enum": ["control"], "required": True},
                    "limit": {"type": "integer", "min": 1, "max": 100},
                },
                "tags": {"dagster-k8s/config": '{"pod_spec_config": {"service_account_name": "runner-release"}}'},
            },
            "trial": {
                "job": "trial_job",
                "run_config": {"image": "${candidate.digests.app}", "sha": "${candidate.sha}",
                               "outputs": "${outputs}", "faults": "${faults}"},
                "params": {
                    "outputs": {"type": "array", "required": True, "min_items": 1, "max_items": 2,
                                "items": {"type": "string", "pattern": "[a-z]+/[a-z_]+"}},
                    "faults": {"type": "array", "max_items": 1, "default": [],
                               "items": {"type": "string", "enum": ["late", "lost"]}},
                },
            },
        },
    }
    l = launcher.Launcher(config, ["k1", "k2"], gql + "/graphql")
    _, base = serve(launcher.make_handler(l))

    def call(method, path, body=None, key="k1"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    failures = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures.append(name)

    sha1, sha2 = "a" * 40, "b" * 40
    dig = {"app": "sha256:" + "c" * 64}
    good = {"target": "slice", "candidate": {"sha": sha1, "digests": dig, "release": "r-42"}, "params": {"scope": "control", "limit": 5}}

    s, b = call("GET", "/healthz", key=None)
    check("healthz needs no key", s == 200)
    s, _ = call("POST", "/launch", good, key=None)
    check("auth: missing key refused", s == 401)
    s, _ = call("POST", "/launch", good, key="nope")
    check("auth: wrong key refused", s == 401)
    s, _ = call("GET", "/active", key="k2")
    check("auth: second key accepted (rotation)", s == 200)

    s, b = call("POST", "/launch", {**good, "target": "production_job"})
    check("target refusal", s == 403 and not fake.launches, (s, b))
    for name, bad in [
        ("override refusal: run_config", {**good, "run_config": {}}),
        ("override refusal: tags", {**good, "tags": {"dagster-k8s/config": "{}"}}),
        ("override refusal: location", {**good, "location": "other"}),
        ("override refusal: unknown param", {**good, "params": {"scope": "control", "bucket": "x"}}),
        ("override refusal: enum", {**good, "params": {"scope": "everything"}}),
        ("override refusal: type", {**good, "params": {"scope": "control", "limit": "5"}}),
        ("override refusal: range", {**good, "params": {"scope": "control", "limit": 10_000}}),
        ("candidate: bad release", {**good, "candidate": {"sha": sha1, "digests": dig, "release": "a b"}}),
        ("candidate: bad sha", {**good, "candidate": {"sha": "main", "digests": dig}}),
        ("candidate: missing digest", {**good, "candidate": {"sha": sha1, "digests": {}}}),
        ("candidate: extra digest", {**good, "candidate": {"sha": sha1, "digests": {**dig, "x": dig["app"]}}}),
    ]:
        s, b = call("POST", "/launch", bad)
        check(name, s == 400 and not fake.launches, (s, b))

    s, b = call("POST", "/launch", good)
    check("image check: missing deployment refused", s == 409 and b["error"] == "not found in kubernetes"
          and not fake.launches, (s, b))
    fake.deploy("sha256:" + "e" * 64)
    s, b = call("POST", "/launch", good)
    check("image check: other digest refused", s == 409 and "not the candidate" in b["error"] and not fake.launches, (s, b))
    fake.deploy(dig["app"], rolled_out=False)
    s, b = call("POST", "/launch", good)
    check("image check: rollout in progress refused", s == 409 and "rolling out" in b["error"] and not fake.launches, (s, b))
    fake.deploy(dig["app"])
    fake.deployment["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "registry.example/app:latest"
    s, b = call("POST", "/launch", good)
    check("image check: run image env must match too", s == 409 and not fake.launches, (s, b))
    fake.deploy(dig["app"])

    s, b = call("POST", "/launch", good)
    check("launch", s == 201 and len(fake.launches) == 1, (s, b))
    rid = b.get("run_id")
    p = fake.launches[0]
    check("launch uses fixed location and job",
          p["selector"]["repositoryLocationName"] == "example-location" and p["selector"]["jobName"] == "slice_job")
    check("launch renders typed params",
          p["runConfigData"] == {"ops": {"build": {"config": {"scope": "control", "limit": 5}}}}, p["runConfigData"])
    tags = {t["key"]: t["value"] for t in p["executionMetadata"]["tags"]}
    check("launch stamps owner, candidate and fixed tags",
          tags.get("launcher/owner") == "selftest" and tags.get("launcher/candidate-sha") == sha1
          and tags.get("launcher/digest-app") == dig["app"] and tags.get("launcher/release") == "r-42" and "runner-release" in tags.get("dagster-k8s/config", ""), tags)

    s, b = call("GET", f"/runs/{rid}")
    check("status", s == 200 and b["status"] == "QUEUED" and b["candidate"]["sha"] == sha1 and not b["terminal"], b)

    checks = l.cfg.pop("image_checks")  # reach the reuse logic, not the image check
    s, b = call("POST", "/launch", {**good, "candidate": {**good["candidate"], "digests": {"app": "sha256:" + "d" * 64}}})
    l.cfg["image_checks"] = checks
    check("same sha, different digest is never reused", s == 409 and b["error"] == "busy" and len(fake.launches) == 1, (s, b))
    s, b = call("POST", "/launch", good)
    check("retry is idempotent", s == 200 and b["reused"] and b["run_id"] == rid and len(fake.launches) == 1, (s, b))

    foreign = str(uuid.uuid4())
    fake.runs[foreign] = {"runId": foreign, "status": "STARTED", "tags": [{"key": "launcher/owner", "value": "someone-else"}]}
    s, b = call("GET", f"/runs/{foreign}")
    check("foreign run refusal", s == 404, (s, b))
    s, b = call("GET", f"/runs/{uuid.uuid4()}")
    check("missing run looks the same", s == 404, (s, b))
    s, b = call("GET", "/runs/../active")
    check("path games refused", s in (401, 404), (s, b))
    s, b = call("GET", "/active")
    check("active lists only own runs", [r["run_id"] for r in b["active"]] == [rid], b)

    next_ = {**good, "candidate": {"sha": sha2, "digests": dig}}
    s, b = call("POST", "/launch", next_)
    check("serialization: different candidate refused while active", s == 409 and b["error"] == "busy"
          and len(fake.launches) == 1, (s, b))

    s, b = call("POST", "/launch", {**next_, "supersede": True})
    check("supersede: terminates, still refuses", s == 409 and b["error"] == "superseding"
          and fake.runs[rid]["status"] == "CANCELING" and len(fake.launches) == 1, (s, b))
    s, b = call("POST", "/launch", {**next_, "supersede": True})
    check("supersede: refused until terminal", s == 409 and len(fake.launches) == 1, (s, b))

    fake.runs[rid]["status"] = "CANCELED"
    s, b = call("GET", f"/runs/{rid}")
    check("superseded run reads terminal, not success", b["terminal"] and not b["success"], b)
    fake.pods = [{"metadata": {"name": "step-a", "labels": {"role": "writer"}}, "status": {"phase": "Running"}},
                 {"metadata": {"name": "run-old", "labels": {"role": "writer"}}, "status": {"phase": "Succeeded"}},
                 {"metadata": {"name": "other"}, "spec": {"serviceAccountName": "someone"}, "status": {"phase": "Running"}}]
    s, b = call("POST", "/launch", {**next_, "supersede": True})
    check("writer check: terminal run with a live step pod refused", s == 409 and b["error"] == "writers still running"
          and b["pods"] == ["step-a"] and len(fake.launches) == 1, (s, b))
    fake.pods[0]["status"]["phase"] = "Failed"
    fake.pods.append({"metadata": {"name": "unlabelled"}, "spec": {"serviceAccountName": "runner-release"},
                      "status": {"phase": "Pending"}})
    s, b = call("POST", "/launch", {**next_, "supersede": True})
    check("writer check: unlabelled pod caught by service account", s == 409 and b["pods"] == ["unlabelled"]
          and len(fake.launches) == 1, (s, b))
    fake.pods[-1]["status"]["phase"] = "Succeeded"
    s, b = call("POST", "/launch", {**next_, "supersede": True})
    check("supersede: launches once terminal", s == 201 and len(fake.launches) == 2, (s, b))
    rid2 = b["run_id"]
    fake.runs[rid2]["status"] = "SUCCESS"
    s, b = call("GET", f"/runs/{rid2}")
    check("success reads success", b["terminal"] and b["success"] and b["candidate"]["sha"] == sha2, b)

    trial = {"target": "trial", "candidate": {"sha": sha2, "digests": dig}, "params": {"outputs": ["source/series"]}}
    for name, params in [
        ("list params: not a list", {"outputs": "source/series"}),
        ("list params: empty below min_items", {"outputs": []}),
        ("list params: over max_items", {"outputs": ["a/b", "c/d", "e/f"]}),
        ("list params: duplicate items", {"outputs": ["a/b", "a/b"]}),
        ("list params: item fails pattern", {"outputs": ["../etc"]}),
        ("list params: item outside enum", {"outputs": ["a/b"], "faults": ["everything"]}),
        ("list params: item wrong type", {"outputs": ["a/b"], "faults": [1]}),
        ("candidate values: not a param", {"outputs": ["a/b"], "image": "sha256:" + "e" * 64}),
    ]:
        s, b = call("POST", "/launch", {**trial, "params": params})
        check(name, s == 400 and len(fake.launches) == 2, (s, b))
    s, b = call("POST", "/launch", trial)
    check("list params: launch", s == 201 and len(fake.launches) == 3, (s, b))
    rid3 = b.get("run_id")
    p = fake.launches[-1]
    check("list params: default and candidate values rendered",
          p["runConfigData"] == {"image": dig["app"], "sha": sha2, "outputs": ["source/series"], "faults": []},
          p["runConfigData"])
    s, b = call("POST", "/launch", trial)
    check("params identity: same params reused", s == 200 and b["reused"] and b["run_id"] == rid3, (s, b))
    s, b = call("POST", "/launch", {**trial, "params": {"outputs": ["source/series"], "faults": ["late"]}})
    check("params identity: different params not reused", s == 409 and b["error"] == "busy"
          and len(fake.launches) == 3, (s, b))
    s, b = call("GET", f"/runs/{rid3}")
    check("params identity: status reports target and params digest",
          b["target"] == "trial" and b["params_digest"] == launcher.params_digest(
              {"outputs": ["source/series"], "faults": []}), b)
    check("params identity: runs from before params were tagged read as empty params",
          l.summary({"runId": rid, "status": "SUCCESS", "tags": []})["params_digest"] == launcher.params_digest({}))

    def refused(name, target):
        try:
            launcher.Launcher({**config, "targets": {"t": target}}, ["k"], gql)
        except SystemExit:
            check(name, True)
        else:
            check(name, False, "launcher started")
    refused("config refusal: undeclared placeholder", {"job": "j", "run_config": {"x": "${nope}"}})
    refused("config refusal: unknown candidate digest", {"job": "j", "run_config": {"x": "${candidate.digests.db}"}})
    refused("config refusal: invalid default", {"job": "j", "params": {
        "f": {"type": "array", "default": ["x"], "items": {"type": "string", "enum": ["y"]}}}})
    refused("config refusal: array without item type", {"job": "j", "params": {"f": {"type": "array"}}})
    refused("config refusal: unknown param type", {"job": "j", "params": {"f": {"type": "object"}}})

    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
