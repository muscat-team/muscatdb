#!/usr/bin/env bash
# Put selected ollama models on muscat-ut4's local SSD; leave the rest on NFS.
#
#   sudo bash scripts/ollama_models_local.sh                 # gemma4:latest local
#   sudo bash scripts/ollama_models_local.sh gemma4:latest qwen3.6:latest
#   sudo bash scripts/ollama_models_local.sh --rollback      # back to NFS-only
#
# Builds $LOC with every manifest, real copies of the chosen models' blobs
# (sha256-verified), and symlinks to NFS for every other blob, then points the
# systemd service at it with a drop-in. The NFS store and the Oct-28
# /usr/share/ollama/.ollama symlink are never modified. If the service does not
# come back with the same model list, the drop-in is removed automatically.
set -euo pipefail

NFS=/mnt_ut2/raid_ut2/services/ollama/models
LOC=/var/lib/ollama/models
DROPIN_DIR=/etc/systemd/system/ollama.service.d
DROPIN=$DROPIN_DIR/models-local.conf
API=http://127.0.0.1:11434
MIN_FREE_AFTER_GB=50          # refuse if / would drop below this much free space
HEALTH_WAIT_S=60

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

restart_ollama() {
    systemctl daemon-reload
    systemctl restart ollama
    for _ in $(seq 1 "$HEALTH_WAIT_S"); do
        curl -sf "$API/api/version" >/dev/null && return 0
        sleep 1
    done
    return 1
}

model_names() {  # sorted model names the running server lists
    curl -sf "$API/api/tags" | python3 -c \
        'import json,sys; print("\n".join(sorted(m["name"] for m in json.load(sys.stdin)["models"])))'
}

rollback() {
    log "removing $DROPIN and restarting ollama (NFS store only)"
    rm -f "$DROPIN"
    restart_ollama || die "ollama did not come back after rollback; check: journalctl -u ollama -n 50"
    log "ollama is back on NFS. Local copy kept at $LOC; delete it with: rm -rf /var/lib/ollama"
}

manifest_path() {  # gemma4:latest -> $NFS/manifests/registry.ollama.ai/library/gemma4/latest
    local name=${1%%:*} tag=latest
    [[ $1 == *:* ]] && tag=${1#*:}
    [[ $name == */* ]] || name=library/$name
    echo "$NFS/manifests/registry.ollama.ai/$name/$tag"
}

blob_digests() {  # "<hex> <size>" per config+layer of a manifest
    python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
for l in [d["config"], *d["layers"]]:
    print(l["digest"].split(":", 1)[1], l["size"])' "$1"
}

# ---------------------------------------------------------------- preflight
[[ $EUID -eq 0 ]] || die "run with sudo"
if [[ ${1:-} == --rollback ]]; then rollback; exit 0; fi
[[ -d $NFS/blobs && -d $NFS/manifests ]] || die "NFS store not found at $NFS"
id ollama >/dev/null 2>&1 || die "no 'ollama' user"
[[ -e $DROPIN ]] && die "$DROPIN already exists; run --rollback first to redo"
MODELS=("${@:-gemma4:latest}")

before=$(model_names) || die "ollama API not answering at $API"
log "models currently listed: $(echo "$before" | wc -l)"

declare -A LOCAL=()   # hex digest -> size
need=0
for m in "${MODELS[@]}"; do
    mf=$(manifest_path "$m")
    [[ -f $mf ]] || die "no manifest for $m ($mf)"
    while read -r hex size; do
        [[ -f $NFS/blobs/sha256-$hex ]] || die "$m: blob sha256-$hex missing on NFS (cloud-only model?)"
        LOCAL[$hex]=$size
        need=$((need + size))
    done < <(blob_digests "$mf")
done
avail=$(( $(df --output=avail -B1 / | tail -1) ))
after_gb=$(( (avail - need) / 1024**3 ))
log "copying ${#LOCAL[@]} blobs, $((need / 1024**2)) MiB; / free after: ${after_gb} GiB"
(( after_gb >= MIN_FREE_AFTER_GB )) || die "would leave < ${MIN_FREE_AFTER_GB} GiB free on /"

# ---------------------------------------------------------------- build local store
# every level explicitly: install -d chowns only the directories it is named
install -d -o ollama -g ollama -m 755 "$LOC" "$LOC/blobs"
log "copying manifests"
sudo -u ollama cp -r "$NFS/manifests" "$LOC/"

for hex in "${!LOCAL[@]}"; do
    dst=$LOC/blobs/sha256-$hex
    if [[ -f $dst && ! -L $dst ]] && echo "$hex  $dst" | sha256sum -c --status -; then
        log "sha256-${hex:0:12} already local and verified"; continue
    fi
    rm -f "$dst" "$dst.partial"
    log "copying sha256-${hex:0:12} ($(( LOCAL[$hex] / 1024**2 )) MiB)"
    sudo -u ollama cp "$NFS/blobs/sha256-$hex" "$dst.partial"
    echo "$hex  $dst.partial" | sha256sum -c --status - || { rm -f "$dst.partial"; die "checksum mismatch for sha256-$hex"; }
    sudo -u ollama mv "$dst.partial" "$dst"
done

n_links=0
for f in "$NFS"/blobs/sha256-*; do
    b=$(basename "$f")
    [[ -e $LOC/blobs/$b || -L $LOC/blobs/$b ]] && continue
    sudo -u ollama ln -s "$f" "$LOC/blobs/$b"
    n_links=$((n_links + 1))
done
log "symlinked $n_links other blobs to NFS"

# ---------------------------------------------------------------- switch service
mkdir -p "$DROPIN_DIR"
printf '[Service]\nEnvironment=OLLAMA_MODELS=%s\n' "$LOC" > "$DROPIN"
log "restarting ollama with OLLAMA_MODELS=$LOC"
if ! restart_ollama; then
    log "ollama did not answer within ${HEALTH_WAIT_S}s"; rollback; exit 1
fi
after=$(model_names) || { rollback; exit 1; }
if [[ $before != "$after" ]]; then
    log "model list changed:"; diff <(echo "$before") <(echo "$after") || true
    rollback; exit 1
fi

log "done: $(echo "$after" | wc -l) models listed, local: ${MODELS[*]}"
log "env: $(systemctl show ollama -p Environment | grep -o 'OLLAMA_MODELS=[^ ]*')"
df -h / | tail -1
