"""Detector local contínuo de palavra-chave, independente da transcrição do pedido."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import queue
import tarfile
import tempfile
import threading
from urllib.request import urlopen

import numpy as np
from app.core.config import ROOT, DATA_DIR
from app.services.audio_engine import RATE

log = logging.getLogger("apolo.keyword")
MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL_NAME}.tar.bz2"
FILES = {
    "encoder": "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
    "decoder": "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
    "joiner": "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
    "tokens": "tokens.txt", "lexicon": "en.phone",
}
HASHES = {
    "encoder": "2ca84d6bfe73e1ea3c9c49f600f7cad1c9ddd423c53c906b8bfe802444dd78d5",
    "decoder": "63a22dd60f40fff082ac3e09afa507f6787da36df76ded2fbe145fa233e22c21",
    "joiner": "190d4067b4cc20b72a42a1916e69d92052000fb7051a427ebb1bc72a69207dc1",
    "tokens": "2d3f32311f9b692b964da3c90e830258d3e78e013cb0c992dbfb15cd5a1a71b0",
    "lexicon": "f7000ec3a90544c0c7c16090d8951779c2b322e14dad5006290f498567d439ea",
}


def model_valid(directory: Path) -> bool:
    try:
        return all(hashlib.sha256((directory / filename).read_bytes()).hexdigest() == HASHES[key]
                   for key, filename in FILES.items())
    except OSError:
        return False


def prepare_model(directory: Path) -> Path:
    """Baixa somente os arquivos necessários do pacote oficial, sem extrair caminhos do TAR."""
    directory = Path(directory)
    if model_valid(directory):
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        archive = Path(temporary) / "model.tar.bz2"
        with urlopen(MODEL_URL, timeout=45) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                if output.tell() > 50 * 1024 * 1024:
                    raise ValueError("Pacote de modelo maior que o esperado")
        with tarfile.open(archive, "r:bz2") as package:
            for key, filename in FILES.items():
                member = package.getmember(f"{MODEL_NAME}/{filename}")
                if not member.isfile() or member.size > 20 * 1024 * 1024:
                    raise ValueError("Arquivo de modelo inválido")
                with package.extractfile(member) as source:
                    data = source.read()
                    if hashlib.sha256(data).hexdigest() != HASHES[key]:
                        raise ValueError("Modelo diferente do pacote oficial verificado")
                    (Path(temporary) / filename).write_bytes(data)
            for filename in FILES.values():
                (Path(temporary) / filename).replace(directory / filename)
    return directory


class StreamingKeywordSpotter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ready = False
        self.error = ""
        self._queue = queue.Queue(maxsize=150)  # no máximo 3 s de atraso
        self._stop = threading.Event()
        self._spotter = None
        self.dropped = 0

    def load(self) -> bool:
        if self.ready:
            return True
        try:
            import sherpa_onnx
            bundled = ROOT / "models" / "wake" / "keyword"
            directory = bundled if model_valid(bundled) else DATA_DIR / "models" / "wake" / "keyword"
            if not model_valid(directory):
                log.info("Baixando o detector contínuo de palavra-chave (primeira execução)")
                prepare_model(directory)
            word = self.cfg.wake_word.strip().upper()
            pronunciation = None
            for line in (directory / FILES["lexicon"]).read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if parts and parts[0] == word:
                    pronunciation = parts[1:]
                    break
            phrases = [" ".join(pronunciation)] if pronunciation else []
            if word in {"APOLO", "APOLLO"}:
                # Aproximações fonéticas de a-PÓ-lo em português brasileiro.
                # O léxico original do modelo permanece intacto e verificável.
                phrases += ["AH0 P AO1 L UW0", "AH0 P AO1 L OW0", "AH0 P AA1 L OW0"]
            if not phrases:
                raise ValueError("Palavra personalizada não está no léxico acústico")
            keyword_text = "\n".join(phrase + " @" + word for phrase in dict.fromkeys(phrases))
            # Arquivo por configuração; nunca altera o modelo compartilhado.
            tag = hashlib.sha256(keyword_text.encode()).hexdigest()[:12]
            keyword_file = DATA_DIR / "models" / "wake" / f"keywords-{tag}.txt"
            keyword_file.parent.mkdir(parents=True, exist_ok=True)
            keyword_file.write_text(keyword_text, encoding="utf-8")
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(directory / FILES["tokens"]), encoder=str(directory / FILES["encoder"]),
                decoder=str(directory / FILES["decoder"]), joiner=str(directory / FILES["joiner"]),
                keywords_file=str(keyword_file), num_threads=1, keywords_score=2.0, max_active_paths=8,
                keywords_threshold=self.cfg.wake_keyword_threshold, num_trailing_blanks=1)
            self.ready = True
            threading.Thread(target=self._run, daemon=True, name="keyword-stream").start()
            return True
        except Exception as exc:
            self.error = "Detector contínuo indisponível; usando reconhecimento local em português."
            log.warning("%s (%s)", self.error, type(exc).__name__)
            return False

    def feed(self, block, capture, epoch, callback):
        if not self.ready:
            return
        try:
            self._queue.put_nowait((np.asarray(block, dtype=np.float32).copy(), capture, epoch, callback))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        identity, stream, fired = None, None, False
        while not self._stop.is_set():
            try:
                audio, capture, epoch, callback = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if audio is None:
                    callback.set()
                    continue
                tag = (epoch, capture.wake_capture_id)
                if tag != identity:
                    identity, stream, fired = tag, self._spotter.create_stream(), False
                if fired:
                    continue
                stream.accept_waveform(RATE, audio)
                while self._spotter.is_ready(stream):
                    self._spotter.decode_stream(stream)
                    # A API nativa entrega o evento ao ler o resultado. Leia
                    # palavra e tempos juntos, sem consultar o evento duas vezes.
                    result = self._spotter.keyword_spotter.get_result(stream)
                    keyword = result.keyword.strip()
                    if keyword:
                        times = result.timestamps
                        # Chamada no começo da frase. Menções tardias ficam para
                        # o reconhecedor em português, que verifica o contexto.
                        if times and times[0] <= 0.65:
                            fired = True
                            callback(capture, epoch)
                        self._spotter.reset_stream(stream)
                        break
            except Exception:
                self.ready = False
                log.exception("Detector contínuo falhou; reconhecimento em português permanece disponível")
                break

    def stop(self):
        self.ready = False
        self._stop.set()

    def flush(self, timeout=5):
        if self.ready and not self._stop.is_set():
            completed = threading.Event()
            self._queue.put((None, None, None, completed), timeout=timeout)
            if not completed.wait(timeout):
                raise TimeoutError("Detector contínuo não concluiu os blocos pendentes")
