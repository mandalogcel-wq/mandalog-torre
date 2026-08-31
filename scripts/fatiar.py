#!/usr/bin/env python3
"""
Quebra os dados em um payload por cliente, e prova que não vazou nada entre eles.

Roda depois do build_data.py e do mesclar_greenmile.py, antes do cifrar.py:

    data/operacao.json   café, já com o desfecho do GreenMile
    data/viagens.json    Arcor e Supley
        ↓  fatiar.py
    data/painel-3coracoes.json
    data/painel-arcor.json
    data/painel-supley.json
    data/painel-mandalog.json
        ↓  cifrar.py     uma senha por arquivo

Por que existe
--------------
Cada cliente decifra o próprio arquivo no navegador. Se todos estivessem no
mesmo payload, a senha de um abriria o arquivo inteiro — esconder na tela não
adiantaria, bastaria baixar o `.enc` e ler o campo. Um cliente enxergando
manifesto, motorista e placa da operação de outro não é bug de tela, é
incidente comercial.

Por isso o script não se limita a filtrar: ele **confere o resultado e derruba
o robô** se encontrar registro de um cliente no arquivo de outro. Filtro errado
é fácil de escrever e difícil de notar; a conferência é o que transforma isso
em falha barulhenta em vez de vazamento silencioso.

Uso:
    python3 scripts/fatiar.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "data"
CAFE = DADOS / "operacao.json"
VIAGENS = DADOS / "viagens.json"

# Prefixo da operação no café. `build_data.py` nomeia tudo como "Café · <praça>",
# então o prefixo identifica o cliente sem precisar listar praça por praça — e
# uma praça nova entra sozinha, em vez de silenciosamente ficar de fora.
PREFIXO_CAFE = "Café ·"

# slug -> o que o cliente enxerga.
#   cafe:    inclui as notas de café
#   viagens: nomes de cliente da aba de viagem que entram
# `selo` é a linha que descreve a operação no cabeçalho. Vai dentro do payload
# cifrado, e não no HTML: o script da página é público, e uma lista de clientes
# lá contaria a cada um quem são os outros.
CLIENTES = {
    "3coracoes": {"rotulo": "3 Corações", "cafe": True, "viagens": [],
                  "selo": "Operação de distribuição · Vale do Paraíba"},
    "arcor": {"rotulo": "Arcor", "cafe": False, "viagens": ["Arcor"],
              "selo": "Transferência e coleta · Campinas, Extrema e Bragança"},
    "supley": {"rotulo": "Supley", "cafe": False, "viagens": ["Supley"],
               "selo": "Transferência · Matão, Jundiaí e matriz Guarulhos"},
    # A visão da torre. Senha interna, nunca compartilhada com cliente.
    "mandalog": {"rotulo": "Mandalog", "cafe": True,
                 "viagens": ["Arcor", "Supley"], "interno": True,
                 "selo": "Torre de controle · todas as operações"},
}


def ler(caminho: Path) -> dict:
    if not caminho.exists():
        return {}
    return json.loads(caminho.read_text(encoding="utf-8"))


def norm_placa(v) -> str:
    """Placa comparável: só letra e número. Mesma regra do mesclar_krona.py."""
    s = "".join(c for c in unicodedata.normalize("NFD", str(v or ""))
                if unicodedata.category(c) != "Mn").upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def placas_do(payload: dict) -> set[str]:
    """Placas que este payload já expõe, por viagem ou por frota.

    É o conjunto do que o cliente pode ver. A posição do rastreador só entra
    para placa que já está aqui — quinze veículos rodam para Arcor e para
    Supley, e sem esse recorte a Arcor veria o caminhão dela parado em Matão,
    que é rota do Supley.
    """
    out = set()
    for campo in ("viagens", "frota"):
        for v in payload.get(campo, []):
            for chave in ("placa", "carreta"):
                p = norm_placa(v.get(chave))
                if p:
                    out.add(p)
    return out


def conferir(slug: str, cfg: dict, payload: dict) -> None:
    """Falha se o payload contiver algo que este cliente não pode ver.

    Confere o arquivo pronto, e não a intenção do filtro. É de propósito: o
    filtro é o que pode estar errado, então validá-lo contra ele mesmo não
    provaria nada.
    """
    permitidas = set(cfg["viagens"])
    # Toda lista que carrega `cliente` é conferida, e não só `viagens`. Quando a
    # frota entrou no payload, a conferência que olhava só viagens teria deixado
    # passar a placa e o motorista de outro cliente — o dado mais sensível dos
    # dois. Campo novo com `cliente` dentro tem que entrar aqui junto.
    for campo in ("viagens", "frota"):
        intrusas = sorted({v.get("cliente") for v in payload.get(campo, [])}
                          - permitidas)
        if intrusas:
            sys.exit(f"VAZAMENTO em painel-{slug}: {campo} de "
                     f"{', '.join(map(str, intrusas))} num payload que só pode "
                     f"conter {sorted(permitidas) or 'nenhuma'}.")

    tem_cafe = any(n.get("operacao", "").startswith(PREFIXO_CAFE)
                   for n in payload.get("notas", []))
    if tem_cafe and not cfg["cafe"]:
        sys.exit(f"VAZAMENTO em painel-{slug}: notas de café num payload que "
                 "não deveria ter nenhuma.")

    fora = sorted({n.get("operacao") for n in payload.get("notas", [])
                   if not str(n.get("operacao", "")).startswith(PREFIXO_CAFE)})
    if fora:
        sys.exit(f"VAZAMENTO em painel-{slug}: operações inesperadas "
                 f"({', '.join(map(str, fora))}).")

    # A posição do rastreador é o dado mais sensível que entrou depois: diz onde
    # o veículo está agora. Placa que não aparece na frota nem nas viagens deste
    # payload não pode ter ponto no mapa dele — nem para veículo compartilhado
    # entre dois clientes, que é o caso de quinze placas.
    minhas = placas_do(payload)
    intrusas = sorted({norm_placa(p.get("placa"))
                       for p in (payload.get("rastreamento") or {}).get("posicoes", [])}
                      - minhas)
    if intrusas:
        sys.exit(f"VAZAMENTO em painel-{slug}: posição de rastreador das placas "
                 f"{', '.join(intrusas)}, que não estão na frota nem nas "
                 "viagens deste cliente.")


def main() -> None:
    cafe = ler(CAFE)
    viagens = ler(VIAGENS)
    if not cafe and not viagens:
        sys.exit("Nada para fatiar. Rode antes: python3 scripts/build_data.py")

    notas_cafe = [n for n in cafe.get("notas", [])
                  if str(n.get("operacao", "")).startswith(PREFIXO_CAFE)]
    descartadas = len(cafe.get("notas", [])) - len(notas_cafe)
    if descartadas:
        print(f"  aviso: {descartadas} notas sem operação de café reconhecida "
              "ficaram fora de todos os payloads")

    for slug, cfg in CLIENTES.items():
        payload: dict = {
            "gerado_em": cafe.get("gerado_em") or viagens.get("gerado_em"),
            "cliente": cfg["rotulo"],
            "selo": cfg["selo"],
            "retencao_dias": cafe.get("retencao_dias") or viagens.get("retencao_dias"),
        }

        if cfg["cafe"]:
            planos = {p: m for p, m in (cafe.get("planos") or {}).items()}
            payload["fonte"] = cafe.get("fonte", "")
            payload["planos"] = planos
            payload["notas"] = notas_cafe
            if cafe.get("greenmile"):
                payload["greenmile"] = cafe["greenmile"]
        else:
            payload["notas"] = []
            payload["planos"] = {}

        if cfg["viagens"]:
            payload["viagens"] = [v for v in viagens.get("viagens", [])
                                  if v.get("cliente") in cfg["viagens"]]
            payload["frota"] = [f for f in viagens.get("frota", [])
                                if f.get("cliente") in cfg["viagens"]]
            payload["frota_dias"] = viagens.get("frota_dias")

            # Posição do rastreador, recortada pelas placas que este cliente já
            # vê. O recorte é feito aqui, e não no mesclar_krona.py, porque é
            # aqui que existe a noção de cliente — e é aqui que o `conferir`
            # confere o resultado logo abaixo.
            rastro = viagens.get("rastreamento") or {}
            if rastro.get("disponivel"):
                minhas = placas_do(payload)
                payload["rastreamento"] = dict(
                    rastro,
                    posicoes=[p for p in rastro.get("posicoes", [])
                              if norm_placa(p.get("placa")) in minhas])
        else:
            payload["viagens"] = []
            payload["frota"] = []

        # Os avisos citam número de linha da planilha e erro de apontamento.
        # É material de operação, não de cliente — fica só na visão interna.
        if cfg.get("interno"):
            payload["avisos"] = (cafe.get("avisos") or []) + (viagens.get("avisos") or [])

        conferir(slug, cfg, payload)

        destino = DADOS / f"painel-{slug}.json"
        destino.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        tam = destino.stat().st_size / 1024
        print(f"  painel-{slug}.json: {len(payload['notas'])} notas · "
              f"{len(payload['viagens'])} viagens · {tam:.0f} KB")

    print("  isolamento conferido: nenhum payload contém dado de outro cliente")


if __name__ == "__main__":
    main()
