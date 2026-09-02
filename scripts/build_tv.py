#!/usr/bin/env python3
"""Gera data/tv.json — o alimento da torre de parede (tv.html).

Roda depois de mesclar_greenmile.py e antes de cifrar.py. Não coleta nada:
lê o data/operacao.json que o resto do pipeline já produziu e reduz ao que a
parede precisa.

POR QUE UM ARQUIVO EM CLARO, E SEPARADO DO operacao.enc

A torre fica ligada sozinha numa TV da operação. Pedir senha a cada recarga
não funciona, e embutir a senha do painel na página seria muito pior: ela
decifra o operacao.enc, que carrega Arcor, Supley e 3 Corações juntos —
publicá-la abriria todos os clientes de uma vez.

Então a parede recebe um arquivo próprio, em claro, com o mínimo. Ficam de
fora, de propósito, os dois campos que identificam a carga do cliente:

    nf       — número da nota fiscal
    cliente  — razão social do destinatário

Nenhuma tela usa esses campos. Publicar o que não se mostra é custo sem
contrapartida. O que sai daqui é o que aparece na tela: operação, placa,
motorista, contagem e horário.

TAMBÉM NÃO SAI DAQUI: DIVERGÊNCIA DE APONTAMENTO

O invariante 6 do CLAUDE.md é explícito: nada de uso interno no payload nem na
tela. A seção "Pendências de apontamento" foi removida do painel em 05/08/2026
justamente porque esconder no HTML não bastava — bastava baixar o arquivo e
ler o campo. Como este aqui é público e sem senha, vale a mesma lógica: o
cliente consegue lê-lo.

Por isso "entregue sem GreenMile", "GreenMile sem SAC" e "notas sem peso"
ficam de fora, e a tela de qualidade da torre fica sem alimento até que se
decida onde ela vai morar. A adesão ao GreenMile fica, porque já é indicador
publicado no painel do cliente.

Liberar data/tv.json exigiu uma linha explícita no .gitignore, que bloqueia
data/* por lista positiva. Isso é de propósito — ver o comentário de lá.

POR QUE EXISTE UM tv.hash

Mesma razão do cifrar.py: gerado_em muda a cada rodada, e o workflow comita a
pasta inteira. Sem comparar o conteúdo sem esse campo, o robô comitaria um
arquivo por hora para sempre, inclusive de madrugada, quando nada acontece.
Durante a operação o dado muda de verdade quase toda rodada e o arquivo é
reescrito — que é o desejado.

O RITMO NÃO É CALCULADO AQUI

Este script publica fatos: quantas entregas o veículo tem, quantas já saíram,
em que turno ele começou. O aperto — quantas vezes mais rápido ele precisa ir
do que o planejado — depende do relógio, e é calculado no navegador a cada
recarga. Calcular aqui congelaria o risco no instante da coleta e a tela
mentiria até a rodada seguinte.

O QUE ESTA TORRE AINDA NÃO SABE

A hora de cada baixa. O coletor do GreenMile no n8n pede uma projeção fixa de
11 campos e nenhum deles é horário de serviço; o `atualizado_em` que ele grava
é `new Date()` da execução. Sem isso não há "intervalo médio entre baixas",
não há "última baixa às 14:23" e a hora de saída fica no turno da planilha.
Para ter: acrescentar os campos de horário ao array `filtros` do nó
"Montar consulta" nos coletores 0GUyiqtaNTHadd67 e PrnRQSyVxQsKchn2.
"""

from __future__ import annotations

import collections
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / "data" / "operacao.json"
SAIDA = RAIZ / "data" / "tv.json"
HASH = RAIZ / "data" / "tv.hash"

FUSO = timezone(timedelta(hours=-3))

hm = lambda s: int(s[:2]) * 60 + int(s[3:])   # 'HH:MM' -> minutos

