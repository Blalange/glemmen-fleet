#!/bin/sh
# ECMP route agent for the dual-link 1xx nodes, glemmen80 and the single-link
# glemmen50/glemmen60/glemmen250 nodes.
#
# Felix route programming is disabled (FelixConfiguration
# programClusterRoutes: Disabled); this agent is the single owner of pod-CIDR
# routes on every node.  On nodes with two independent uplinks it installs a
# two-nexthop multipath route:
#
#   nexthop via 192.168.6.<peer> dev eno8303 weight 1
#   nexthop via 192.168.7.<peer> dev eno8403.7 weight 1 onlink
#
# The agent only uses the VLAN 7 path when the peer's 192.168.7.x address is
# reachable, but a single lost ICMP packet must not rewrite a live route:
# routes are changed only after several consecutive probe failures or
# successes.  The kernel's fib_multipath_use_neigh also skips a nexthop whose
# neighbour entry has failed, so the dataplane can fail over while the
# control-plane decision is still damped.
#
# A peer that is completely down gets a blackhole route rather than a stale
# nexthop or a leak to the default route.  A 10.42.0.0/16 blackhole is the
# safety net for pod blocks that are not in the map (e.g. a node that was
# added without updating this script).
#
# The agent also raises NIC rings at startup and exports ecmp_* metrics into
# the node-exporter textfile collector.
set -u

NODE_NAME="${NODE_NAME:-$(hostname)}"
STATE_DIR="${STATE_DIR:-/var/lib/ecmp-route-agent}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
COLLAPSE_AFTER="${COLLAPSE_AFTER:-2}"
RESTORE_AFTER="${RESTORE_AFTER:-2}"

mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR="/tmp/ecmp-route-agent"
mkdir -p "$TEXTFILE_DIR" 2>/dev/null || TEXTFILE_DIR="/tmp"
mkdir -p "$STATE_DIR" 2>/dev/null || true

if command -v fping >/dev/null 2>&1; then
  HAVE_FPING=1
else
  HAVE_FPING=""
fi

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
}

state_get() {
  file="$STATE_DIR/$1"
  if [ -f "$file" ]; then
    cat "$file"
  else
    echo 0
  fi
}

state_set() {
  echo "$2" > "$STATE_DIR/$1" 2>/dev/null || true
}

bump() {
  value=$(state_get "$1")
  case "$value" in
    ''|*[!0-9]*) value=0 ;;
  esac
  value=$((value + 1))
  state_set "$1" "$value"
  echo "$value"
}

# Resolve interfaces from their addresses rather than from `ip route get`:
# during an in-place conversion a stale bond0 can still win the default route
# lookup even though the address has moved to eno8303.  Prefer a non-bond
# interface, but fall back to bond0 on unconverted nodes.
iface_for_prefix() {
  ip -o -4 addr show 2>/dev/null \
    | awk -v pat="$1" '$4 ~ pat { print $2 }' \
    | grep -v '^bond' | head -1
}
iface_for_prefix_any() {
  ip -o -4 addr show 2>/dev/null \
    | awk -v pat="$1" '$4 ~ pat { print $2; exit }'
}

set_peer() {
  case "$1" in
    glemmen50)  DST_POD=10.42.7.0/24;  DST_SRV=192.168.6.50;  DST_V7="" ;;
    glemmen60)  DST_POD=10.42.2.0/24;  DST_SRV=192.168.6.60;  DST_V7="" ;;
    glemmen80)  DST_POD=10.42.4.0/24;  DST_SRV=192.168.6.80;  DST_V7=192.168.7.80 ;;
    glemmen130) DST_POD=10.42.1.0/24;  DST_SRV=192.168.6.130; DST_V7=192.168.7.130 ;;
    glemmen150) DST_POD=10.42.0.0/24;  DST_SRV=192.168.6.150; DST_V7=192.168.7.150 ;;
    glemmen160) DST_POD=10.42.3.0/24;  DST_SRV=192.168.6.160; DST_V7=192.168.7.160 ;;
    glemmen170) DST_POD=10.42.6.0/24;  DST_SRV=192.168.6.170; DST_V7=192.168.7.170 ;;
    glemmen180) DST_POD=10.42.5.0/24;  DST_SRV=192.168.6.180; DST_V7=192.168.7.180 ;;
    glemmen250) DST_POD=10.42.14.0/24; DST_SRV=192.168.6.250; DST_V7="" ;;
    *) return 1 ;;
  esac
  return 0
}

