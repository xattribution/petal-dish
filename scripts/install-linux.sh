#!/usr/bin/env bash
# PETAL static hosting. Installs only the prebuilt browser app and its web server.
set -Eeuo pipefail

usage() {
  cat <<'HELP'
Usage: sudo bash install-linux.sh [--port 56302] [--ref main] [--localhost]

Installs PETAL and a dedicated Caddy service on systemd Linux.
Supported package managers: apt-get, dnf, yum, zypper, pacman.
Architectures: x86_64, aarch64/arm64, armv7l.
--port       TCP port 1-65535; default 56302.
--ref        GitHub branch, tag or commit; default main.
--localhost  Bind only 127.0.0.1 (default: all IPv4 interfaces).
--help       Show this help without changing the system.

Re-run to update the selected ref or change the port. Requires outbound HTTPS.
Firewall/router rules are not modified. No Node, npm, database or PDF service is needed.
HELP
}

render_config() {
  local root=$1 port=$2 bind=$3
  cat <<CONFIG
{
    admin off
    auto_https off
}
http://:$port {
    bind $bind
    root * $root
    encode zstd gzip
    header {
        Cache-Control "no-cache"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        -Server
    }
    file_server
}
CONFIG
}

install_dependencies() {
  if command -v apt-get >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl tar python3
  elif command -v dnf >/dev/null; then
    dnf install -y ca-certificates curl tar python3
  elif command -v yum >/dev/null; then
    yum install -y ca-certificates curl tar python3
  elif command -v zypper >/dev/null; then
    zypper --non-interactive install ca-certificates curl tar python3
  elif command -v pacman >/dev/null; then
    # Use the existing synchronized database; never trigger a full OS upgrade here.
    pacman -S --needed --noconfirm ca-certificates curl tar python
  else
    echo 'Unsupported package manager. See docs/SELF-HOSTING.md for manual static hosting.' >&2
    return 1
  fi
}

