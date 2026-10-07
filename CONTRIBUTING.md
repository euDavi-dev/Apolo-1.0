# Contribuindo com o Apolo 1.0

Contribuições são bem-vindas: correções, melhorias de acessibilidade, documentação, testes e novos recursos que tornem o assistente mais útil no Windows.

## Prepare o ambiente

Faça um fork, clone seu fork e crie uma branch para a mudança. Use Python 3.11 de 64 bits para reproduzir o ambiente de validação desta versão:

```powershell
git clone https://github.com/euDavi-dev/Apolo-1.0.git
cd Apolo-1.0
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
git switch -c minha-melhoria
```

Ao contribuir por fork, substitua a URL pelo endereço do seu fork. Execute o app com:

```powershell
.\.venv\Scripts\python.exe run.py
```

As contas são locais. Configure as chaves pelo aplicativo e mantenha dados pessoais fora do repositório.

## Encontre o lugar da mudança

| Caminho | Responsabilidade |
| --- | --- |
| `app/core/` | Sessões, contas, configurações, estados e coordenação do assistente. |
| `app/services/` | Áudio, voz, IA, comandos, música, clima, produtividade e WhatsApp. |
| `app/ui/` | Interface PySide6, diálogos, bandeja e apresentação. |
| `app/utils/` | Utilitários compartilhados. |
| `app/tests/` | Regressões automatizadas e diagnósticos executados manualmente. |
| `scripts/` | Preparação de recursos, prévia da interface e empacotamento. |
| `docs/images/` | Capturas usadas na documentação pública. |

Prefira mudanças focadas, mensagens e rótulos em português claro e processamento demorado fora da thread da interface. Preserve a compatibilidade dos dados existentes e o isolamento entre contas.

## Testes automatizados

Na raiz do repositório:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s app/tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -X utf8 -m app.tests.test_flow
.\.venv\Scripts\python.exe -X utf8 -m app.tests.test_commands
```

A descoberta executa os testes `unittest`. Alguns arquivos em `app/tests/` são diagnósticos com um ponto de entrada próprio e não são executados pela descoberta. O teste de fluxo usa dublês; o teste de comandos verifica interpretação sem executar os comandos.

Para mudanças na agenda ou no WhatsApp, também é possível executar apenas as regressões relacionadas:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest app.tests.test_productivity app.tests.test_productivity_integration app.tests.test_whatsapp -v
```

Use bancos temporários, relógios controláveis e dublês de rede, navegador e áudio. Testes automatizados não devem criar contas no perfil real, enviar mensagens ou chamar serviços externos.

## Verificação manual

Confira a interface e a experiência relevante à mudança. A prévia abre uma interface de demonstração sem inicializar microfone ou serviços:

```powershell
.\.venv\Scripts\python.exe scripts/preview_visual.py
```

Mudanças de layout devem funcionar também na janela mínima de 900 × 620 e com textos longos. Verifique contraste, foco pelo teclado, mensagens de erro e funcionamento ao ocultar a janela na bandeja.

Diagnósticos reais são uma etapa separada. Por exemplo, os comandos abaixo acessam o microfone e devem ser executados conscientemente no computador de teste:

```powershell
.\.venv\Scripts\python.exe -m app.tests.test_microphone
.\.venv\Scripts\python.exe -m app.tests.test_activation --live
```

Da mesma forma, testes de APIs, OAuth e música podem acessar serviços externos ou abrir o navegador. Leia a descrição do script antes de executá-lo. Registre no pull request o que foi verificado e o que depende de hardware ou credenciais e ficou pendente.

## Abra um pull request

1. Explique o problema e o comportamento resultante, com um exemplo quando ajudar.
2. Mantenha a mudança limitada ao objetivo e inclua regressões quando elas protegerem comportamento relevante.
3. Atualize os guias se mudar instalação, comandos ou fluxos de uso.
4. Descreva a validação. Para mudanças visuais, inclua capturas com dados de demonstração.
5. Informe novas dependências, alterações no formato dos dados e migrações necessárias.

Relatos de problemas devem informar versão do Windows, versão do Python, passos para reproduzir, resultado esperado e resultado observado. Remova nomes pessoais, pedidos privados, chaves, tokens e caminhos sensíveis de logs e capturas antes de publicar.

## Credenciais e recursos externos

Não versione `.env`, chaves, tokens OAuth, perfis em `%APPDATA%\APOLO`, bancos de agenda, caches, logs ou gravações pessoais. Nunca coloque credenciais reais em testes ou exemplos.

Novas integrações devem deixar claro quais dados são enviados e quais ações o usuário conclui. A integração atual de WhatsApp prepara uma conversa; o envio permanece manual. Não transforme esse fluxo em envio automático sem discutir e documentar a mudança de comportamento.

Ao adicionar bibliotecas, modelos, imagens ou áudio de terceiros, confira a licença e atualize [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Evite incluir pesos de modelos e outros arquivos grandes sem necessidade e autorização de redistribuição.

## Licença das contribuições

Ao enviar uma contribuição, você concorda que o código contribuído seja disponibilizado sob a [licença MIT](LICENSE) do projeto. Recursos de terceiros conservam suas próprias licenças e precisam ser identificados.
