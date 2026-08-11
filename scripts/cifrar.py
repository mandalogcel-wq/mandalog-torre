#!/usr/bin/env python3
"""
Cifra um payload por cliente, cada um com a sua senha.

    data/painel-3coracoes.json  -> data/operacao.enc   PAINEL_SENHA
    data/painel-arcor.json      -> data/arcor.enc      SENHA_ARCOR
    data/painel-supley.json     -> data/supley.enc     SENHA_SUPLEY
    data/painel-mandalog.json   -> data/mandalog.enc   SENHA_MANDALOG

Só os arquivos cifrados vão para o repositório. Os JSON em texto claro estão no
.gitignore e nunca são publicados — quem baixar um .enc sem a senha recebe bytes
aleatórios.

Criptografia: PBKDF2-SHA256 com 600.000 iterações para derivar a chave, e
AES-256-GCM para cifrar. Os mesmos parâmetros são usados no navegador pela
WebCrypto API, sem biblioteca externa.

Modo antigo
-----------
Se `data/painel-3coracoes.json` não existir, o script volta a cifrar
`data/operacao.json` em `data/operacao.enc`, como sempre fez. Isso é o que
permite subir este arquivo antes de atualizar o workflow sem derrubar o painel
que está no ar: enquanto o robô não chamar o `fatiar.py`, nada muda.

Uso local:
    PAINEL_SENHA='...' python3 scripts/cifrar.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "data"

ITERACOES = 600_000
MINIMO_SENHA = 12

# O nome de saída da 3 Corações continua sendo `operacao.enc`, e não
# `3coracoes.enc`: é o arquivo que o painel no ar já busca e que o cliente já
# acessa. Renomear por simetria quebraria o que funciona, para ganhar estética.
PAYLOADS = [
    {"slug": "3coracoes", "saida": "operacao.enc",
     "env": "PAINEL_SENHA", "obrigatorio": True},
    {"slug": "arcor", "saida": "arcor.enc",
     "env": "SENHA_ARCOR", "obrigatorio": False},
    {"slug": "supley", "saida": "supley.enc",
     "env": "SENHA_SUPLEY", "obrigatorio": False},
    {"slug": "mandalog", "saida": "mandalog.enc",
     "env": "SENHA_MANDALOG", "obrigatorio": False},
]


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def impressao(claro: bytes) -> str:
    """Hash do conteúdo, ignorando o carimbo de geração.

    Salt e IV são sorteados a cada execução, e `gerado_em` muda sempre. Sem isso
    o .enc sairia diferente a cada rodada mesmo com a planilha intacta, e o robô
    comitaria de hora em hora para sempre. Comparar o conteúdo em si é o que
    permite não republicar quando nada mudou.
    """
    d = json.loads(claro)
    d.pop("gerado_em", None)
    canon = json.dumps(d, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(canon).hexdigest()


def validar_senha(senha: str, env: str) -> None:
    if len(senha) < MINIMO_SENHA:
        sys.exit(
            f"{env}: senha com {len(senha)} caracteres é curta demais. O arquivo "
            "cifrado fica publicamente acessível, então a resistência a tentativa "
            f"e erro depende inteiramente da senha. Mínimo {MINIMO_SENHA} "
            "caracteres, preferencialmente 20 ou mais."
        )


def cifrar(entrada: Path, saida: Path, senha: str) -> bool:
    """Cifra se o conteúdo mudou. Devolve True se gravou."""
    claro = entrada.read_bytes()
    marca = impressao(claro)
    hash_path = saida.with_suffix(".hash")

    if saida.exists() and hash_path.exists() \
            and hash_path.read_text(encoding="utf-8").strip() == marca:
        print(f"  {saida.name}: conteúdo idêntico ao publicado — nada a recifrar.")
        return False

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    chave = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, ITERACOES, 32)
    cifrado = AESGCM(chave).encrypt(iv, claro, None)

    saida.write_text(json.dumps({
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iter": ITERACOES,
        "cipher": "AES-256-GCM",
        "salt": b64(salt),
        "iv": b64(iv),
        "ct": b64(cifrado),
    }, indent=1), encoding="utf-8")
    hash_path.write_text(marca + "\n", encoding="utf-8")

    print(f"  {entrada.name} ({len(claro)/1024:.0f} KB) -> "
          f"{saida.name} ({saida.stat().st_size/1024:.0f} KB)")
    return True


def modo_antigo() -> None:
    entrada = DADOS / "operacao.json"
    if not entrada.exists():
        sys.exit(f"{entrada.relative_to(RAIZ)} não existe. "
                 "Rode antes: python3 scripts/build_data.py")
    senha = os.environ.get("PAINEL_SENHA", "")
    if not senha:
        sys.exit("Defina PAINEL_SENHA.\n"
                 "No GitHub: Settings > Secrets and variables > Actions.")
    validar_senha(senha, "PAINEL_SENHA")
    print("  fatiar.py não rodou — cifrando operacao.json no modo antigo.")
    cifrar(entrada, DADOS / "operacao.enc", senha)


def main() -> None:
    if not (DADOS / "painel-3coracoes.json").exists():
        modo_antigo()
        return

    # Conferir tudo antes de gravar qualquer coisa.
    #
    # Numa primeira versão a checagem acontecia dentro do laço, e a trava de
    # senha repetida disparava depois de o arquivo anterior já estar gravado —
    # deixando o repositório meio publicado, com um cliente atualizado e outro
    # não. Configuração se valida inteira, antes de produzir efeito.
    trabalho: list[tuple[Path, Path, str]] = []
    senhas: dict[str, str] = {}
    for p in PAYLOADS:
        entrada = DADOS / f"painel-{p['slug']}.json"
        senha = os.environ.get(p["env"], "")

        if not entrada.exists():
            print(f"  aviso: {entrada.name} não existe — {p['saida']} não será "
                  "atualizado.")
            continue

        if not senha:
            if p["obrigatorio"]:
                sys.exit(f"Defina {p['env']}. Sem ela o painel da 3 Corações, que "
                         "está no ar, deixa de ser atualizado.")
            # Cliente ainda sem senha configurada não é erro: é implantação em
            # andamento. Mas precisa aparecer, senão o painel dele congela em
            # silêncio e ninguém liga uma coisa à outra semanas depois.
            print(f"  aviso: {p['env']} não definida — {p['saida']} não será "
                  f"publicado, e o painel de {p['slug']} fica sem dado.")
            continue

        validar_senha(senha, p["env"])

        # Senha repetida entre clientes anula todo o fatiamento: quem tem a de um
        # decifra o arquivo do outro. É erro de configuração, silencioso, e do
        # tipo que só se descobre no pior momento possível.
        if senha in senhas:
            sys.exit(f"{p['env']} é igual a {senhas[senha]}. Cada cliente precisa "
                     "de senha própria — senão a senha de um abre o arquivo do "
                     "outro e o isolamento por payload não vale nada.")
        senhas[senha] = p["env"]
        trabalho.append((entrada, DADOS / p["saida"], senha))

    for entrada, saida, senha in trabalho:
        cifrar(entrada, saida, senha)

    print(f"  PBKDF2-SHA256 · {ITERACOES:,} iterações · AES-256-GCM"
          .replace(",", "."))


if __name__ == "__main__":
    main()
