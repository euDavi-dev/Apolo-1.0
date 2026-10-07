# Origem do modelo de palavra-chave

O Apolo 1.0 não redistribui pesos, léxico ou tokens desse pacote. O aplicativo
baixa os arquivos selecionados do fornecedor na primeira utilização, quando
necessário. Consulte [THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md)
antes de redistribuir modelos obtidos separadamente.

Arquivos selecionados do pacote oficial sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20, variante chunk-8/int8.

Origem: https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
Documentação: https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html
Projeto: https://github.com/k2-fsa/sherpa-onnx

Cópia da licença do código do projeto em LICENSE; ela não confirma a licença dos pesos ou do léxico. Os arquivos baixados são usados sem modificações. As pronúncias de Apolo usadas pelo app são configuradas separadamente. Os hashes dos arquivos oficiais estão fixados em app/services/keyword_spotter.py e são conferidos antes do carregamento. O léxico é um dicionário genérico de terceiros; suas entradas não definem a palavra de ativação do aplicativo.

O modelo foi treinado para chinês/inglês. As pronúncias de Apolo são aproximações em seus fonemas disponíveis. A precisão acústica desta adaptação ainda precisa ser conferida com voz real; o reconhecimento em português permanece como apoio para chamadas que esse modelo não detecta.
