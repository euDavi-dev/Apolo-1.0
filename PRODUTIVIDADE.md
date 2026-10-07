# Agenda e WhatsApp no Apolo

A barra lateral e o menu da bandeja agora oferecem **Agenda** e **WhatsApp**.
As novas funções usam as dependências já presentes no projeto. Execute `python run.py`
na raiz do repositório, usando o ambiente Python da instalação.

## Tarefas

Na Agenda, abra **Tarefas**, escreva um título e clique em **Adicionar**.
Selecione uma tarefa para concluir ou cancelar. Marque **Mostrar concluídos e avisos
entregues** para consultar os itens finalizados.

Você também pode falar ou digitar:

- `Adicionar tarefa comprar café`
- `Minhas tarefas`
- `Concluir tarefa 1`
- `Cancelar tarefa 1`

O número mostrado na lista identifica o item. Ele é único na agenda, inclusive entre
tarefas, temporizadores e lembretes.

## Temporizadores e lembretes

Na aba **Temporizadores**, escolha a duração e, se quiser, um nome. Na aba
**Lembretes**, informe o título, a data e o horário.

Exemplos de comandos:

- `Temporizador de 5 minutos`
- `Temporizador de uma hora e trinta minutos`
- `Meus temporizadores`
- `Cancelar temporizador 2`
- `Me lembre de beber água em 20 minutos`
- `Me lembre de ligar para Ana amanhã às 9h`
- `Meus lembretes`
- `Cancelar lembrete 3`
- `Ajuda produtividade`

Os avisos aparecem na conversa e na bandeja do Windows. Eles não interrompem a
resposta falada do Apolo. Mantenha o aplicativo aberto, mesmo que oculto na bandeja,
para receber os avisos no horário. Se o computador estiver desligado ou o Apolo
estiver fechado, os itens vencidos serão avisados ao reabrir o aplicativo.

A agenda fica no arquivo `productivity.sqlite3` dentro do perfil local da conta
(`%APPDATA%\APOLO\profiles\USUARIO`). Tarefas e horários continuam salvos depois de
fechar o programa. Cada conta tem sua própria agenda. Esse arquivo contém os títulos
dos itens em texto legível, separado do cofre de senhas e chaves. Inclua-o no backup
do seu perfil se quiser preservar a agenda.

## WhatsApp

Abra **WhatsApp** na barra lateral para iniciar o WhatsApp Web ou preparar uma
conversa. Informe o telefone completo, incluindo DDI e DDD, e escreva a mensagem.
O botão **Copiar link** permite copiar o link da conversa preparada.

Na primeira utilização do WhatsApp Web, vincule sua conta pelo QR code exibido pelo
próprio WhatsApp. O Apolo não armazena credenciais do WhatsApp.

Por voz ou texto:

- `Abra o WhatsApp`
- `WhatsApp para +55 11 99999-9999`
- `Prepare mensagem no WhatsApp para +55 11 99999-9999 dizendo Olá, chego às 15:30!`
- `Ajuda WhatsApp`

A mensagem é preenchida na conversa para revisão. **O envio é concluído por você
no WhatsApp.** Esta integração não lê conversas, acompanha respostas ou envia
mensagens agendadas. O Apolo usa o recurso oficial
[clique para conversa](https://faq.whatsapp.com/5913398998672934/?locale=pt_BR).
O navegador precisa estar disponível e o telefone precisa ter uma conta no WhatsApp;
abrir o link não confirma que o destinatário está cadastrado.

## Validação

Testes locais, sem enviar mensagens ou usar o microfone:

```powershell
python -m unittest app.tests.test_productivity app.tests.test_productivity_integration app.tests.test_whatsapp -v
python -m app.tests.test_flow
python -m app.tests.test_commands
```

Os testes verificam persistência, isolamento das contas, cancelamento, entrega única
dos avisos, interpretação dos comandos, preservação do texto das mensagens e erros
de abertura do navegador. O funcionamento com uma conta real do WhatsApp e os
avisos do Windows dependem da sessão e das configurações locais do usuário.
