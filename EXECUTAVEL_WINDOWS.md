# Apolo 1.0 para Windows

O Apolo 1.0 é distribuído como código-fonte. Este repositório não contém um executável Windows pronto; você pode executar com Python ou gerar um pacote no seu computador.

## Executar a partir do código

Em Windows x64 com Python 3.11, abra o PowerShell na pasta do projeto:

```powershell
python -m pip install -r requirements.txt
python run.py
```

Na primeira utilização da ativação por voz, o aplicativo baixa do fornecedor os arquivos necessários ao detector contínuo e o modelo de apoio Whisper, quando ainda não estão no computador. Essa etapa precisa de internet. Depois do download, a detecção de “Apolo” funciona localmente. O reconhecimento de pedidos e os demais recursos seguem os requisitos do motor e dos serviços escolhidos nas configurações. Kokoro é opcional e requer seus próprios arquivos de modelo.

As contas e os dados do aplicativo ficam em `%APPDATA%/APOLO`. Fechar a janela mantém o Apolo na bandeja; para encerrar, use **Sair** no ícone da bandeja.

## Gerar um executável

Na mesma pasta do projeto, execute:

```powershell
./scripts/build_windows.ps1
```

O script prepara um ambiente de compilação, instala as versões de `requirements-windows-build.txt`, reúne os avisos das dependências e usa `Apolo.spec` para gerar `dist/Apolo/Apolo.exe`. A instalação das dependências precisa de internet; os pesos de ativação não são necessários para compilar esta versão.

Distribua a pasta `dist/Apolo` inteira, mantendo `_internal` junto do executável. Um pacote compilado a partir desta versão fonte também obtém os modelos necessários na primeira utilização. A opção **Iniciar com o Windows** usa o executável quando o aplicativo está empacotado.

O empacotamento usa [PyInstaller](https://pyinstaller.org/en/stable/spec-files.html). A configuração inclui os recursos presentes no projeto; não baixa modelos durante a compilação. Antes de incluir pesos ou outros arquivos de terceiros em um pacote próprio, consulte [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Verificar um pacote gerado

Depois de compilar, execute:

```powershell
./dist/Apolo/Apolo.exe --self-test ./validacao-pacote.json
```

O teste usa dados temporários e gera um relatório JSON e uma imagem da tela de entrada. Verifica importações, processamento nativo, conversão FLAC, interface e comando de inicialização com o Windows. Não abre o microfone, usa uma conta real nem baixa modelos.

O campo `checks.bundled_keyword_model` informa `status: "skipped"` e o motivo quando os pesos locais estão ausentes ou não passam pela verificação de integridade. Quando um modelo válido está incluído, o teste carrega os arquivos locais e processa silêncio. Isso verifica o carregamento e a execução nativa; a precisão com voz real deve ser conferida no aplicativo. Uma verificação opcional marcada `skipped` não é um erro de compilação. Falhas das demais verificações aparecem em `errors` e fazem `ok` ficar falso.

Um relatório de uma compilação anterior não valida um executável novo. Esta distribuição fonte não afirma que um pacote Windows já tenha sido compilado e validado para o Apolo 1.0.