# Janela de entrega combinada com a operação. O ritmo de cada veículo sai de
# quanto sobra dela a partir da saída dele — quem sai mais tarde tem menos
# tempo para as mesmas notas, e é cobrado por isso, não apesar disso.
ABRE = "08:00"
FECHA = "18:00"

# Acima deste aperto o veículo não fecha o dia sem reforço: é o vermelho.
# Entre 1 e ele, amarelo. O número saiu da operação, não de estatística: 50%
# acima do planejado é o que um motorista consegue absorver num dia ruim.
LIMITE_APERTO = 1.5

# Onda de saída -> hora estimada. É a ÚNICA fonte de hora de saída hoje, e é
# grosseira: diz o turno, não o minuto. Só as abas de SJC e Guarulhos têm a
# coluna; nas outras todo veículo entra como 08:00. Ver saida_do_veiculo().
ONDAS = {
    "1ª SAÍDA": "08:00",
    "2ª SAÍDA": "11:00",
    "3ª SAÍDA": "14:00",
    "2º DIA": "08:00",
    "REENTREGA": "08:00",
}


def hora(iso: str) -> str:
    """UTC do GreenMile -> 'HH:MM' de Brasília. Vazio se não der para ler.

    O GreenMile devolve "2026-08-26T14:59:42+0000". Sem a conversão, uma
    entrega das 11:59 apareceria como 14:59 e todo o cálculo de ritmo andaria
    três horas para a frente.
    """
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("+0000", "+00:00")) \
            .astimezone(FUSO).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def saida_do_veiculo(notas: list[dict]) -> tuple[str, str]:
    """Hora de saída do veículo e de onde ela veio.

    A chegada à primeira parada do GreenMile é a melhor âncora que existe: o
    veículo já estava em rota antes dela, então a janela sai subestimada, e é
    esse o erro que se prefere — cobrar um pouco mais, nunca menos.

    NÃO CONFUNDIR COM gm_atualizado_em. Aquele é o carimbo do coletor: o nó
    "Achatar por nota" faz `new Date()` uma vez por execução e marca todas as
    linhas igual. Em 02/09/2026 ele dava 16:00 para 48 dos 50 veículos.
    O horário de verdade é gm_chegada, de `stop.actualArrival`.

    Sem GreenMile na rota, sobra a onda da planilha (1ª/2ª/3ª SAÍDA), que diz
    o turno e não o minuto, e por fim 08:00.
    """
    chegadas = sorted(h for h in (hora(n.get("gm_chegada", "")) for n in notas) if h)
    if chegadas:
        return chegadas[0], "greenmile"

    ondas = [ONDAS[n["onda"]] for n in notas if n.get("onda") in ONDAS]
    if ondas:
        return min(ondas), "onda"

    return ABRE, "padrao"


def cidade_regiao(notas: list[dict]) -> str:
    """A região da rota: a cidade que mais aparece nas notas do veículo.

    Não é "onde ele está agora" — para isso seria preciso a posição do
    rastreador, que para o café só existe no GreenMile e não entra aqui.
    Serve para a tela de ação responder "onde procurar esse cara".
    """
    cidades = collections.Counter(n["cidade"] for n in notas if n.get("cidade"))
    return cidades.most_common(1)[0][0] if cidades else ""


