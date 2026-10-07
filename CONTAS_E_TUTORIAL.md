# Apolo 1.0 — contas e primeiros passos

## Executar

Na raiz do repositório, use o ambiente Python criado na instalação:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

A biblioteca `cryptography` protege o cofre da conta e já consta nas dependências.

## Primeiro acesso

1. Crie um usuário, uma senha de pelo menos 10 caracteres, seu nome e cidade.
2. Confira o perfil e cole suas chaves de Gemini e, se desejar, YouTube e Spotify Client ID. Você pode continuar sem chaves e adicioná-las depois.
3. Leia o guia de voz, texto, atalhos e primeiros pedidos. Clique em **Começar a usar** para iniciar os serviços.

O cadastro e o tutorial não abrem microfone nem chamam APIs. Se interromper o guia, a conta continua criada, mas o guia será apresentado novamente após entrar. As alterações dos campos do guia só são salvas ao concluir.

Nas próximas execuções, entre com usuário e senha. Cada novo usuário recebe seu próprio tutorial. **Minha conta** permite editar nome, cidade e chaves, e **Tutorial** reabre o guia. Deixar uma chave vazia e salvar remove aquela chave. A chave Gemini passa a valer no próximo pedido; alterações de música requerem encerrar e abrir o Apolo novamente. Salvar a chave não confirma sua validade: a API é consultada ao inicializar/usar o serviço.

**Sair da conta** encerra o aplicativo. Abra-o novamente para entrar em outra conta. Fechar pelo X apenas minimiza para a bandeja e mantém a sessão ativa. O início com Windows também exige login.

## Dados e privacidade

As contas são locais a este usuário do Windows, sem servidor, e-mail, recuperação de senha ou sincronização entre computadores.

- `%APPDATA%/APOLO/accounts/<usuario>.json`: perfil e chaves criptografados com AES-GCM; a chave de criptografia é derivada da senha com scrypt e salt aleatório. A senha não é armazenada.
- `%APPDATA%/APOLO/profiles/<usuario>/`: preferências, comandos, caches, logs e tokens dos serviços separados por conta. Esses arquivos operacionais não são todos criptografados; o token OAuth Spotify mantém o armazenamento local do provedor existente. As permissões do Windows continuam sendo necessárias para proteger acesso aos arquivos.
- No aplicativo, uma conta sem chave nunca herda as chaves do `.env` ou de outra conta. Scripts auxiliares sem conta ativa preservam a compatibilidade com `.env`.

Guarde sua senha e faça backup da pasta APOLO. Sem a senha, não é possível recuperar o cofre. O cadastro não importa automaticamente configurações nem chaves antigas; os arquivos anteriores são preservados. Cole suas chaves em Minha conta e ajuste suas preferências pelo aplicativo.

Para autorizar o Spotify, salve o Client ID em Minha conta e execute `python scripts/music_setup.py spotify`. Havendo contas cadastradas, o script pede usuário e senha do Apolo e salva o token na pasta da conta escolhida. Os demais pré-requisitos de música continuam descritos no README.

## Validação

Os testes de contas e de escuta usam dados temporários, sem chaves reais ou
microfone físico. Execute na raiz do repositório:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest app.tests.test_accounts app.tests.test_command_listening
```

Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para a suíte completa e as verificações
manuais. Credenciais e serviços externos precisam ser conferidos no seu ambiente.
