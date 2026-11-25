#!/usr/bin/env python3
"""Escalating multi-voice macOS TTS + chirp wakeup alarm for Colab Pilot.

Supports:
  --action start [--daemon]   Start alarm (or spawn detached daemon)
  --action stop               Stop running alarm via flag file + SIGTERM
  --action status             Print JSON status of alarm
  --action self-test          Synthesize 3 distinct voices + WAVs in temp dir without audio
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

STOP_FLAG = Path("/tmp/stop_wakeup_alarm")
DEFAULT_PID_FILE = Path("/tmp/colab_pilot_alarm.pid")
STATE_FILE = Path("/tmp/colab_pilot_alarm.json")

_PREFERRED_VOICES = [
    "Samantha (English (US))",
    "Daniel",
    "Karen",
    "Moira",
    "Rishi",
    "Fred",
    "Albert",
    "Aman",
    "Tara",
    "Ralph",
    "Kathy",
    "Junior",
    "Superstar",
    "Reed (English (US))",
    "Rocko (English (US))",
    "Shelley (English (US))",
    "Sandy (English (US))",
    "Eddy (English (US))",
    "Flo (English (US))",
    "Grandma (English (US))",
    "Grandpa (English (US))",
    "Jester",
    "Whisper",
    "Zarvox",
    "Good News",
    "Bad News",
    "Organ",
    "Bells",
    "Boing",
    "Bubbles",
    "Cellos",
    "Wobble",
]


def discover_english_voices() -> list[str]:
    """Query `say -v ?` for available English voices on macOS, or fallback on headless Linux."""
    if not shutil.which("say"):
        return list(_PREFERRED_VOICES[:8])
    res = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    available: list[str] = []
    seen: set[str] = set()
    for line in res.stdout.splitlines():
        m = re.match(r"^(.+?)\s{2,}(en_(?:US|GB|AU|IN|IE|SC|ZA))\s+#", line)
        if not m:
            continue
        name = m.group(1).strip()
        if name in {"Bahh"} or name in seen:
            continue
        seen.add(name)
        available.append(name)

    ordered = [v for v in _PREFERRED_VOICES if v in seen]
    extras = [v for v in available if v not in set(ordered)]
    return ordered + extras if (ordered or extras) else ["Samantha"]


def compute_schedule_params(progress: float) -> dict[str, float | int]:
    """Compute volume, speech WPM, beep count/timing, and pause from progress in [0, 1]."""
    p = max(0.0, min(1.0, float(progress)))
    volume = round(0.04 + 1.96 * (p ** 1.35), 4)
    wpm = int(round(140 + 220 * p))
    n_beeps = 1 + int(5 * p)
    beep_dur = round(0.18 - 0.125 * p, 4)
    beep_gap = round(0.15 - 0.115 * p, 4)
    f_low = round(660.0 + 660.0 * p, 1)
    pause_sec = round(max(0.05, 2.0 * ((1.0 - p) ** 1.6)), 4)
    return {
        "progress": round(p, 4),
        "volume": volume,
        "wpm": wpm,
        "n_beeps": n_beeps,
        "beep_dur": beep_dur,
        "beep_gap": beep_gap,
        "f_low": f_low,
        "pause_sec": pause_sec,
    }


def write_beep_wav(
    out_wav: Path,
    n_beeps: int,
    beep_dur: float,
    beep_gap: float,
    f_low: float,
    sample_rate: int = 22050,
) -> float:
    """Synthesize a multi-beep PCM WAV file and return its total duration in seconds."""
    frames: list[int] = []
    fade_samples = max(1, int(0.008 * sample_rate))
    beep_samples = max(fade_samples * 2 + 1, int(beep_dur * sample_rate))
    gap_samples = max(0, int(beep_gap * sample_rate))

    for idx in range(n_beeps):
        freq = f_low if (idx % 2 == 0) else (f_low * 1.25)
        for s in range(beep_samples):
            t = s / sample_rate
            env = 1.0
            if s < fade_samples:
                env = 0.5 * (1.0 - math.cos(math.pi * s / fade_samples))
            elif s >= beep_samples - fade_samples:
                rem = beep_samples - 1 - s
                env = 0.5 * (1.0 - math.cos(math.pi * rem / fade_samples))
            val = 0.92 * env * math.sin(2.0 * math.pi * freq * t)
            frames.append(int(max(-32767, min(32767, round(val * 32767.0)))))
        if idx < n_beeps - 1 and gap_samples > 0:
            frames.extend([0] * gap_samples)

    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return len(frames) / float(sample_rate)


def _pick_next_voice(pool: list[str], deck: list[str], last_voice: str | None) -> tuple[str, list[str]]:
    if not deck:
        deck = list(pool)
        random.shuffle(deck)
        if last_voice and len(deck) > 1 and deck[0] == last_voice:
            deck[0], deck[-1] = deck[-1], deck[0]
    return deck.pop(0), deck


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_status(pid_file: Path) -> dict[str, object]:
    state: dict[str, object] = {}
    if STATE_FILE.is_file():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    pid = int(state.get("pid", 0) or 0)
    if pid <= 0 and pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
    alive = _is_pid_alive(pid)
    return {
        "running": alive,
        "pid": pid if alive else None,
        "phrase": state.get("phrase"),
        "started_at": state.get("started_at"),
        "ramp_seconds": state.get("ramp_seconds"),
        "max_seconds": state.get("max_seconds"),
        "stop_flag_exists": STOP_FLAG.exists(),
    }


def run_self_test() -> dict[str, object]:
    """Verify voice diversity and accelerating beep+TTS synthesis without playing audio."""
    voices = discover_english_voices()
    assert len(voices) >= 3, f"Expected >= 3 voices, found {voices}"
    has_say = bool(shutil.which("say"))

    tmp_dir = Path(tempfile.mkdtemp(prefix="colab_alarm_selftest_"))
    try:
        chosen: list[str] = []
        durations: list[float] = []
        deck: list[str] = []
        last_voice: str | None = None
        for idx, prog in enumerate((0.0, 0.5, 1.0)):
            voice, deck = _pick_next_voice(voices, deck, last_voice)
            last_voice = voice
            chosen.append(voice)

            sched = compute_schedule_params(prog)
            wav_p = tmp_dir / f"beep_{idx}.wav"
            beep_sec = write_beep_wav(
                wav_p,
                n_beeps=int(sched["n_beeps"]),
                beep_dur=float(sched["beep_dur"]),
                beep_gap=float(sched["beep_gap"]),
                f_low=float(sched["f_low"]),
            )
            assert wav_p.stat().st_size > 100
            if has_say:
                aiff_p = tmp_dir / f"tts_{idx}.aiff"
                subprocess.run(
                    ["say", "-v", voice, "-r", str(sched["wpm"]), "-o", str(aiff_p), "0 0 0 0 0 Colab job complete"],
                    check=True,
                    capture_output=True,
                )
                assert aiff_p.stat().st_size > 100
            durations.append(float(sched["pause_sec"]) + beep_sec / max(1, int(sched["n_beeps"])))

        assert len(set(chosen)) == 3, f"Voices were not distinct: {chosen}"
        assert durations[2] < durations[0], f"Expected faster cadence at prog=1.0: {durations}"
        return {
            "ok": True,
            "tts_engine": "macos_say" if has_say else "headless_wav_only",
            "voice_count": len(voices),
            "tested_voices": chosen,
            "cadence_seconds": [round(d, 4) for d in durations],
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def stop_alarm(pid_file: Path) -> dict[str, object]:
    STOP_FLAG.touch()
    pid = 0
    if STATE_FILE.is_file():
        try:
            pid = int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("pid", 0) or 0)
        except Exception:
            pid = 0
    if pid <= 0 and pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
    killed = False
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            pass
    for p in (pid_file, STATE_FILE):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    return {"ok": True, "stopped": True, "killed_pid": pid if killed else None}


def run_alarm_loop(args: argparse.Namespace) -> None:
    if STOP_FLAG.exists():
        STOP_FLAG.unlink(missing_ok=True)
    pid_file = Path(args.pid_file)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    STATE_FILE.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "phrase": args.phrase,
                "started_at": time.time(),
                "ramp_seconds": args.ramp_seconds,
                "max_seconds": args.max_seconds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    voices = discover_english_voices()
    tmp_dir = Path(tempfile.mkdtemp(prefix="colab_alarm_"))
    running = True

    def _handle_sig(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    start_ts = time.monotonic()
    deck: list[str] = []
    last_voice: str | None = None
    step = 0

    try:
        while running:
            if STOP_FLAG.exists():
                break
            elapsed = time.monotonic() - start_ts
            if elapsed >= args.max_seconds:
                break

            progress = min(1.0, elapsed / max(1.0, args.ramp_seconds))
            sched = compute_schedule_params(progress)
            voice, deck = _pick_next_voice(voices, deck, last_voice)
            last_voice = voice

            wav_p = tmp_dir / "beep.wav"
            aiff_p = tmp_dir / "tts.aiff"
            write_beep_wav(
                wav_p,
                n_beeps=int(sched["n_beeps"]),
                beep_dur=float(sched["beep_dur"]),
                beep_gap=float(sched["beep_gap"]),
                f_low=float(sched["f_low"]),
            )
            subprocess.run(
                ["say", "-v", voice, "-r", str(sched["wpm"]), "-o", str(aiff_p), args.phrase],
                check=True,
            )
            if not STOP_FLAG.exists() and running:
                subprocess.run(["afplay", "-v", str(sched["volume"]), str(wav_p)], check=False)
            if not STOP_FLAG.exists() and running:
                subprocess.run(["afplay", "-v", str(sched["volume"]), str(aiff_p)], check=False)

            step += 1
            sleep_rem = float(sched["pause_sec"])
            while running and sleep_rem > 0 and not STOP_FLAG.exists():
                dt = min(0.1, sleep_rem)
                time.sleep(dt)
                sleep_rem -= dt
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for p in (pid_file, STATE_FILE):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Escalating multi-voice macOS wakeup alarm")
    p.add_argument("--action", choices=("start", "stop", "status", "self-test"), default="status")
    p.add_argument("--phrase", default="0 0 0 0 0 Colab job complete")
    p.add_argument("--ramp-seconds", type=float, default=300.0)
    p.add_argument("--max-seconds", type=float, default=1200.0)
    p.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    p.add_argument("--daemon", action="store_true", help="Spawn detached background alarm on start")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    pid_file = Path(args.pid_file)

    if args.action == "self-test":
        res = run_self_test()
        print(json.dumps(res, indent=2))
        return 0

    if args.action == "status":
        print(json.dumps(_read_status(pid_file), indent=2))
        return 0

    if args.action == "stop":
        print(json.dumps(stop_alarm(pid_file), indent=2))
        return 0

    if args.action == "start":
        if args.daemon:
            if STOP_FLAG.exists():
                STOP_FLAG.unlink(missing_ok=True)
            cmd = [
                sys.executable,
                "-B",
                os.path.abspath(__file__),
                "--action",
                "start",
                "--phrase",
                args.phrase,
                "--ramp-seconds",
                str(args.ramp_seconds),
                "--max-seconds",
                str(args.max_seconds),
                "--pid-file",
                args.pid_file,
            ]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.25)
            print(json.dumps({"ok": True, "started": True, "pid": proc.pid, "phrase": args.phrase}, indent=2))
            return 0
        run_alarm_loop(args)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
