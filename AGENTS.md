## philosophy
* adapt scientific software standards focused on managing extreme complexity while ensuring reproducibility, portability, and performance.
* maintain seamless integration between database, photometry, and transit fitting pipeline
* do not implement critical design choices from assumptions without discussing implications

## Data
* do not delete muscat.db and data/
* always make a daily backup of muscat.db in $HOME/temp. Delete if the backup is stale.
* there are currently seven unique instruments: muscat, muscat2, muscat3, muscat4, sinistro, sbig, qhy600
* each instrument has telescope and camera specifications defined in prose2/data/*.telescope files read by prose package
* header keyword should precede over hardcoded parameters keeping in mind that the header keyword may change over time
* muscat and muscat2 has no wcs in header. muscat3, muscat4, sinistro, sbig, qhy600 has wcs. muscat4 may have constant wcs offset.
* muscat and muscat2 fits require calibration first before photometry
* muscat3, muscat4, sinistro, sbig, and qhy600 has been reduced or calibrated with BANZAI-pipeline. Confirmed for qhy600 via a real archived frame (coj0m416-sq36-20260804-0098-e91.fits.fz, RLEVEL=91, WCS present).
* for BANZAI-reduced fits data, saturation unit is e- when gain is 1 in header
* sbig (SBIG STL-6303, the LCO 0.4m network's old CCD camera) is archival-only: LCO's live instrument API (observe.lco.global/api/instruments/) has no instrument_type code for it, so it cannot be scheduled, only downloaded from the archive and reduced. qhy600 (QHY600 CMOS on DeltaRho 350, the current 0.4m camera) is live and schedulable (instrument_type "0M4-SCICAM-QHY600"). Both are multi-site like sinistro (deployed across all 6 LCO sites, unlike sinistro's 5 -- sinistro has no unit at ogg). prose2's get_instrument() and muscat-db's infer_archive_instrument() auto-detect both from a FITS header/archive metadata: sbig's INSTRUME is prefixed "kb" (e.g. kb27), qhy600's is prefixed "sq" (e.g. sq30-33, sq36, sq38, sq40, sq41, sq46 -- confirmed across coj/elp/ogg/tfn in a live archive scan, 2026-08).
* muscat.db is updated daily via a cronjob
* do not modify fits files directly, store metadata if needed
* the target page should cross-match using catalog CSV coordinates. If the target is in neither catalog, resolve via SIMBAD. As a last resort (not in any catalog and unresolved by SIMBAD), use the header pointing centre (the per-target coord_repr median RA/Dec).
* do not send requests that overload external database when querying data

## paths 
* use uv run for muscat-db
* use conda env prose when running run_photometry
* path: $HOME/miniconda3/envs/prose
* photometry.py depends on run_photometry.py in $HOME/github/research/project/ext_tools/prose2
* transit_fit.py depends on timer package in $HOME/github/research/project/ext_tools/timer
* ttv_fit.py depends on $HOME/github/research/project/ext_tools/harmonic
* do not duplicate functions between muscat-db and prose2. all photometry functions should live in prose2.
* do not use /tmp. use $HOME/temp

## external engines (prose2, timer, harmonic)
* upstream is the owner's repo: prose2 = jpdeleon, timer and harmonic = john-livingston. never fork an engine you do not own in order to carry a local patch
* when an engine blocks you, fix muscat-db so it is unblocked, then open an issue on the engine with a reproduction. leave the engine's own fix to its owner
* the exception is a change you would want even if muscat-db did not exist, e.g. an undocumented contract or an unhelpful error. that is a genuine upstream fix and is still the owner's to make
* widening an engine's behaviour so it tolerates something only muscat-db does is always the wrong fix. adapt muscat-db instead
* never leave an engine patched only on the deploy host. an uncommitted fix there is reverted by the next git pull, and both repos then behave differently from their source
* when an engine is updated on the host, record the remote and commit in notes/DEPLOYMENT.md so a fork checkout is detectable

## frontend and GUI
* GUI settings should be consistent with the arguments in run_photometry.py
* maintian design consistency across all pages based on styles.css
* Print or display values up to 6 decimal only. The significant figures should depend on the precision of uncertainty if available.
* table column widths cannot be wider than the text length of their row values or column names (e.g. in jobs.html, instrument column should be narrow to fit the content).
* test all GUI elements the same way a user interacts in practice
* ensure all new inputs and checkboxes added to templates (e.g. photometry.html) are registered in the corresponding JavaScript helper arrays 
(collectOptions, restoreOptions, and the default settings listener) so they persist in localStorage across page navigation.
* the jobs are run in a 24-core remote server with 100 Gb memory so queuing heavy jobs should be handled safely
* in the future, the pipeline will use celery and redis across several servers with 48, 120, and 120 cores

## backend and scripts
* whenever appropriate, use the API when querying muscat-db: http://localhost:8000/docs
* the output should be high-quality lightcurves from photometry, and robust inferences from transit fit
* when writing new code, choose correctness over simplicity
* check background process, report any idle or background processes related to muscat-db before running a new one
* all one-off scripts should live in $HOME/temp but useful scripts should be kept in repo
* the server lives inside tmux session named muscatdb-gui
*  The --reload flag only watches Python files, not Jinja2 templates. Remind the user if a restart is needed to see the HTML/JavaScript changes.
If agent restarts the muscat-db by itself, make sure do it inside tmux session muscatdbgui

## optimization
* consider CPU parallelization with a JIT compiler such as Numba, porting the inner loop into Cython, or implementing a CUDA GPU function with Numba or CuPy

## git branch
* CONTRIBUTING.md has the human-contributor-facing version of this section (setup, pre-PR checklist, the same branch rules in plain form). Keep both in sync when either changes; this file keeps the mechanics CONTRIBUTING.md deliberately leaves out -- ruleset internals, review-dismissal behavior, and the specific incidents that motivated each rule
* strip any Claude/AI-tool attribution before it lands in this repo: no `Co-Authored-By: Claude` trailers, no `Generated with Claude Code` / session-link footers in commits or PR bodies, no assistant name-drops in code comments or docs. commit messages, PR descriptions, and file content should read as the author's own work
* work goes on a feature branch off test, which PRs into test. never PR a feature branch straight into main
* features accumulate on test, then test is merged into main as a release
* no direct pushes to test or main. everything goes through a PR, except that org admins bypass the review and status-check rules on both branches, so this is a convention rather than something enforced against them
* merged branches are deleted automatically, so short-lived feature branches are expected
* feature PRs target test, not another PR's head. GitHub closes a PR whose base branch is deleted, so stacking dies when the lower PR merges (#96 when #95 merged). wait until the first is on test, then branch the next
* never rename the head branch of an open PR. GitHub closes the PR instead of retargeting it, so pick the name before opening
* test is the default branch, so a new PR targets it without being told to, and `Closes #N` in a feature PR body closes the issue when that PR merges
* the release PR is the one case needing an explicit base: `gh pr create --base main --head test`, because from test the default base is test itself
* merge the release PR with a **merge commit, never a squash**. a squash rewrites every file it carries as a new commit with no shared ancestry, so the next test -> main merge conflicts on files both branches already had. release #67 was squashed and #85 then hit add/add conflicts on notify_slack.yml and AGENTS.md; #32, #42 and #56 were merge commits and merged cleanly. this is now enforced: ruleset 20973974 ("main: merge commits only") restricts allowed_merge_methods to merge on refs/heads/main and has no bypass actors, so squash is not offered there at all. if the divergence ever recurs, fix it with a main -> test sync PR taking test's side, since test is the content superset
* an issue therefore closes at the test merge, before the fix is deployed. deploy.yml only runs on a push to main. write `Refs #N` instead and close by hand at release when an issue should outlive the merge
* a bare `#N` resolves inside muscatdb. use `owner/repo#N` for prose2, timer and harmonic
* main is the release branch. deploy.yml pins production with `git reset --hard origin/main` at line 52; its checkout step is incidental and follows whatever ref triggered the run
* branch protection is rulesets, not the classic settings page, and names main and test explicitly. a ruleset scoped to `~DEFAULT_BRANCH` would follow the default and leave the other branch unprotected
* deletion and force-push on both branches come from no-bypass rulesets. everything else is bypassable by org admins. main needed its own such ruleset because a branch is only undeletable by default while it *is* the default
* reviews and status checks are split. `test: ruff, pytest, coverage` (19754673) and `main: ruff, pytest, coverage` (21541815) both set dismiss_stale_reviews_on_push, required_review_thread_resolution, and one approving review. only the test ruleset sets require_last_push_approval. a push therefore drops existing approvals. on a feature PR into test, the most recent push has to be approved by someone who did not make it. a release PR into main does not have that last-push check, so the reviewer can approve even if they merged the last feature into test
* so when you want a change on the other person's PR, put it in the review and let them implement. do not open a PR against their branch, that is stacking, and do not push to it: your own approval cannot cover your own push, and github refuses an approval from the PR author
* that push should not happen, but if it does anyway, say so in a comment. there is no notification for it, and github will not let them request a review from you because they are the author

## Photometry job lifecycle
The pipeline is launched with `start_new_session=True` and prose spawns multiprocessing workers (SequenceParallel) that keep appending to the per-target log (`_webrun_<digest>.log`) **after** the tracked parent process has exited. Do not declare a job terminal the instant `job.proc.poll()` returns: `_resolve_job_state` keeps it in a non-terminal `finalizing` state until the log mtime has been quiescent for `_FINALIZE_GRACE_S` (env `MUSCAT_PHOT_FINALIZE_GRACE_S`), so the photometry page's live log keeps streaming the trailing output instead of freezing at parent-exit. `finalizing` is a live-view-only state; `sync_jobs` persists it to the DB as `running` so the Jobs page (which reads state from the DB) stays consistent. Cancelled jobs bypass the grace window and go terminal immediately.

## Testing
* The default suite is fast: `pyproject.toml` sets `addopts = "-m 'not slow'"`, so anything marked `@pytest.mark.slow` is deselected unless you opt in with `pytest -m slow`.
* `tests/test_slow_runs.py` holds heavyweight full-pipeline runtime-profiling runs (real `prose`/`timer`/`harmonic` conda tools + real data on the production host). They `pytest.skip` cleanly when raw data, CSV lightcurves, or the external conda envs are absent, so they collect/skip safely anywhere and only do real work on the host. Run them on the host with `uv run pytest -m slow`.
* A bugfix test has to red-green: run it with the fix reverted and confirm it fails, then restore the fix and confirm it passes. A test written against already-fixed code routinely passes for the wrong reason and then reads as coverage it does not provide. Two cases found in review: a stale-sidecar test that `build_db` already satisfied in its preserve step before the code under test ran, and a negative-declination test that could only fail under prose's Python 3.11, never under our own 3.12.
* Verify transit and visibility from https://exoplanetarchive.ipac.caltech.edu/docs/transit/transit_API.html

## Prompt
* Ask questions for clarifications if prompt is vague or confusing.
* Verify non-obvious assumptions before implementing edit.
