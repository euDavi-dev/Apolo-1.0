# Componentes de terceiros

A licença MIT na raiz cobre o código e a documentação originais do Apolo.
Dependências, modelos, fontes de dados e serviços externos mantêm suas próprias
licenças e condições de uso.

## Detector de palavra-chave

O código do [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) é distribuído sob
Apache-2.0. Uma cópia dessa licença está em
[models/wake/keyword/LICENSE](models/wake/keyword/LICENSE).

Os pesos, o léxico `en.phone` e os tokens do modelo **não são redistribuídos neste
repositório**. O Apolo pode baixar os arquivos selecionados do pacote oficial
`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` na primeira execução. A origem e
as limitações estão em [models/wake/keyword/ORIGEM.md](models/wake/keyword/ORIGEM.md).
Os arquivos são conferidos com os hashes presentes em
`app/services/keyword_spotter.py`.

O fornecedor distingue a licença do código da licença de cada modelo. Consulte o
[aviso oficial sobre licenciamento de modelos](https://k2-fsa.github.io/sherpa/onnx/kws/apk.html)
antes de redistribuir os arquivos baixados. A presença da licença do sherpa-onnx
não altera a licença dos pesos nem do léxico.

## Reconhecimento e síntese de voz

`faster-whisper`, seus modelos Whisper e a voz local opcional Kokoro são obtidos
separadamente. Este repositório não inclui pesos desses modelos nem gravações de
voz geradas. Consulte as respectivas fontes e licenças antes de empacotá-los ou
redistribuí-los:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Whisper](https://github.com/openai/whisper)
- [Kokoro ONNX](https://github.com/thewh1teagle/kokoro-onnx)

As vozes do Windows e o serviço usado pelo `edge-tts` seguem as condições de seus
fornecedores.

## Interface, bibliotecas e mídia

O ícone do Apolo é gerado pelo código em `app/ui/icons.py` e
`scripts/make_exe_icon.py`. As imagens em `docs/images/` são capturas da interface
com dados de demonstração. Fontes do Windows não são incluídas neste repositório.

PySide6/Qt e as demais bibliotecas listadas nos arquivos `requirements*.txt`
mantêm suas próprias licenças. Ao gerar um executável, o script
`scripts/collect_package_licenses.py` reúne avisos disponíveis nas distribuições
instaladas. Revise esses avisos e as obrigações das dependências antes de
distribuir o pacote compilado.

Músicas pessoais e conteúdo do Spotify e do YouTube não são incluídos no projeto.
Gemini, Spotify, YouTube, WhatsApp e Open-Meteo são serviços independentes; suas
condições e políticas continuam aplicáveis ao uso das integrações.
