# Getting started

This page walks one target from the target page to a transit fit. You need
an account first. The app is at [https://db.muscat-team.jp](https://db.muscat-team.jp).

## Sign in

You sign in with a one-time PIN sent to your email address. An admin must link
that address to your account before you can sign in. See
[Signing in and access](signing-in.md) for the steps.

## The home page and navbar

The home page shows a globe with a button for each telescope site, such as
**MuSCAT3 (OGG)**, and weather cards for the LCO sites. Click **MuSCAT-db** in
the navbar from any page to return to it.

The navbar links are:

- **Targets**: every target in the database, with search and filters.
- **Obs**: the observation log for one night.
- **Projects**: targets grouped by observing program.
- **Photometry**: reduce a night and view the light curves.
- **Transit Fit**: fit a transit to those light curves.
- **Ephemeris**: transit timing and TTV fitting.
- **Jobs**: every running and finished job.
- **LCO**: schedule observations on the LCO network.
- **FOV**: find a field of view and comparison stars.
- **Exp Calc**: the exposure time calculator.
- **TOI** and **NExScI**: browse the TESS and NASA catalogs.
- **Settings**: your personal settings, such as API tokens.

**Photometry**, **Transit Fit** and **Ephemeris** reopen the last night or
target you used in this browser.

## Find a target

1. Open **Targets**.
2. Type a name in **Search by object name in fits header…**. Use **Filter by instrument** to narrow the list.
3. Click the name in the **Normalized Target** column.

The target page lists one row per night under **Photometric Observations**.
Each row shows the date, instrument, filters, airmass range and frame count.
The **Phot** and **Fit** columns show **Y** when a full photometry run or a
transit fit exists, and **N** otherwise. A test run alone counts as **N**.

Below the table are collapsible panels for ExoFOP, NASA ADS and other catalogs.
Some panels, such as JWST, appear only when the target is listed there. The
**LCO archive** panel searches for LCO science frames that cover the target.
Buttons at the bottom open the ephemeris page, the LCO scheduler and the LCO
archive search for this target.

## Open a night's observation log

Click a date in the first column of the table. The log lists the frames for that
night. [Browse a night](browse-a-night.md) describes it in detail.

## Run photometry

Photometry runs on one instrument, date and target. The telescope icon in the
**Phot** column opens the **Photometry** page already filled in for that night.

1. Check that **Instrument**, **Obs date** and **Target** are correct.
2. Scroll to **Run reduction**. After a first run, the heading reads **Configure & Run Photometry**.
3. Expand **Pipeline options** and check the boxes under **Bands**.
4. Type a **Run name**, for example `first-pass`. Leave it blank to use `default`.
5. Click **▶ Run Test Reduction (first N frames)**.
6. When the test looks right, click **▶ Run Full Reduction (all frames)**.

Both buttons use the same options. The test run processes only a few frames per
band, 10 by default, so it finishes quickly. Change that count with **# of
test-run frames** under **Reduction & Binning**. The defaults suit most nights,
so leave the other options alone on your first run.

If you reuse a run name for the same target, a dialog offers **Save as**,
**Overwrite** or **Cancel**. Different run names are stored side by side and
appear as **Run:** chips on the page. If the page warns that raw data was not
found, the run buttons stay disabled. Ask an admin.

Results appear on the same page once the run finishes. **Light curves &
Covariates** holds the summary plots and a **Download csv** link for each band.
**Per-band products** has one tab per band, and **Download all output (.zip)**
collects everything.

### Follow a job

A banner at the top of the page shows the job state, and the log streams under
the buttons. A full run waits as Pending if another full run is going. Click
**To Jobs** at the bottom of the page, or open **Jobs** in the navbar.

On **Jobs**, the **Photometry Jobs** table shows the **Status** of each run:
Running, Pending, Done, Failed or Cancelled. The **📄 Log** button shows the log.
**Cancel** stops a running job. Only the owner of a run, an admin, or a member
of the owner's group can cancel or delete it.

## Run a transit fit

1. On the **Photometry** page, click **To Transit Fit**. You can also click the chart icon in the **Fit** column on the target page.
2. Under **Discovered Lightcurves**, keep the light curves you want checked. **Select All** and **Deselect All** change every box at once.
3. Expand **Fitting Configurations & Parameters**.
4. Pick **NASA Archive** or **TOI Catalog** and click **Query Archive**. This fills in the stellar and planet parameters.
5. Check the values. The app asks you to fill any required field marked in red.
6. Type a **Run name**.
7. Click **▶ Test run (optimize)**.
8. When the result looks reasonable, click **▶ Run Full Fit**.

The test run optimizes only, with no MCMC, so you can check your inputs
quickly. The full run samples with MCMC chains and takes much longer. If no
light curves are found, the page links back to the photometry page.

Results appear under **Transit Fit Results**: model plots, **Systematics
Plots**, **Fitted Parameters Summary** and **Download all output (.zip)**. The
fit also appears in the **Transit Fit Jobs** table on **Jobs**.

The photometry and transit-fit pipelines run on the prose2 and timer engines.

## Where to go next

- **TTV fitting**: open **Ephemeris**, pick a target with **Load Datasets**, and use **Configure & Run TTV Fitting**. Only targets with finished transit fits are listed. The TTV fit uses the harmonic engine.
- **Observation planning**: [Scheduling LCO observations](lco-scheduling.md) covers the **LCO** page.
- **Exposure times**: see [Exposure magnitude lookup](../exposure.md).

To report a problem or ask for a feature, open an issue at
[github.com/muscat-team/muscatdb/issues](https://github.com/muscat-team/muscatdb/issues).
