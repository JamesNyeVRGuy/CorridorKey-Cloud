# CorridorKey Cloud

![Status](https://corridorkey.cloud/api/status/badge)

https://github.com/user-attachments/assets/1fb27ea8-bc91-4ebc-818f-5a3b5585af08


AI-powered green screen keying for professional VFX pipelines, running on a community-powered GPU farm. Upload your footage, get production-ready EXR output — no GPU required on your end.

**[corridorkey.cloud](https://corridorkey.cloud)**

## How It Works

Traditional keyers struggle with semi-transparent pixels — motion blur, hair, out-of-focus edges. They force you into hours of garbage mattes and manual rotoscoping. CorridorKey's neural network solves the *unmixing* problem: for every pixel, it predicts the true foreground color and a clean linear alpha channel, as if the green screen was never there.

1. **Upload** — Drag in your green screen video or image sequence. Any resolution, any length.
2. **Process** — The pipeline generates alpha hints automatically, then runs inference. Jobs are sharded across available GPUs in the community render farm.
3. **Download** — Get your keyed EXRs: premultiplied RGBA ready for Nuke, After Effects, DaVinci Resolve, or Blender.

### Output Passes

| Pass | Format | Description |
|------|--------|-------------|
| **Processed** | 4-channel EXR (linear, premultiplied RGBA) | Drop directly into any compositor |
| **FG** | 3-channel EXR (sRGB straight) | Raw foreground color |
| **Matte** | 1-channel EXR (linear) | Clean alpha channel |
| **Comp** | PNG (sRGB) | Quick preview over checkerboard |

## Getting Started

### Use the Cloud (Recommended)

1. Go to [corridorkey.cloud](https://corridorkey.cloud)
2. Sign up (open beta — tell us about yourself to speed up approval)
3. Upload your footage
4. Download your keyed results

No installation, no GPU, no Python. Processing happens on community-contributed GPU nodes.

### Self-Host the Server

Run your own CorridorKey Cloud instance on your local network or studio infrastructure.

**Docker Compose (recommended):**
```bash
git clone https://github.com/JamesNyeVRGuy/CorridorKey.git
cd CorridorKey/deploy
cp .env.example .env
# Edit .env — set your domain, auth settings, etc.
docker compose -f docker-compose.web.yml up -d
# Open http://localhost:3000
```

**From source:**
```bash
git clone https://github.com/JamesNyeVRGuy/CorridorKey.git
cd CorridorKey
uv sync --group dev --extra web --extra cuda
uv run uvicorn web.api.app:create_app --factory --port 3000
```

## Contributing a GPU Node

CorridorKey Cloud runs on a distributed render farm powered by the community. Connect your idle GPU as a node — it processes jobs for other users, and you earn credits to process your own footage.

### Option 1: Docker (Linux)

From the Nodes page in the web UI, select your GPU type (NVIDIA or AMD), generate a token, and copy the Docker Compose file. Or manually:

```yaml
services:
  corridorkey-node:
    image: ghcr.io/jamesnyevrguy/corridorkey-node:nvidia
    restart: unless-stopped
    labels:
      - com.centurylinklabs.watchtower.enable=true
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    environment:
      - CK_MAIN_URL=https://corridorkey.cloud
      - CK_AUTH_TOKEN=<your-token>
      - CK_NODE_NAME=my-node
      - CK_NODE_GPUS=auto
    volumes:
      - ck-weights:/app/CorridorKeyModule/checkpoints
      - ck-weights-gvm:/app/gvm_core/weights
      - ck-weights-vm:/app/VideoMaMaInferenceModule/checkpoints
      - ck-compile-cache:/app/.cache/corridorkey

  # Auto-updater: pulls new node images when you push a cloud tag
  watchtower:
    image: nickfedor/watchtower
    restart: unless-stopped
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=300
      - WATCHTOWER_LABEL_ENABLE=true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  ck-weights:
  ck-weights-gvm:
  ck-weights-vm:
  ck-compile-cache:
```

```bash
docker compose up -d
```

For **AMD GPUs**, use `ghcr.io/jamesnyevrguy/corridorkey-node:amd` and replace the `deploy` block with:
```yaml
    devices:
      - /dev/kfd
      - /dev/dri
    security_opt:
      - seccomp=unconfined
    group_add:
      - video
```

### Option 2: Standalone Binary (Windows & Linux)

Download the installer from [GitHub Releases](https://github.com/JamesNyeVRGuy/CorridorKey/releases). No Docker or Python needed.

1. Run the installer
2. Paste your server URL and auth token on first launch
3. The node runs in the system tray — shows status, credits earned, and GPU info
4. Auto-updates from GitHub Releases

The standalone binary ships with CPU torch and downloads GPU acceleration (CUDA or ROCm) on first launch.

### Node Configuration

| Variable | Default | Description |
|---|---|---|
| `CK_MAIN_URL` | `http://localhost:3000` | Server address |
| `CK_AUTH_TOKEN` | | Auth token from the Nodes page |
| `CK_NODE_NAME` | hostname | Display name in the UI |
| `CK_NODE_GPUS` | `auto` | GPU indices: `auto`, `0`, `0,1` |
| `CK_SHARED_STORAGE` | | Path to shared NAS mount (skips HTTP transfer) |
| `CK_NODE_PREWARM` | `true` | Pre-load model into VRAM on startup |
| `CK_NODE_ACCEPTED_TYPES` | | Comma-separated job types to accept (empty = all) |

### Render Farm Features

- **Auto-sharding** — large jobs split across all available GPUs and nodes
- **Multi-GPU** — nodes with multiple GPUs process jobs in parallel
- **GPU credit system** — contribute compute, earn processing credits
- **Shared storage** — mount the same NAS on server and nodes for zero-transfer processing
- **Auto weight sync** — nodes download model weights on first start
- **Watchtower auto-update** — Docker nodes pull new images automatically
- **Per-node scheduling** — set active hours for overnight rendering
- **Pause / resume** — stop accepting jobs without shutting down
- **Node health monitoring** — CPU, RAM, VRAM, job history, logs viewable from the web UI

## GPU Support

| GPU | VRAM | Status |
|-----|------|--------|
| NVIDIA GeForce RTX 30xx/40xx/50xx | 8GB+ | Full support (CUDA) |
| NVIDIA RTX Pro / Quadro | 8GB+ | Full support (CUDA) |
| AMD RX 7900 XTX / XT | 20-24GB | Supported (ROCm, Linux) |
| AMD RX 7800 XT | 16GB | Supported (ROCm, Linux with GTT fallback) |
| Apple Silicon M1+ | 8GB+ unified | Supported (MLX backend) |
| Intel ARC | | Community extension: [CorridorKeyOpenVINO](https://github.com/daniil-lyakhov/CorridorKeyOpenVINO) |

**Minimum VRAM:** 6-8GB for inference. CorridorKey processes at 2048x2048 internally and scales output back to your original resolution.

## Platform Features

### Web UI
- Drag-and-drop upload (video or image sequences)
- One-click full pipeline: extract → alpha hint → inference
- Real-time progress via WebSocket
- Frame viewer with A/B comparison, zoom, wipe mode
- Per-pass download (FG, Matte, Processed, Comp)
- Job queue with priority ordering
- Keyboard shortcuts (press `?`)

### Authentication & Multi-Tenancy
- Open signup with structured profile and admin approval
- Organization workspaces with per-org file isolation
- Trust tiers: pending, member, contributor, org admin, platform admin
- Per-tier resource limits (frame count, concurrent jobs, storage retention)
- GPU credit system — contribute compute to earn processing credits
- Automatic clip cleanup with tier-based retention policies

### Infrastructure
- Redis-backed stateless architecture for multi-instance deployment behind load balancers
- WebSocket pub/sub fan-out across server instances
- Distributed job reaper with Redis lock (single-writer safety)

### Monitoring
- Prometheus metrics endpoint (`/metrics`)
- Grafana dashboards (platform overview, node fleet, log explorer)
- Node reputation scoring (success rate, speed, uptime)
- Per-node health history graphs
- Status badge: ![Status](https://corridorkey.cloud/api/status/badge)

## Development

```bash
# Setup
uv sync --group dev --extra web --extra cuda

# Tests
uv run pytest                      # all tests
uv run pytest -m "not gpu"         # skip GPU tests (CI default)

# Lint & format
uv run ruff check                  # lint
uv run ruff format --check         # format check
```

For the CLI wizard (local processing without the cloud):
```bash
uv sync --group dev --extra cuda
uv run corridorkey wizard /path/to/clips
```

For detailed engine internals, see `/CorridorKeyModule/README.md` and `/docs/LLM_HANDOVER.md`.

Auto-generated codebase docs: [DeepWiki](https://deepwiki.com/nikopueringer/CorridorKey)

## Contributing

We welcome contributions — bug fixes, new features, GPU optimizations, documentation improvements.

### Getting Set Up

1. **Fork** the repository on GitHub
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/CorridorKey.git
   cd CorridorKey
   ```
3. **Install** dependencies:
   ```bash
   uv sync --group dev --extra web --extra cuda
   ```
4. **Create a branch** for your work:
   ```bash
   git checkout -b feat/my-feature
   ```

### Before Submitting a PR

Run the full check suite — CI will reject PRs that fail any of these:

```bash
uv run pytest -m "not gpu"     # tests pass (GPU tests skipped in CI)
uv run ruff check              # no lint errors
uv run ruff format --check     # code is formatted
```

Auto-fix formatting:
```bash
uv run ruff format             # auto-format
uv run ruff check --fix        # auto-fix safe lint issues
```

### PR Guidelines

- **One logical change per PR.** A bug fix + a feature = two PRs.
- **Write a clear title and description.** Explain *what* and *why*, not just *how*.
- **Include tests** for new functionality when possible. Tests live in `tests/`.
- **Don't break the NVIDIA path.** All AMD/ROCm code must be gated behind detection checks. Existing NVIDIA and MPS users should never notice your changes.
- **Vendored code** (`gvm_core/`, `VideoMaMaInferenceModule/`) is excluded from linting. Keep changes to these directories minimal.
- **Frontend** is SvelteKit 5 (runes mode) at `web/frontend/src/`. Follow the existing design system in `app.css`.

### Architecture Overview

| Directory | Purpose |
|-----------|---------|
| `CorridorKeyModule/` | Neural network model, inference engine, color math |
| `backend/` | Service layer — frame I/O, job queue, clip state |
| `web/api/` | FastAPI server — routes, auth, WebSocket, metrics |
| `web/frontend/` | SvelteKit UI |
| `web/node/` | Node agent — tray app, file transfer, weight sync |
| `web/shared/` | GPU subprocess worker (shared between server and node) |
| `device_utils.py` | Cross-platform GPU detection (NVIDIA, AMD, MPS) |
| `gvm_core/` | GVM alpha hint generator (vendored, upstream research code) |
| `VideoMaMaInferenceModule/` | VideoMaMa alpha hint generator (vendored) |
| `BiRefNetModule/` | BiRefNet salient object detector (vendored) |

### Test Markers

- `@pytest.mark.gpu` — requires CUDA GPU (skipped in CI)
- `@pytest.mark.mlx` — requires Apple Silicon + MLX
- `@pytest.mark.slow` — long-running tests

## Licensing

Use CorridorKey for whatever you'd like, including commercial projects. You MAY NOT repackage and sell it. Variations must remain under the same license and include the CorridorKey name.

You MAY NOT offer inference with this model as a paid API service. For commercial software integration, contact contact@corridordigital.com.

This license is a variation of [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).

## Credits

**CorridorKey** — Created by [Niko Pueringer](https://github.com/nikopueringer) / [Corridor Digital](https://www.youtube.com/@corridorcrew)

**CorridorKey Cloud** — Distributed GPU platform by [James Nye](https://github.com/JamesNyeVRGuy) and [DCRepublic](https://github.com/DCRepublic)

**Alpha Hint Generators:**
- **[GVM](https://github.com/aim-uofa/GVM)** — Generative Video Matting by the AIM research team at Zhejiang University. Licensed under [BSD-2-Clause](https://opensource.org/license/bsd-2-clause).
- **[VideoMaMa](https://github.com/cvlab-kaist/VideoMaMa)** — Video matting by CVLAB at KAIST. Licensed under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/). Model checkpoints subject to [Stability AI Community License](https://stability.ai/license).

By using these optional modules, you agree to their respective licenses.

## Community

- **Discord:** [Corridor Creates](https://discord.gg/44tHTSCGVQ)
- **Easy install UI:** [EZ-CorridorKey](https://github.com/edenaion/EZ-CorridorKey) by edenaion
- **Intel support:** [CorridorKeyOpenVINO](https://github.com/daniil-lyakhov/CorridorKeyOpenVINO) by daniil-lyakhov
