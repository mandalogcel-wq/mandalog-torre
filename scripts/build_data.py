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

# Arcor e Supley saem em arquivo separado, e isso não é arrumação: é isolamento.
# `operacao.json` é o payload que a 3 Corações decifra no navegador dela. Um
# cliente não pode ter em mãos o manifesto, o motorista e a placa da operação de
# outro — esconder na tela não bastaria, bastaria baixar o .enc e ler o campo.
SAIDA_VIAGENS = RAIZ / "data" / "viagens.json"

# Nome da operação por arquivo. Acrescente aqui ao incluir novas abas.
OPERACOES = {
    "cafe-sjc": "Café · SJC",
    "cafe-gru": "Café · Guarulhos",
    # Antes misturada dentro da aba de Guarulhos; virou aba própria em 26/08/2026
    # (gid 1726352493). O layout NÃO é cópia do de Guarulhos — sondado direto na
    # planilha via n8n antes de escrever COL_ITAPEVA, porque assumir teria
    # publicado plano/nf errados sem ninguém perceber.
    "cafe-itapeva": "Café · Itapeva",
}

# A aba de Sumaré não tem uma operação só: a coluna MESO separa três praças.
# TP SUMARÉ entra no CD por ser a mesma praça — decisão da operação, não técnica.
MESOS = {
    "CD SUMARE": "Café · CD Sumaré",
    "TP SUMARE": "Café · CD Sumaré",
    "MESO MARILIA": "Café · Meso Marília",
    "MESO BAURU": "Café · Meso Bauru",
}

# Este é um painel de status diário, não um relatório histórico. A aba de
# Guarulhos guarda o ano inteiro — 15 mil linhas, que virariam um .enc de 11 MB
# baixado e decifrado a cada abertura, e um repositório que bate no limite de
# 1 GB do Pages em poucos meses. Cortar em 30 dias mantém o arquivo em ~1,3 MB.
# Notas sem data de saída ficam, porque são justamente as que pedem atenção.
RETENCAO_DIAS = 7

