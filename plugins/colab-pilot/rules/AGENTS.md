# Colab Pilot Guardrails

- **Never invoke TTY-blocking `colab` commands**: Do not run `colab drivemount`, `colab auth`, `colab pay`, interactive `colab repl`, or bare `colab ssh` (which allocates a VM and waits on a TTY). Use `colab_mount_rclone` for headless Drive access and `colab ssh -s <name> -- '<cmd>' </dev/null` for one-shot shell commands.
- **Zero-waste Compute Unit (CU) discipline**: Never rent a GPU VM while running CPU-only extraction (`ffmpeg`) or slow multi-GB uploads. Validate archives and manifests locally first, and stage `>24 MiB` inputs to Google Drive at `0.00 CU` via `colab_smart_transfer` (`mode="drive_stage"`) before calling `colab_allocate_cascade`.
- **Never rely on long-lived `colab exec` WebSockets**: Runs exceeding ~5 minutes risk `RuntimeError: Connection was lost` and `\r` `tqdm` line-buffering freezes. Launch via `colab_launch_detached` and monitor via `colab_poll_job` or `scripts/colab_watchdog.py`.
- **Hardware & OOM safety**:
  - Verify `bf16_supported` before using `torch.bfloat16` (`T4` lacks native BF16).
  - Set `DataLoader(num_workers=0)` when datasets are preloaded in System RAM to avoid Linux fork OOM-killer crashes.
  - Call `colab_reset_vram` to clear `sys.last_traceback` and IPython `Out` references before retrying after a CUDA OOM or cell exception.
  - On short runs, guard EMA decay with warmup (`decay = min(0.9995, (1 + step) / (10 + step))`).
- **Zero-orphan guarantee**: Confirm `Active assignments: 0` (`0.00/hr`) via `colab_stop_all` or `colab usage` before finishing unless the session holds an explicit `colab_keep_warm` lease.
