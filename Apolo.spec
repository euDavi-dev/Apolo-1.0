# Pacote portátil Windows: Apolo.exe + _internal, sem console.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ('sherpa_onnx', 'faster_whisper', 'google.genai', 'kokoro_onnx', 'espeakng_loader'):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
datas += collect_data_files('speech_recognition', includes=['flac-win32.exe'])
binaries += collect_dynamic_libs('ctranslate2')
for name in ('sherpa-onnx', 'sherpa-onnx-core', 'faster-whisper', 'kokoro-onnx', 'edge-tts',
             'SpeechRecognition', 'google-genai', 'phonemizer'):
    datas += copy_metadata(name)
for folder in ('models/wake/keyword', 'assets'):
    for path in (root / folder).rglob('*'):
        if path.is_file():
            datas.append((str(path), str(path.parent.relative_to(root))))
datas.append((str(root / 'build/third_party_licenses'), 'third_party_licenses'))
hiddenimports += ['pyttsx3.drivers.sapi5', 'pythoncom', 'pywintypes', 'app.packaging_check']
a = Analysis(
    [str(root / 'run.py')], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[str(root / 'packaging/hooks')], runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pandas', 'torch', 'torchaudio', 'tensorflow',
              'app.tests', 'pytest', 'IPython'], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Apolo',
          debug=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets/apolo.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Apolo')
