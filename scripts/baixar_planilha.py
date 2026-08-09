#!/usr/bin/env python3
"""
Baixa abas da planilha ROTEIRIZAÇÃO 2026 e grava em data/raw/*.csv.

Usa uma conta de serviço do Google, o que mantém a planilha privada: em vez de
publicá-la na web, você a compartilha com o e-mail da conta de serviço como
Leitor. Nada é exposto publicamente.

Variáveis de ambiente:
    SHEET_ID        id da planilha (o trecho entre /d/ e /edit na URL)
    GOOGLE_SA_JSON  conteúdo do JSON da conta de serviço

Uso local, para testar antes de subir:
    export SHEET_ID="..."
    export GOOGLE_SA_JSON="$(cat conta-servico.json)"
    python3 scripts/baixar_planilha.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "data" / "raw"

# aba na planilha -> nome do arquivo em data/raw
ABAS = {
    "CAFÉ - SJC": "cafe-sjc.csv",
    # A aba de Guarulhos guarda o ano inteiro, ~15 mil linhas. Baixa tudo e o
    # corte por data acontece no build_data.py (RETENCAO_DIAS).
    "CAFÉ - GRU.": "cafe-gru.csv",
    # Layout próprio, com 27 colunas e a coluna MESO separando três praças.
    # Ver COL_SUMARE e MESOS em build_data.py.
    "CAFÉ - SUMARÉ": "cafe-sumare.csv",
    # Descomente conforme for incluindo operações no painel.
}

ESCOPO = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def main() -> None:
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    sa_json = os.environ.get("GOOGLE_SA_JSON", "").strip()

    if not sheet_id or not sa_json:
        sys.exit(
            "Defina SHEET_ID e GOOGLE_SA_JSON.\n"
            "No GitHub: Settings > Secrets and variables > Actions."
        )

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        sys.exit("Instale as dependências: pip install gspread google-auth")

    cred = Credentials.from_service_account_info(json.loads(sa_json), scopes=ESCOPO)
    planilha = gspread.authorize(cred).open_by_key(sheet_id)

    DESTINO.mkdir(parents=True, exist_ok=True)
    disponiveis = {w.title: w for w in planilha.worksheets()}

    for aba, arquivo in ABAS.items():
        ws = disponiveis.get(aba)
        if ws is None:
            print(f"  aviso: aba {aba!r} não encontrada. "
                  f"Abas disponíveis: {', '.join(sorted(disponiveis))}")
            continue

        linhas = ws.get_all_values()
        caminho = DESTINO / arquivo
        with caminho.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(linhas)
        print(f"  {aba} -> {caminho.relative_to(RAIZ)} ({len(linhas)} linhas)")


if __name__ == "__main__":
    main()
