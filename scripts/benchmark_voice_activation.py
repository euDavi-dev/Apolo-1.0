"""Compara detectores em arquivos de áudio; sem abrir o microfone."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--runtime-path", default="")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
if args.runtime_path:
    sys.path.insert(0, str(Path(args.runtime_path).resolve()))

import miniaudio
import numpy as np
from app.core.config import Settings
import app.services.keyword_spotter as keywords
from app.services.wake_word import WhisperWakeSTT, WakeWordDetector, match_wake, wake_confidence


def main():
    with tempfile.TemporaryDirectory() as temporary:
        keywords.DATA_DIR = Path(temporary)
        cfg = Settings()
        kws = keywords.StreamingKeywordSpotter(cfg)
        if not kws.load():
            raise RuntimeError(kws.error)
        whisper = WhisperWakeSTT(cfg)
        if not whisper.load():
            raise RuntimeError(whisper.error)
        rows = []
        for file in sorted((root / "validacao" / "voice_samples").glob("*.mp3")):
            samples = np.asarray(miniaudio.decode_file(str(file), nchannels=1, sample_rate=16000,
                                                     output_format=miniaudio.SampleFormat.SIGNED16).samples, dtype=np.int16)
            audio = samples.astype(np.float32) / 32768
            stream = kws._spotter.create_stream()
            spotted = False
            t0 = time.perf_counter()
            for offset in range(0, len(audio) + 16000, 1280):
                block = audio[offset:offset+1280] if offset < len(audio) else np.zeros(1280, dtype=np.float32)
                stream.accept_waveform(16000, block)
                while kws._spotter.is_ready(stream):
                    kws._spotter.decode_stream(stream)
                    result = kws._spotter.keyword_spotter.get_result(stream)
                    if result.keyword:
                        starts = result.timestamps
                        spotted = bool(starts and starts[0] <= 0.65)
                        break
                if spotted:
                    break
            kws_time = time.perf_counter() - t0
            heard = whisper(samples.tobytes())
            match = match_wake(heard.text)
            accepted = heard.no_speech < 0.6 and wake_confidence(match, heard.confidence, len(audio)/16000) >= cfg.wake_min_confidence
            expected = "negative_" not in file.name
            row = dict(sample=file.name, expected=expected, keyword=spotted, whisper=bool(accepted),
                       hybrid=bool(spotted or accepted), heard=heard.text, keyword_cpu_s=round(kws_time,4))
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        kws.stop()
        (root / "validacao" / "voice_benchmark.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Corretos:", sum(r["hybrid"] == r["expected"] for r in rows), "/", len(rows))
        pipeline_rows = []
        for file in sorted((root / "validacao" / "voice_samples").glob("*.mp3")):
            samples = np.asarray(miniaudio.decode_file(str(file), nchannels=1, sample_rate=16000,
                                                     output_format=miniaudio.SampleFormat.SIGNED16).samples, dtype=np.int16)
            audio = np.pad(samples.astype(np.float32) / 32768, (16000, 16000))
            detected = []
            detector = None
            def wake(command, text, info):
                detected.append(dict(text=text, partial=info.partial))
                detector.take_carry()
                return True
            detector = WakeWordDetector(cfg, whisper, wake)
            detector.streaming = keywords.StreamingKeywordSpotter(cfg)
            detector.streaming.load()
            detector.ready = True
            try:
                for i, offset in enumerate(range(0, len(audio)-319, 320)):
                    detector.feed(audio[offset:offset+320])
                    if i % 100 == 99:
                        detector.streaming.flush()
                detector.flush(timeout=15)
                expected = 'negative_' not in file.name
                item = dict(sample=file.name, expected=expected, detected=bool(detected), events=detected)
                pipeline_rows.append(item)
                print('pipeline', json.dumps(item, ensure_ascii=False), flush=True)
            finally:
                detector.shutdown()
        (root / "validacao" / "voice_pipeline.json").write_text(json.dumps(pipeline_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        print('Pipeline correto:', sum(r['expected'] == r['detected'] for r in pipeline_rows), '/', len(pipeline_rows))


if __name__ == "__main__":
    main()