main() {
  local port=56302 ref=main bind=0.0.0.0 arch work sha version=2.11.4
  while (($#)); do
    case "$1" in
      --help|-h) usage; return 0 ;;
      --port|--ref)
        [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; return 2; }
        if [[ $1 == --port ]]; then port=$2; else ref=$2; fi
        shift 2 ;;
      --localhost) bind=127.0.0.1; shift ;;
      *) echo "Unknown option: $1" >&2; usage >&2; return 2 ;;
    esac
  done
  [[ $port =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1 && 10#$port <= 65535)) || { echo 'Port must be an integer from 1 to 65535.' >&2; return 2; }
  port=$((10#$port))
  [[ $ref =~ ^[A-Za-z0-9._/-]+$ && $ref != -* && $ref != *..* ]] || { echo 'Invalid GitHub ref.' >&2; return 2; }
  [[ $(uname -s) == Linux && -d /run/systemd/system ]] || { echo 'This installer requires Linux booted with systemd. See manual hosting instructions for containers/other init systems.' >&2; return 1; }
  [[ $EUID -eq 0 ]] || { echo 'Run this script with sudo or as root.' >&2; return 1; }
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=armv7 ;;
    *) echo 'Unsupported CPU architecture.' >&2; return 1 ;;
  esac
  install_dependencies
  work=$(mktemp -d /tmp/petal-install.XXXXXXXX)
  trap "rm -rf -- '$work'" EXIT
  local encoded
  encoded=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$ref")
  curl --fail --show-error --silent --location --retry 3 "https://api.github.com/repos/xattribution/petal-dish/commits/$encoded" -o "$work/commit.json"
  sha=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha"])' "$work/commit.json")
  [[ $sha =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid commit response from GitHub.' >&2; return 1; }
  curl --fail --show-error --silent --location --retry 3 "https://codeload.github.com/xattribution/petal-dish/tar.gz/$sha" -o "$work/petal.tar.gz"
  # Extract only checksum-listed, regular web files; never execute repository code.
  python3 - "$work/petal.tar.gz" "$work/web" <<'PY'
import hashlib,json,pathlib,sys,tarfile
archive,dest=sys.argv[1:]; dest=pathlib.Path(dest);dest.mkdir()
with tarfile.open(archive) as tar:
    manifests=[m for m in tar.getmembers() if m.name.endswith('/dist/BUILD.json') and m.isfile()]
    if len(manifests)!=1: raise SystemExit('Selected ref lacks the new build manifest. While PR #3 is unmerged, use --ref codex/integral-flange-petals.')
    member=manifests[0];prefix=member.name[:-len('BUILD.json')]
    data=tar.extractfile(member).read();meta=json.loads(data)
    if tuple(map(int,meta['version'].split('.'))) < (5,1): raise SystemExit('PETAL 5.1 or newer is required.')
    required={'index.html','app.bundle.js','style.css',meta['offline_file']}
    if not required.issubset(meta['files']): raise SystemExit('Incomplete web build manifest.')
    for name,digest in meta['files'].items():
        if pathlib.PurePosixPath(name).name!=name or name.startswith('.'): raise SystemExit('Unsafe build filename.')
        entry=tar.getmember(prefix+name)
        if not entry.isfile(): raise SystemExit('Web assets must be regular files.')
        content=tar.extractfile(entry).read()
        if hashlib.sha256(content).hexdigest()!=digest: raise SystemExit('Build checksum mismatch: '+name)
        (dest/name).write_bytes(content)
    (dest/'BUILD.json').write_bytes(data)
print('Verified PETAL',meta['version'],'build',meta['build'])
PY
  local asset="caddy_${version}_linux_${arch}.tar.gz"
  curl --fail --show-error --silent --location --retry 3 "https://github.com/caddyserver/caddy/releases/download/v${version}/$asset" -o "$work/$asset"
  curl --fail --show-error --silent --location --retry 3 "https://github.com/caddyserver/caddy/releases/download/v${version}/caddy_${version}_checksums.txt" -o "$work/checksums.txt"
  python3 - "$work" "$asset" <<'PY'
import hashlib,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1]);name=sys.argv[2]
expected=[line.split()[0] for line in (root/'checksums.txt').read_text().splitlines() if len(line.split())==2 and line.split()[1].lstrip('*')==name]
if len(expected)!=1 or hashlib.sha512((root/name).read_bytes()).hexdigest()!=expected[0]: raise SystemExit('Caddy checksum mismatch.')
with tarfile.open(root/name) as tar:
    entry=tar.getmember('caddy')
    if not entry.isfile(): raise SystemExit('Invalid Caddy archive.')
    (root/'caddy').write_bytes(tar.extractfile(entry).read())
PY
  chmod 755 "$work/caddy"
  render_config /opt/petal/current "$port" "$bind" > "$work/Caddyfile"
  "$work/caddy" validate --config "$work/Caddyfile" --adapter caddyfile
  # Refuse to overwrite unrelated applications at our fixed paths.
  if [[ -e /etc/systemd/system/petal.service ]] && ! grep -q '^# Managed by PETAL installer$' /etc/systemd/system/petal.service; then
    echo 'An unrelated petal.service already exists; leaving it untouched.' >&2; return 1
  fi
  if [[ -e /opt/petal && ! -f /opt/petal/.managed-by-petal ]]; then
    echo '/opt/petal exists without a PETAL ownership marker; leaving it untouched.' >&2; return 1
  fi
  install -d -m 755 /opt/petal/releases /etc/petal
  touch /opt/petal/.managed-by-petal
  local release="/opt/petal/releases/$sha" previous='' was_active=0
  [[ ! -L /opt/petal/current ]] || previous=$(readlink /opt/petal/current)
  systemctl is-active --quiet petal.service && was_active=1 || true
  [[ ! -f /etc/petal/Caddyfile ]] || cp /etc/petal/Caddyfile "$work/previous.Caddyfile"
  [[ ! -f /opt/petal/caddy ]] || cp /opt/petal/caddy "$work/previous.caddy"
  [[ ! -f /etc/systemd/system/petal.service ]] || cp /etc/systemd/system/petal.service "$work/previous.service"
  if [[ -e $release ]]; then release="$release-$(date +%s)-$$"; fi
  mv "$work/web" "$release"
  chmod -R a+rX "$release"
  install -m 755 "$work/caddy" /opt/petal/caddy.next
  mv -f /opt/petal/caddy.next /opt/petal/caddy
  install -m 644 "$work/Caddyfile" /etc/petal/Caddyfile
  ln -s "$release" /opt/petal/current.next
  mv -Tf /opt/petal/current.next /opt/petal/current
  cat > /etc/systemd/system/petal.service <<'UNIT'
# Managed by PETAL installer
[Unit]
Description=PETAL static generator
After=network.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=petal
Environment=XDG_CONFIG_HOME=/var/lib/petal/config
Environment=XDG_DATA_HOME=/var/lib/petal/data
ExecStart=/opt/petal/caddy run --config /etc/petal/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
RestrictSUIDSGID=yes
LockPersonality=yes
LimitNOFILE=32768

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  local good=0
  if systemctl restart petal.service; then
    for _ in {1..20}; do
      if curl --fail --silent --max-time 2 "http://127.0.0.1:$port/BUILD.json" -o "$work/live.json" && cmp -s "$release/BUILD.json" "$work/live.json" && systemctl is-active --quiet petal.service; then good=1;break;fi
      sleep 1
    done
  fi
  if (( ! good )); then
    echo 'Startup/health check failed. Restoring the previous PETAL configuration.' >&2
    systemctl stop petal.service || true
    [[ ! -f $work/previous.Caddyfile ]] || cp "$work/previous.Caddyfile" /etc/petal/Caddyfile
    [[ ! -f $work/previous.caddy ]] || cp "$work/previous.caddy" /opt/petal/caddy
    if [[ -n $previous ]]; then ln -s "$previous" /opt/petal/rollback.next;mv -Tf /opt/petal/rollback.next /opt/petal/current;fi
    if [[ -f $work/previous.service ]]; then cp "$work/previous.service" /etc/systemd/system/petal.service;else rm -f /etc/systemd/system/petal.service;fi
    systemctl daemon-reload
    if ((was_active)); then systemctl start petal.service || true;fi
    echo 'See: journalctl -u petal.service --no-pager -n 60' >&2
    return 1
  fi
  systemctl enable petal.service
  printf '\nPETAL is running: http://%s:%s\nCommit: %s\n' "${bind/0.0.0.0/<server-IP>}" "$port" "$sha"
  echo "Allow TCP $port in your firewall if needed. Settings and exports stay in the browser."
  echo 'Status: sudo systemctl status petal.service'
  echo 'Logs: sudo journalctl -u petal.service -n 60 --no-pager'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