def main() -> None:
    if not ENTRADA.exists():
        sys.exit(f"{ENTRADA.relative_to(RAIZ)} não existe. "
                 "Rode antes: python3 scripts/build_data.py")

    dados = json.loads(ENTRADA.read_text(encoding="utf-8"))
    agora = datetime.now(FUSO)
    hoje = agora.date().isoformat()

    # Só entrega conta. Transferência entre bases não é entrega a recebedor e
    # fica fora do denominador — mesma regra do painel.
    entregas = [n for n in dados["notas"]
                if n["tipo"] == "ENTREGA" and n["data"] == hoje]

    por_plano: dict[str, list[dict]] = collections.defaultdict(list)
    for n in entregas:
        if n["plano"]:
            por_plano[n["plano"]].append(n)

    planos_meta = dados.get("planos") or {}
    veiculos = []
    for plano, notas in por_plano.items():
        meta = planos_meta.get(plano, {})
        feitas = [n for n in notas if n["classe"] == "entregue"]
        saida, fonte_saida = saida_do_veiculo(notas)
        baixas = sorted(h for h in
                        (hora(n.get("gm_partida") or n.get("gm_chegada", "")) for n in feitas)
                        if h)
        veiculos.append({
            "plano": plano,
            "op": meta.get("operacao") or notas[0]["operacao"],
            "placa": meta.get("placa") or notas[0]["placa"],
            "motorista": meta.get("motorista") or notas[0]["motorista"],
            "cidade": cidade_regiao(notas),
            "total": len(notas),
            "ok": len(feitas),
            # Sem apontamento não é pendente: é desfecho não informado. A tela
            # mostra os dois, nunca somados — somar produz número falso.
            "semapont": sum(1 for n in notas if n["classe"] == "semapont"),
            "gm": sum(1 for n in feitas if n["gm_ok"]),
            "saida": saida,
            "saida_fonte": fonte_saida,
            "ultima_baixa": baixas[-1] if baixas else "",
            # Ritmo observado: minutos por entrega entre a primeira e a última
            # baixa. Só existe com duas baixas ou mais — com uma só não há
            # intervalo para medir, e inventar um seria pior que não ter.
            "ritmo_real": round((hm(baixas[-1]) - hm(baixas[0])) / (len(baixas) - 1))
                          if len(baixas) > 1 else 0,
        })

    # Do mais carregado ao menos: a ordenação por risco é do navegador, que é
    # quem conhece o relógio. Aqui só se garante uma ordem estável.
    veiculos.sort(key=lambda v: (-v["total"], v["placa"]))

    pracas: dict[str, dict] = {}
    for v in veiculos:
        p = pracas.setdefault(v["op"], {"op": v["op"], "veic": 0, "total": 0,
                                        "ok": 0, "semapont": 0, "gm": 0})
        p["veic"] += 1
        for c in ("total", "ok", "semapont", "gm"):
            p[c] += v[c]

    payload = {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "dia": hoje,
        "fonte": dados.get("fonte", ""),
        "janela": {"abre": ABRE, "fecha": FECHA},
        "limite_aperto": LIMITE_APERTO,
        "veiculos": veiculos,
        "pracas": sorted(pracas.values(), key=lambda p: -p["total"]),
        "baixas_por_hora": [{"h": h, "n": n} for h, n in sorted(collections.Counter(
            hora(n.get("gm_partida") or n.get("gm_chegada", ""))[:2]
            for n in entregas
            if n["classe"] == "entregue" and (n.get("gm_partida") or n.get("gm_chegada"))
        ).items()) if h],
    }

    # Comparação sem gerado_em: só o conteúdo decide se vale reescrever.
    corpo = dict(payload)
    corpo.pop("gerado_em")
    digest = hashlib.sha256(
        json.dumps(corpo, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    if HASH.exists() and HASH.read_text(encoding="utf-8").strip() == digest:
        print(f"{SAIDA.relative_to(RAIZ)} inalterado — nada a regravar")
        return

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    HASH.write_text(digest + "\n", encoding="utf-8")

    print(f"{SAIDA.relative_to(RAIZ)} gravado")
    print(f"  {len(veiculos)} veículos · {sum(v['total'] for v in veiculos)} notas "
          f"· {sum(v['ok'] for v in veiculos)} entregues · {len(pracas)} praças")
    fontes = collections.Counter(v["saida_fonte"] for v in veiculos)
    if fontes:
        print("  hora de saída: " + ", ".join(f"{n} por {f}" for f, n in fontes.most_common()))
    if not veiculos:
        print("  nenhum veículo com saída hoje — a torre mostra a tela vazia, "
              "que é o correto: não há operação para acompanhar")


if __name__ == "__main__":
    main()
