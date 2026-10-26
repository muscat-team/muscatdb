# Running the system

Deploy, schema, engines, and host paths still live in the in-app Guide
(`/guide` on a running instance) and in unpublished notes.

Host inventory and nginx: [notes/DEPLOYMENT.md](https://github.com/muscat-team/muscatdb/blob/test/notes/DEPLOYMENT.md) on GitHub.

This section will grow. It is not a dump of `guide.html`.

## Photometry HTTP API

The running app's `/docs` and `/openapi.json` describe the photometry request
fields and JSON responses. Requests use the same authentication and proposal
access rules as the browser. Through the existing SSH tunnel, scripts can use
the browser listener's Basic authentication; a separate API token is not
required. Mutating requests must also send an `Origin` matching the app URL.

To preview a command, POST to `/api/photometry/command` with a JSON body such as:

```json
{
  "inst": "muscat4",
  "date": "250512",
  "target": "TOI-6715",
  "test_run": true,
  "options": {"bands": ["gp", "rp"], "overwrite": false}
}
```

Use the same body at `/api/photometry/run` to start the job. Set `test_run`
explicitly: it defaults to `true` for `/run` and `false` for `/command`.

A launch under a run name that is already taken answers **409** with
`"conflict": "run_exists"` and starts nothing. The body names the run's
`owner`, whether it is your own (`own_run`), a free `suggested_run_name`, and a
`confirm_token`. Either launch again under a new run name, or send the same
body with `"confirm_overwrite": "<confirm_token>"` to overwrite that run. The
token names the run's owner, so it stops working if someone else re-runs the
run in between. The transit-fit (`/api/transit-fit/run`), TTV-fit
(`/api/ttv-fit/start`) and Jobs re-run (`/api/jobs/rerun`, which also takes a
`run_name` to re-run under a new name) routes answer the same way. A viewer who
may not change the run at all still gets 403.

Poll `/api/photometry/status` with `inst`, `date`, `target`, and the returned
`run_id` as the `run` query parameter. `pending`, `running`, `cancelling`, and
`finalizing` are live states; `done`, `error`, and `cancelled` are terminal.
`none` means there is no tracked job. The log stream carries the same status
payload as server-sent events. `/status-batch` accepts up to 100 job selectors
and returns an error for each malformed entry while preserving valid entries.

GET `/api/photometry/runs` with `inst`, `date`, and `target` to discover runs
with products, newest first. GET `/api/photometry/outputs` with those selectors
and `run=<run_id>` to discover a run's filenames. An empty `run` selects legacy
products in the night's directory. Optional `site`, `telescope`, and `mode`
parameters select a multi-site reduction's products. Fetch named-run files
through `/api/photometry/file/{inst}/{date}/{target}/run/{run_id}/{name}` and
legacy files (including calibration masters) through
`/api/photometry/file/{inst}/{date}/{name}`. The corresponding `/download-all`
routes return ZIP archives. Hidden runs return empty discovery results.

Unknown request fields are ignored for compatibility with saved form settings.
Numeric options accept numbers, numeric strings, and the form's empty string;
blank run options keep pipeline defaults. A blank post-process `sigma` instead
disables sigma clipping. Comma-separated IDs and JD lists are strings (a bare number is accepted and converted) and
retain the pipeline's syntax validation. Type/shape errors return HTTP 422;
existing option rules still apply, with their existing error responses.

These contracts cover photometry. Transit-fit and TTV-fit models are subsequent
parts of issue #264.
