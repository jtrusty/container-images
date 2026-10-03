# dagster-launcher

A narrow HTTP front for launching [Dagster](https://dagster.io) runs, for
callers that must be able to start *some* runs but must not hold a credential
that can start *any* run.

The launcher talks to the Dagster webserver's GraphQL API from inside the
cluster. Callers never see that API: they present a launcher key and can only
do what the operator's config allows.

## API

All endpoints except `/healthz` need `Authorization: Bearer <key>`.

### `POST /launch`

```json
{
  "target": "slice",
  "candidate": {
    "sha": "<40-hex commit>",
    "digests": {"app": "sha256:<64-hex>"},
    "release": "optional-release-id"
  },
  "params": {"scope": "control"},
  "supersede": false
}
```

- `target` must be a key of `targets` in the config (403 otherwise).
- `candidate.digests` must name exactly the config's `required_digests`.
- `params` are checked against the target's schema: unknown names, wrong
  types, values outside an `enum`, `pattern` or `min`/`max` are refused (400).
  An `array` param checks every item against `items`, its length against
  `min_items`/`max_items`, and refuses duplicate items. An omitted param
  takes its `default` if one is declared.
- Any other field (run config, tags, location, job...) is refused (400).

The launched run gets the config's code location, repository, job, asset
selection, run config and tags, plus launcher tags: `launcher/owner`,
`launcher/target`, `launcher/candidate-sha`, `launcher/release`,
`launcher/digest-<name>` and `launcher/params` (a digest of the params after
defaults).

A run config string that is exactly `"${...}"` is replaced with a typed value:

- `"${name}"`: the param `name` (a list stays a list).
- `"${candidate.sha}"`, `"${candidate.release}"`,
  `"${candidate.digests.<name>}"`: taken from the request's candidate after
  it is validated. With `image_checks`, a digest has also been checked
  against what the code location runs. Use these instead of a param when
  the job needs the candidate's identity, so a caller cannot pass one value
  to the image check and another to the job.

The same references can also sit inside a longer string, in run config or
in a run tag's value: `"${cpu}m"`, or a `dagster-k8s/config` JSON tag such as
`{"container_config": {"resources": {"limits": {"memory": ${mem}}}}}`. There
they are replaced with their text. Only `integer` and `boolean` params and
the candidate fields may appear inside a longer string, because their text
can't break out of the surrounding string or JSON. A free-form `string` param
never can. Bounded integer params are the way to let a caller size a run (for
example its pod's memory and CPU) without letting it touch anything else.

A run tag whose whole value is one placeholder (`"${classification}"`) gets
that value's text, so any scalar param, string included, can label a run.

A placeholder naming an undeclared param, a digest outside
`required_digests`, or a string param embedded in a longer string stops the
launcher at startup, as does an invalid `default`.

One run owned by the launcher is active at a time:

| Situation | Response |
| --- | --- |
| nothing active | `201` `{run_id, status, reused: false}` |
| same target, candidate and params active | `200` that run, `reused: true` (retries are safe) |
| other candidate or params active | `409` `busy` |
| other candidate active, `supersede: true` | the active run is asked to terminate; `409` `superseding` until Dagster reports it finished |
| no active run, but pods matching `writer_checks` still live | `409` `writers still running` until they stop, then a normal launch |

### `GET /runs/{run_id}`

Status of a run this launcher started: `status`, `terminal`, `success`,
`target`, `candidate`, `started_at`/`completed_at` (UTC ISO-8601), and
`step_timings`: for each step its status, start and end, and every attempt
(retries included). A time Dagster has not recorded is `null`, never guessed.
A run it did not start returns `404`, the same as a run that does not exist.

### `GET /runs/{run_id}/usage`

Memory and CPU used by a run this launcher started, when `usage` is
configured. Pods are found by the names Dagster's Kubernetes run launcher and
step executor give them: the run worker (`dagster-run-<run id>`, including
resumed workers) and one Job per step attempt
(`dagster-step-<md5(run id + step key)>`, then `-1`, `-2`… for retries). So
attempts whose pods are already deleted are still found in the metrics, and
another run's pods never are. Each attempt reports, per container:

- `samples`, `first_sample`, `last_sample`;
- `memory_working_set_max_observed_bytes`: the largest sampled working set;
- `memory_high_water_bytes`: the kernel's cgroup high-water mark
  (`memory.peak`, cAdvisor's `container_memory_max_usage_bytes`) as of the
  last sample, a lower bound if usage grew after it;
- `cpu_seconds_observed` and `cpu_cores_max_observed`.

Each attempt also reports `measured`, `observed_seconds` and `coverage`
(observed sample span over the attempt's duration). The response states
`sampling_interval_seconds` and its limits: values are maximums *observed* at
that interval, so a shorter spike, or a whole pod shorter than the interval,
can be missed. An attempt with no samples has `measured: false` and must be
treated as unmeasured, not as zero. The launcher only runs its own fixed
queries; there is no way to send it a query.

### `GET /active`

This launcher's runs that have not finished.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `DAGSTER_GRAPHQL_URL` | required | e.g. `http://dagster-webserver/graphql` |
| `LAUNCHER_CONFIG` | `/config/launcher.json` | the targets file below |
| `LAUNCHER_KEYS_FILE` / `LAUNCHER_KEYS` | required | accepted keys, one per line or comma-separated; list two while rotating |
| `PORT` | `8080` | |
| `KUBE_API_URL` | in-cluster | override the Kubernetes API for `image_checks` (tests) |

```json
{
  "owner": "release",
  "location": "my-code-location",
  "repository": "__repository__",
  "required_digests": ["app"],
  "writer_checks": [
    {"namespace": "apps", "service_account": "runner-restricted"}
  ],
  "image_checks": [
    {"namespace": "apps", "deployment": "my-code-location", "digest": "app", "env": ["DAGSTER_CURRENT_IMAGE"]}
  ],
  "targets": {
    "slice": {
      "job": "slice_job",
      "asset_selection": ["group/asset_a"],
      "run_config": {"ops": {"build": {"config": {"scope": "${scope}"}}}},
      "params": {"scope": {"type": "string", "enum": ["control"], "required": true}},
      "tags": {"dagster-k8s/config": "{\"pod_spec_config\": {\"service_account_name\": \"runner-restricted\"}}"}
    }
  }
}
```

`writer_checks` (optional): a run's status can be terminal while pods it
started (step Jobs under a k8s executor) are still writing. Before any
launch, the launcher lists the pods in `namespace` matching `label_selector`
and/or running as `service_account`, and refuses (`409 writers still
running`) while any is `Pending` or `Running`. Prefer `service_account` when
admission pins it: labels are chosen by whoever creates the pod. Needs
`list` on pods in that namespace.

`image_checks` (optional) makes the candidate's digests more than a claim:
before launching, the launcher reads each Deployment and refuses (`409`)
unless every container image (or only those named in `containers`) and every
env var named in `env` ends in `@<that digest>`, and the rollout has finished.
This needs `get` on those Deployments for the launcher's service account.

`usage` (optional) enables `GET /runs/{id}/usage`:
`{"metrics_url": "http://vmsingle:8428", "namespace": "apps", "scrape_interval_seconds": 30}`.
`metrics_url` is a Prometheus-compatible query API that understands MetricsQL
(VictoriaMetrics: the queries use `tfirst_over_time`/`tlast_over_time`) and
holds cAdvisor container metrics with `namespace`, `pod` and `container`
labels. `scrape_interval_seconds` is reported back as the sampling interval,
so set it to the real kubelet/cAdvisor scrape interval.

`owner` scopes everything: runs are found and authorized by the
`launcher/owner` tag, so two launchers with different owners never see each
other's runs. Parameter types are `string`, `integer`, `boolean` and `array`
(of one of the other three).

## What it does not do

- It holds no Dagster credential: it relies on reaching the webserver on the
  cluster network, so restrict that path (network policy) to the launcher.
- It does not constrain what the run itself can do. Pin that where the run is
  created (service account, secrets, admission policy); the launcher only
  guarantees which job, config and tags it asked for.
- Dagster run tags are visible to anyone who can read runs in Dagster.

`selftest.py` exercises each promise above against a fake Dagster API.
