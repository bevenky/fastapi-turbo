# Shared bench-output → TSV row parser. Used by every benchmark
# runner so they all share the same fail-on-unparsable contract
# the v3 runner has (R34). R35 audit caught DB / Redis / SQLA
# runners still using a grep+``${rps:-?}`` fallback that silently
# emitted ``?`` placeholder rows when the bench command failed —
# downstream rendered docs would either drop those rows or pick
# up stale numbers from a previous run, masking the failure.
#
# Usage from a sibling runner:
#
#   source "$SCRIPT_DIR/_bench_row.sh"
#   bench_row "label-or-fw" "endpoint" "$out"
#
# The third arg is the captured ``$BENCH ...`` stdout. Stdout
# of ``bench_row`` is one TSV line; stderr carries diagnostics
# on failure.
#
# Soft-fail (legacy behaviour) is opt-in via
# ``BENCH_ALLOW_UNPARSABLE=1``.

bench_row() {
    local label="$1" endpoint="$2" out="$3"
    local rps p50 p99
    # Each ``grep`` substitution gets ``|| true`` because under the
    # parent script's ``set -e``, ``grep``'s no-match exit (1) would
    # propagate out of the command substitution and abort the runner
    # BEFORE we reach the ``BENCH_ALLOW_UNPARSABLE`` / diagnostic
    # branch — meaning the script silently exited on the first
    # unparsable row with no diagnostic and no row written. R36
    # audit caught this. Now ``rps`` / ``p50`` / ``p99`` end up
    # empty on no-match, the ``-z`` check below fires as intended,
    # and the diagnostic / soft-fail path runs as documented.
    rps=$(echo "$out" | grep -oE '[0-9]+ (req|msg)/s' | head -1 | cut -d' ' -f1 || true)
    p50=$(echo "$out" | grep -oE 'p50=[0-9]+' | head -1 | cut -d= -f2 || true)
    p99=$(echo "$out" | grep -oE 'p99=[0-9]+' | head -1 | cut -d= -f2 || true)
    if [ -z "$rps" ] || [ -z "$p50" ] || [ -z "$p99" ]; then
        if [ "${BENCH_ALLOW_UNPARSABLE:-0}" = "1" ]; then
            printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$endpoint" \
                "${rps:-?}" "${p50:-?}" "${p99:-?}"
        else
            echo "bench row for ${label} ${endpoint} produced unparsable output:" >&2
            echo "$out" >&2
            return 1
        fi
    else
        printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$endpoint" "$rps" "$p50" "$p99"
    fi
}
