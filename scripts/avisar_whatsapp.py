#!/usr/bin/env python3
"""
Monta o resumo da posição de entrega do dia e posta no grupo do WhatsApp.

Lê data/operacao.json — o mesmo dado do painel, gerado por build_data.py — e
envia um texto curto pela Z-API. Roda depois do build_data.py no workflow.

Por que Z-API e não a API oficial: a WhatsApp Cloud API da Meta não envia para
grupos, só para conversas 1:1. Grupo só por cima do WhatsApp Web, que é o que a
Z-API faz. Use um chip dedicado — o número corre risco de bloqueio.

O grupo do Vale tem gente da 3 Corações. Por isso a mensagem sai na versão
pública: sem a contagem de notas sem apontamento, que é pendência interna de
lançamento e não posição de entrega. Para incluir, defina MOSTRAR_INTERNO=1.

Variáveis de ambiente:
    ZAPI_INSTANCE        id da instância na Z-API
    ZAPI_TOKEN           token da instância
    ZAPI_CLIENT_TOKEN    token de segurança da conta (header Client-Token)
    ZAPI_GRUPO           id do grupo, no formato 1203630000000000@g.us
    MOSTRAR_INTERNO      1 para incluir dados internos (padrão: não)
    JANELA               faixa horária de envio, Brasília (padrão: 6-20)
    PAINEL_URL           link mostrado no rodapé da mensagem

Sem as variáveis da Z-API o script apenas imprime a mensagem e sai com 0, o que
serve para testar o formato sem enviar nada:

    python3 scripts/build_data.py && python3 scripts/avisar_whatsapp.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / "data" / "operacao.json"

BRASILIA = timezone(timedelta(hours=-3))
PAINEL_PADRAO = "mandalogcel-wq.github.io/mandalog-torre"

# Mesmo agrupamento do painel: devolução reúne três desfechos distintos.
DEVOLUCAO = {"devolucao"}


def pct(a: int, b: int) -> int:
    return round(100 * a / b) if b else 0


def primeiro_nome(nome: str) -> str:
    """Nome curto cabe melhor na notificação e evita a linha quebrar no meio."""
    partes = [p for p in nome.split() if p]
    if not partes:
        return "—"
    return " ".join(partes[:2]).upper()


def dentro_da_janela(agora: datetime, janela: str) -> bool:
    try:
        ini, fim = (int(x) for x in janela.split("-", 1))
    except ValueError:
        sys.exit(f"JANELA inválida: {janela!r}. Use algo como '6-20'.")
    return ini <= agora.hour <= fim


def carregar() -> dict:
    if not ENTRADA.exists():
        sys.exit(f"{ENTRADA.relative_to(RAIZ)} não existe. "
                 "Rode antes: python3 scripts/build_data.py")
    return json.loads(ENTRADA.read_text(encoding="utf-8"))


def montar(base: dict, dia: str, interno: bool, painel: str) -> str | None:
    """Devolve o texto do resumo, ou None se não houve operação no dia."""
    notas = [n for n in base["notas"] if n["tipo"] == "ENTREGA" and n["data"] == dia]
    if not notas:
        return None

    total = len(notas)
    conta = lambda k: sum(1 for n in notas if n["classe"] == k)  # noqa: E731
    entregues = conta("entregue")
    andamento = conta("andamento")
    reentrega = conta("reentregar")
    devolucao = sum(1 for n in notas if n["classe"] in DEVOLUCAO)
    semapont = conta("semapont")
    gm_ok = sum(1 for n in notas if n["classe"] == "entregue" and n.get("gm_ok"))

    # Um plano é um veículo no dia. "Em rota" é o que ainda tem nota sem
    # desfecho — não vem de telemetria, vem do apontamento.
    planos: dict[str, list[dict]] = {}
    for n in notas:
        planos.setdefault(n.get("plano") or "(sem plano)", []).append(n)

    em_rota, finalizados, sem_entrega = [], 0, 0
    for plano, ns in planos.items():
        meta = base["planos"].get(plano, {})
        abertos = sum(1 for n in ns if n["classe"] in ("semapont", "andamento"))
        feitas = sum(1 for n in ns if n["classe"] == "entregue")
        if not meta.get("motorista"):
            continue
        if abertos:
            em_rota.append({
                "motorista": primeiro_nome(meta.get("motorista", "")),
                "placa": meta.get("placa", "—"),
                "feitas": feitas,
                "total": len(ns),
                "cidades": sorted({n["cidade"] for n in ns if n.get("cidade")}),
            })
        elif feitas:
            finalizados += 1
        else:
            sem_entrega += 1

    em_rota.sort(key=lambda v: (v["feitas"] / v["total"] if v["total"] else 0))

    agora = datetime.now(BRASILIA)
    ops = " · ".join(sorted({n["operacao"] for n in notas}))
    d = f"{dia[8:10]}/{dia[5:7]}"

    L = [
        f"🚚 *TORRE VALE DO PARAÍBA* · {d} {agora:%Hh}",
        ops,
        "",
        f"Em rota {len(em_rota)} · Finalizados {finalizados} · S/ entrega {sem_entrega}",
        "",
        f"*NOTAS DO DIA: {total}*",
        f"✅ Entregues      {entregues}  ({pct(entregues, total)}%)",
        f"⏳ Em andamento   {andamento}",
        f"↩️ Devolução      {devolucao}  ({pct(devolucao, total)}%)",
        f"🔄 Reentrega      {reentrega}  ({pct(reentrega, total)}%)",
        f"📱 GreenMile         {pct(gm_ok, entregues)}%",
    ]

    # Nota sem Status SAC é desfecho não informado, não entrega não realizada.
    # Some da versão que vai ao grupo com o cliente, mas nunca vira "pendente".
    if interno and semapont:
        L.append(f"⚠️ Sem apontamento {semapont}")

    if em_rota:
        L += ["", "*EM ROTA AGORA*"]
        for v in em_rota:
            L.append(f"• {v['motorista']} · {v['placa']} · {v['feitas']}/{v['total']} notas")
            if v["cidades"]:
                cidades = " · ".join(v["cidades"][:3])
                if len(v["cidades"]) > 3:
                    cidades += f" +{len(v['cidades']) - 3}"
                L.append(f"  {cidades}")
    else:
        L += ["", "Nenhum veículo em rota no momento."]

    L += ["", f"Painel: {painel}"]
    return "\n".join(L)


def enviar(texto: str) -> None:
    inst = os.environ.get("ZAPI_INSTANCE", "").strip()
    token = os.environ.get("ZAPI_TOKEN", "").strip()
    cliente = os.environ.get("ZAPI_CLIENT_TOKEN", "").strip()
    grupo = os.environ.get("ZAPI_GRUPO", "").strip()

    if not (inst and token and grupo):
        print("Z-API não configurada — mensagem não enviada. Prévia:\n")
        print(texto)
        return

    url = f"https://api.z-api.io/instances/{inst}/token/{token}/send-text"
    corpo = json.dumps({"phone": grupo, "message": texto}).encode()
    req = urllib.request.Request(url, data=corpo, method="POST")
    req.add_header("Content-Type", "application/json")
    if cliente:
        req.add_header("Client-Token", cliente)

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  enviado ao grupo · HTTP {r.status} · {len(texto)} caracteres")
    except urllib.error.HTTPError as e:
        # Falhar aqui não pode derrubar a atualização do painel, que é o que
        # importa. Registra e segue.
        sys.exit(f"Z-API recusou o envio: HTTP {e.code} · {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"Não foi possível falar com a Z-API: {e.reason}")


def main() -> None:
    janela = os.environ.get("JANELA", "6-20").strip()
    agora = datetime.now(BRASILIA)
    if not dentro_da_janela(agora, janela):
        print(f"  {agora:%H:%M} está fora da janela {janela}h — nada a enviar.")
        return

    base = carregar()
    interno = os.environ.get("MOSTRAR_INTERNO", "").strip() == "1"
    painel = os.environ.get("PAINEL_URL", PAINEL_PADRAO).strip()

    dia = agora.strftime("%Y-%m-%d")
    texto = montar(base, dia, interno, painel)
    if texto is None:
        print(f"  sem notas com saída em {dia} — nada a enviar.")
        return

    enviar(texto)


if __name__ == "__main__":
    main()