case "${NODE_NAME}" in
  glemmen50)  LOCAL_POD=10.42.7.0/24 ;;
  glemmen60)  LOCAL_POD=10.42.2.0/24 ;;
  glemmen80)  LOCAL_POD=10.42.4.0/24 ;;
  glemmen130) LOCAL_POD=10.42.1.0/24 ;;
  glemmen150) LOCAL_POD=10.42.0.0/24 ;;
  glemmen160) LOCAL_POD=10.42.3.0/24 ;;
  glemmen170) LOCAL_POD=10.42.6.0/24 ;;
  glemmen180) LOCAL_POD=10.42.5.0/24 ;;
  glemmen250) LOCAL_POD=10.42.14.0/24 ;;
  *) LOCAL_POD="" ;;
esac

DESTINATIONS="glemmen50 glemmen60 glemmen80 glemmen130 glemmen150 glemmen160 glemmen170 glemmen180 glemmen250"

route_text() {
  ip route show "$1" 2>/dev/null | tr '\n' ' '
}

is_dual() {
  text=$(route_text "$DST_POD")
  echo "$text" | grep -Fq "nexthop via $DST_SRV dev $SERVER_IFACE" && \
    echo "$text" | grep -Fq "nexthop via $DST_V7 dev $VLAN7_IFACE"
}

is_single() {
  text=$(route_text "$DST_POD")
  if echo "$text" | grep -Fq "nexthop"; then
    return 1
  fi
  echo "$text" | grep -Fq "via $DST_SRV dev $SERVER_IFACE"
}

is_blackhole() {
  route_text "$DST_POD" | grep -Fq "blackhole"
}

current_mode() {
  if is_blackhole; then
    echo blackhole
  elif is_dual; then
    echo dual
  elif is_single; then
    echo single
  else
    echo missing
  fi
}

note_change() {
  old_mode=$1
  new_mode=$2
  if [ "$old_mode" != "$new_mode" ] && [ "$old_mode" != "missing" ]; then
    flaps=$(bump "flaps.$DST")
    log "flap $DST $old_mode->$new_mode (total $flaps)"
  fi
}

install_dual() {
  old=$(current_mode)
  if [ "$old" != dual ]; then
    ip route replace "$DST_POD" \
      nexthop via "$DST_SRV" dev "$SERVER_IFACE" weight 1 \
      nexthop via "$DST_V7" dev "$VLAN7_IFACE" onlink weight 1
    note_change "$old" dual
    log "dual $DST_POD via $DST_SRV/$DST_V7"
  fi
}

install_single() {
  old=$(current_mode)
  if [ "$old" != single ]; then
    ip route replace "$DST_POD" \
      nexthop via "$DST_SRV" dev "$SERVER_IFACE" weight 1
    note_change "$old" single
    log "single $DST_POD via $DST_SRV"
  fi
}

install_blackhole() {
  old=$(current_mode)
  if [ "$old" != blackhole ]; then
    ip route replace blackhole "$DST_POD"
    note_change "$old" blackhole
    log "down $DST_POD (peer unreachable)"
  fi
}

