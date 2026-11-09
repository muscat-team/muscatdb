# Schedule and follow LCO observations

Use the LCO pages to plan transit observations on MuSCAT3, MuSCAT4, Sinistro and QHY600, book them, and follow them until the frames arrive. Open **LCO** in the navbar, then **Schedule Observations**.

## Before you start

You need two things.

1. An LCO API token. Copy it from your LCO profile, open **Settings**, expand **LCO API Token**, paste it into **Token**, and click **Save token**. **Clear user token** removes it.
2. The ID of an LCO proposal you can submit to. The **Proposal ID** list on the schedule page fills from your LCO account once the token is saved.

The banner at the top of the LCO pages has three indicators: **API token**, **Download root** and **Live submit**. If **Live submit** is not green, ask an admin to enable it before you submit.

You can also connect your own ephemeris spreadsheet under **My ephemeris Google Sheet** in **Settings**. Paste the **Sheet URL or ID**, name the **Ephemeris tab** and **Transit-centers tab** if they differ from the defaults, map columns with **Load columns**, then click **Save sheet**. The sheet must be published or shared by link.

## Plan a request

1. Under **Target & Transit Windows**, enter the **Target** and the **Planet** letter. **Fetch ephemeris** reloads the catalogs and picks up newly finished transit fits.
2. Choose the **Ephemeris source**. **Auto** uses the reference ephemeris: NASA, else TOI, else the local catalog. Other choices are **NASA catalog**, **TOI catalog**, **linear fit**, **Shared Google Sheet**, **My Google Sheet (private)**, **Uploaded mid-times**, and one **TTV model** entry per finished TTV run. Sheet entries stay disabled until a sheet is configured.
3. Read the line of text below **Duration (hr)**. It names the catalog, and the paper where one is recorded, behind **t0 (BJD)**, **Period (d)** and **Duration (hr)**. You can edit any value, and its source tag then reads manual.
4. Set **Range start (UTC)** and **Range end (UTC)**, then adjust the padding with **Pre-ingress (min)** and **Post-egress (min)**. Both default to 60. Set **Site(s)**, **Twilight**, **Max airmass**, **Min lunar (deg)** and **Max lunar phase** for the observability rating.
5. Click **Generate windows**. Each transit gets a rating in the **Transit obs** column: full, partial, split (a relay between two sites) or none. **Default plan** sets how each plan starts, and you can change it per row.
6. Tick the transits you want. They appear in the table under **Requests**.
7. Under **Imaging Request**, pick the **Proposal ID** and the **Instrument**. Pick a **Site**: MuSCAT3 is at ogg and MuSCAT4 at coj. A blank site lets LCO choose.
8. Set exposures. Sinistro and QHY600 take **Exp time (s)**, **Filter**, **Readout** and **Exp count**. MuSCAT takes one exposure time per band, **g (s)**, **r (s)**, **i (s)** and **z (s)**. **Show ObsLog** lists past exposures of the target, and clicking a row reuses its exposure time.
9. In the **Requests** table, check the **Observing duration (hr)** of each request. It starts at the longest value the window fits, after the 180 s setup overhead, and follows the window until you type a value. A shorter value is accepted and a longer one is refused. **Reset** returns to following the window.
10. Choose a **Strategy**. **Single** books one request per transit. **Overlapping** keeps the full length and shifts each following request later by **Overlap shift (min)**. **Chunked** splits the window into equal pieces and adds **Chunk padding before (min)** and **Chunk padding after (min)** to each piece, so padded requests can overlap. **Requests per window** takes 2 to 10. Every request is an independent booking.
11. Click **Create drafts**. **JSON** shows each draft's LCO payload for editing.
12. Click **Check settings**. The page runs LCO's dry-run on every draft without booking anything. A passing draft shows **checked** and the maximum allowable IPP. If the request is longer than the time LCO sees the target above the horizon, the page shortens the repeat duration to fit and checks again. A failing draft shows LCO's error.
13. Click **Submit** on one row, or **Submit all** for every checked draft. Confirm the dialog. Each booking is separate and cannot be undone from this page. Successful bookings stay booked if another fails.

## Follow your requests

Submitted requests appear under **Submitted Request Monitoring**. The page links each **Request** to LCO and shows the **LCO state**, the **Pipeline** state and the **Frames** count (downloaded, and reduced out of raw).

Frame downloads need no action from you. After the observing window, muscat-db polls LCO and downloads the final reduced frames as they appear. When every raw frame has a reduced counterpart, the data is scanned and added to the database, and the **Pipeline** state becomes `complete`. If some frames never get a reduced product, the monitor settles after a grace period on what it has, or marks the request `terminal_no_data`.

When the state is `complete`, a telescope button appears in the **Action** column for each dataset. It opens the photometry page for that instrument, date and target. To fetch observations scheduled outside muscat-db, use **Download LCO Data**.

Three row actions are available:

- **Clone** copies a request into the form. Generate new windows.
- **Cancel on LCO** cancels the whole request group and all its unfinished requests. It asks for confirmation, and for a relay it asks whether to cancel the other leg too.
- **Delete** removes local tracking only. The LCO booking and any ingested data stay.

Only the submitter or an admin sees **Cancel on LCO** and **Delete**.

## Auto-resubmit dropped observations

**Auto-resubmit dropped observations** is off by default. Tick it before **Create drafts** and it applies to each new draft. It books one replacement when LCO drops your observation, which means it was aborted or failed, or was not attempted and its window has ended.

Two thresholds decide whether a replacement is worth booking. **Minimum original-window fraction** defaults to 0.5, and **Minimum remainder (min)** defaults to 30. The observable time left must meet every positive threshold. If both are zero, the feature is off.

Recovery has these requirements:

- one request with one REPEAT_EXPOSE configuration, and a pinned site;
- your own saved token;
- live submission enabled, and a proposal budget set by an admin. Without a budget the request is refused. Ask an admin for one.

The monitor waits while LCO has a pending or running observation, and it never replaces canceled or completed requests. The budget is shared by all users and never resets. A line above the monitoring table reports usage per proposal.

Each original gets at most one replacement, and replacements do not opt in again. The replacement is booked first. The original is canceled only if it is still unscheduled. If it is scheduled or running, both stay and you get a private duplicate-risk notice. A timeout or server error never triggers a second submission. The monitor looks the replacement up by its unique name instead.

The **Detail** column shows `auto-resubmitted as #N` or the reason a request was not replaced. Recovery is best effort and does not guarantee telescope time.

## Tips and limits

- A site where LCO currently lists no unit of your instrument shows "currently unavailable" in the site list. The option stays selectable, and the default plans avoid such sites when another plan exists.
- Sinistro has no unit at ogg, according to the site help text.
