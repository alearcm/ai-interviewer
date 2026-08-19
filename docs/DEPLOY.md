# Deployment

The web pane is a single-user-friendly, multi-session-capable server
with **no auth of its own, by design**. The trust model: bind it to a
private network, and when you eventually want friends on it, put an
authenticating proxy in front and turn on the container run backend.
Nothing in the app needs rewriting between these stages.

## Stage 0 — laptop

```sh
python -m harness web          # http://127.0.0.1:8765
# or
docker compose up --build
```

The compose file publishes the port on 127.0.0.1 only and mounts
`sessions/`, `packs/`, and `config.toml` from the host, so transcripts
survive rebuilds and pack edits need no image rebuild.

## Stage 1 — home machine, reachable from your iPad (recommended)

1. Install [Tailscale](https://tailscale.com) on the home machine and
   on the iPad (App Store). Same tailnet, zero config.
2. Run the harness bound to the tailnet interface:

   ```sh
   python -m harness web --host 0.0.0.0
   ```

   With the compose file, change the ports line to
   `"8765:8765"` — the tailnet firewall is your perimeter; do NOT also
   forward the port on your router.
3. On the iPad, open `http://<machine-tailnet-name>:8765`.

The interviewer model: run Ollama on the same machine (see README),
or point `[model]` at a hosted key — presets below.

## Stage 2 — always-on VPS

Any small VPS works (1 GB RAM is enough without local inference — the
server idles under ~100 MB; 8 GB if you want Ollama on-box). Join it
to your tailnet and keep the port un-exposed publicly.

Bootstrap on a fresh Debian/Ubuntu box:

```sh
# as root, once
apt update && apt install -y git python3-venv curl ufw
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh          # prints a login URL; --ssh gives key-free
                            # SSH over the tailnet from your devices

# FIREWALL — REQUIRED. `--host 0.0.0.0` listens on the public IP too,
# and the pane has no auth (and /run executes commands). This closes
# everything public except SSH while allowing the whole tailnet:
ufw allow OpenSSH
ufw allow in on tailscale0
ufw --force enable

adduser --disabled-password --gecos "" harness && su - harness
git clone https://github.com/<you>/ai-interviewer && cd ai-interviewer
# (private repo: use https://<fine-grained-PAT>@github.com/<you>/ai-interviewer)
python3 -m venv .venv && ./.venv/bin/pip install -e ".[ui]"
# put your model key in the environment (see presets below), then:
./.venv/bin/harness web --host 0.0.0.0   # tailnet-only thanks to ufw above
```

Run it as a service — `/etc/systemd/system/harness-web.service`:

```ini
[Unit]
Description=interview harness web pane
After=network-online.target tailscaled.service

[Service]
User=harness
WorkingDirectory=/home/harness/ai-interviewer
Environment=OPENROUTER_API_KEY=...
Environment=ANTHROPIC_API_KEY=...
ExecStart=/home/harness/ai-interviewer/.venv/bin/harness web --host 0.0.0.0
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now harness-web`. (`WorkingDirectory` matters: the
repo directory is where `config.toml`, `packs/`, `webui/` and
`sessions/` resolve.)

Back up the only state that matters with a nightly cron:

```sh
0 3 * * * rsync -a /home/harness/ai-interviewer/sessions/ backup-host:harness-sessions/
```

## Migrating hosts

All durable state is the repo directory — specifically `sessions/`
(transcripts, reports, the recurrence log) plus your `config.toml`.
Moving machines is:

```sh
rsync -a old-host:ai-interviewer/sessions/ ai-interviewer/sessions/
scp old-host:ai-interviewer/config.toml ai-interviewer/
```

Transcripts are self-contained (each embeds its full pack), so a
session directory copied to any path on any machine regrades
byte-identically — `tests/test_portability.py` enforces this. Nothing
stores absolute paths; the recurrence log travels inside `sessions/`
and recurrence-weighted ordering picks up where it left off. ARM boxes
(Hetzner CAX, Oracle A1) work: the stack is pure Python, watchdog and
aiohttp ship aarch64 wheels, and the docker base image is multi-arch.

## Variant — public HTTPS + password, no Tailscale

If you'd rather have a normal URL with a login prompt than a private
network: any hostname that resolves to the server — a subdomain of a
domain you own (one A record at your registrar), or a free DuckDNS
subdomain if you have none — plus Caddy gives automatic Let's
Encrypt TLS and basic-auth in front of the pane. The harness itself
keeps its default 127.0.0.1 bind, so the auth cannot be bypassed by
hitting :8765 directly — only Caddy (80/443) and SSH are open.

1. Point the name at the server: at your registrar, add an A record
   (e.g. host `harness`, value = the server's public IPv4) — or on
   duckdns.org create `yourname.duckdns.org` with that IP.
2. On the server, after the standard app setup (user, venv, systemd
   unit with `ExecStart=... harness web` and NO --host flag):

```sh
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable

HASH=$(caddy hash-password)   # prompts for your chosen password
printf '%s {\n    basic_auth {\n        %s %s\n    }\n    reverse_proxy 127.0.0.1:8765\n}\n' \
    "yourname.duckdns.org" "yourlogin" "$HASH" > /etc/caddy/Caddyfile
systemctl reload caddy
```

Then open `https://yourname.duckdns.org` and log in. Notes: this is
ONE shared password over TLS guarding a pane whose run button executes
commands — make it long and random; upgrade to Cloudflare Access
(below) for per-person logins before sharing with friends.

## Stage 3 — friends (auth + sandboxing)

Two switches, in this order:

**1. Sandbox runs.** Anyone who can reach the web pane can execute
commands via the run button. Before sharing, flip the run backend in
`config.toml`:

```toml
[run]
backend = "container"
container_image = "python:3.12-slim"   # whatever your packs need
container_cpus = "1.0"
container_memory = "512m"
```

Every `/run` and self-check then executes inside
`docker run --rm --network none --pids-limit 256` with only the
session workspace mounted. (Known limit: a run that hits the hard
timeout kills the docker client; a wedged container can linger —
`docker container prune` cleans up. If the harness itself runs in
compose, mount the docker socket or run it on the host so it can spawn
sibling containers.)

**2. Put Cloudflare Access in front** (free tier, ≤50 users, no auth
to write ourselves):

```sh
cloudflared tunnel create harness
cloudflared tunnel route dns harness harness.yourdomain.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: harness
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: harness.yourdomain.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Then in the Cloudflare Zero Trust dashboard: Access → Applications →
add `harness.yourdomain.com`, policy = allowlist of your friends'
emails (one-time-PIN login works with any address; no accounts to
manage). Run `cloudflared tunnel run harness` as a service. No ports
forwarded, no public IP needed, TLS handled for you.

Notes for the friends stage:
- Sessions are not namespaced per user yet — everyone sees the same
  session list. Fine for a small trusted group; per-identity
  namespacing (from the `Cf-Access-Authenticated-User-Email` header
  Access injects) is the designed next increment.
- Raise or lower the concurrent-session cap with `[web] max_live`.
- A shared Ollama serializes under concurrent sessions; a hosted key
  (below) removes that ceiling for pennies.

## Hosted-model presets

Set `[model]` (live interviewer — small and cheap is right) and/or
`[analyze]` (one call per session — strongest model you have):

```toml
# Gemini free tier (Google's OpenAI-compatible endpoint) — the active
# default in config.toml; free-tier rate limits are far above what a
# session needs (a handful of calls per minute at most)
[model]
provider = "openai-compat"
base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
name = "gemini-3.5-flash-lite"
api_key_env = "GEMINI_API_KEY"

# OpenRouter (any OpenAI-compatible small model, ~a penny/session)
[model]
provider = "openai-compat"
base_url = "https://openrouter.ai/api/v1"
name = "qwen/qwen3-32b"
api_key_env = "OPENROUTER_API_KEY"

# Groq (very fast small models)
# base_url = "https://api.groq.com/openai/v1"
# name = "llama-3.3-70b-versatile"
# api_key_env = "GROQ_API_KEY"

# Anthropic for the live interviewer
# provider = "anthropic"
# name = "claude-haiku-4-5-20251001"

[analyze]
provider = "anthropic"
name = "claude-sonnet-5"        # ANTHROPIC_API_KEY
```

Export the key in the environment that runs the server (compose
forwards `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
`GROQ_API_KEY`). Never commit a key: `.env` is git-ignored, and
`config.toml` only ever names the env var.
