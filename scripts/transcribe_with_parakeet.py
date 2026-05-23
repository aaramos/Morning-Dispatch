from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


LECTURE_PROCESSOR_SRC = Path("/Users/macstudio/Apps/Lecture Transcriber/src")


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe one audio file with the local Parakeet MLX setup.")
    parser.add_argument("--audio", required=True, help="Input audio path")
    parser.add_argument("--output", required=True, help="Transcript text output path")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    output_path = Path(args.output)
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    os.environ["PATH"] = f"/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    sys.path.insert(0, str(LECTURE_PROCESSOR_SRC))

    from lecture_processor.config import TranscriptionEngine, TranscriptionQuality
    from lecture_processor.transcription import build_transcriber

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="morning-dispatch-parakeet-") as temp_dir:
        wav_path = Path(temp_dir) / "input-16khz-mono.wav"
        _convert_for_parakeet(audio_path, wav_path)
        transcriber = build_transcriber(
            TranscriptionEngine.PARAKEET_MLX,
            "animaslabs/parakeet-tdt-0.6b-v3-mlx",
            quality=TranscriptionQuality.ACCURATE,
            profile_id="parakeet",
        )
        result = transcriber.transcribe(wav_path)
        text = " ".join(str(result.text or "").split()).strip()
        if not text:
            raise RuntimeError("Parakeet returned an empty transcript")
        output_path.write_text(text + "\n", encoding="utf-8")
    return 0


def _convert_for_parakeet(input_path: Path, output_path: Path) -> None:
    subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output_path),
        ],
        check=True,
        timeout=1800,
    )


if __name__ == "__main__":
    raise SystemExit(main())
