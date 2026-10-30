# AvatarWeave

**English** | [简体中文](README.zh-CN.md)

A real-time interactive avatar SDK, a configurable LiveKit Agent, and a standalone demo. The frontend provides core, renderer, and React packages; the backend combines cloud or self-hosted ASR, LLM, and TTS services. SQLite works out of the box, with PostgreSQL support available. The project maintains a single current API.

## Installation and startup

On Debian/Ubuntu Linux, run the following from the project root:

```bash
bash scripts/deploy.sh
```

The script installs system dependencies, Node.js, uv, Python, LiveKit Server, locked frontend and backend dependencies, and local models, then builds and starts the services. Existing `.env` files, databases, and model caches are preserved. If `.env` does not exist, the script creates a template that defaults to text-only mock mode without model credentials.

```bash
bash scripts/deploy.sh install  # Install, build, and deploy Nginx; start the app separately
bash scripts/deploy.sh start
bash scripts/deploy.sh restart
bash scripts/deploy.sh status
bash scripts/deploy.sh stop
```

Installation also supports `install --skip-system-deps` and `install --all-providers`. The first installation requires access to GitHub, PyPI, npm, and other package sources. Configure network proxies through standard environment variables. Logs are stored in `logs/`.

For real voice interaction, set `PIPELINE_MODE=pipeline` in `.env`, provide credentials for the selected providers, and choose a profile in `config/pipeline.yaml`. Use `PIPELINE_PROFILE` to override the profile explicitly, or `PIPELINE_CONFIG` to select a configuration file.

