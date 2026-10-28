#!/usr/bin/env python3
"""
Funde o desfecho do GreenMile em data/operacao.json, antes de cifrar.

Roda depois de build_data.py e antes de cifrar.py. Busca a API da Torre no n8n
(workflow "Torre 3C · API GreenMile"), que por sua vez lê as tabelas alimentadas
pelos dois workflows de coleta (CD GRU e CD Campinas/Sumaré) — cada um autentica
no GreenMile com seu próprio usuário, porque nenhum usuário enxerga as cinco
operações de uma vez.

A chave de cruzamento é (plano, nf) — confirmada batendo peso planejado e
realizado, casa decimal por casa decimal, contra a planilha. O GreenMile nunca
substitui a planilha como fonte de quais notas existem: só complementa o
desfecho de notas que a planilha já lista. Nota sem correspondência no
GreenMile mantém o STATUS SAC digitado, sem alteração nenhuma.

Se a API do n8n estiver fora do ar, o script não falha o robô — publica o
payload só com o dado da planilha, como sempre foi. Falha aqui não pode
derrubar a atualização do painel, que é o que importa.

Variáveis de ambiente:
    N8N_WEBHOOK_URL     URL de produção do webhook (https://.../webhook/torre-gm)
    N8N_WEBHOOK_TOKEN   valor do header enviado pela credencial Header Auth
    N8N_WEBHOOK_HEADER  nome do header (padrão: veja HEADER_PADRAO)

Uso:
    python3 scripts/build_data.py
    python3 scripts/mesclar_greenmile.py
    python3 scripts/cifrar.py
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
ARQUIVO = RAIZ / "data" / "operacao.json"

HEADER_PADRAO = "X-Torre-Token"

# GreenMile -> classe do painel. Mesmo vocabulário de build_data.py:classificar,
# porque as duas fontes descrevem o mesmo conjunto de desfechos possíveis.
CLASSE_GM = {
    "DELIVERED": "entregue",
    "REDELIVERED": "reentregar",
    "UNDELIVERED": "devolucao",
    "PARTIALLY_DELIVERED": "devolucao",
    "PENDING": "andamento",
    "IN_PROGRESS": "andamento",
}

# Só desfecho fechado pode sobrepor o apontamento manual. `PENDING` no GreenMile
# significa "parada ainda aberta", e parada fica aberta por muito tempo: depois
# que a consulta passou a buscar por número de plano em vez de janela de data,
# vieram 493 notas nesse estado, várias de rotas de julho já entregues e
# apontadas na planilha. Deixar `PENDING` sobrescrever rebaixaria essas notas
# para "em andamento" e faria o painel piorar justamente onde a operação já
# tinha feito o trabalho. O status ainda é guardado em `gm_status`, para
# consulta, mas não manda na classe.
CLASSE_FECHADA = {"entregue", "reentregar", "devolucao"}

# Cada coletor do n8n cobre um grupo de praças e grava a sua fatia (`origem`)
# nas tabelas. Quando um coletor falha, a fatia dele não é apagada: as linhas
# antigas continuam lá e a API segue respondendo. Em 27/10/2026 a credencial
# do CD GRU passou a levar 401 e SJC, Guarulhos e Itapeva ficaram um dia
# inteiro sem GreenMile sem nenhum aviso, porque `disponivel` só olha se a API
# respondeu. O carimbo `atualizado_em` de cada fatia é o que denuncia o coletor
# parado.
ORIGEM_OPERACOES = {
    "CD GRU": ["Café · SJC", "Café · Guarulhos", "Café · Itapeva"],
    "CD CAMPINAS": ["Café · CD Sumaré", "Café · Meso Bauru", "Café · Meso Marília"],
}
# Os coletores rodam a cada 30 min. Duas rodadas perdidas e mais folga.
MINUTOS_ATRASO = 75


def frescor_coletores(payload: dict | None) -> tuple[dict, list[str]]:
    """Última coleta de cada fatia e as praças cujo coletor está parado.

    Sem payload (API fora do ar), todas as praças ficam sem GreenMile.
    """
    ultima: dict[str, str] = {}
    for linha in (payload or {}).get("entregas", []) + (payload or {}).get("posicoes", []):
        o, t = linha.get("origem"), linha.get("atualizado_em") or ""
        if o and t > ultima.get(o, ""):
            ultima[o] = t
    agora = datetime.now(timezone.utc)
    fontes, sem_gm = {}, []
    for origem, operacoes in ORIGEM_OPERACOES.items():
        t = ultima.get(origem, "")
        try:
            idade = (agora - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            idade = None
        atrasada = payload is None or idade is None or idade > MINUTOS_ATRASO
        fontes[origem] = {"ultima_coleta": t, "atrasada": atrasada, "operacoes": operacoes}
        if atrasada:
            sem_gm += operacoes
    return fontes, sem_gm


def buscar_gm() -> dict | None:
    url = os.environ.get("N8N_WEBHOOK_URL", "").strip()
    token = os.environ.get("N8N_WEBHOOK_TOKEN", "").strip()
    if not url or not token:
        print("  N8N_WEBHOOK_URL/N8N_WEBHOOK_TOKEN não definidos — "
              "seguindo só com a planilha.")
        return None

    header = os.environ.get("N8N_WEBHOOK_HEADER", HEADER_PADRAO).strip() or HEADER_PADRAO
    req = urllib.request.Request(url, headers={header: token})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  aviso: GreenMile indisponível via n8n ({e}) — "
              "seguindo só com a planilha.")
        return None
    except json.JSONDecodeError as e:
        print(f"  aviso: resposta do n8n não é JSON válido ({e}) — "
              "seguindo só com a planilha.")
        return None


def main() -> None:
    if not ARQUIVO.exists():
        sys.exit(f"{ARQUIVO.relative_to(RAIZ)} não existe. "
                 "Rode antes: python3 scripts/build_data.py")

    payload = buscar_gm()
    dado = json.loads(ARQUIVO.read_text(encoding="utf-8"))

    fontes, sem_gm = frescor_coletores(payload)
    for origem, f in fontes.items():
        if f["atrasada"]:
            print(f"  aviso: coletor {origem} sem atualização desde "
                  f"{f['ultima_coleta'] or 'nunca'} — {', '.join(f['operacoes'])} sem GreenMile")

    if payload is None:
        dado.setdefault("greenmile", {"disponivel": False})
        dado["greenmile"]["coletores"] = fontes
        dado["greenmile"]["operacoes_sem_gm"] = sem_gm
        ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1), encoding="utf-8")
        return

    entregas_gm = {(e["plano"], str(e["nf"]).strip()): e for e in payload.get("entregas", [])}

    aplicadas = divergentes = 0
    for nota in dado["notas"]:
        if nota["tipo"] != "ENTREGA":
            continue
        nota.setdefault("sac_origem", "planilha")
        g = entregas_gm.get((nota["plano"], str(nota["nf"]).strip()))
        if not g:
            continue

        classe_gm = CLASSE_GM.get(g["status"], "outro")
        nota["gm_status"] = g["status"]
        nota["gm_placa"] = g.get("placa") or nota.get("placa")
        nota["gm_atualizado_em"] = g.get("atualizado_em")
        # Horário real da parada, vindo do GreenMile em UTC. Diferente de
        # gm_atualizado_em, que é só o carimbo de quando o coletor rodou.
        # Sondado em 02/09/2026: os atributos válidos são stop.actualArrival e
        # stop.actualDeparture — as variantes com sufixo "Time" dão erro 500.
        nota["gm_chegada"] = g.get("chegada") or ""
        nota["gm_partida"] = g.get("partida") or ""
        # Peso da NF: o planejado do GreenMile bate com a planilha casa decimal
        # por casa decimal onde os dois existem, e cobre Sumaré, cuja aba não
        # tem coluna de peso. Planejado, não realizado: numa devolução parcial
        # o realizado encolhe, e o peso da nota é o que saiu carregado.
        if float(g.get("peso_planejado") or 0) > 0:
            nota["peso_kg"] = round(float(g["peso_planejado"]), 3)
            nota["peso_origem"] = "greenmile"

        # Desfecho não fechado no GreenMile só vale onde a planilha também não
        # tem nada. Nunca rebaixa uma nota já apontada.
        if classe_gm not in CLASSE_FECHADA:
            if nota["classe"] == "semapont" and classe_gm == "andamento":
                nota["classe"] = "andamento"
                nota["sac_origem"] = "greenmile"
                aplicadas += 1
            continue
        # Se o GreenMile fechou a parada, houve baixa eletrônica — é o que a
        # palavra significa. A coluna GM da planilha é transcrição manual e
        # atrasa: dava "GreenMile 0%" em veículo com as quatro notas baixadas
        # pelo próprio motorista no aplicativo, subestimando a adesão.
        nota["gm_ok"] = True
        if nota["classe"] != classe_gm:
            divergentes += 1
        # O GreenMile é a fonte primária do desfecho onde cobre: o motorista
        # lança direto, sem a espera e sem o erro de transcrição manual. Nota
        # sem correspondência mantém o que veio da planilha, intocado.
        nota["classe"] = classe_gm
        nota["sac_origem"] = "greenmile"
        aplicadas += 1

    dado["greenmile"] = {
        "disponivel": True,
        "gerado_em": payload.get("gerado_em"),
        "posicoes": payload.get("posicoes", []),
        "coletores": fontes,
        "operacoes_sem_gm": sem_gm,
    }

    ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  GreenMile: {aplicadas} notas atualizadas "
          f"({divergentes} divergiam do apontamento manual) · "
          f"{len(payload.get('posicoes', []))} posições de veículo")
    ent = [n for n in dado["notas"] if n["tipo"] == "ENTREGA"]
    por_origem = {o: sum(1 for n in ent if n.get("peso_origem") == o) for o in ("greenmile", "planilha")}
    corte = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    print(f"  peso: {por_origem['greenmile']} do GreenMile · {por_origem['planilha']} da planilha · "
          f"{len(ent) - sum(por_origem.values())} sem peso")
    for op in sorted({n["operacao"] for n in ent}):
        recentes = [n for n in ent if n["operacao"] == op and (n.get("data") or "") >= corte]
        com = sum(1 for n in recentes if n.get("peso_kg"))
        print(f"    {op}: {com}/{len(recentes)} notas dos últimos 30 dias com peso")
        perfis = {}
        for n in recentes:
            perfis[n.get("perfil") or "(vazio)"] = perfis.get(n.get("perfil") or "(vazio)", 0) + 1
        print("      perfis: " + ", ".join(f"{k} {v}" for k, v in sorted(perfis.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
