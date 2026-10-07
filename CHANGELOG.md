# Changelog

As mudanças públicas do projeto são registradas neste arquivo. A numeração desta distribuição começa em **1.0.0**.

## 1.0.0 — 2026-10-07

Publicação 1.0.0 do **Apolo 1.0**, um assistente pessoal para Windows com interface em português.

### Recursos

- Interface PySide6 com presença visual animada, conversa por texto, atalhos e execução na bandeja.
- Contas locais com cadastro, login, tutorial, preferências por usuário e cofre criptografado de chaves.
- Ativação por “Apolo”, duas palmas, botão ou Ctrl + Espaço; detecção híbrida local com apoio em português.
- Reconhecimento de fala com Google, Gemini ou Whisper local e síntese com voz neural online, voz do Windows ou Kokoro opcional.
- Conversa com Gemini e comandos reconhecidos para aplicativos, sites e ações do Windows.
- Clima por cidade e música por arquivos locais, Spotify ou YouTube, conforme os requisitos de cada integração.
- Agenda persistente com tarefas, conclusão, lembretes, temporizadores e cancelamento pela interface ou conversa.
- Avisos na conversa e na bandeja, com recuperação de itens vencidos ao reabrir a conta.
- WhatsApp Web e preparação de conversas por link, com revisão e envio manual pelo usuário.

### Documentação e distribuição

- README com instalação, primeiro acesso, exemplos, configurações opcionais e informações de privacidade.
- Guia de contribuição e instruções de validação automatizada e manual.
- Guias de contas, produtividade, ativação por voz e empacotamento Windows.
- Distribuição do código sob licença MIT, com identificação de recursos de terceiros e suas licenças próprias.

### Validação e limites

As regressões automatizadas usam dados temporários e dublês de serviços. O funcionamento com microfone e alto-falante físicos, credenciais reais e serviços externos depende de validação no ambiente do usuário. Alertas no horário requerem que o aplicativo permaneça aberto; mensagens de WhatsApp são enviadas pelo próprio usuário.
