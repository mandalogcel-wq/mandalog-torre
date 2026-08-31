#!/usr/bin/env python3
"""
Funde a última posição do rastreador (Krona) em data/viagens.json.

Roda depois de build_data.py e antes de fatiar.py. É o que resolve o limite
que o painel carregava desde o início: a planilha diz qual veículo está alocado
em qual viagem, mas não diz **onde ele está**. A Krona diz.

Por que chama a Krona direto, sem passar pelo n8n
-------------------------------------------------
O GreenMile precisa do n8n porque exige dois usuários distintos (nenhum enxerga
as cinco operações) e uma consulta paginada por chave de rota. A Krona é uma
chamada SOAP única que devolve a frota inteira — pôr um workflow no meio só
acrescentaria uma peça para quebrar e uma cópia para envelhecer. Aqui vale a
mesma regra do resto do repositório: biblioteca padrão, sem dependência nova.

**A planilha continua sendo a base.** A Krona nunca decide quais veículos
existem nem a que cliente pertencem — isso vem da aba de roteirização. Ela só
responde "onde está a placa X". Placa que a planilha não lista fica de fora do
payload, mesmo que a Krona tenha posição dela: é isso que impede a Arcor de
enxergar a posição de um veículo enquanto ele roda para o Supley (são 15 placas
compartilhadas entre as duas abas).

Se a Krona estiver fora do ar, o script avisa e segue. Falha aqui não pode
derrubar a atualização do painel — mesma regra do mesclar_greenmile.py.

Variáveis de ambiente:
    KRONA_EMPRESA   código da empresa (produção da Mandalog: 61634)
    KRONA_USUARIO   código do usuário
    KRONA_SENHA     senha

Uso:
    python3 scripts/build_data.py
    python3 scripts/mesclar_krona.py
    python3 scripts/fatiar.py
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO = RAIZ / "data" / "viagens.json"

URL = "https://krona.api.br:8443/ws/ImpExpSM.asmx"
NS = {"ns": "https://www.smv.log.br/ws/"}

# Posição mais velha que isto não entra no payload. O mapa colore por idade e
# já mostra cinza acima de 12h, mas metade da frota da Krona tem última posição
# de meses atrás (rastreador desativado, agregado que saiu). Pôr esses pontos no
# mapa sugeriria que o veículo está lá agora, que é justamente o que a torre não
# pode fazer. Ver o levantamento de 30/08/2026: 54 placas com posição, só 20 nos
# últimos dois dias.
VALIDADE_HORAS = 48


def sem_acento(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", v or "")
                   if unicodedata.category(c) != "Mn").upper()


def norm_placa(v) -> str:
    """Placa comparável entre planilha e Krona: só letra e número.

    A planilha escreve 'TLJ6C56 ' com espaço à direita e 'BYC-6H3' com hífen; a
    Krona devolve 'BYC-6H3' e 'DBB-7I3'. Sem normalizar, o cruzamento perde
    justamente as placas de carreta, que é onde a grafia varia mais.
    """
    return re.sub(r"[^A-Z0-9]", "", sem_acento(str(v or "")))


def soap(operacao: str, campos: str, timeout: int = 45) -> str | None:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        f'<soap12:Body><{operacao} xmlns="https://www.smv.log.br/ws/">'
        f'{campos}</{operacao}></soap12:Body></soap12:Envelope>'
    )
    req = urllib.request.Request(
        URL, data=envelope.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                    timeout=timeout) as r:
            corpo = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"  aviso: Krona indisponível em {operacao} ({e}) — "
              "seguindo sem posição.")
        return None

    try:
        raiz = ET.fromstring(corpo)
    except ET.ParseError as e:
        print(f"  aviso: resposta da Krona em {operacao} não é XML ({e}).")
        return None

    no = raiz.find(f".//ns:{operacao}Result", NS)
    if no is None:
        print(f"  aviso: Krona não devolveu {operacao}Result.")
        return None
    if no.text:
        return no.text
    # Alguns métodos devolvem XML aninhado em vez de texto.
    return "".join(ET.tostring(c, encoding="unicode") for c in list(no))


def numero(v: str) -> float | None:
    try:
        n = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n


def coordenada(v: str) -> float | None:
    """Latitude/longitude da Krona, que às vezes vem como inteiro escalado.

    A maioria dos registros traz '-21.326164'. Alguns trazem '-237359168', que é
    o mesmo valor multiplicado por 1e7 — visto na base antiga. Sem tratar, o
    ponto vai parar fora do planeta e o `fitBounds` do mapa abre no mundo todo.
    """
    n = numero(v)
    if n is None or n == 0:
        return None
    while abs(n) > 180:
        n /= 10
    return n


def posicoes_krona(empresa: str, senha: str) -> list[dict] | None:
    """Última posição de toda a frota cadastrada — não só de quem tem SM.

    Verificado em 30/08/2026: 54 placas com posição para 3 SMs abertas, e 11
    veículos reportando no mesmo dia sem nenhuma SM. `LastPosition` (sem o
    sufixo AllVehicles) devolveria só os que estão em SM, e não serve aqui.
    """
    txt = soap("LastPositionAllVehicles",
               f"<Empresa>{empresa}</Empresa><Senha>{senha}</Senha>")
    if txt is None:
        return None
    try:
        raiz = ET.fromstring(txt)
    except ET.ParseError:
        return []

    out = []
    for t in raiz.findall(".//Table"):
        g = lambda k: (t.findtext(k) or "").strip()
        placa = norm_placa(g("Placa"))
        if not placa:
            continue
        endereco = g("Endereco")
        out.append({
            "placa": placa,
            "placa_exibida": g("Placa").strip(),
            "data_posicao": g("DataGPS"),
            # A Krona escreve INDEFINIDO quando não conseguiu geocodificar.
            # Virar string vazia deixa o painel decidir o texto, em vez de
            # imprimir "INDEFINIDO" para o cliente.
            "endereco": "" if sem_acento(endereco) == "INDEFINIDO" else endereco,
            "lat": coordenada(g("Latitude")),
            "lon": coordenada(g("Longitude")),
            "velocidade_kmh": numero(g("Velocidade")) or 0,
            "ignicao": g("ignicao") == "1",
        })
    return out


def sms_krona(empresa: str, usuario: str, senha: str) -> dict[str, dict]:
    """Contexto da SM por placa: valor da carga, gerenciadora e situação.

    Enriquecimento, não base: só existe para quem está com SM aberta. Quem não
    tem SM continua aparecendo na torre pela planilha, como sempre.
    """
    txt = soap("Get_SMsByEmpresaID",
               f"<EmpresaID>{empresa}</EmpresaID>"
               f"<OperadorID>{usuario}</OperadorID><senha>{senha}</senha>")
    if txt is None:
        return {}
    try:
        raiz = ET.fromstring(txt)
    except ET.ParseError:
        return {}

    out = {}
    for t in raiz.findall(".//Table"):
        g = lambda k: (t.findtext(k) or "").strip()
        placa = norm_placa(g("Placa"))
        if not placa:
            continue
        out[placa] = {
            "valor_carga": numero(g("ValorCarga")),
            "gerenciadora": g("nmgerenciadora"),
            "mercadoria": g("MERCADORIA"),
            "previsao_chegada": g("DataPrevistaChegada"),
            "origem": g("CidadeOrigem"),
            "destino": g("CidadeDestino"),
        }
    return out


def recente(iso: str, limite_horas: int) -> bool:
    if not iso:
        return False
    try:
        quando = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone(timedelta(hours=-3)))
    agora = datetime.now(timezone(timedelta(hours=-3)))
    return (agora - quando) <= timedelta(hours=limite_horas)


def main() -> None:
    if not ARQUIVO.exists():
        print(f"  {ARQUIVO.name} não existe — nada a mesclar "
              "(rode antes o build_data.py).")
        return

    empresa = os.environ.get("KRONA_EMPRESA", "").strip()
    usuario = os.environ.get("KRONA_USUARIO", "").strip()
    senha = os.environ.get("KRONA_SENHA", "").strip()

    dado = json.loads(ARQUIVO.read_text(encoding="utf-8"))

    if not (empresa and usuario and senha):
        print("  KRONA_EMPRESA/KRONA_USUARIO/KRONA_SENHA não definidos — "
              "seguindo sem posição.")
        dado.setdefault("rastreamento", {"disponivel": False})
        ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        return

    posicoes = posicoes_krona(empresa, senha)
    if posicoes is None:
        dado["rastreamento"] = {"disponivel": False}
        ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        return

    sms = sms_krona(empresa, usuario, senha)
    por_placa = {p["placa"]: p for p in posicoes}

    # A planilha manda: só entra no payload placa que a roteirização lista.
    # Sem este filtro a Krona injetaria a frota do café nos payloads de
    # transferência, e a posição de um veículo em viagem de outro cliente.
    da_planilha: set[str] = set()
    for v in dado.get("viagens", []):
        da_planilha.add(norm_placa(v.get("placa")))
        da_planilha.add(norm_placa(v.get("carreta")))
    for f in dado.get("frota", []):
        da_planilha.add(norm_placa(f.get("placa")))
        da_planilha.add(norm_placa(f.get("carreta")))
    da_planilha.discard("")

    # Posição na frota: é onde o veículo aparece como cartão na torre.
    com_posicao = velhas = 0
    for f in dado.get("frota", []):
        p = por_placa.get(norm_placa(f.get("placa")))
        if not p:
            continue
        if not recente(p["data_posicao"], VALIDADE_HORAS):
            velhas += 1
            continue
        f["posicao"] = {
            "data": p["data_posicao"], "endereco": p["endereco"],
            "lat": p["lat"], "lon": p["lon"],
            "velocidade_kmh": p["velocidade_kmh"], "ignicao": p["ignicao"],
        }
        com_posicao += 1
        sm = sms.get(norm_placa(f.get("placa")))
        if sm:
            f["sm"] = sm

    # Lista para o mapa. Mesmo formato que `greenmile.posicoes`, para o
    # renderMapa consumir os dois sem saber de onde veio cada ponto.
    pontos = [{
        "placa": p["placa_exibida"], "lat": p["lat"], "lon": p["lon"],
        "data_posicao": p["data_posicao"],
        "velocidade_kmh": p["velocidade_kmh"],
        "origem": p["endereco"] or "posição sem endereço",
    } for p in posicoes
        if p["placa"] in da_planilha
        and p["lat"] is not None and p["lon"] is not None
        and recente(p["data_posicao"], VALIDADE_HORAS)]

    dado["rastreamento"] = {
        "disponivel": True,
        "fonte": "Krona · última posição do rastreador",
        "gerado_em": datetime.now(timezone(timedelta(hours=-3)))
                             .isoformat(timespec="seconds"),
        "validade_horas": VALIDADE_HORAS,
        "posicoes": pontos,
    }

    ARQUIVO.write_text(json.dumps(dado, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"  Krona: {len(posicoes)} posições na frota cadastrada · "
          f"{com_posicao} veículos da planilha posicionados · "
          f"{len(pontos)} pontos no mapa · {len(sms)} SMs"
          + (f" · {velhas} posições descartadas por passar de "
             f"{VALIDADE_HORAS}h" if velhas else ""))


if __name__ == "__main__":
    main()
