#!/usr/bin/env python3
"""
Converte os CSV exportados da planilha ROTEIRIZAÇÃO 2026 no JSON que o painel consome.

Uso:
    python3 scripts/build_data.py

Lê todos os arquivos de data/raw/*.csv e grava data/operacao.json.

Grão do dado: uma linha do CSV = uma nota fiscal em uma tentativa de entrega.
A DATA SAÍDA é por nota, não por plano — reentregas recebem a data da nova saída
mantendo o mesmo número de plano. O painel depende disso para separar os dias.
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / "data" / "raw"
SAIDA = RAIZ / "data" / "operacao.json"

# Nome da operação por arquivo. Acrescente aqui ao incluir novas abas.
OPERACOES = {
    "cafe-sjc": "Café · SJC",
    "cafe-gru": "Café · Guarulhos",
}

# Este é um painel de status diário, não um relatório histórico. A aba de
# Guarulhos guarda o ano inteiro — 15 mil linhas, que virariam um .enc de 11 MB
# baixado e decifrado a cada abertura, e um repositório que bate no limite de
# 1 GB do Pages em poucos meses. Cortar em 30 dias mantém o arquivo em ~1,3 MB.
# Notas sem data de saída ficam, porque são justamente as que pedem atenção.
RETENCAO_DIAS = 7

# A planilha tem dois pares de colunas com o mesmo cabeçalho (CARREGAMENTO e
# PAGAMENTO). O csv.reader preserva a ordem, então usamos o índice da coluna.
COL = {
    "emissao": 0,
    "manifesto": 1,
    "faixa": 2,
    "data_saida": 3,
    "origem": 4,
    "saida_num": 5,
    "status": 6,
    "cod_cliente": 8,
    "plano": 9,
    "cliente": 10,
    "nf": 11,
    "peso": 12,
    "cidade": 13,
    "motorista": 14,
    "placa": 15,
    "perfil_cobranca": 16,
    "status_sac": 19,
    "greenmile": 20,
    "canhoto": 23,
}


def limpar(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").replace("\u200e", "").strip())


def titulo(v: str) -> str:
    v = limpar(v)
    return v.title() if v.isupper() or v.islower() else v


def cidade(v: str) -> str:
    v = limpar(v).upper().replace(" - SP", "")
    return v.title()


def sem_acento(v: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", v) if unicodedata.category(c) != "Mn"
    ).upper()


def classificar(sac: str) -> str:
    """Traduz a coluna STATUS SAC nas cinco classes exibidas no painel."""
    s = sem_acento(limpar(sac))
    if not s:
        return "semapont"
    if s.startswith("ENTREGUE"):
        return "entregue"
    if "REENTREG" in s:
        return "reentregar"
    if "DEVOLU" in s or "RECUSA" in s:
        return "devolucao"
    return "andamento"


def data_iso(v: str) -> str:
    v = limpar(v)
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", v)
    if not m:
        return ""
    d, mes, ano = m.groups()
    if not (1 <= int(mes) <= 12 and 1 <= int(d) <= 31 and 2020 <= int(ano) <= 2100):
        return ""
    return f"{ano}-{mes}-{d}"


def ler_csv(caminho: Path) -> tuple[list[dict], list[str]]:
    notas: list[dict] = []
    avisos: list[str] = []
    operacao = OPERACOES.get(caminho.stem, caminho.stem)

    with caminho.open(encoding="utf-8-sig", newline="") as fh:
        linhas = list(csv.reader(fh))

    if not linhas:
        return notas, [f"{caminho.name}: arquivo vazio"]

    for i, linha in enumerate(linhas[1:], start=2):
        if len(linha) <= COL["greenmile"]:
            linha = linha + [""] * (COL["greenmile"] + 1 - len(linha))
        get = lambda k: limpar(linha[COL[k]])

        if not any(linha):
            continue

        iso = data_iso(get("data_saida"))
        if get("data_saida") and not iso:
            avisos.append(f"{caminho.name} linha {i}: data de saída inválida "
                          f"({get('data_saida')!r})")

        notas.append({
            "operacao": operacao,
            "linha": i,
            "tipo": sem_acento(get("status")) or "ENTREGA",
            "plano": get("plano"),
            "manifesto": get("manifesto"),
            "data": iso,
            "origem": get("origem"),
            "nf": get("nf"),
            "cliente": get("cliente"),
            "cidade": cidade(get("cidade")),
            "peso": get("peso"),
            "motorista": titulo(get("motorista")),
            "placa": get("placa").upper(),
            "perfil": get("perfil_cobranca").upper(),
            "sac": get("status_sac"),
            "classe": classificar(get("status_sac")),
            "gm": get("greenmile"),
            "gm_ok": "ELETR" in sem_acento(get("greenmile")),
            "emitido": sem_acento(get("emissao")) == "EMITIDO",
            "canhoto": get("canhoto"),
        })

    return notas, avisos


def divergencias(notas: list[dict]) -> dict:
    """Inconsistências que impedem o indicador de fechar. Só uso interno."""
    entregas = [n for n in notas if n["tipo"] == "ENTREGA"]
    return {
        "sem_data_saida": sorted({n["plano"] for n in entregas if not n["data"]} - {""}),
        "sem_motorista": sorted({n["plano"] for n in entregas if not n["motorista"]} - {""}),
        "sem_emissao": sorted({n["plano"] for n in entregas if not n["emitido"]} - {""}),
        "entregue_sem_greenmile": sum(
            1 for n in entregas if n["classe"] == "entregue" and not n["gm_ok"]
        ),
        "greenmile_sem_sac": sum(
            1 for n in entregas if n["gm_ok"] and n["classe"] == "semapont"
        ),
        "sem_peso": sum(1 for n in entregas if not n["peso"]),
    }


def main() -> None:
    arquivos = sorted(ENTRADA.glob("*.csv"))
    if not arquivos:
        raise SystemExit(f"Nenhum CSV encontrado em {ENTRADA}. "
                         "Exporte a aba da planilha e salve aqui.")

    agora = datetime.now(timezone(timedelta(hours=-3)))
    corte = (agora.date() - timedelta(days=RETENCAO_DIAS)).isoformat()

    notas: list[dict] = []
    avisos: list[str] = []
    for arq in arquivos:
        n, a = ler_csv(arq)
        recentes = [x for x in n if not x["data"] or x["data"] >= corte]
        notas.extend(recentes)
        avisos.extend(a)
        fora = len(n) - len(recentes)
        sufixo = f" ({fora} fora da janela de {RETENCAO_DIAS} dias)" if fora else ""
        print(f"  {arq.name}: {len(recentes)} notas{sufixo}")

    # Metadados por plano, a partir da primeira linha que traz cada informação.
    planos: dict[str, dict] = {}
    for n in notas:
        if not n["plano"]:
            continue
        p = planos.setdefault(n["plano"], {
            "motorista": "", "placa": "", "perfil": "",
            "origem": "", "operacao": n["operacao"],
        })
        for campo in ("motorista", "placa", "perfil", "origem"):
            if not p[campo] and n[campo]:
                p[campo] = n[campo]

    saida = {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "fonte": "Planilha ROTEIRIZAÇÃO 2026 · export das abas em data/raw",
        "retencao_dias": RETENCAO_DIAS,
        "arquivos": [a.name for a in arquivos],
        "planos": planos,
        "notas": notas,
        # As divergências de apontamento não vão no payload publicado: o painel
        # é acessível ao cliente, e esconder a seção no HTML não bastaria —
        # bastaria baixar o .enc e ler o campo. Elas seguem impressas abaixo,
        # visíveis no log do robô e ao rodar local.
        "avisos": avisos,
    }

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(
        json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    entregas = [n for n in notas if n["tipo"] == "ENTREGA"]
    print(f"\n{SAIDA.relative_to(RAIZ)} gravado")
    print(f"  {len(entregas)} notas de entrega · {len(planos)} planos "
          f"· {len({n['data'] for n in entregas if n['data']})} dias")
    for k, v in divergencias(notas).items():
        if v:
            print(f"  divergência · {k}: {v if isinstance(v, int) else ', '.join(v)}")
    for a in avisos:
        print(f"  aviso · {a}")


if __name__ == "__main__":
    main()
