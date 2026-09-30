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
- Any other field (run config, tags, location, job...) is refused (400).

The launched run gets the config's code location, repository, job, asset
selection, run config (with `"${param}"` placeholders filled from `params`)
and tags, plus launcher tags: `launcher/owner`, `launcher/target`,
`launcher/candidate-sha`, `launcher/release`, `launcher/digest-<name>`.

One run owned by the launcher is active at a time:

| Situation | Response |
| --- | --- |
| nothing active | `201` `{run_id, status, reused: false}` |
| same target and candidate active | `200` that run, `reused: true` (retries are safe) |
| other candidate active | `409` `busy` |
| other candidate active, `supersede: true` | the active run is asked to terminate; `409` `superseding` until Dagster reports it finished |
| no active run, but pods matching `writer_checks` still live | `409` `writers still running` until they stop, then a normal launch |

### `GET /runs/{run_id}`

Status of a run this launcher started: `status`, `terminal`, `success`,
`target`, `candidate`, start and end times. A run it did not start returns
`404`, the same as a run that does not exist.

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

`owner` scopes everything: runs are found and authorized by the
`launcher/owner` tag, so two launchers with different owners never see each
other's runs. Parameter types are `string`, `integer` and `boolean`.

## What it does not do

- It holds no Dagster credential: it relies on reaching the webserver on the
  cluster network, so restrict that path (network policy) to the launcher.
- It does not constrain what the run itself can do. Pin that where the run is
  created (service account, secrets, admission policy); the launcher only
  guarantees which job, config and tags it asked for.
- Dagster run tags are visible to anyone who can read runs in Dagster.

`selftest.py` exercises each promise above against a fake Dagster API.
