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

Same as stage 1 on any small VPS (2 vCPU / 4 GB is plenty without
local inference; 8 GB if you want Ollama on-box). Join it to your
tailnet and keep the port un-exposed publicly. All state lives in the
repo directory (`sessions/`); back it up with rsync.

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

The default config never needs a key. To use one, set `[model]` (live
interviewer — small and cheap is right) and/or `[analyze]` (one call
per session — strongest model you have):

```toml
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
forwards `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`).
