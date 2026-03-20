# MikroTik Matrix Bot

[![CI](https://github.com/webstudiobond/matrix-bot-mikrotik/actions/workflows/ci.yml/badge.svg)](https://github.com/webstudiobond/matrix-bot-mikrotik/actions/workflows/ci.yml)
[![GitHub last commit](https://img.shields.io/github/last-commit/webstudiobond/matrix-commander-rs-gateway)](https://github.com/webstudiobond/matrix-bot-mikrotik/commits/main)
[![GitHub issues](https://img.shields.io/github/issues/webstudiobond/matrix-commander-rs-gateway)](https://github.com/webstudiobond/matrix-bot-mikrotik/issues)
[![GitHub repo size](https://img.shields.io/github/repo-size/webstudiobond/matrix-commander-rs-gateway)](https://github.com/webstudiobond/matrix-bot-mikrotik)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A containerised MikroTik management bot that bridges Matrix rooms to MikroTik routers. Extends the [`matrix-commander-rs-gateway`](https://github.com/webstudiobond/matrix-commander-rs-gateway) base image and supports the full range of deployed MikroTik hardware.

---

## Table of Contents

- [Architecture](#architecture)
- [Transport Selection](#transport-selection)
- [Repository Layout](#repository-layout)
- [Requirements](#requirements)
- [Quick Start (pre-built image)](#quick-start-pre-built-image)
- [Host Setup](#host-setup)
- [Configuration](#configuration)
- [MikroTik API Credentials](#mikrotik-api-credentials)
- [Building Locally (development)](#building-locally-development)
- [Matrix Session Login](#matrix-session-login)
- [Running the Container](#running-the-container)
- [Matrix Command Reference](#matrix-command-reference)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Container (UID 10001, read-only rootfs)                     │
│                                                              │
│  PID 1: mikrotik_entrypoint.sh                               │
│   └── python3 bot.py   →  matrix-commander-rs listener       │
│                                │                             │
│                     ┌──────────┴──────────┐                  │
│                     ▼                     ▼                  │
│             REST API :443/80       RouterOS API :8728/8729   │
│             (RouterOS 7.1+)        (RouterOS 3.x – 6.x)      │
└──────────────────────────────────────────────────────────────┘
```

The entrypoint is PID 1. It starts the Python bot and restarts it on transient exit with exponential back-off.

---

## Transport Selection

The bot selects the router communication protocol automatically based on the `port` value in `config.yaml`. No extra flags are needed.

| Port | Protocol | RouterOS version |
|------|----------|-----------------|
| `443` or `80` | RouterOS REST API (HTTPS/HTTP) | 7.1+ |
| `8729` | RouterOS API over TLS (`librouteros`) | 6.x+ |
| `8728` (or any other) | RouterOS API plaintext (`librouteros`) | 3.x+ |

> Port `8728` is unencrypted. Use it only on isolated management networks.
> Prefer port `8729` for RouterOS 6.x routers whenever possible.

---

## Repository Layout

```
.
├── .github/
│   └── workflows/
│       ├── ci.yml              # lint and type-check on every push / PR
│       └── build.yml           # build and publish image on release / schedule
├── bot/
│   ├── bot.py                  # bot application logic
│   ├── pyproject.toml          # Python dependencies
│   └── uv.lock                 # locked dependency manifest — commit this
├── config/
│   └── routers_example.yaml    # copy to project root, rename to config.yaml, fill in, keep out of git
├── docker/
│   ├── Dockerfile
│   └── mikrotik_entrypoint.sh  # process supervisor
├── docker-compose.yaml         # production: pulls pre-built image from GHCR
├── docker-compose.dev.yaml     # development: builds image locally
├── .gitignore
└── README.md
```

---

## Requirements

| Tool | Minimum version |
|------|----------------|
| Docker Engine | 24.x (BuildKit enabled) |
| Docker Compose | v2.x |

No tools beyond Docker are required to run the pre-built image.

---

## Quick Start (pre-built image)

For end users who want to run the bot without building anything.

```bash
# 1. Create a working directory
mkdir matrix-bot-mikrotik && cd matrix-bot-mikrotik

# 2. Download the production compose file
curl -O https://raw.githubusercontent.com/webstudiobond/matrix-bot-mikrotik/main/docker-compose.yaml

# 3. Prepare directories and files — see Host Setup below
# 4. Complete Matrix Session Login — see Matrix Session Login below
# 5. Start
docker compose up -d
```

---

## Host Setup

Run these commands once in your working directory before starting the container. The container runs as **UID/GID 10001** — all writable mount points must be owned by that UID.

### 1. Persistent data directory

```bash
mkdir -p bot_data
sudo chown -R 10001:10001 bot_data
```

### 2. config.yaml

```bash
# Download the example config and rename it
curl -o config.yaml https://raw.githubusercontent.com/webstudiobond/matrix-bot-mikrotik/main/config/routers_example.yaml
sudo chown 10001:10001 config.yaml
chmod 0600 config.yaml
```

Fill in your router entries — see [Configuration](#configuration).

---

## Configuration

### config.yaml structure

The configuration file contains security settings, a command whitelist, and router connection details. Download the annotated example to get started:

```bash
curl -o config.yaml https://raw.githubusercontent.com/webstudiobond/matrix-bot-mikrotik/main/config/config_example.yaml
sudo chown 10001:10001 config.yaml && chmod 0600 config.yaml
```

Key sections:

| Section | Description |
|---------|-------------|
| `bot_user` | Full Matrix ID of the bot account — used to ignore its own messages |
| `command_room` | Room ID where commands are accepted |
| `admin_room` | Room ID for security alerts only — does not accept commands |
| `allowed_users` | List of Matrix user IDs permitted to issue commands |
| `allowed_commands` | Whitelist of RouterOS API paths the bot will execute |
| `routers` | Router connection details — `router_id` maps to host, port, credentials |

The `router_id` must match `[A-Za-z0-9_-]`, 1–64 characters.

```yaml
# Security
bot_user: "@mikrotik-bot:your.homeserver"
command_room: "!yourRoomId:your.homeserver"
admin_room: "!adminRoomId:your.homeserver"
allowed_users:
  - "@admin:your.homeserver"

# Command whitelist
allowed_commands:
  - "system/resource"
  - "ip/address"
  # ... see config_example.yaml for the full default list

# Routers
routers:

  # RouterOS 7.1+ — REST API over HTTPS, valid CA-signed certificate
  core-01:
    host: "192.168.88.1"
    port: 443
    username: "api-bot"
    password: "strong-random-password"
    tls_verify: true

  # RouterOS 7.1+ — REST API over HTTPS, self-signed certificate
  branch-02:
    host: "10.0.1.1"
    port: 443
    username: "api-bot"
    password: "another-strong-password"
    tls_verify: false       # TLS is still in use; certificate is not verified

  # RouterOS 6.x — RouterOS API over TLS (librouteros)
  office-03:
    host: "10.10.0.1"
    port: 8729
    username: "api-bot"
    password: "yet-another-password"
    tls_verify: false       # RouterOS 6.x uses a self-signed cert on 8729

  # RouterOS 3.x / 4.x / 5.x / 6.x — RouterOS API plaintext (librouteros)
  legacy-04:
    host: "172.16.5.1"
    port: 8728
    username: "admin"
    password: "old-password"
    tls_verify: false
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_WRITES` | `false` | Set to `true` to allow write (add/set) operations. Default is read-only. |
| `BOT_CONFIG` | `/home/bot/config/config.yaml` | Config path inside the container. No need to change. |
| `TARGETARCH` | `amd64` | Build target: `amd64` or `arm64`. For local dev builds only. |

Set persistent overrides in a `.env` file in the working directory:

```env
ALLOW_WRITES=false
```

---

## MikroTik API Credentials

Create a dedicated, least-privilege user on each router. Never use the `admin` account for automated access.

### Step 1 — Create an API-only group

Connect to the router via Winbox, WebFig, or SSH and run:

```routeros
# Read-only group (recommended default — matches ALLOW_WRITES=false)
/user group add name=api-readonly \
  policy=read,api,!local,!telnet,!ssh,!ftp,!reboot,!write,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon

# Read-write group (use only when ALLOW_WRITES=true)
/user group add name=api-readwrite \
  policy=read,write,api,!local,!telnet,!ssh,!ftp,!reboot,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon
```

### Step 2 — Create the API user

```routeros
/user add name=api-bot group=api-readonly password="strong-random-password"
```

### Step 3 — Restrict access by IP (strongly recommended)

Allow connections only from the container's host IP:

```routeros
/user set [find name=api-bot] address=192.168.1.100/32
```

### Step 4 — Enable the correct service per RouterOS version

**RouterOS 7.1+ (REST API, ports 80/443):**

```routeros
/ip service print
/ip service enable www-ssl
/ip service set www-ssl port=443
# Optional: disable plain HTTP if not needed
/ip service disable www
```

**RouterOS 6.x (RouterOS API, ports 8728/8729):**

Port 8728 (plaintext) and 8729 (TLS) are enabled by default. To verify:

```routeros
/ip service print
```

To enable TLS on port 8729, assign a certificate to the `api-ssl` service.
RouterOS can generate a self-signed one:

```routeros
/certificate add name=api-ssl common-name=api-ssl key-size=2048 \
  key-usage=key-cert-sign,crl-sign,tls-server days-valid=3650
/certificate sign api-ssl
/ip service set api-ssl certificate=api-ssl
/ip service enable api-ssl
```

**RouterOS 3.x – 5.x (RouterOS API plaintext, port 8728 only):**

Port 8728 is enabled by default. No additional setup required.

---

## Building Locally (development)

Clone the repository and build the image with the dev compose file.
The build context is the repository root; `docker/Dockerfile` is referenced explicitly.

### Generate uv.lock

`uv.lock` must exist in `bot/` before building. If you have `uv` installed locally:

```bash
cd bot && uv lock && cd ..
```

Or without installing anything on the host:

```bash
docker run --rm -v "$(pwd)/bot:/work" -w /work ghcr.io/astral-sh/uv:latest uv lock
```

Commit `uv.lock` to version control. The Docker build uses `--frozen` and
fails loudly if the lockfile is absent or out of sync with `pyproject.toml`.
Re-run `uv lock` whenever `pyproject.toml` changes.

### Build

```bash
# amd64 (default)
docker compose -f docker-compose.dev.yaml build

# arm64 (e.g. Raspberry Pi 4, Apple Silicon server)
TARGETARCH=arm64 docker compose -f docker-compose.dev.yaml build

# Force full rebuild (after base image update)
docker compose -f docker-compose.dev.yaml build --no-cache
```

---

## Matrix Session Login

The bot uses `matrix-commander-rs` for all Matrix communication. A session must be created once before the first start. The session is persisted in `bot_data/` and reused on every subsequent start.

### Step 1 — Create a dedicated bot account

Register a new Matrix account on your homeserver (e.g. via Element or the homeserver's admin panel). Use a dedicated account — do not use your personal account.

Example: `@mikrotik-bot:your.homeserver`

### Step 2 — Start the container

```bash
docker compose up -d
```

### Step 3 — Open a shell inside the container

```bash
docker compose exec -it matrix-bot-mikrotik /bin/bash
```

### Step 4 — Run the initial login

Inside the container
([full CLI reference](https://github.com/8go/matrix-commander-rs?tab=readme-ov-file#usage)):

```bash
matrix-commander-rs --login password \
  --homeserver "https://your.homeserver" \
  --user-login "@mikrotik-bot:your.homeserver" \
  --device "MikroTik Bot" \
  --password "YourStr0ng-Pa$$word" \
  --room-default "!yourRoomId:your.homeserver"
```

Session credentials are written to `bot_data/` on the host and persist across container restarts.

### Step 5 — Device verification (cross-signing)

After login, verify the bot device and your personal device to establish cross-signing trust. Run both commands inside the container:

```bash
# Verify the bot's own device
matrix-commander-rs --verify emoji-req \
  --user "@mikrotik-bot:your.homeserver" \
  --device "DEVICEIDHERE"

# Verify your personal device
matrix-commander-rs --verify emoji-req \
  --user "@you:your.homeserver" \
  --device "YOURDEVICEID"
```

Device IDs are printed in the console output during the login step. For full verification details see the [upstream documentation](https://github.com/8go/matrix-commander-rs?tab=readme-ov-file#usage).

### Step 6 — Exit and restart

```bash
exit
```

```bash
docker compose down && docker compose up -d
```

### Step 7 — Invite the bot to your room

In your Matrix client:

```
/invite @mikrotik-bot:your.homeserver
```

---

## Running the Container

```bash
# Start as a daemon
docker compose up -d

# Tail logs
docker compose logs -f

# Stop gracefully
docker compose down
```

---

## Matrix Command Reference

### How commands work

Every message in the room is inspected by the bot. Messages that do not start with `!mtik` are silently ignored — normal conversation is unaffected.

### Syntax

```
!mtik <router_id> <path> [=key=value ...]
```

| Part | Description |
|------|-------------|
| `!mtik` | Bot command prefix. Required. |
| `router_id` | Key from `config.yaml` — identifies which router to target. |
| `path` | RouterOS menu path with `/` separators (see below). |
| `=key=value` | Write parameters in RouterOS format. Requires `ALLOW_WRITES=true`. |

### Understanding the path

MikroTik organises all configuration as a menu tree. The path you use in a bot command is the same path you navigate in the RouterOS terminal — with `/` as a separator instead of a space. The `print` verb is implicit for read commands and is never typed.

| RouterOS terminal | Bot command |
|-------------------|-------------|
| `/ip address print` | `!mtik core-01 ip/address` |
| `/interface print` | `!mtik core-01 interface` |
| `/system resource print` | `!mtik core-01 system/resource` |
| `/ip firewall filter print` | `!mtik core-01 ip/firewall/filter` |
| `/ip address add address=X interface=Y` | `!mtik core-01 ip/address =address=X =interface=Y` |

> **Official API documentation:**
> - REST API (RouterOS 7.1+): https://help.mikrotik.com/docs/display/ROS/REST+API
> - RouterOS API (all versions): https://help.mikrotik.com/docs/display/ROS/API

Once you understand the path mapping, any resource visible in the RouterOS terminal can be queried directly — no need to memorise bot-specific syntax.

---

### Example: read — check system health

A quick way to confirm a router is reachable and see its load, uptime, and firmware version in one response.

```
!mtik core-01 system/resource
```

Equivalent terminal command:
```routeros
/system resource print
```

Example response:
```
✅ `core-01` → `system/resource`
[
  {
    "uptime": "15d2h34m12s",
    "version": "7.14.3 (stable)",
    "cpu-load": "3",
    "free-memory": "198901760",
    "total-memory": "268435456",
    "architecture-name": "arm64"
  }
]
```

---

### Example: write — add an IP address to an interface

Requires `ALLOW_WRITES=true`. Write commands modify the running configuration immediately — there is no confirmation prompt.

```
!mtik core-01 ip/address =address=10.99.0.1/24 =interface=ether2
```

Equivalent terminal command:
```routeros
/ip address add address=10.99.0.1/24 interface=ether2
```

Example response:
```
✅ `core-01` → `ip/address =address=10.99.0.1/24 =interface=ether2`
[{"ret": "*6"}]
```

`ret` is the internal ID assigned to the new entry by RouterOS. Verify the result with a follow-up read:

```
!mtik core-01 ip/address
```

---

### Error responses

| Situation | Response |
|-----------|---------|
| Unknown `router_id` | `❌ Unknown router \`xyz\`. Known IDs: core-01, branch-02` |
| Router unreachable | `❌ Router \`core-01\`: Cannot reach 192.168.88.1:443: ...` |
| Auth failure | `❌ Router \`core-01\`: Authentication failed — check credentials in config.yaml` |
| Write blocked | `❌ Router \`core-01\`: Write operations are disabled. Set ALLOW_WRITES=true to enable.` |
| Invalid path characters | `❌ Router \`core-01\`: Invalid API path: '../../etc'` |
| RouterOS API trap | `❌ Router \`legacy-04\`: RouterOS API trap: no such command` |

---

## Troubleshooting

**Container exits immediately — no output**

`bot_data/` is likely empty (no Matrix session). Complete the [Matrix Session Login](#matrix-session-login) steps first.

---

**Bot is running but does not respond to commands**

1. Confirm the bot account is a member of the Matrix room.
2. Verify `bot_data/` contains a valid session (non-empty directory).
3. Check for errors in the logs:

```bash
docker compose logs -f | grep -iE "error|warn|critical"
```

---

**Docker build fails: `frozen lockfile` error**

The lockfile is missing or out of sync with `pyproject.toml`. Regenerate it:

```bash
cd bot && uv lock && cd ..
docker compose -f docker-compose.dev.yaml build --no-cache
```

---

**RouterOS API returns 401**

- Check `username` and `password` in `config.yaml`.
- Confirm the user has the `api` policy:

```routeros
/user print detail where name=api-bot
```

- If an IP restriction is set, confirm the container host's IP is included:

```routeros
/user print detail where name=api-bot
```

---

**Legacy router (port 8728) connects but returns no data**

RouterOS 3.x/4.x may require enabling the API service explicitly:

```routeros
/ip service enable api
/ip service set api port=8728
```
