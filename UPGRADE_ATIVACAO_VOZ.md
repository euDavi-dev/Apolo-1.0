# Ativação por voz no Apolo 1.0

## Executar a versão atualizada

Esta distribuição contém o código-fonte, sem executável compilado. Na pasta do projeto, use o ambiente `.venv` criado conforme a [instalação no README](README.md#instalação):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

O código usa `sherpa-onnx`, mas esta versão fonte não inclui pesos, léxico nem tokens do detector contínuo. Na primeira utilização da ativação por voz, o aplicativo obtém os arquivos necessários do pacote oficial e confere seus hashes. O modelo de apoio Whisper `base` também é baixado quando ainda não está no computador. Aguarde essa preparação com uma conexão à internet. Depois de instalar e obter os modelos, a detecção de “Apolo” funciona localmente, sem chave de API. O reconhecimento do pedido continua usando o motor escolhido na aba Voz, que pode exigir internet.

Em **Configurações → Ativação**, use **Aplicar reconhecimento recomendado**, confirme em **OK** e aguarde o motor ficar pronto. Reabra essa tela para usar **Testar chamada por 12 segundos**. O teste mostra a chamada reconhecida ou o texto ouvido, e não executa pedidos. Ele usa os ajustes já salvos.

Experimente “Apolo”, aguarde o sinal e faça seu pedido, ou diga “Apolo, abra a calculadora” numa frase só.

## O que mudou

- Confiança mínima padrão de 0,50. Configurações anteriores com o padrão 0,60 passam para 0,50 ao carregar; outros valores personalizados são mantidos. O botão de reconhecimento recomendado também aplica 0,50.
- Sistema híbrido: um detector fonético contínuo procura o nome durante a fala; o Whisper em português também verifica as chamadas. O detector contínuo roda numa fila própria, sem bloquear o recebimento de áudio.
- Modelo de apoio padrão passou de `tiny` para `base`, priorizando reconhecimento. As preferências personalizadas por outros modelos são mantidas. O `base` pode consumir mais CPU e memória.
- Chamadas curtas agora podem ser avaliadas a partir de 160 ms de voz. Antes, menos de 250 ms era descartado. A entrada no VAD exige 80 ms de voz, e ruídos impulsivos são filtrados pela energia/duração real, além do texto e confiança.
- O áudio aprovado pelo VAD recebe normalização de volume e margem de silêncio antes da transcrição da chamada.
- Reconhecimento antecipado a partir de 700 ms, com até três tentativas enquanto a fala continua. Isso evita depender de uma única leitura prematura do nome.
- Até 8 segundos de trechos já concluídos ficam disponíveis durante a detecção. Um pedido que começa e termina enquanto o nome ainda é reconhecido pode ser reaproveitado.
- Quando “Apolo, abra…” é seguido de mais fala já capturada, a primeira metade não é executada sozinha: chamada e continuação são entregues juntas à captura do pedido.
- O gravador do pedido fica instalado antes da confirmação. O áudio acumulado na troca não é apagado. Se você já começou a falar, o sinal é dispensado.
- Chamadas por voz usam um sinal curto de 90 ms ou indicação visual, evitando que “Estou aqui” entre como pedido no próprio microfone. A opção de confirmação falada permanece para botão e palmas.
- Após a resposta, a voz é rearmada com uma pausa curta de 350 ms, além da proteção contra eco. A sessão continua impedindo ativações simultâneas.
- Música tocando não força mais o VAD ao modo mais restritivo. Isso corrigiu o cenário de teste “Apolo → pause”, que antes perdia a chamada. Filtros de duração e contexto continuam ativos.

## Testes e limites

Os testes offline de pronúncia usam um léxico e uma lista de tokens mínimos criados em uma pasta temporária. Eles conferem os rótulos e as aproximações fonéticas de Apolo sem pesos de terceiros, rede ou microfone:

```powershell
.\.venv\Scripts\python.exe -m unittest app.tests.test_apolo_name
```

O teste de empacotamento separa as bibliotecas nativas do modelo acústico opcional. Sem pesos locais válidos, registra a verificação do modelo como `skipped`, com motivo, e não tenta baixá-los. Consulte [EXECUTAVEL_WINDOWS.md](EXECUTAVEL_WINDOWS.md) para compilar e verificar um pacote próprio.

O detector fonético foi treinado originalmente para inglês/chinês; as pronúncias de Apolo são aproximações, com apoio do reconhecimento em português. Os testes offline não medem precisão acústica com voz real. Distância, sotaque, ruído e eco no seu computador devem ser conferidos com **Testar chamada por 12 segundos**, depois que os modelos estiverem prontos. Não há cancelamento acústico de eco nesta versão.

Os WAVs de confirmação falada são opcionais; o sinal curto funciona sem eles. Resultados antigos de benchmarks não validam uma instalação ou compilação nova do Apolo 1.0.

## Referências do detector

- [Arquitetura e palavras personalizadas do sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)
- [Modelo fonético oficial e arquivos disponibilizados](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)

A origem e os hashes dos arquivos obtidos pelo aplicativo estão descritos em [models/wake/keyword/ORIGEM.md](models/wake/keyword/ORIGEM.md) e no código do detector. Esta distribuição fonte não redistribui esses pesos, léxico ou tokens. Consulte [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) antes de redistribuir modelos obtidos separadamente.