# Parallel probe of a list of hosts on one interface.  fping is in the
# netshoot image; fall back to serial ping if it ever disappears.
probe_group() {
  iface=$1
  hosts=$2
  out=$3
  : > "$out"
  [ -z "$hosts" ] && return 0
  if [ -n "$HAVE_FPING" ]; then
    fping -I "$iface" -c2 -p200 -t1000 $hosts > "$out" 2>/dev/null || true
  else
    for host in $hosts; do
      if ping -I "$iface" -c2 -W1 -q "$host" >/dev/null 2>&1; then
        echo "$host : xmt/rcv/%loss = 2/2/0%" >> "$out"
      else
        echo "$host : xmt/rcv/%loss = 2/0/100%" >> "$out"
      fi
    done
  fi
}

host_alive() {
  out=$1
  host=$2
  line=$(grep -F "$host : xmt/rcv/%loss" "$out" 2>/dev/null | tail -1)
  received=$(echo "$line" | sed -n 's/.*= [0-9]*\/\([0-9]*\)\/.*/\1/p')
  [ -n "$received" ] && [ "$received" -gt 0 ]
}

# The 1xx BCM5720 (tg3), glemmen80's 10G (ixgbe), glemmen60's bnx2 and
# glemmen250's r8169 all support different ring names/maxima.  Set the
# hardware maximum where it is useful, log the actual result, and never claim
# success when ethtool rejected the request.
RING_DONE=""
set_rings_once() {
  iface=$1
  [ -z "$iface" ] && return
  case " $RING_DONE " in
    *" $iface "*) return ;;
  esac
  driver=$(ethtool -i "$iface" 2>/dev/null | sed -n 's/^driver: //p')
  ok=1
  case "$driver" in
    tg3)
      ethtool -G "$iface" rx 2047 rx-jumbo 1023 >/dev/null 2>&1 || ok=0
      ;;
    bnx2)
      ethtool -G "$iface" rx 2040 rx-jumbo 8160 >/dev/null 2>&1 || ok=0
      ;;
    ixgbe)
      ethtool -G "$iface" rx 4096 tx 8192 >/dev/null 2>&1 || ok=0
      ;;
    r8169)
      ethtool -G "$iface" rx 256 tx 256 >/dev/null 2>&1 || ok=0
      ethtool -K "$iface" tx on tso on gso on >/dev/null 2>&1 || true
      ;;
    *)
      maxrx=$(ethtool -g "$iface" 2>/dev/null \
        | awk '/Pre-set maximums:/{f=1;next} f && /^RX:/{print $2; exit}')
      if [ -n "$maxrx" ]; then
        ethtool -G "$iface" rx "$maxrx" >/dev/null 2>&1 || ok=0
      else
        ok=0
      fi
      ;;
  esac
  RING_DONE="$RING_DONE $iface"
  current=$(ethtool -g "$iface" 2>/dev/null \
    | awk '/Current hardware settings:/{f=1;next} f && /^(RX|TX|RX Jumbo):/{printf "%s=%s ", $1, $2}')
  if [ "$ok" = 1 ]; then
    log "ring sizes set on $iface (${driver:-unknown}): $current"
  else
    log "warning: failed to set rings on $iface (${driver:-unknown}): $current"
  fi
}

