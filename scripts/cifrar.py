#!/usr/bin/env python3
"""
Cifra data/operacao.json em data/operacao.enc usando a senha de acesso ao painel.

Só o arquivo cifrado vai para o repositório. O JSON em texto claro fica no
.gitignore e nunca é publicado — quem baixar operacao.enc sem a senha recebe
bytes aleatórios.

Criptografia: PBKDF2-SHA256 com 600.000 iterações para derivar a chave, e
AES-256-GCM para cifrar. Os mesmos parâmetros são usados no navegador pela
WebCrypto API, sem biblioteca externa.

Variável de ambiente:
    PAINEL_SENHA    senha de acesso ao painel

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
ENTRADA = RAIZ / "data" / "operacao.json"
SAIDA = RAIZ / "data" / "operacao.enc"
IMPRESSAO = RAIZ / "data" / "operacao.hash"

ITERACOES = 600_000


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


def main() -> None:
    senha = os.environ.get("PAINEL_SENHA", "")
    if not senha:
        sys.exit(
            "Defina PAINEL_SENHA.\n"
            "No GitHub: Settings > Secrets and variables > Actions > PAINEL_SENHA."
        )
    if len(senha) < 12:
        sys.exit(
            f"Senha com {len(senha)} caracteres é curta demais. O arquivo cifrado "
            "fica publicamente acessível, então a resistência a tentativa e erro "
            "depende inteiramente da senha. Use no mínimo 12 caracteres, "
            "preferencialmente 20 ou mais."
        )
    if not ENTRADA.exists():
        sys.exit(f"{ENTRADA.relative_to(RAIZ)} não existe. "
                 "Rode antes: python3 scripts/build_data.py")

    claro = ENTRADA.read_bytes()
    marca = impressao(claro)

    if SAIDA.exists() and IMPRESSAO.exists() \
            and IMPRESSAO.read_text(encoding="utf-8").strip() == marca:
        print("  conteúdo idêntico ao publicado — nada a recifrar.")
        return

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    chave = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, ITERACOES, 32)
    cifrado = AESGCM(chave).encrypt(iv, claro, None)

    SAIDA.write_text(json.dumps({
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iter": ITERACOES,
        "cipher": "AES-256-GCM",
        "salt": b64(salt),
        "iv": b64(iv),
        "ct": b64(cifrado),
    }, indent=1), encoding="utf-8")
    IMPRESSAO.write_text(marca + "\n", encoding="utf-8")

    print(f"  {ENTRADA.name} ({len(claro)/1024:.0f} KB) -> "
          f"{SAIDA.name} ({SAIDA.stat().st_size/1024:.0f} KB)")
    print(f"  PBKDF2-SHA256 · {ITERACOES:,} iterações · AES-256-GCM".replace(",", "."))


if __name__ == "__main__":
    main()
