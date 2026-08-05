# Torre de controle · Status diário da operação

Painel de acompanhamento diário dos veículos em operação: veículos em rota,
entregas realizadas, pendentes, reentregas, devoluções e adesão ao GreenMile.

**Fase atual: uso interno da equipe Mandalog.** A seção "Pendências de
apontamento" contém diagnóstico interno e não deve ser compartilhada com o
cliente. Antes de liberar acesso à 3 Corações, remova essa seção do `index.html`
(bloco `<section class="interno">` e o trecho que monta `itens` no script).

---

## ⚠️ Como o acesso é protegido

O GitHub Pages serve arquivos estáticos, sem servidor para checar senha. Por isso
os dados **não são escondidos atrás de uma senha — são cifrados com a senha**:
apenas `data/operacao.enc` é publicado, e o navegador o decifra com PBKDF2-SHA256
e AES-256-GCM depois que a senha é digitada. Quem baixar o arquivo sem a senha
recebe bytes aleatórios.

O CSV e o JSON em texto claro estão no `.gitignore` e nunca foram comitados —
confirme com `git ls-files` antes de qualquer push.

A força da proteção é a força da senha: o arquivo cifrado é baixável, então
tentativas podem ser feitas offline. Use no mínimo 12 caracteres, de preferência
20. Detalhes, limites e o que isso **não** protege estão em [DEPLOY.md](DEPLOY.md).

---

## Para o Claude Code

`CLAUDE.md` traz o contexto do projeto, o grão do dado, as regras de cálculo e
os invariantes de segurança. `HANDOFF.md` traz o roteiro de implantação passo a
passo, com o que é automatizável e o que exige o operador.

---

## Publicando online

O procedimento completo — repositório privado, conta de serviço do Google, GitHub Actions, Cloudflare Pages e login por e-mail — está em [DEPLOY.md](DEPLOY.md).

---

## Como funciona

```
data/raw/*.csv        export das abas da planilha        (gitignored)
   ↓  scripts/build_data.py
data/operacao.json    dado normalizado                   (gitignored)
   ↓  scripts/cifrar.py  ·  PAINEL_SENHA
data/operacao.enc     único arquivo publicado
   ↓  index.html  ·  senha digitada decifra no navegador
painel
```

Grão do dado: **uma linha do CSV é uma nota fiscal em uma tentativa de
entrega**. A data de saída é por nota, não por plano — reentregas recebem a data
da nova saída mantendo o mesmo número de plano. É por isso que um plano aparece
em mais de um dia no painel, e o cálculo depende disso.

---

## Rodando localmente

```bash
git clone <url-do-repositorio>
cd mandalog-torre

# 1. coloque o CSV exportado da aba em data/raw/cafe-sjc.csv
# 2. gere e cifre
python3 scripts/build_data.py
PAINEL_SENHA='a-senha' python3 scripts/cifrar.py

# 3. sirva os arquivos (abrir o index.html direto do disco não funciona,
#    porque o navegador bloqueia o fetch em file://)
python3 -m http.server 8000
# abra http://localhost:8000
```

O `build_data.py` não tem dependências externas — só a biblioteca padrão do
Python 3.

---

## Atualização automática

`.github/workflows/atualizar-dados.yml` roda quatro vezes por dia, baixa a aba
da planilha, regera o JSON e comita se algo mudou.

Para ligar, configure em **Settings › Secrets and variables › Actions**:

| Secret | Conteúdo |
|---|---|
| `SHEET_ID` | trecho entre `/d/` e `/edit` na URL da planilha |
| `GOOGLE_SA_JSON` | JSON completo da conta de serviço |

Criando a conta de serviço:

1. No Google Cloud Console, crie um projeto e habilite a **Google Sheets API**.
2. Crie uma **conta de serviço** e gere uma chave em formato JSON.
3. Copie o conteúdo do JSON para o secret `GOOGLE_SA_JSON`.
4. Na planilha, compartilhe com o e-mail da conta de serviço como **Leitor**.

Esse desenho mantém a planilha privada: em vez de publicá-la na web, você
concede leitura a uma identidade específica, revogável a qualquer momento.

**Nunca comite o arquivo JSON da conta de serviço.** O `.gitignore` já bloqueia
os nomes mais comuns, mas a proteção real é não colocá-lo na pasta.

---

## Incluindo novas operações

1. Em `scripts/baixar_planilha.py`, acrescente a aba ao dicionário `ABAS`.
2. Em `scripts/build_data.py`, dê nome de exibição em `OPERACOES`.
3. Rode `python3 scripts/build_data.py`.

O painel agrupa por data de saída e mostra a operação no subtítulo, então várias
abas convivem no mesmo JSON.

---

## Regras de cálculo

Todos os percentuais saem da coluna **STATUS SAC**, sobre o total de notas com
saída no dia. Transferências entre bases ficam fora do denominador, por não
serem entrega a recebedor.

| Indicador | Fórmula |
|---|---|
| % finalizadas | notas `ENTREGUE` ÷ notas do dia |
| % reentrega | notas `REENTREGAR` ÷ notas do dia |
| % devoluções | `DEVOLUÇÃO`, `FINALIZADO C/ DEV PARCIAL` e `RECUSA` ÷ notas do dia |
| % GreenMile | notas com baixa eletrônica ÷ **entregas realizadas** |
| Veículo em rota | plano com nota cuja data de saída é o dia, e ao menos uma nota sem desfecho |
| Veículo finalizado | todas as notas do dia com desfecho lançado |

**Sem apontamento** é exibido em separado e nunca somado a "pendente": nota sem
Status SAC significa desfecho não informado, não entrega não realizada. Somar as
duas coisas produziria número falso para o cliente.

---

## Limitação conhecida

A posição do veículo não vem da planilha. "Em rota" é derivado da data de saída
e do desfecho das notas, não de telemetria. Quando a integração com a Sascar
entrar, a posição passa a ser a fonte de "em rota" e a planilha segue como fonte
de "entregue / reentrega / devolução".
