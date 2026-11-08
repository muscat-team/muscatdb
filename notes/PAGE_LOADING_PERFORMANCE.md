# Page loading investigation (2026-11-08)

Follow-up to [#415](https://github.com/muscat-team/muscatdb/issues/415#issuecomment-6716953056)
and the transit-fit discovery bottleneck in [#417](https://github.com/muscat-team/muscatdb/issues/417).

## Transit-fit previous-fit discovery

The previous-fit selector enumerated all observation and prose dates for every
instrument, checked each night's visibility, and resolved a timer directory even
when the target had no fit on that night. The cost grows with the full observation
history rather than the target's fits. A restricted viewer also incurs database
lookups for thousands of irrelevant nights.

Discovery now examines timer directories for the requested target first. Only
matching dates are passed to run discovery, and only dates with actual outputs
receive night-visibility checks. Existing obslog/prose date eligibility, instrument
and date ordering, newest-first runs, legacy runs, and product-visibility rules
are retained. Discovery is uncached so new runs appear immediately and permission
changes apply on the next request. No prose2 or timer engine changes are needed.

A read-only comparison used the daily production database backup, existing
production output directories, and the before/after handlers in the same Python
process. SQLite connections had `query_only=ON`; no pipelines were launched or
outputs modified. The target was `muscat2/260128/TOI06109.02`. Each variant ran
twice under cProfile. Template rendering was replaced with context capture to
compare the exact previous-fit list, which remained identical (five choices).

| Viewer | Before, seconds | After, seconds |
| --- | --- | --- |
| Unrestricted | 2.802, 2.995 | 0.470, 0.545 |
| Nonempty denied-proposal set | 9.718, 7.761 | 0.526, 0.497 |

The second case exercises the restricted visibility path using a synthetic denied
proposal; it does not impersonate a real account. Regression tests separately
verify that hidden nights and hidden products remain excluded, eligible legacy
runs remain included, and new fits appear without cache expiry. Both restricted
and unrestricted regression cases failed before the fix because the old handler
performed 7,000 night checks for a fixture with only three relevant nights.

These are handler timings under profiling, not end-to-end production browser
benchmarks. Filesystem/database caching and concurrent host work affect them.
An earlier first-call profile took 6.865 seconds, including 4.935 seconds in date
summary queries and 3,807 calls to `list_fit_runs`; its repeat took 1.803 seconds.
The revised handler no longer performs those thousands of directory resolutions
or queries dates for instruments with no matching timer directory.

## Other routes: observations and remaining work

One sequential authenticated loopback-origin GET per route returned:

| Route | Time to response headers, seconds |
| --- | --- |
| Photometry, same dataset | 0.481 |
| Transit-fit, same dataset, before this fix | 0.944 |
| Target `TOI6109` | 0.614 |
| Jobs | 0.295 |
| Jobs status (GET, shared handler with POST) | 0.135 |
| NASA archive lookup for `TOI06109.02` | 1.422 |

These warm samples do not disprove the slow production log entries or establish
an upper bound. They bypass Cloudflare and do not measure image download/render
cost. Only one NASA lookup was made; no external service load test was run.

Separate read-only profiles found a cold target handler spending 2.309 seconds
expanding aliases/loading the TOI and NASA catalogs, plus 0.972 seconds collecting
dataset statuses; a warm HTML-cache hit took about 0.005 seconds. A later cold
process was slower again, so cold catalog work is a reproducible lead, not a
claimed fix. Any future optimization must preserve catalog matching and invalidate
on catalog updates. Broad database-cache invalidation is also relevant to #401.

Jobs/status were fast in the isolated profile, but orphan-fit scanning and archive
job collection were disabled to keep that diagnostic read-only. That profile
cannot explain their production outliers. Profile those phases and restricted
viewer filtering during a slow request before changing persistence or discovery.
The live authenticated jobs sample above used the normal route.

The 10–15 second NASA outliers still require distinguishing local-catalog lookup
from external TAP latency. Do not silently substitute TOI values for an explicitly
selected NASA source. The 4.5–5.5 second postprocessing POST was not repeated: it
performs scientific work and writes outputs. Its phases need separate tracing
before changing execution or scheduling.

The photometry preview work in this PR addresses browser image transfer and
layout. The transit discovery change addresses the independently reproduced
server bottleneck. The remaining intermittent routes keep #415 open; neither
set of measurements establishes database corruption or a database-lock problem.
No production service was restarted or deployed during this investigation.
