#!/bin/bash
# Benchmark: UI button press -> EVSE current write
set -u
LC_ALL=C
LOG=/var/www/html/openWB/ramdisk/main.log
CP=7
SAMPLES=${1:-5}

echo "BENCHMARK START samples=$SAMPLES cp=$CP"
CUR=$(mosquitto_sub -p 1883 -t "openWB/chargepoint/${CP}/set/manual_lock" -C 1 -W 2 2>/dev/null || echo "false")
echo "Current manual_lock state: $CUR"

parse_ts() {
    local ts=$(echo "$1" | sed -E 's/^([0-9-]+ [0-9:]+,[0-9]+).*/\1/' | tr ',' '.')
    date -d "$ts" +%s.%3N
}

declare -a TRIG_DELTAS
declare -a EVSE_DELTAS

for i in $(seq 1 $SAMPLES); do
    if [ "$CUR" = "true" ]; then NEW=false; else NEW=true; fi
    CUR=$NEW
    OFFSET=$(wc -c < $LOG)
    T_PUB=$(date +%s.%3N)
    mosquitto_pub -p 1883 -t "openWB/set/chargepoint/${CP}/set/manual_lock" -m "$NEW"
    sleep 5

    NEW_LOG=$(tail -c +$((OFFSET+1)) $LOG)
    START_LINE=""
    while IFS= read -r line; do
        TS=$(parse_ts "$line")
        if awk "BEGIN{exit !($TS >= $T_PUB)}"; then
            START_LINE="$line"; break
        fi
    done < <(echo "$NEW_LOG" | grep '# \*\*\*Start\*\*\*')

    if [ -z "$START_LINE" ]; then
        echo "[$i] NO ALGORITHM START >= publish timestamp"; continue
    fi
    T_START=$(parse_ts "$START_LINE")
    DELTA_TRIG=$(awk "BEGIN{printf \"%.3f\", $T_START - $T_PUB}")

    TIMINGS_LINE=""
    while IFS= read -r line; do
        TS=$(parse_ts "$line")
        if awk "BEGIN{exit !($TS >= $T_START)}"; then
            TIMINGS_LINE="$line"; break
        fi
    done < <(echo "$NEW_LOG" | grep 'handler10Sec timings')

    if [ -n "$TIMINGS_LINE" ]; then
        T_END=$(parse_ts "$TIMINGS_LINE")
        GRAPH=$(echo "$TIMINGS_LINE" | sed -nE 's/.*graph=([0-9.]+).*/\1/p')
        PRINT=$(echo "$TIMINGS_LINE" | sed -nE 's/.*print=([0-9.]+).*/\1/p')
        # EVSE write happens at end of proc, BEFORE graph+print.
        EVSE_REL=$(awk "BEGIN{printf \"%.3f\", $T_END - $GRAPH - $PRINT - $T_PUB}")
        TIMINGS=$(echo "$TIMINGS_LINE" | sed 's/.*timings //')
        echo "[$i] trig=${DELTA_TRIG}s evse=${EVSE_REL}s | $TIMINGS"
        TRIG_DELTAS+=($DELTA_TRIG)
        EVSE_DELTAS+=($EVSE_REL)
    else
        echo "[$i] trig=${DELTA_TRIG}s (no timings)"
    fi
done

if [ ${#TRIG_DELTAS[@]} -gt 0 ]; then
    echo ""
    echo "SUMMARY (n=${#TRIG_DELTAS[@]}):"
    awk -v t="${TRIG_DELTAS[*]}" -v p="${EVSE_DELTAS[*]}" 'BEGIN{
        n=split(t,a," "); s=0;mn=1e9;mx=0;
        for(i=1;i<=n;i++){s+=a[i]; if(a[i]<mn)mn=a[i]; if(a[i]>mx)mx=a[i]}
        printf "  publish -> algorithm start: min=%.3f max=%.3f mean=%.3fs\n", mn, mx, s/n
        n=split(p,b," "); s=0;mn=1e9;mx=0;
        for(i=1;i<=n;i++){s+=b[i]; if(b[i]<mn)mn=b[i]; if(b[i]>mx)mx=b[i]}
        printf "  publish -> EVSE write done: min=%.3f max=%.3f mean=%.3fs\n", mn, mx, s/n
    }'
fi
