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

    if payload is None:
        dado.setdefault("greenmile", {"disponivel": False})
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

        if classe_gm == "outro":
            continue
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
    }

    ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  GreenMile: {aplicadas} notas atualizadas "
          f"({divergentes} divergiam do apontamento manual) · "
          f"{len(payload.get('posicoes', []))} posições de veículo")


if __name__ == "__main__":
    main()