The deployed frontend supports HTTPS at the configured IP/domain (initially https://111.56.189.4/), with API documentation under `/docs`; HTTP remains available. Development access remains http://localhost:5174 and http://localhost:8090/docs. `scripts/dev.py` starts an already installed project; it does not install dependencies.

`install` (and the default `up`) installs Nginx through apt if missing, copies `infra/nginx.conf` to `/etc/nginx/conf.d/avatarweave.conf`, installs shared `infra/nginx-routes.conf` as `/etc/nginx/snippets/avatarweave-routes.conf`, and copies the frontend build to `/var/www/avatarweave/`. It moves the Debian/Ubuntu default site symlink to `/etc/nginx/avatarweave-default-site.backup`, checks `nginx -t`, then starts or reloads Nginx. On systemd hosts it also enables Nginx at boot. Repeating `install` updates the deployment. Other sites declaring `default_server` must be adjusted if they conflict. `--skip-system-deps` requires an existing Nginx installation; configuration deployment still requires root/sudo.

Installation enables both HTTPS and HTTP without redirecting HTTP. Nginx serves `/`, `/api/`, `/docs`, and `/realtime/`. Leave `RTC_PUBLIC_URL` empty to follow the current host and protocol (`ws` for HTTP, `wss` for HTTPS).

Configure public IP addresses or domain names in `infra/https.json` (`hosts`, optional contact `email`, and `fallback_self_signed`). The initial host is `111.56.189.4`. Installation requests a free trusted certificate using Certbot and Let's Encrypt HTTP-01 validation, accepting its terms (an empty email registers without email). Public certificates are stored under `/etc/letsencrypt/live/avatarweave/`. If issuance fails and fallback is enabled, OpenSSL creates a self-signed certificate with the configured IP/DNS SANs under `/etc/avatarweave/tls/`. Self-signed certificates require explicit browser trust.

To switch to a domain, point its DNS A record to this server, update `hosts`, and run `bash scripts/deploy.sh install`. No application changes or manual certificate paths are needed. The script generates `/etc/nginx/conf.d/avatarweave-https.conf` from `infra/nginx-tls.conf`; HTTP and HTTPS share `infra/nginx-routes.conf`. Open TCP 80/443 and media TCP 17881/UDP 50100–50200. HTTP-01 validation requires public access to port 80.

The `avatarweave-certificate.timer` checks every six hours, renews certificates when due, and validates/reloads Nginx. A self-signed fallback also retries public issuance and switches automatically when successful. Hosts without systemd use `/etc/cron.d/avatarweave-certificate`. Inspect the timer with `systemctl list-timers avatarweave-certificate.timer` and logs with `journalctl -u avatarweave-certificate.service`. Keep the checkout and `.tools/certbot` available for the renewal job.

## Hotel check-in speaking practice (mobile web MVP)

`apps/speaking` adds a mobile-first hotel roleplay with the existing cartoon avatar, three task goals, live captions, Chinese help, transcript-based feedback, and guest history. It reuses the backend and SDK packages. Feedback does not score pronunciation.

Run `python3 scripts/dev.py --restart --speaking` to restart tracked project services and include the product frontend. With the current branch's API, Agent and LiveKit already running, `npm run dev:speaking` starts only the page at http://localhost:5175/speaking/. `SPEAKING_API_URL` overrides the default API proxy at port 8090. Existing backend processes must be restarted to load the new routes. The root build adds this application under `apps/demo/dist/speaking/`, served at `/speaking/` after deployment.

Use **https://111.56.189.4/speaking/** (or your configured domain) for phone microphone access. It shares the original site's Nginx, TLS certificate renewal, API and WSS proxy. On an existing installation, `bash scripts/deploy.sh publish` builds and publishes both frontends and routes, validates and reloads Nginx, and retains existing certificates and hashed assets. It does not reinstall dependencies or restart the backend. Verify with `node scripts/browser-speaking-https-smoke.mjs`.

Guest credentials expire after 30 days and remain in browser localStorage. Each guest gets 50 practices with an assistant reply following learner input per UTC day; failed connections and greetings alone do not consume practice quota. Connection attempts are separately capped at 250 per day. Configure `PRACTICE_DAILY_LIMIT` and `PRACTICE_DAILY_ATTEMPT_LIMIT` in `.env`; `/api/practice/quota` supplies remaining counts and the reset time shown in the learner's local timezone. Sessions are capped at six minutes and twelve help requests. Captions remain synchronized with audio playback; service failures are shown explicitly. Clearing browser storage loses access to that guest's history. Signup limiting is per process; shared rate limiting and account/entitlement management remain necessary for a broad public launch.

After `npm run build`, run `backend/.venv/bin/python scripts/practice-smoke.py` for isolated LiveKit, temporary database, mock agent and mobile Chromium acceptance against the built application. Add `--pipeline` to exercise configured paid providers. Both modes clean up their test services. See [Chinese product instructions](README.zh-CN.md) for details.

## SDK and service boundaries

| Path | Responsibility |
| --- | --- |
| `packages/core` / `@avatarweave/core` | Rooms, text streams, audio binding, and the current action protocol |
| `packages/renderer` / `@avatarweave/renderer` | Model rendering, lip sync, actions, and lifecycle management |
| `packages/react` / `@avatarweave/react` | The `AvatarView` React component |
| `apps/demo` | Reference frontend built with the SDK |
| `backend/avatarweave_agent` | API, Agent, configuration, history, and inference orchestration |
| `backend/livekit/plugins` | Provider protocol adapters |
| `backend/kokoro-runtime-gpu` | Isolated GPU ONNX Runtime environment |
| `config` | Model profiles, avatars, and voice catalogs |
| `infra` | Infrastructure configuration |
| `scripts` | Installation, startup, model downloads, and verification |
| `docs` | Current architecture, protocol, schemas, and verification records |

```ts
import { AvatarClient } from '@avatarweave/core';
import { AvatarRenderer } from '@avatarweave/renderer';
import { AvatarView } from '@avatarweave/react';
```

```bash
npm run check
npm run pack:libs
```

Packaging produces three `avatarweave-*.tgz` archives in the project root. The Python application uses the `avatarweave_agent` namespace; Kokoro TTS is imported directly with `from livekit.plugins.kokoro import TTS`. The packages are not currently published to a public registry.

## Voice and avatar configuration

LiveKit provides real-time transport and the Agent framework. Model providers are selected independently:

```text
Microphone → LiveKit → ASR → LLM → TTS → LiveKit → Browser audio and lip sync
```

- `config/pipeline.yaml`: ASR/LLM/TTS providers, endpointing, turn detection, interruption, and preemptive generation settings.
- `config/avatars.yaml`: Names, asset URLs, the default avatar, and semantic action mappings.
- `config/voices.yaml`: Available voices and descriptions for each provider, extending the plugin catalogs.
- `.env`: Credentials, database settings, configuration paths, and local device settings. Public APIs do not expose these values.

The configuration includes profiles for OpenAI, Volcengine, Alibaba, self-hosted services, Cartesia, Deepgram/ElevenLabs, Google, and others, as well as OpenAI or Alibaba ASR paired with Kokoro TTS. Only environment variables for selected nodes are resolved. `LLM_BASE_URL`, `ASR_BASE_URL`, and `TTS_BASE_URL` configure the corresponding nodes where used by the YAML options. Provider availability depends on account permissions and access to the required resources.

Select an avatar and voice before establishing a session. These choices are passed to the Agent through a single `avatarweave_session` JSON envelope. Actions use only `avatarweave.command.v1` and are sent once; transcription uses the official Text Stream API. See the [protocol documentation](docs/protocol.md) for the full specification.

## Local Kokoro FP32

Local inference uses the unquantized `kokoro-v1.0.onnx` model and its voice files, verified against pinned SHA256 checksums and cached in `models/kokoro/`. ASR and LLM services continue to use their own configurations.

```dotenv
PIPELINE_MODE=pipeline
PIPELINE_PROFILE=openai-asr-kokoro-tts
OPENAI_API_KEY=your-api-key
KOKORO_PRECISION=fp32
KOKORO_DEVICE=gpu
KOKORO_GPU_ID=0
```

The GPU environment installs only `onnxruntime-gpu` and uses the machine's existing CUDA/cuDNN libraries. It **does not download or install CUDA/cuDNN**. If needed, point `KOKORO_CUDA_LIB_DIR` or `LD_LIBRARY_PATH` to existing libraries. GPU initialization failures raise an error; there is no silent fallback to the CPU.

```bash
# After changing devices, ensure the corresponding dependencies are installed.
bash scripts/deploy.sh install
python3 scripts/dev.py --restart --kokoro-device gpu --kokoro-gpu-id 0
python3 scripts/dev.py --restart --kokoro-device cpu
```

The GPU ID is the CUDA device index visible to the current process. `http://127.0.0.1:8880/health` reports the actual device, FP32 precision, model files, and execution providers.

Streaming follows this sequence: incremental text → short-phrase synthesis → PCM chunks sent to LiveKit. Each phrase must finish inference before its audio becomes available. The standard settings are `speed=1.0`, `min_phrase_chars=12`, and `max_phrase_chars=80`; refer to the YAML for the configured values. Chinese voices have fewer training samples, so preview them before choosing. The catalog includes 8 Chinese, 20 American English, and 8 British English voices; see the upstream voice cards for details.

Set `MOCK_TTS_PREVIEW=true` to enable TTS previews for the selected profile in mock mode; only the TTS configuration is resolved. Set `KOKORO_MANAGED=false` to use a separately deployed endpoint without having the local launcher create an inference process.

## Services and databases

| Service | Local ports |
| --- | --- |
| LiveKit | TCP 17880/17881, UDP 50100–50200 |
| API/Token | 127.0.0.1:8090 |
| Agent health check | 8091 |
| Demo frontend | 5174 |
| Kokoro, when selected | 127.0.0.1:8880 |

Cloud TTS profiles do not require local Kokoro. `compose.yaml` currently provides LiveKit and optional PostgreSQL; it does not containerize the API, Agent, frontend, and Kokoro together.

SQLite uses `avatar.db` by default. To use PostgreSQL:

```bash
docker compose --profile postgres up -d postgres
```

```dotenv
DATABASE_URL=postgresql+asyncpg://avatar:avatar@127.0.0.1:5433/avatar
```

`AGENT_HISTORY_LIMIT=100` controls how many recent messages are restored, with a supported range of 1–2000. The latest records are fetched first, then passed to the model in chronological order, preserving the latest context even when history exceeds 500 messages. History is scoped to a room; cross-room user memory and multi-tenant management are not provided.

Public deployments require a browser-accessible LiveKit address, HTTPS/WSS, media ports, and user authentication. Vite is used for development; deploy `apps/demo/dist` for the production frontend.

## Verification and maintenance

```bash
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/python scripts/config-schema.py --check
backend/.venv/bin/python scripts/check-config.py
node scripts/browser-config-smoke.mjs
node scripts/browser-voice-smoke.mjs
backend/.venv/bin/python scripts/voice-smoke.py --exercise-asr
```

The final two checks call the selected model services and may incur provider charges. CI covers builds, SQLite/PostgreSQL, the current protocol, and browser checks. Manual workflows are available for real CPU/GPU inference. Renovate dependency updates require integration with the remote repository platform.

[Architecture](docs/architecture.md) · [Protocol](docs/protocol.md) · [Extension guide](docs/extending.md) · [Maintenance](docs/maintenance.md) · [Verification records](docs/verification.md) · [Third-party sources and licenses](THIRD_PARTY_NOTICES.md)
