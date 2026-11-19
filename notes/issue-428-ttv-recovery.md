# Recover the historical HIP67522 TTV product record

The production run `HIP67522/_runs/lco_only` has eight timings and no
`_product.json`. A viewer with access to all eight inputs consequently loses
access to the fit. Restore its record using the archived jobs. The existing
product visibility checks then evaluate the original inputs on each request.

The July 21 backup contains the completed TTV job, including its author,
submitted CSV and checked transit-fit identifiers. The CSV matches the run's
`data.csv` byte for byte. Its saved selection contains eight fits for this
target and no manual points. The July 17 backup contains all eight original
transit-fit jobs and their selected legacy, night-level photometry CSVs.
Seven time/uncertainty pairs still match their fits. The Sinistro 250319 fit
was rerun at 04:39 UTC on July 21, after the TTV fit at 01:30 UTC. Its
original and replacement jobs selected the same single light curve. Use the
earlier jobs to pin the inputs, including that of the replaced fit.

## On ut2

Run from the project environment with the two July database backups under
`$HOME/temp`. First point `run_dir` at a private copy of the run. Use the
production directory only when applying the reviewed correction, with no
writer running for that fit. The command verifies the audited artifacts and
creates the missing record atomically. It refuses to replace an existing
record.

```sh
uv run python - "$run_dir" <<'PY'
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from muscat_db import product_record

run = Path(sys.argv[1])
for name, expected in {
    "data.csv": "6e62713477e114f785fbf27b99ff643d33f9f2ec1f999b694443aa069c0765e0",
    "meta.yaml": "03aa15706b0463930282714573e0933215714a9f1c9c6c8fd3c5753dd177a6f8",
    "samples.csv.gz": "011e1e41811e08e41b97a9cc4fb0ead00401768804a80438c29348a496eb0077",
}.items():
    with (run / name).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise SystemExit(f"Unexpected historical artifact: {name}")

def job(day, key):
    backup = Path.home() / "temp" / f"muscat.db.2026-07-{day}"
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE key=? AND state='done'", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"Missing completed historical job: {key}")
    return row

ttv = job("21", "ttv_fit:_/_/HIP67522/lco_only")
options = json.loads(ttv["params"])["options"]
snapshot = options["input_snapshot"]
if options["csv_content"].encode() != (run / "data.csv").read_bytes() or snapshot["manual_points"]:
    raise SystemExit("Historical TTV inputs have changed")

inputs = []
for key, checked in snapshot["checked_datasets"].items():
    if not checked:
        continue
    target, inst, date, run_id = key.split("|")
    target = target.replace(" ", "")
    fit = job("17", f"transit_fit:{inst}/{date}/{target}/{run_id}")
    files = json.loads(fit["params"])["selected_csvs"]
    if target != "HIP67522" or not files or any(Path(f).name != f for f in files):
        raise SystemExit(f"Unexpected historical light curves for {key}")
    curves = [product_record.lightcurve_input(inst, date, target, Path(f), None) for f in files]
    inputs.append(dict(kind="product", level="transit_fit", instrument=inst, obsdate=date,
                       target=target, run_id=run_id, pinned=dict(maker=fit["user_name"] or "", inputs=curves)))
if len(inputs) != 8:
    raise SystemExit("Expected eight historical transit fits")
record = product_record.build_record(
    "ttv_fit", author=ttv["user_name"], inputs=inputs, backfilled=True,
    created_at=datetime.fromtimestamp(ttv["started_at"], timezone.utc).isoformat(),
    note="Recovered from July 2026 job backups (muscatdb #428).",
)
payload = json.dumps(record).encode()
expected = "90f2b2b7f856b891abf35dfd6d74747f857ae1f97bbba71e9be729525b9417b1"
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit("Recovered record differs from the verified copy")
with NamedTemporaryFile(mode="wb", dir=run) as staged:
    staged.write(payload)
    staged.flush()
    os.fchmod(staged.fileno(), 0o644)
    os.link(staged.name, run / product_record.RECORD_NAME)
PY
```

Verify that the run appears for a viewer granted its eight inputs and stays
hidden from a non-owner missing a contributing night. For rollback, remove
only the newly created record after verifying that it still matches the
recovered one. The other historical runs with missing records had manual
points in their saved selections. Their permissions cannot be established
from this recovery.