# A planilha tem dois pares de colunas com o mesmo cabeçalho (CARREGAMENTO e
# PAGAMENTO). O csv.reader preserva a ordem, então usamos o índice da coluna.
#
# Cada aba tem seu próprio layout: SJC e Guarulhos compartilham um; Sumaré tem
# outro, com 27 colunas em ordem diferente, sem código de cliente e sem peso, e
# com o desfecho em STATUS NOTA em vez de STATUS SAC. Por isso um mapa por
# layout, e não um COL único.
COL_PADRAO = {
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

# Sondada em 26/08/2026 (gid 1726352493) e RESSONDADA em 27/08/2026 depois de
# um bug real: a primeira sondagem leu a aba por nome de cabeçalho via n8n, e
# essa leitura colapsa cabeçalho duplicado numa chave só — exatamente como
# CARREGAMENTO e PAGAMENTO se duplicam no layout padrão (ver COL_PADRAO). A
# aba de Itapeva TAMBÉM tem "CARREGAMENTO" duas vezes (colunas 4 e 5), e a
# sondagem por nome escondeu a segunda, deslocando tudo a partir de "status"
# uma posição para trás. Resultado em produção: "plano" lia na verdade
# "Cod cliente" (quase único por cliente — daí ~56 "veículos" na torre para
# 3-4 caminhões reais) e "status_sac" lia "TIPO DE PAGAMENTO" em vez do
# status de verdade. Ressondado com leitura posicional crua (values.get da
# API do Sheets, sem mapear por nome) para não repetir o erro. Sem ORIGEM
# nem Nº SAÍDA nesta aba — "origem" fica de fora de propósito, ver nota
# original abaixo.
#
# "origem" fica de fora daqui: preencher com CARREGAMENTO (que guarda algo
# como "1ª SAÍDA", não um nome de base) inventaria dado que a aba não tem.
COL_ITAPEVA = {
    "emissao": 0,
    "manifesto": 1,
    "faixa": 2,
    "data_saida": 3,
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

COL_SUMARE = {
    "data_saida": 2,
    "meso": 3,
    "status": 4,
    "plano": 5,
    "cliente": 6,
    "nf": 7,
    "cidade": 8,
    "emissao": 10,
    "perfil_cobranca": 11,
    "motorista": 12,
    "placa": 13,
    "manifesto": 15,
    "status_sac": 17,
    "canhoto": 23,
    "greenmile": 24,
}

# arquivo em data/raw -> como lê-lo
LAYOUTS = {
    "cafe-sjc": COL_PADRAO,
    "cafe-gru": COL_PADRAO,
    "cafe-sumare": COL_SUMARE,
    "cafe-itapeva": COL_ITAPEVA,
}

# ---------------------------------------------------------------------------
# Viagens: Arcor e Supley
#
# Grão diferente do café. Lá uma linha é uma nota fiscal numa tentativa de
# entrega; aqui **uma linha é uma viagem**, ponto a ponto, carreta com uma
# entrega só. Não existe "% de reentrega" numa transferência Matão → Jundiaí.
# Por isso viagem não é modelada como nota: é outra tabela.
#
# Estas abas são lidas por NOME de coluna, e não por índice como as de café.
# A regra do índice existe porque as abas de café têm cabeçalho duplicado
# (CARREGAMENTO e PAGAMENTO aparecem duas vezes). Estas não têm, e ler por nome
# sobrevive a alguém inserir uma coluna no meio — o que nessas abas, editadas à
# mão todo dia, é questão de tempo. `mapa_colunas` recusa cabeçalho duplicado,
# para não trocar o índice em silêncio se isso mudar.
CLIENTES_VIAGEM = {"arcor": "Arcor", "supley": "Supley"}

# campo lógico -> cabeçalho na planilha. Ausente = a aba não tem aquele dado.
VIAGEM_COMUM = {
    "data": "DATA",
    "manifesto": "MANIFESTO",
    "status_manifesto": "STATUS MANIFESTO",
    "tipo_operacao": "TIPO DE OPERAÇÃO",
    "origem": "ORIGEM",
    "destino": "DESTINO",
    "tipo_veiculo": "TIPO VEICULO",
    "nf": "NOTA FISCAL",
    "motorista": "MOTORISTA",
    "placa": "PLACA",
    "carreta": "CARRETA",
    "status": "STATUS ENTREGA",
    "canhoto": "CANHOTO",
    "observacao": "OBSERVAÇÃO",
}

VIAGEM_SUPLEY = dict(VIAGEM_COMUM, **{
    "rastreada": "RASTREADA",
    "chegada_origem": "CHEGADA MATÃO",
    "carregamento": "CARREGAMENTO",
    "liberacao_origem": "LIBERAÇÃO",
    "chegada_destino": "DATA CHEGADA JUNDIAÍ",
    "hora_chegada_destino": "HORARIO CHEGADA EM JUNDIAÍ",
    "liberacao_destino": "DATA LIBERAÇÃO",
    "hora_liberacao_destino": "HORA LIBERAÇÃO",
})

VIAGEM_ARCOR = dict(VIAGEM_COMUM, **{"oc": "OC", "nf_pbr": "NOTA PBR"})

VIAGENS_LAYOUT = {"supley": VIAGEM_SUPLEY, "arcor": VIAGEM_ARCOR}

# Sem estas colunas a viagem não é identificável e o painel não fecha. Falta de
# uma delas derruba o build em vez de publicar tabela pela metade.
VIAGEM_OBRIGATORIAS = ("data", "manifesto", "origem", "destino", "placa")

# VALOR DO FRETE, TIPO DE FRETE e PAGAMENTO existem nessas abas e **não entram
# no payload**. São o acerto comercial entre a Mandalog e o transportador; não
# é assunto do cliente, e uma vez publicado não tem como despublicar.


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


# Status SAC que caem em "andamento" de propósito, e por isso não entram no
# aviso de vocabulário novo abaixo. PERNOITE é de Itapeva (26/08/2026): o
# motorista pernoita no caminho e a entrega ainda não aconteceu — confirmado
# com a operação, não suposição.
EM_TRANSITO_SAC = {"EM ROTA", "ROTA DE ENTREGA", "EM TRANSITO", "A CAMINHO", "PERNOITE"}


def classificar(sac: str) -> str:
    """Traduz a coluna STATUS SAC nas cinco classes exibidas no painel."""
    s = sem_acento(limpar(sac))
    if not s:
        return "semapont"
    if s.startswith("ENTREGUE"):
        return "entregue"
    if "REENTREG" in s:
        return "reentregar"
    # "FINALIZADO C/ DEV PARCIAL" não contém "DEVOLU" — o teste antigo deixava
    # essas notas em "andamento" e subestimava o % de devoluções.
    if "DEVOLU" in s or "RECUSA" in s or "DEV PARCIAL" in s:
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
    sem_meso: list[int] = []
    # Sem isto, um Status SAC novo (como "PERNOITE" em Itapeva) cai em
    # "andamento" sem ninguém notar — e uma entrega já feita, mas apontada com
    # um texto que o classificar() não reconhece, some do % de finalizadas em
    # silêncio. ler_viagens() já faz esse rastreio para STATUS ENTREGA; isto é
    # o equivalente para STATUS SAC.
    vocabulario: set[str] = set()
    col = LAYOUTS.get(caminho.stem, COL_PADRAO)
    operacao_fixa = OPERACOES.get(caminho.stem, caminho.stem)
    largura = max(col.values())

    with caminho.open(encoding="utf-8-sig", newline="") as fh:
        linhas = list(csv.reader(fh))

    if not linhas:
        return notas, [f"{caminho.name}: arquivo vazio"]

    for i, linha in enumerate(linhas[1:], start=2):
        if len(linha) <= largura:
            linha = linha + [""] * (largura + 1 - len(linha))
        get = lambda k: limpar(linha[col[k]]) if k in col else ""

        if not any(linha):
            continue

        # Em Sumaré a operação é por linha: a coluna MESO separa as praças.
        if "meso" in col:
            m = sem_acento(get("meso"))
            operacao = MESOS.get(m)
            if not operacao:
                # Nota sem MESO é nota ainda não roteirizada: sem data de saída
                # e sem praça atribuída. Fica fora das três divisões, mas o
                # total é contado num aviso só — 189 linhas de aviso afogariam
                # tudo que importa no log.
                if get("plano") or get("nf"):
                    sem_meso.append(i)
                continue
        else:
            operacao = operacao_fixa

        iso = data_iso(get("data_saida"))
        if get("data_saida") and not iso:
            avisos.append(f"{caminho.name} linha {i}: data de saída inválida "
                          f"({get('data_saida')!r})")

        # Só transferência entre bases fica fora do denominador, porque não é
        # entrega a recebedor. Reentrega, diária e coleta são entrega e contam —
        # tratá-las como tipo próprio derrubava 11% das notas do painel e
        # zerava o % de reentrega, que ficou meses mostrando 0%.
        tipo = ("TRANSFERENCIA" if sem_acento(get("status")).startswith("TRANSFER")
                else "ENTREGA")

        bruto_sac = get("status_sac")
        classe = classificar(bruto_sac)
        if classe == "andamento" and bruto_sac and sem_acento(bruto_sac) not in EM_TRANSITO_SAC:
            vocabulario.add(bruto_sac.upper())

        notas.append({
            "operacao": operacao,
            "linha": i,
            "tipo": tipo,
            "plano": get("plano"),
            "manifesto": get("manifesto"),
            "data": iso,
            "origem": get("origem"),
            # Onda de saída ("1ª SAÍDA", "2ª SAÍDA"...). Só as abas de SJC e
            # Guarulhos têm a coluna; nas outras sai vazio. A torre de parede
            # usa isso para estimar a hora de saída quando o GreenMile ainda
            # não registrou nenhuma baixa do veículo.
            "onda": get("saida_num"),
            "nf": get("nf"),
            "cliente": get("cliente"),
            "cidade": cidade(get("cidade")),
            "peso": get("peso"),
            "motorista": titulo(get("motorista")),
            "placa": get("placa").upper(),
            "perfil": get("perfil_cobranca").upper(),
            "sac": bruto_sac,
            "classe": classe,
            "gm": get("greenmile"),
            "gm_ok": "ELETR" in sem_acento(get("greenmile")),
            "emitido": sem_acento(get("emissao")) == "EMITIDO",
            "canhoto": get("canhoto"),
        })

    if sem_meso:
        avisos.append(f"{caminho.name}: {len(sem_meso)} notas sem MESO "
                      "(aguardando roteirização) ficaram fora do painel")

    if vocabulario:
        avisos.append(f"{caminho.name}: STATUS SAC não mapeado, tratado como "
                      f"andamento: {', '.join(sorted(vocabulario)[:8])}")

    return notas, avisos


# Status que significam trânsito e caem em `andamento` de propósito. Estão aqui
# só para o aviso de vocabulário novo não disparar com eles todo dia.
EM_TRANSITO = {"ROTA DE ENTREGA", "EM ROTA", "EM TRANSITO", "A CAMINHO"}

# ---------------------------------------------------------------------------
# Frota: o ciclo do veículo
#
# A pergunta que Arcor e Supley fazem primeiro é "quantas carretas tenho para
# carregar". A aba não responde isso diretamente — ela registra viagem, não
# estado de frota —, mas a **última linha de cada placa** responde, desde que se
# corte pela recência.
#
# Sem o corte o número mente feio: das 70 placas do Supley, 65 apareceriam como
# disponíveis, com mediana de 78 dias desde a última viagem. São agregados que
# saíram da operação, não carretas paradas no pátio.
#
# FROTA_DIAS = 3 não é chute: com 3 dias a lista bate exatamente com o
# "Relatório de Tempos e Movimentos" que a operação manda no grupo — 9 veículos
# no Supley, 7 na Arcor, medido em 11/08/2026.
FROTA_DIAS = 3

# Situação do veículo -> o que ela significa para quem quer carregar.
# Vocabulário exato do menu suspenso configurado na planilha em 11/08/2026.
# `ROTA DE ENTREGA` é o trânsito carregado, e é o par de `EM ROTA (VAZIO)`:
# ficou com o nome antigo de propósito, para não invalidar o histórico.
CICLO = {
    "AG CARREGAMENTO": "disponivel",
    "EM CARREGAMENTO": "carregando",
    "ROTA DE ENTREGA": "em_viagem",
    "AG DESCARGA": "descarga",
    "EM DESCARGA": "descarga",
    "EM ROTA (VAZIO)": "chegando",
    "FINALIZADO": "livre",
    "FINALIZADO COM DEV PARCIAL": "livre",
    "RECUSADO": "livre",
    "MANUTENCAO": "parado",
}

# Estados terminais: a viagem acabou e o veículo não está mais preso a ela.
# Serve para escolher qual linha descreve o veículo agora, quando há mais de uma
# aberta — ver `montar_frota`.
TERMINAIS = {"livre"}

# O menu suspenso é novo e a adoção é gradual; o texto que já está na planilha
# precisa cair em algum lugar razoável, senão o painel nasce vazio e ninguém
# confia nele. Casamento por trecho, na ordem — o mais específico primeiro,
# senão "AG DESCARGA" seria capturado por "DESCARGA" e "EM ROTA (VAZIO)" por
# "EM ROTA".
CICLO_APROXIMADO = (
    ("AGUARDANDO CARREG", "disponivel"),
    ("AG CARREG", "disponivel"),
    ("EM CARREGAMENTO", "carregando"),
    ("CARREGANDO", "carregando"),
    ("VAZIO", "chegando"),
    ("AGUARDANDO DESCARGA", "descarga"),
    ("AG DESCARGA", "descarga"),
    ("EM DESCARGA", "descarga"),
    ("DESCARREGANDO", "descarga"),
    ("NO CLIENTE", "descarga"),
    ("MANUTEN", "parado"),
    ("PROBLEMA", "parado"),
    ("QUEBRA", "parado"),
    ("ROTA DE ENTREGA", "em_viagem"),
    ("EM ROTA", "em_viagem"),
    ("FINALIZ", "livre"),
    ("RECUSA", "livre"),
)

# Rótulo de cada grupo no painel, na ordem em que a operação lê.
GRUPOS_FROTA = [
    ("disponivel", "Disponíveis para carregamento"),
    ("chegando", "Chegando vazios"),
    ("carregando", "Em carregamento"),
    ("em_viagem", "Em viagem"),
    ("descarga", "Em descarga"),
    ("livre", "Sem viagem em aberto"),
    ("parado", "Parados"),
]


def situacao_frota(status: str) -> str:
    """Situação do veículo a partir do STATUS ENTREGA da última viagem."""
    s = sem_acento(limpar(status))
    if not s:
        return "indefinido"
    if s in CICLO:
        return CICLO[s]
    for trecho, grupo in CICLO_APROXIMADO:
        if trecho in s:
            return grupo
    return "indefinido"


def montar_frota(viagens: list[dict], hoje: str) -> list[dict]:
    """Uma linha por placa: qual viagem descreve o veículo agora.

    Não é simplesmente "a linha mais recente". O planejamento da Arcor amarra a
    OC ao motorista já com origem e destino, e o mesmo motorista pode ter duas
    OCs para as 24 horas. Pegar a última linha mostraria a **viagem seguinte**,
    ainda não iniciada, em vez da que está acontecendo — e o veículo apareceria
    como disponível estando em rota.

    A regra é: vale a **primeira viagem ainda não encerrada**. Se todas já
    encerraram, vale a última, e o veículo está livre. A OC seguinte não é
    descartada: é dela que sai o destino do retorno vazio, porque é para lá que
    a carreta está indo carregar.
    """
    hoje_d = datetime.fromisoformat(hoje).date()
    corte = (hoje_d - timedelta(days=FROTA_DIAS)).isoformat()

    porPlaca: dict[tuple[str, str], list[dict]] = {}
    for v in viagens:
        placa = v["placa"]
        if not placa or len(placa) < 6 or not v["data"] or v["data"] < corte:
            continue
        # Data no futuro é erro de digitação, não operação. Quatro linhas do
        # Supley com 27/04/2029 colocaram quatro veículos fantasma na frota
        # ativa, todos exibidos como livres — a torre dizia que havia carreta
        # disponível que não existe. Veículo não termina viagem amanhã.
        if v["data"] > hoje:
            continue
        porPlaca.setdefault((v["cliente"], placa), []).append(v)

    frota = []
    for (cliente, placa), lista in sorted(porPlaca.items()):
        # Empate de data resolvido pela linha: a de baixo foi lançada depois.
        lista.sort(key=lambda v: (v["data"], v["linha"]))
        abertas = [v for v in lista if situacao_frota(v["sac"]) not in TERMINAIS]

        atual = abertas[0] if abertas else lista[-1]
        seguinte = abertas[1] if len(abertas) > 1 else None
        situacao = situacao_frota(atual["sac"])

        # Para onde o vazio está indo. A operação define que é o campo de origem
        # do próximo carregamento — e não o destino da viagem que acabou, que
        # apontaria para o lado contrário.
        destino_vazio = ""
        if situacao == "chegando" and seguinte:
            destino_vazio = seguinte["origem"]

        frota.append({
            "cliente": cliente,
            "placa": placa,
            "carreta": atual["carreta"],
            "motorista": atual["motorista"],
            "situacao": situacao,
            "sac": atual["sac"],
            "origem": atual["origem"],
            "destino": atual["destino"],
            "manifesto": atual["manifesto"],
            "oc": atual.get("oc", ""),
            "tipo": atual["tipo"],
            "data": atual["data"],
            "rastreada": atual["rastreada"],
            "destino_vazio": destino_vazio,
            # A OC seguinte já planejada. Serve para a torre mostrar o que vem
            # depois sem misturar com o que está acontecendo.
            "proxima_oc": (seguinte or {}).get("oc", ""),
            "proxima_origem": (seguinte or {}).get("origem", ""),
            "viagens_abertas": len(abertas),
            # Sem isto o painel não distingue apontamento de agora de
            # apontamento de anteontem, e foi assim que a lista chegou a mostrar
            # 65 veículos "disponíveis" com mediana de 78 dias.
            "dias_desde": (hoje_d - datetime.fromisoformat(atual["data"]).date()).days,
        })
    return frota


def hoje_iso() -> str:
    return datetime.now(timezone(timedelta(hours=-3))).date().isoformat()


def classificar_viagem(status: str) -> str:
    """Traduz STATUS ENTREGA das abas de viagem nas classes do painel.

    Vocabulário observado em 11/08/2026: FINALIZADO, NO CLIENTE, RECUSA,
    MANUTENÇAO D VEICULO. Qualquer coisa fora disso vira `andamento` em vez de
    ser inventada como desfecho — e aparece no aviso, para a gente mapear.
    """
    s = sem_acento(limpar(status))
    if not s:
        return "semapont"
    if "FINALIZ" in s:
        return "finalizada"
    if "RECUSA" in s or "DEVOLU" in s:
        return "recusa"
    if "MANUTEN" in s or "PROBLEMA" in s or "QUEBRA" in s:
        return "ocorrencia"
    # "NO CLIENTE" é chegada, não pendência: a carreta está no destino,
    # aguardando ou em descarga. Vale classe própria porque é o estado em que a
    # operação mais precisa agir, e somá-lo a "em trânsito" esconderia isso.
    if s.startswith("NO CLIENTE") or "NO DESTINO" in s:
        return "no_cliente"
    return "andamento"


def mapa_colunas(cabecalho: list[str], arquivo: str) -> dict[str, int]:
    """Cabeçalho -> índice, recusando duplicado.

    Cabeçalho repetido é justamente o que obrigou as abas de café a serem lidas
    por índice. Se aparecer aqui, é melhor quebrar alto do que escolher em
    silêncio uma das duas colunas e produzir número errado por meses.
    """
    indices: dict[str, int] = {}
    repetidos = []
    for i, bruto in enumerate(cabecalho):
        nome = limpar(bruto).upper()
        if not nome:
            continue
        if nome in indices:
            repetidos.append(nome)
            continue
        indices[nome] = i
    if repetidos:
        raise SystemExit(
            f"{arquivo}: cabeçalho duplicado ({', '.join(sorted(set(repetidos)))}). "
            "Leitura por nome ficou ambígua — resolva na planilha ou passe esta "
            "aba a ler por índice, como as de café."
        )
    return indices


def ler_viagens(caminho: Path) -> tuple[list[dict], list[str]]:
    cliente = CLIENTES_VIAGEM[caminho.stem]
    campos = VIAGENS_LAYOUT[caminho.stem]
    avisos: list[str] = []

    with caminho.open(encoding="utf-8-sig", newline="") as fh:
        linhas = list(csv.reader(fh))
    if not linhas:
        return [], [f"{caminho.name}: arquivo vazio"]

    indices = mapa_colunas(linhas[0], caminho.name)

    ausentes = [c for c, h in campos.items() if h.upper() not in indices]
    faltando_grave = [c for c in VIAGEM_OBRIGATORIAS if c in ausentes]
    if faltando_grave:
        raise SystemExit(
            f"{caminho.name}: faltam colunas essenciais "
            f"({', '.join(campos[c] for c in faltando_grave)}). "
            "A aba mudou de layout — corrija VIAGENS_LAYOUT antes de publicar."
        )
    if ausentes:
        avisos.append(f"{caminho.name}: colunas ausentes, ignoradas: "
                      + ", ".join(campos[c] for c in ausentes))

    viagens: list[dict] = []
    vocabulario: set[str] = set()
    for i, linha in enumerate(linhas[1:], start=2):
        if not any(limpar(c) for c in linha):
            continue

        def get(campo: str) -> str:
            h = campos.get(campo, "").upper()
            j = indices.get(h)
            return limpar(linha[j]) if j is not None and j < len(linha) else ""

        manifesto, placa = get("manifesto"), get("placa")
        if not manifesto and not placa:
            continue

        iso = data_iso(get("data"))
        if get("data") and not iso:
            avisos.append(f"{caminho.name} linha {i}: data inválida "
                          f"({get('data')!r})")

        bruto = get("status")
        classe = classificar_viagem(bruto)
        # O aviso só serve se apontar palavra nova. Termo já conhecido que cai
        # em `andamento` de propósito não entra, senão o log grita todo dia e a
        # gente para de ler — e aí o dia em que aparecer status de verdade novo
        # passa batido.
        if classe == "andamento" and bruto and sem_acento(bruto) not in EM_TRANSITO:
            vocabulario.add(bruto.upper())

        viagens.append({
            "cliente": cliente,
            "linha": i,
            "data": iso,
            "manifesto": manifesto,
            "oc": get("oc"),
            "tipo": titulo(get("tipo_operacao")),
            "origem": titulo(get("origem")),
            "destino": titulo(get("destino")),
            "veiculo": titulo(get("tipo_veiculo")),
            "nf": get("nf"),
            "motorista": titulo(get("motorista")),
            "placa": get("placa").upper().replace(" ", ""),
            "carreta": get("carreta").upper().replace(" ", ""),
            "sac": bruto,
            "classe": classe,
            "canhoto": get("canhoto"),
            # Só o Supley tem apontamento de tempo hoje, e só desde junho. Na
            # Arcor esses campos nascem vazios e só ganham conteúdo quando o
            # rastreador entrar — decisão da operação, não limitação daqui.
            "tempos": {
                "chegada_origem": get("chegada_origem"),
                "carregamento": get("carregamento"),
                "liberacao_origem": get("liberacao_origem"),
                "chegada_destino": get("chegada_destino"),
                "hora_chegada_destino": get("hora_chegada_destino"),
                "liberacao_destino": get("liberacao_destino"),
                "hora_liberacao_destino": get("hora_liberacao_destino"),
            },
            # SIM/NÃO na planilha do Supley. Serve para o painel distinguir
            # "sem posição porque o rastreador falhou" de "viagem que nunca foi
            # rastreada" — sem isso o mapa mostra buraco e parece defeito.
            "rastreada": sem_acento(get("rastreada")).startswith("SIM")
            if "rastreada" in campos and "rastreada" not in ausentes else None,
        })

    futuras = [v for v in viagens if v["data"] and v["data"] > hoje_iso()]
    if futuras:
        avisos.append(f"{caminho.name}: {len(futuras)} linhas com data no futuro "
                      f"(ex.: linha {futuras[0]['linha']}, {futuras[0]['data']}) — "
                      "fora da frota ativa, provável erro de digitação")

    if vocabulario:
        avisos.append(f"{caminho.name}: STATUS ENTREGA não mapeado, tratado como "
                      f"andamento: {', '.join(sorted(vocabulario)[:8])}")
    return viagens, avisos


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
    todos = sorted(ENTRADA.glob("*.csv"))
    # O laço abaixo trata todo CSV como aba de café. Sem esta separação, Arcor e
    # Supley entrariam em `operacao.json` — o payload que a 3 Corações decifra.
    arquivos = [a for a in todos if a.stem not in CLIENTES_VIAGEM]
    arquivos_viagem = [a for a in todos if a.stem in CLIENTES_VIAGEM]
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

    # --- viagens: Arcor e Supley, arquivo à parte ---
    viagens: list[dict] = []
    avisos_v: list[str] = []
    for arq in arquivos_viagem:
        v, a = ler_viagens(arq)
        recentes = [x for x in v if not x["data"] or x["data"] >= corte]
        viagens.extend(recentes)
        avisos_v.extend(a)
        fora = len(v) - len(recentes)
        sufixo = f" ({fora} fora da janela de {RETENCAO_DIAS} dias)" if fora else ""
        print(f"  {arq.name}: {len(recentes)} viagens{sufixo}")

    frota = montar_frota(viagens, agora.date().isoformat())

    SAIDA_VIAGENS.write_text(json.dumps({
        "gerado_em": agora.isoformat(timespec="seconds"),
        "fonte": "Planilha ROTEIRIZAÇÃO 2026 · abas Arcor e Supley",
        "retencao_dias": RETENCAO_DIAS,
        "frota_dias": FROTA_DIAS,
        "modelo": "viagem",
        "viagens": viagens,
        "frota": frota,
        "avisos": avisos_v,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    entregas = [n for n in notas if n["tipo"] == "ENTREGA"]
    print(f"\n{SAIDA.relative_to(RAIZ)} gravado")
    print(f"  {len(entregas)} notas de entrega · {len(planos)} planos "
          f"· {len({n['data'] for n in entregas if n['data']})} dias")
    for k, v in divergencias(notas).items():
        if v:
            print(f"  divergência · {k}: {v if isinstance(v, int) else ', '.join(v)}")
    for a in avisos:
        print(f"  aviso · {a}")

    if arquivos_viagem:
        porc = {}
        for v in viagens:
            porc.setdefault(v["cliente"], []).append(v)
        print(f"\n{SAIDA_VIAGENS.relative_to(RAIZ)} gravado")
        for cliente, lista in sorted(porc.items()):
            classes = {}
            for v in lista:
                classes[v["classe"]] = classes.get(v["classe"], 0) + 1
            resumo = ", ".join(f"{k} {n}" for k, n in sorted(classes.items()))
            print(f"  {cliente}: {len(lista)} viagens · {resumo}")

        print(f"\n  frota ativa (linha nos últimos {FROTA_DIAS} dias)")
        for cliente in sorted({f["cliente"] for f in frota}):
            da_vez = [f for f in frota if f["cliente"] == cliente]
            grupos = {}
            for f in da_vez:
                grupos[f["situacao"]] = grupos.get(f["situacao"], 0) + 1
            resumo = " · ".join(f"{r.lower()} {grupos[k]}"
                                for k, r in GRUPOS_FROTA if grupos.get(k))
            indef = grupos.get("indefinido", 0)
            if indef:
                resumo += f" · situação não reconhecida {indef}"
            print(f"    {cliente}: {len(da_vez)} veículos · {resumo}")

        for a in avisos_v:
            print(f"  aviso · {a}")


if __name__ == "__main__":
    main()
