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
import re
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "data" / "raw"

# aba na planilha -> nome do arquivo em data/raw
ABAS = {
    "CAFÉ - SJC": "cafe-sjc.csv",
    # Layout próprio, com 27 colunas e a coluna MESO separando três praças.
    # Ver COL_SUMARE e MESOS em build_data.py.
    "CAFÉ - SUMARÉ": "cafe-sumare.csv",
    # Descomente conforme for incluindo operações no painel.
}

# Café · Guarulhos por gid, e não por nome — mesmo depois do casamento por nome
# achatado (achatar()), um nome inteiramente novo ainda quebraria a busca. Gid
# não muda nem com o nome reescrito por completo. É a maior operação do
# payload da 3C, então falta dela é crítica: ver ABAS_CAFE_POR_GID abaixo, que
# derruba o robô, diferente de ABAS_POR_GID (Arcor/Supley), que só avisa.
ABAS_CAFE_POR_GID = {
    564645926: ("cafe-gru.csv", "CAFÉ - GRU."),
    # Aba nova em 26/08/2026: antes as entregas de Itapeva vinham misturadas
    # dentro da aba de Guarulhos, e a operação separou numa aba própria. Mesmo
    # tratamento crítico de Guarulhos: falta dela derruba o robô.
    1726352493: ("cafe-itapeva.csv", "CAFÉ - ITAPEVA"),
}

# Abas identificadas pelo gid, e não pelo título.
#
# O gid é o número no fim da URL da aba e não muda quando alguém a renomeia; o
# título muda, e aí o robô deixa de achar a aba e publica um painel sem aquela
# operação — sem quebrar, o que é pior, porque ninguém percebe. As abas de café
# continuam por título por já estarem assim e funcionando. Aba nova entra aqui.
ABAS_POR_GID = {
    734086343: "supley.csv",
    447429539: "arcor.csv",
}

ESCOPO = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def achatar(nome: str) -> str:
    """Nome da aba sem acento, sem pontuação e sem espaço, em maiúsculas.

    Em 21/08/2026 alguém renomeou 'CAFÉ - GRU.' para 'CAFÉ-GRU' — só tirou os
    espaços e o ponto. O robô não achou a aba, avisou no log e publicou o painel
    sem a operação de Guarulhos, que é a maior. Casar por nome achatado
    sobrevive a esse tipo de edição, que numa planilha usada por muita gente é
    questão de tempo.
    """
    s = unicodedata.normalize("NFD", nome or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def salvar(ws, arquivo: str, rotulo: str) -> None:
    linhas = ws.get_all_values()
    caminho = DESTINO / arquivo
    with caminho.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(linhas)
    print(f"  {rotulo} -> {caminho.relative_to(RAIZ)} ({len(linhas)} linhas)")


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
    por_nome = {achatar(t): w for t, w in disponiveis.items()}

    faltando = []
    for aba, arquivo in ABAS.items():
        ws = disponiveis.get(aba) or por_nome.get(achatar(aba))
        if ws is None:
            faltando.append(aba)
            continue
        if ws.title != aba:
            print(f"  nota: aba {aba!r} está como {ws.title!r} na planilha — "
                  "casada pelo nome achatado.")
        salvar(ws, arquivo, ws.title)

    por_gid_todas = {w.id: w for w in disponiveis.values()}
    for gid, (arquivo, rotulo_antigo) in ABAS_CAFE_POR_GID.items():
        ws = por_gid_todas.get(gid)
        if ws is None:
            faltando.append(f"{rotulo_antigo} (gid {gid})")
            continue
        if ws.title != rotulo_antigo:
            print(f"  nota: aba de gid {gid} está como {ws.title!r} "
                  f"(era {rotulo_antigo!r}) — casada por gid.")
        salvar(ws, arquivo, ws.title)

    # Aba faltando derruba o robô, e isso é deliberado.
    #
    # Antes era só um aviso, e o resultado foi um painel publicado sem a
    # operação inteira de Guarulhos: o cliente via zero veículo e acreditava.
    # Parar de atualizar é ruim, mas o selo de frescor na tela mostra que o
    # dado envelheceu. Publicar dado incompleto com cara de completo não
    # mostra nada.
    if faltando:
        sys.exit(
            f"Aba nao encontrada: {', '.join(repr(a) for a in faltando)}.\n"
            f"Abas disponiveis: {', '.join(sorted(disponiveis))}\n"
            "Se foi renomeada, ajuste ABAS em scripts/baixar_planilha.py."
        )

    por_gid = {w.id: w for w in disponiveis.values()}
    for gid, arquivo in ABAS_POR_GID.items():
        ws = por_gid.get(gid)
        if ws is None:
            print(f"  aviso: aba de gid {gid} não encontrada — "
                  f"{arquivo} não será atualizado.")
            continue
        salvar(ws, arquivo, f"{ws.title} (gid {gid})")


if __name__ == "__main__":
    main()
