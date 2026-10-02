# Self-host PETAL on Linux

PETAL runs entirely in the browser. The server only delivers static files; models, STL/ZIP exports and the illustrated PDF are generated on the user's device. No database, Node runtime, printer software or PDF daemon is required on the server.

## One-shot installation

Download [install-linux.sh](../scripts/install-linux.sh), then run:

```bash
sudo bash install-linux.sh --port 56302 --ref main
```

Or download and install in one command (requires curl for this initial download):

```bash
curl -fL https://raw.githubusercontent.com/xattribution/petal-dish/main/scripts/install-linux.sh -o /tmp/petal-install.sh && sudo bash /tmp/petal-install.sh --port 56302 --ref main
```

The commands install the current merged release from `main`. A commit SHA or tag can be used instead of a branch for repeatable installs.

Change `56302` to your preferred TCP port (1-65535). Open `http://SERVER-IP:56302`. Add `--localhost` to bind only 127.0.0.1, for example behind an existing reverse proxy.

The downloaded script installs its remaining dependencies. If you start from a downloaded file, Bash and sudo/root are the only bootstrap requirements. The single curl command also needs curl installed beforehand.

## What it installs

Supported: systemd Linux on x86_64, ARM64 or ARMv7, using apt-get (Debian/Ubuntu), dnf/yum (Fedora/RHEL-family), zypper (openSUSE), or pacman (Arch). Arch uses your existing package database; update the OS normally first if it is out of date. The installer does not perform a full OS upgrade. Unsupported init systems/architectures stop with an explanation.

- Installs curl, CA certificates, tar and Python 3 through the system package manager. Python is only used during installation to validate/extract downloads.
- Resolves the selected GitHub ref to one commit, downloads that archive, and verifies the prebuilt web files against BUILD.json. It refuses old builds rather than silently hosting an older interface.
- Downloads Caddy 2.11.4 from its official GitHub release and verifies its SHA-512 against the release checksum list. Caddy is isolated at `/opt/petal/caddy`; existing web-server configuration is untouched.
- Installs static releases under `/opt/petal/releases/COMMIT` (with a suffix on repeat installs), with `/opt/petal/current` pointing to the active release.
- Writes `/etc/petal/Caddyfile` and a dedicated `/etc/systemd/system/petal.service`, running as a systemd dynamic user. The service starts now and at boot. A failed startup/health check restores the prior PETAL configuration and active release.
- Sends `Cache-Control: no-cache`, so clients revalidate assets. The interface shows the version/build ID and has a versioned offline download. BUILD.json exposes the exact installed build.

Package-manager changes are not rolled back. Previous static releases are retained for recovery; there is no automatic OS, Caddy or application update process.

## Network access

The default listens on all IPv4 interfaces over **HTTP**. The installer does not change firewall rules, router forwarding, DNS or TLS. On a filtered host, allow your chosen TCP port using your existing firewall policy. For internet access, put it behind your normal HTTPS reverse proxy; `--localhost` is suitable when that proxy is on the same host. This is a public static application with no login, uploads or server-side model storage.

## Updating or changing ports

Re-run the installer with the desired ref and port. To pin a build, use its full commit SHA. Check the header build ID or:

```bash
curl http://127.0.0.1:56302/BUILD.json
sudo systemctl status petal.service
sudo journalctl -u petal.service -n 60 --no-pager
```

If another application already uses the chosen port, select a free one and re-run. Do not stop an unrelated service to make room. System logs show bind/startup errors.

To roll back manually, stop PETAL, point `/opt/petal/current` to a retained release, and start it again. The release must match its bundled index, JavaScript, CSS and offline file; do not mix assets from different builds.

## Offline edition and stale copies

The current standalone download is `petal-5.3-offline.html`. It contains both the geometry and PDF engines. Open that file itself in a browser; a mail/file-manager HTML preview may disable JavaScript. Expect **PETAL 5.3**, a build ID, and **flange joints** in the header, seven default printed parts, and an Assembly manual PDF button. Legacy strap/plate selectors indicate an older file.

The old hosted `petal-offline.html` URL redirects to the versioned download. That small redirect is not itself an offline app. Previously downloaded files do not update themselves: download the new version explicitly and remove or rename old copies.

## Manual hosting / containers

Serve the complete verified `dist/` build from an existing static server, preferably with revalidation enabled. For Caddy:

```caddyfile
http://:56302 {
    root * /absolute/path/to/petal-dish/dist
    header Cache-Control "no-cache"
    file_server
}
```

A container should expose TCP 56302 and mount the static build read-only. The installer intentionally requires a systemd host; it does not install an init system inside containers. To rebuild from source, use a supported Node.js version satisfying package-lock dependencies, run `npm ci && npm run build`, then serve the generated files together. Node is unnecessary when hosting the committed prebuilt distribution.

## Removal

```bash
sudo systemctl disable --now petal.service
sudo rm /etc/systemd/system/petal.service
sudo systemctl daemon-reload
```

After confirming you no longer need the installation or retained releases, remove `/opt/petal`, `/etc/petal` and `/var/lib/petal`. The installer does not remove system packages shared with other applications.

Caddy references: https://caddyserver.com/docs/quick-starts/static-files and https://github.com/caddyserver/caddy/releases/tag/v2.11.4