write_metrics() {
  tmp="$TEXTFILE_DIR/.ecmp.prom.$$"
  {
    echo "# HELP ecmp_agent_up Whether the ECMP route agent is running."
    echo "# TYPE ecmp_agent_up gauge"
    echo "ecmp_agent_up 1"
    echo "# HELP ecmp_agent_last_run_timestamp_seconds Last completed agent loop."
    echo "# TYPE ecmp_agent_last_run_timestamp_seconds gauge"
    echo "ecmp_agent_last_run_timestamp_seconds $(date +%s)"
    echo "# HELP ecmp_paths Number of installed nexthops for a peer pod CIDR."
    echo "# TYPE ecmp_paths gauge"
    echo "# HELP ecmp_expected_paths Number of paths expected for a peer."
    echo "# TYPE ecmp_expected_paths gauge"
    echo "# HELP ecmp_peer_up Whether a peer has a usable route."
    echo "# TYPE ecmp_peer_up gauge"
    echo "# HELP ecmp_route_flaps_total Route mode changes for a peer."
    echo "# TYPE ecmp_route_flaps_total counter"
    echo "# HELP ecmp_vlan7_up Whether the VLAN 7 path to a dual-link peer is alive."
    echo "# TYPE ecmp_vlan7_up gauge"
    for dst in $DESTINATIONS; do
      [ "$dst" = "$NODE_NAME" ] && continue
      set_peer "$dst" || continue
      mode=$(current_mode)
      case "$mode" in
        dual) paths=2 ;;
        single) paths=1 ;;
        *) paths=0 ;;
      esac
      expected=1
      if [ -n "$VLAN7_IFACE" ] && [ -n "$DST_V7" ]; then
        expected=2
      fi
      peer_up=0
      [ "$paths" -gt 0 ] && peer_up=1
      flaps=$(state_get "flaps.$DST")
      echo "ecmp_paths{peer=\"$DST\"} $paths"
      echo "ecmp_expected_paths{peer=\"$DST\"} $expected"
      echo "ecmp_peer_up{peer=\"$DST\"} $peer_up"
      echo "ecmp_route_flaps_total{peer=\"$DST\"} ${flaps:-0}"
      if [ -n "$DST_V7" ] && [ -n "$VLAN7_IFACE" ]; then
        echo "ecmp_vlan7_up{peer=\"$DST\"} $(state_get "vlan7.$DST")"
      fi
    done
  } > "$tmp" 2>/dev/null && mv "$tmp" "$TEXTFILE_DIR/ecmp.prom" 2>/dev/null || true
}

log "ECMP agent v2 on $NODE_NAME"

