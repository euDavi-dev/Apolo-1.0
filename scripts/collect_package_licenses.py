"""Reúne os avisos distribuídos com as bibliotecas do ambiente de compilação."""
from importlib import metadata
from pathlib import Path

destination = Path(__file__).resolve().parents[1] / 'build/third_party_licenses'
destination.mkdir(parents=True, exist_ok=True)
index = []
for distribution in metadata.distributions():
    name = distribution.metadata['Name']
    index.append(f'{name} {distribution.version}')
    for file in distribution.files or []:
        if not any(word in file.name.lower() for word in ('license', 'copying', 'notice')):
            continue
        if file.suffix.lower() in {'.py', '.pyc', '.pyd', '.dll', '.h', '.c'}:
            continue
        source = distribution.locate_file(file)
        if source.is_file():
            folder = destination / name
            folder.mkdir(exist_ok=True)
            (folder / file.name).write_bytes(source.read_bytes())
(destination / 'DEPENDENCIAS.txt').write_text('\n'.join(sorted(index)), encoding='utf-8')
