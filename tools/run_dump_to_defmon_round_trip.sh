#!/bin/bash
# Round-trip a defMON CSV write-log through dump_to_defmon and report
# recall vs the canonical source.
#
# Pipeline:
#   1. dump_to_defmon CSV -> recon .prg
#   2. defmon_player recon.prg -> recon.wav + recon.csv (60 s)
#   3. compute_recall(source.csv, recon.csv) -> stdout summary
#
# Outputs land under ${OUT_DIR}; recon.wav stays on disk so you can
# `aplay` it for an ear audit. CSV-diff recall is the regression gate;
# the WAV is for the human-in-the-loop check (per project policy).
#
# Usage:
#   tools/run_dump_to_defmon_round_trip.sh \
#       [SOURCE_CSV] [TAG]
#
# SOURCE_CSV defaults to the canonical glow_worm fixture.
# TAG defaults to "glow_worm" (used in output filenames).
#
# Exit 0 = comparator ran and printed a recall summary.
# Exit 1 = dump_to_defmon or defmon_player failed.

set -eu

REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
OUT_DIR=${OUT_DIR:-/tmp/dump_to_defmon_round_trip}
IMAGE=${IMAGE:-pydefmon:latest}

SOURCE_CSV=${1:-${REPO}/fixtures/glow_worm.csv}
TAG=${2:-glow_worm}

if [[ ! -f "${SOURCE_CSV}" ]]; then
    echo "missing source CSV: ${SOURCE_CSV}" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"

RECON_PRG="${OUT_DIR}/${TAG}.recon.prg"
RECON_WAV="${OUT_DIR}/${TAG}.recon.wav"
RECON_CSV="${OUT_DIR}/${TAG}.recon.csv"

docker run --rm \
    -v "${REPO}/pydefmon:/work/pydefmon" \
    -v "${REPO}/profile:/work/profile" \
    -v "${OUT_DIR}:/out" \
    -v "$(dirname "${SOURCE_CSV}"):/src_csv_dir:ro" \
    -w /work \
    "${IMAGE}" \
    bash -c "
        set -e
        python3 -m pydefmon.dump_to_defmon \
            /src_csv_dir/$(basename "${SOURCE_CSV}") \
            /out/${TAG}.recon.prg
        python3 -m pydefmon.defmon_player \
            /out/${TAG}.recon.prg \
            /out/${TAG}.recon.wav
    "

cd "${REPO}" && python3 -m profile.dump_to_defmon_recall \
    "${SOURCE_CSV}" "${RECON_CSV}"

echo
echo "artifacts:"
echo "  recon .prg: ${RECON_PRG}"
echo "  recon .wav: ${RECON_WAV}    (audition: aplay ${RECON_WAV})"
echo "  recon .csv: ${RECON_CSV}"