while true; do
  NEW_SERVER=$(iface_for_prefix '^192\.168\.6\.')
  [ -z "${NEW_SERVER}" ] && \
    NEW_SERVER=$(iface_for_prefix_any '^192\.168\.6\.')
  NEW_VLAN7=$(iface_for_prefix '^192\.168\.7\.')
  [ -z "${NEW_VLAN7}" ] && \
    NEW_VLAN7=$(iface_for_prefix_any '^192\.168\.7\.')

  if [ -n "${NEW_SERVER}" ]; then
    if [ "${NEW_SERVER}" != "${SERVER_IFACE:-}" ] || \
       [ "${NEW_VLAN7}" != "${VLAN7_IFACE:-}" ]; then
      SERVER_IFACE="${NEW_SERVER}"
      VLAN7_IFACE="${NEW_VLAN7}"
      VLAN7_PARENT="${VLAN7_IFACE%.*}"
      set_rings_once "$SERVER_IFACE"
      [ -n "$VLAN7_PARENT" ] && set_rings_once "$VLAN7_PARENT"
      log "interfaces: server=$SERVER_IFACE vlan7=${VLAN7_IFACE:-none}"
    fi
  else
    SERVER_IFACE=""
    VLAN7_IFACE=""
  fi

  # Keep this node's own pod block blackholed.  Felix used to install this
  # guard route; now that Felix route programming is disabled, unassigned
  # addresses in the block must not follow the default route.
  if [ -n "$LOCAL_POD" ] && ! route_text "$LOCAL_POD" | grep -Fq blackhole; then
    ip route replace blackhole "$LOCAL_POD" 2>/dev/null || true
  fi

  if [ -n "$SERVER_IFACE" ]; then
    VLAN7_HOSTS=""
    for dst in $DESTINATIONS; do
      [ "$dst" = "$NODE_NAME" ] && continue
      set_peer "$dst" || continue
      if [ -n "$VLAN7_IFACE" ] && [ -n "$DST_V7" ]; then
        VLAN7_HOSTS="$VLAN7_HOSTS $DST_V7"
      fi
    done

    VLAN7_OUT="$STATE_DIR/vlan7.out"
    probe_group "$VLAN7_IFACE" "$VLAN7_HOSTS" "$VLAN7_OUT"

    SERVER_HOSTS=""
    for dst in $DESTINATIONS; do
      [ "$dst" = "$NODE_NAME" ] && continue
      set_peer "$dst" || continue
      if [ -z "$VLAN7_IFACE" ] || [ -z "$DST_V7" ]; then
        SERVER_HOSTS="$SERVER_HOSTS $DST_SRV"
      elif ! host_alive "$VLAN7_OUT" "$DST_V7"; then
        SERVER_HOSTS="$SERVER_HOSTS $DST_SRV"
      fi
    done
    SERVER_OUT="$STATE_DIR/server.out"
    probe_group "$SERVER_IFACE" "$SERVER_HOSTS" "$SERVER_OUT"

    for dst in $DESTINATIONS; do
      [ "$dst" = "$NODE_NAME" ] && continue
      set_peer "$dst" || continue

      vlan7_ok=0
      if [ -n "$VLAN7_IFACE" ] && [ -n "$DST_V7" ]; then
        if host_alive "$VLAN7_OUT" "$DST_V7"; then
          vlan7_ok=1
        fi
        state_set "vlan7.$DST" "$vlan7_ok"
      fi

      if [ -z "$VLAN7_IFACE" ] || [ -z "$DST_V7" ]; then
        # Single-link peer: only the server VLAN path exists.
        if host_alive "$SERVER_OUT" "$DST_SRV"; then
          state_set "srvfail.$DST" 0
          srvok=$(bump "srvok.$DST")
          old=$(current_mode)
          if [ "$old" = blackhole ]; then
            [ "$srvok" -ge "$RESTORE_AFTER" ] && install_single
          elif [ "$srvok" -ge 1 ] && [ "$old" != single ]; then
            install_single
          fi
        else
          state_set "srvok.$DST" 0
          srvfail=$(bump "srvfail.$DST")
          [ "$srvfail" -ge "$COLLAPSE_AFTER" ] && install_blackhole
        fi
        continue
      fi

      # Dual-link peer.
      if [ "$vlan7_ok" = 1 ]; then
        state_set "fail.$DST" 0
        state_set "srvfail.$DST" 0
        ok=$(bump "ok.$DST")
        if [ "$ok" -ge "$RESTORE_AFTER" ] || [ "$(current_mode)" = missing ]; then
          install_dual
        fi
      elif host_alive "$SERVER_OUT" "$DST_SRV"; then
        state_set "fail.$DST" 0
        state_set "srvfail.$DST" 0
        srvok=$(bump "srvok.$DST")
        old=$(current_mode)
        if [ "$old" = blackhole ]; then
          [ "$srvok" -ge "$RESTORE_AFTER" ] && install_single
        else
          fail=$(bump "fail.$DST")
          if [ "$fail" -ge "$COLLAPSE_AFTER" ] && [ "$old" != single ]; then
            install_single
          fi
        fi
      else
        state_set "ok.$DST" 0
        state_set "srvok.$DST" 0
        srvfail=$(bump "srvfail.$DST")
        [ "$srvfail" -ge "$COLLAPSE_AFTER" ] && install_blackhole
      fi
    done
  fi

  # Safety net for pod blocks that are not in the map.  More specific /24
  # routes always win, so this only catches unknown blocks that would
  # otherwise leak to the default route.
  if ! route_text "10.42.0.0/16" | grep -Fq blackhole; then
    ip route replace blackhole 10.42.0.0/16 2>/dev/null || true
  fi

  sysctl -w net.ipv4.fib_multipath_hash_policy=1 >/dev/null 2>&1 || true
  sysctl -w net.ipv4.fib_multipath_use_neigh=1 >/dev/null 2>&1 || true

  write_metrics
  sleep 5
done
