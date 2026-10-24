#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$REPO/data"
TMPDIR="${MUSCAT_TMPDIR:-$HOME/temp}"
mkdir -p "$TMPDIR"
BASE_URL="https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
# Retry every error, DNS included: the 17:30 nightly has failed with "Could not
# resolve host" while the archive answered minutes later.
RETRIES="${CATALOG_RETRIES:-4}"
RETRY_DELAY_S="${CATALOG_RETRY_DELAY_S:-30}"
ATTEMPTED=0
FAILED=()

download_csv() {
    local query="$1"
    local label="$2"
    local out="$3"
    local tmp
    tmp="$(mktemp "$TMPDIR/${label}_XXXXXX.csv")"
    ATTEMPTED=$((ATTEMPTED + 1))

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Downloading $label -> $out ..."
    # --fail keeps an HTTP error page out of data/, and a body that is not a
    # CSV (empty, or a VOTable error document) is rejected the same way.
    if curl -sS --fail --retry "$RETRIES" --retry-delay "$RETRY_DELAY_S" \
         --retry-all-errors --retry-max-time 600 \
         -o "$tmp" -w "HTTP %{http_code}, %{size_download} bytes\n" \
         --data-urlencode "query=$query" \
         --data-urlencode "format=csv" \
         "$BASE_URL" --max-time 300 \
       && [ -s "$tmp" ] && [ "$(head -c 1 "$tmp")" != "<" ]; then
        mv -f "$tmp" "$DATA/$out"
        echo "  -> $DATA/$out updated"
    else
        echo "  WARNING: $label download failed or was not a CSV, keeping existing $out"
        rm -f "$tmp"
        FAILED+=("$out")
    fi
}

download_csv \
  "SELECT tid AS \"TIC ID\", toi AS \"TOI\", toidisplay AS \"Planet Name\", tfopwg_disp AS \"TFOPWG Disposition\", pl_tranmid AS \"Epoch (BJD)\", pl_orbper AS \"Period (days)\", pl_trandurh AS \"Duration (hours)\", pl_trandep AS \"Depth (ppm)\", pl_rade AS \"Planet Radius (R_Earth)\", pl_eqt AS \"Planet Equil Temp (K)\", pl_insol AS \"Planet Insolation (Earth Flux)\", st_tmag AS \"TESS Mag\", st_teff AS \"Stellar Eff Temp (K)\", st_rad AS \"Stellar Radius (R_Sun)\", st_dist AS \"Stellar Distance (pc)\", ra AS \"ra_deg\", dec AS \"dec_deg\", st_pmra AS \"PM RA (mas/yr)\", st_pmdec AS \"PM Dec (mas/yr)\", pl_tranmiderr1 AS \"Epoch (BJD) err\", pl_orbpererr1 AS \"Period (days) err\", pl_trandurherr1 AS \"Duration (hours) err\", pl_trandeperr1 AS \"Depth (ppm) err\", pl_radeerr1 AS \"Planet Radius (R_Earth) err\", st_tmagerr1 AS \"TESS Mag err\", st_tefferr1 AS \"Stellar Eff Temp (K) err\", st_raderr1 AS \"Stellar Radius (R_Sun) err\", st_disterr1 AS \"Stellar Distance (pc) err\" FROM toi" \
  "toi" "TOIs.csv"

download_csv \
  "SELECT pl_name, hostname, tic_id, discoverymethod, disc_facility, st_spectype, disc_year, disc_pubdate, ra AS ra_x, dec AS dec_x, sy_pmra, sy_pmdec, pl_tranmid, pl_tranmiderr1, pl_tranmiderr2, pl_orbper, pl_orbpererr1, pl_orbpererr2, pl_orbsmax, pl_rade, pl_radj, pl_bmasse, pl_bmassj, pl_bmassprov, pl_eqt, pl_insol, pl_ratror, pl_trandep, pl_trandur, pl_trandurerr1, pl_trandurerr2, pl_imppar, pl_orbincl, pl_orbeccen, pl_dens, st_teff, st_rad, st_mass, st_logg, st_met, st_dens, sy_dist, sy_vmag, sy_tmag, sy_gaiamag, sy_kmag, sy_snum, cb_flag, st_age, st_ageerr1, st_agelim, ttv_flag, pl_projobliq, st_nrvc, st_nspec, st_nphot FROM pscomppars" \
  "pscomppars" "nexsci_pscomppars.csv"

download_csv \
  "SELECT pl_name, max(pl_pubdate) as pl_pubdate FROM ps GROUP BY pl_name" \
  "ps_pubdates" "nexsci_pubdates.csv"

download_csv \
  "SELECT distinct pl_name FROM nexolist WHERE facility='NASA 6.5m James Webb Space Telescope (JWST)'" \
  "jwst_targets" "jwst_targets.csv"

download_csv \
  "SELECT distinct pl_name FROM spectra" \
  "spectra_targets" "spectra_targets.csv"

# Every catalog is attempted before failing, so one outage keeps all the old
# files but a partial outage still refreshes what it can.
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${#FAILED[@]} of $ATTEMPTED catalog downloads failed, kept the existing files: ${FAILED[*]}"
    exit 1
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Catalog download complete"
