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
que horas ele começou. O aperto — quantas vezes mais rápido ele precisa ir do
que o planejado — depende do relógio, e é calculado no navegador a cada
recarga. Calcular aqui congelaria o risco no instante da coleta e a tela
mentiria pelos 15 minutos seguintes.
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

# Janela de entrega combinada com a operação. O ritmo de cada veículo sai de
# quanto sobra dela a partir da saída dele — quem sai mais tarde tem menos
# tempo para as mesmas notas, e é cobrado por isso, não apesar disso.
ABRE = "08:00"
FECHA = "18:00"

# Acima deste aperto o veículo não fecha o dia sem reforço: é o vermelho.
# Entre 1 e ele, amarelo. O número saiu da operação, não de estatística: 50%
# acima do planejado é o que um motorista consegue absorver num dia ruim.
LIMITE_APERTO = 1.5

# Onda de saída -> hora estimada. Só as abas de SJC e Guarulhos têm a coluna,
# e ela é grosseira: serve de rede quando o GreenMile ainda não registrou
# nenhuma baixa do veículo e não há nada melhor para usar.
ONDAS = {
    "1ª SAÍDA": "08:00",
    "2ª SAÍDA": "11:00",
    "3ª SAÍDA": "14:00",
    "2º DIA": "08:00",
    "REENTREGA": "08:00",
}


def hora(iso: str) -> str:
    """'2026-09-02T14:23:11-03:00' -> '14:23'. Vazio se não der para ler."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(FUSO).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def saida_do_veiculo(notas: list[dict]) -> tuple[str, str]:
    """Hora de saída do veículo e de onde ela veio.

    A planilha não tem coluna de hora — conferido nas 27 colunas de Sumaré e
    nas 30 de SJC. Então a melhor fonte disponível é a primeira baixa que o
    GreenMile registrou para o veículo: ele já estava em rota antes dela, o
    que torna a estimativa conservadora (nunca tarde demais), e é exatamente
    o erro que se prefere ter aqui — superestimar a janela é dar ao veículo
    mais crédito do que ele tem, e não menos.
    """
    baixas = sorted(h for h in (hora(n.get("gm_atualizado_em", "")) for n in notas) if h)
    if baixas:
        return baixas[0], "greenmile"

    ondas = [ONDAS[n["onda"]] for n in notas if n.get("onda") in ONDAS]
    if ondas:
        return min(ondas), "onda"

    return ABRE, "padrao"


def cidade_atual(notas: list[dict]) -> str:
    """Onde o veículo está: a cidade da baixa mais recente.

    Sem nenhuma baixa, a cidade mais frequente da rota — é para onde ele vai,
    que na tela de ação responde a mesma pergunta ('onde procurar esse cara').
    """
    baixadas = [n for n in notas if n.get("gm_atualizado_em") and n.get("cidade")]
    if baixadas:
        return max(baixadas, key=lambda n: n["gm_atualizado_em"])["cidade"]
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
        ultimas = sorted(h for h in (hora(n.get("gm_atualizado_em", "")) for n in feitas) if h)
        veiculos.append({
            "plano": plano,
            "op": meta.get("operacao") or notas[0]["operacao"],
            "placa": meta.get("placa") or notas[0]["placa"],
            "motorista": meta.get("motorista") or notas[0]["motorista"],
            "cidade": cidade_atual(notas),
            "total": len(notas),
            "ok": len(feitas),
            # Sem apontamento não é pendente: é desfecho não informado. A tela
            # mostra os dois, nunca somados — somar produz número falso.
            "semapont": sum(1 for n in notas if n["classe"] == "semapont"),
            "gm": sum(1 for n in feitas if n["gm_ok"]),
            "saida": saida,
            "saida_fonte": fonte_saida,
            "ultima_baixa": ultimas[-1] if ultimas else "",
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

    baixas_por_hora = collections.Counter(
        hora(n.get("gm_atualizado_em", ""))[:2]
        for n in entregas if n["classe"] == "entregue" and n.get("gm_atualizado_em")
    )
    baixas_por_hora.pop("", None)

    payload = {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "dia": hoje,
        "fonte": dados.get("fonte", ""),
        "janela": {"abre": ABRE, "fecha": FECHA},
        "limite_aperto": LIMITE_APERTO,
        "veiculos": veiculos,
        "pracas": sorted(pracas.values(), key=lambda p: -p["total"]),
        "baixas_por_hora": [{"h": h, "n": n} for h, n in sorted(baixas_por_hora.items())],
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
