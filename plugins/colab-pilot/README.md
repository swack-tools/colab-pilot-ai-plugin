# Colab Pilot

Zero-waste Google Colab orchestration for Antigravity: GPU cascade provisioning, 0-CU Google Drive pre-staging, WebSocket-proof detached jobs, Compute Unit leak guardrails, parallel CPU sharding, and a live MCP App dashboard.

## Components

- **MCP Server (`pilot`)**:
  - `colab_dashboard`: Interactive MCP App UI (`Colab Pilot`) in the conversation or side pane showing live CU balance, burn rate, 1/5th monthly Ultra budget guardrail, active sessions, detached jobs, and alarm controls.
  - `colab_allocate_cascade`: Negotiates GPU tiers (`A100-highmem -> A100 -> G4 -> L4`), verifies BF16 support, and probes VRAM/System RAM/NVENC.
  - `colab_mount_rclone`: Headless `rclone` Drive mounting (`random_seek` vs `streaming`) with `.env` secret forwarding (`HF_TOKEN`).
  - `colab_smart_transfer`: Routes `<=24 MiB` direct, `>24 MiB` via 0-CU Drive staging or 24 MiB chunked upload with SHA-256 verification.
  - `colab_launch_detached` & `colab_poll_job`: `nohup` detached execution with `TQDM_MININTERVAL=5`, `\r -> \n` normalization, and 10s GPU/VRAM heartbeat JSON polled via REST.
  - `colab_reset_vram`: Clears `sys.last_traceback`, IPython `Out` caches, and `torch.cuda.empty_cache()`.
  - `colab_keep_warm` & `colab_stop_all`: Manages keep-warm leases and guarantees `0.00/hr` teardown.
  - `colab_wakeup_alarm`: Escalating multi-voice macOS TTS + chirp wakeup alarm.
- **Custom Agent (`colab-operator`)**: Autonomous Colab SRE that executes the 6-phase zero-waste protocol.
- **Lifecycle Hooks (`hooks.json`)**:
  - `SessionStart`: Surfaces active Colab sessions and CU burn rate on startup.
  - `PreToolUse`: Blocks TTY-hanging `colab drivemount`/`auth`/`pay`/bare `repl`/bare `ssh`, invalid `--gpu` tiers, and `>30 MiB` direct `colab upload`.
  - `Stop`: Prevents ending a turn with unleased active Colab VMs burning CUs.
- **Slash Commands / Skills**: `/colab-pilot`, `/colab-train`, `/colab-shard-cpu`, `/colab-watch`.
- **Always-On Rules (`rules/AGENTS.md`)**: Enforces 0-CU staging, `num_workers=0` for RAM-cached datasets, BF16 checks, and zero-orphan teardown.
