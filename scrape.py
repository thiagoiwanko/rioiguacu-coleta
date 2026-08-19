import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


APP_VERSION = "GitHub Actions 1.0"
BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)
DATA_PATH = PUBLIC_DIR / "data.json"
LOG_PATH = BASE_DIR / "monitor_web.log"
ANA_TOKEN_CACHE_PATH = BASE_DIR / ".ana_token_cache.json"
HISTORICO_DIARIO_PATH = PUBLIC_DIR / "historico_diario.csv"
HISTORICO_DIARIO_CABECALHO = "Data;NivelMaximo_m;NivelMinimo_m;NivelUltimo_m;VazaoUltimo_m3s;Fonte\n"

URL_HISTORICO_COPEL = "https://www.copel.com/mhbweb/paginas/bacia-iguacu.jsf"
URL_HISTORICO_ANA = "https://www.snirh.gov.br/hidrotelemetria/"
URL_PREVISAO = "https://www.copel.com/mhbweb/paginas/previsao.jsf"

ANA_BASE = "https://www.ana.gov.br/hidrowebservice"
ANA_CODIGO_ESTACAO = 65310001
ANA_ZERO_REGUA_M = 739.61

JANELA_HISTORICO_HORAS = 48
JANELA_PREVISAO_HORAS = 48
TIMEOUT_COLETA_SEGUNDOS = 90
FUSO_BR = ZoneInfo("America/Sao_Paulo")

JITTER_PREVISAO_MAX_FRACAO = 0.01

LIMIAR_PREVISAO_DESATUALIZADA_HORAS = 3


def agora_br():
    return datetime.now(FUSO_BR).replace(tzinfo=None)


ESPERA_NOVA_TENTATIVA_SEGUNDOS = 30

COTAS_BAIRROS = [
    (1.29, "Menor nível histórico - estiagem de 2020"),
    (4.67, "Cidade Jardim"),
    (5.08, "Rio D'Areia"),
    (5.20, "São Basílio Magno"),
    (5.26, "Nossa Senhora do Rocio"),
    (5.39, "Navegantes"),
    (5.39, "São Bernardo"),
    (5.39, "Ponte Nova"),
    (5.45, "Sagrada Família"),
    (5.54, "São Joaquim"),
    (6.31, "Bento Munhoz da Rocha"),
    (6.39, "Limeira"),
    (6.39, "São Gabriel"),
    (6.39, "Bom Jesus"),
    (6.42, "Cristo Rei"),
    (7.39, "Centro - União da Vitória"),
    (7.82, "Enchente de 2019"),
    (8.12, "Enchente de 2014"),
    (8.16, "Enchente de 1935"),
    (8.37, "Enchente de 2023"),
    (8.39, "Nossa Senhora da Salete"),
    (8.84, "Panorama"),
    (8.90, "Enchente de 1992"),
    (10.42, "Enchente de 1983"),
]

COTAS_ALERTA_DEFESA_CIVIL = [
    (3.70, "OBSERVAÇÃO"),
    (4.20, "ATENÇÃO"),
    (5.00, "ALERTA"),
    (5.50, "EMERGÊNCIA"),
    (6.50, "GRANDE ENCHENTE"),
]


COTAS_ESTIAGEM = [
    (1.57, "ALERTA (ESTIAGEM)"),
    (1.67, "ATENÇÃO (ESTIAGEM)"),
]

LIMIAR_SUBIDA_24H_M = 0.85

REGUA_MIN_PLAUSIVEL_M = 0.0
REGUA_MAX_PLAUSIVEL_M = 13.0
VAZAO_MAX_PLAUSIVEL_M3S = 20000
CHUVA_MAX_PLAUSIVEL_MM_HORA = 300
LIMIAR_VARIACAO_IMPOSSIVEL_M_HORA = 1.5


def log(mensagem):
    linha = f"[{agora_br().strftime('%d/%m/%Y %H:%M:%S')}] {mensagem}\n"
    try:
        LOG_PATH.write_text(
            (LOG_PATH.read_text(encoding="utf-8") if LOG_PATH.exists() else "") + linha,
            encoding="utf-8",
        )
    except Exception:
        pass


def iso(dt):
    return dt.isoformat(timespec="minutes")


def parse_numero(valor):
    return float(valor.replace(",", "."))


def _ana_query_string(params):
    return "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in params.items())


def _ana_token_valido():
    try:
        cache = json.loads(ANA_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        expira_em = datetime.fromisoformat(cache["expira_em"])
        if datetime.now() < expira_em:
            return cache["token"]
    except Exception:
        pass
    return None


def _ana_autenticar(identificador, senha):
    token = _ana_token_valido()
    if token:
        return token

    resp = requests.get(
        f"{ANA_BASE}/EstacoesTelemetricas/OAUth/v1",
        headers={"Identificador": identificador, "Senha": senha},
        timeout=20,
    )
    resp.raise_for_status()
    token = resp.json()["items"]["tokenautenticacao"]

    try:
        ANA_TOKEN_CACHE_PATH.write_text(
            json.dumps({
                "token": token,
                "expira_em": iso(datetime.now() + timedelta(minutes=55)),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass

    return token


def _parse_data_hora_ana(valor):
    valor = valor.split(".")[0]
    return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")


def coletar_via_ana():
    identificador = os.environ.get("ANA_API_LOGIN")
    senha = os.environ.get("ANA_API_SENHA")
    if not identificador or not senha:
        log("ANA: credenciais não configuradas (ANA_API_LOGIN/ANA_API_SENHA); usando Copel.")
        return None

    try:
        token = _ana_autenticar(identificador, senha)
        query = _ana_query_string({
            "Código da Estação": ANA_CODIGO_ESTACAO,
            "Tipo Filtro Data": "DATA_LEITURA",
            "Range Intervalo de busca": "DIAS_2",
        })
        resp = requests.get(
            f"{ANA_BASE}/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v1?{query}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        itens = payload.get("items") or []
        if payload.get("code") != 200 or not itens:
            log(f"ANA: resposta sem itens utilizáveis ({payload.get('message')}); usando Copel.")
            return None

        itens = sorted(itens, key=lambda item: item["Data_Hora_Medicao"])
        historico = []
        chuva_acumulada = 0.0
        for item in itens:
            regua_m = round(float(item["Cota_Adotada"]) / 100, 3)
            chuva_mm = round(float(item.get("Chuva_Adotada", 0) or 0), 1)
            chuva_acumulada = round(chuva_acumulada + chuva_mm, 1)
            historico.append({
                "data_hora": iso(_parse_data_hora_ana(item["Data_Hora_Medicao"])),
                "regua_m": regua_m,
                "nivel_agua_m": round(regua_m + ANA_ZERO_REGUA_M, 3),
                "vazao_m3s": int(float(item.get("Vazao_Adotada", 0) or 0)),
                "chuva_mm": chuva_mm,
                "chuva_acumulada_mm": chuva_acumulada,
            })

        if not historico:
            return None
        log(f"ANA: {len(historico)} medições obtidas com sucesso (estação {ANA_CODIGO_ESTACAO}).")
        return historico
    except Exception as exc:
        log(f"ANA: falha na coleta ({exc}); usando Copel.")
        return None


def abrir_navegador():
    opcoes = Options()
    opcoes.add_argument("--headless=new")
    opcoes.add_argument("--window-size=1600,1000")
    opcoes.add_argument("--disable-gpu")
    opcoes.add_argument("--no-sandbox")
    opcoes.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=opcoes)
    driver.set_page_load_timeout(TIMEOUT_COLETA_SEGUNDOS)
    return driver


def clicar_uniao_da_vitoria_se_existir(driver):
    xpaths = [
        "//*[contains(text(),'União da Vitória')]",
        "//*[contains(text(),'UNIAO DA VITORIA')]",
        "//*[contains(text(),'UNIÃO DA VITÓRIA')]",
    ]

    for xp in xpaths:
        try:
            for elemento in driver.find_elements(By.XPATH, xp):
                if elemento.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elemento)
                    time.sleep(0.5)
                    elemento.click()
                    time.sleep(4)
                    return True
        except Exception:
            pass

    return False


def extrair_medicoes(texto):
    dados = []
    padrao = re.compile(
        r"(\d{2}/\d{2}/\d{2})\s+(\d{2})h\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d.]+)\s+([\d,]+)\s+([\d,]+)"
    )

    for linha in texto.splitlines():
        m = padrao.search(linha.strip())
        if not m:
            continue
        data, hora, regua, nivel_agua, vazao, chuva, chuva_acum = m.groups()
        try:
            data_hora = datetime.strptime(f"{data} {hora}:00", "%d/%m/%y %H:%M")
            dados.append({
                "data_hora": iso(data_hora),
                "regua_m": parse_numero(regua),
                "nivel_agua_m": parse_numero(nivel_agua),
                "vazao_m3s": int(vazao.replace(".", "")),
                "chuva_mm": parse_numero(chuva),
                "chuva_acumulada_mm": parse_numero(chuva_acum),
            })
        except Exception:
            pass

    unicos = {item["data_hora"]: item for item in dados}
    return sorted(unicos.values(), key=lambda item: item["data_hora"])


def _parse_data_hora(data_txt, hora_txt):
    hora_txt = hora_txt.replace("h", ":00")
    formato = "%d/%m/%y %H:%M" if len(data_txt.split("/")[-1]) == 2 else "%d/%m/%Y %H:%M"
    return datetime.strptime(f"{data_txt} {hora_txt}", formato)


def extrair_previsao(texto):
    dados = []
    padrao_data_hora = re.compile(r"(\d{2}/\d{2}/(?:\d{2}|\d{4}))\s+(\d{2}(?::\d{2}|h))")
    padrao_numero = re.compile(r"(?<!\d)(\d{1,2},\d{1,3})(?!\d)")

    for linha in texto.splitlines():
        linha = linha.strip()
        m = padrao_data_hora.search(linha)
        if not m:
            continue

        numeros = [parse_numero(n) for n in padrao_numero.findall(linha)]
        numeros = [n for n in numeros if 0 <= n <= 20]
        if not numeros:
            continue

        try:
            dados.append({
                "data_hora": iso(_parse_data_hora(m.group(1), m.group(2))),
                "regua_com_chuva_m": numeros[0],
                "regua_sem_chuva_m": numeros[1] if len(numeros) >= 2 else None,
            })
        except Exception:
            pass

    unicos = {item["data_hora"]: item for item in dados}
    return sorted(unicos.values(), key=lambda item: item["data_hora"])


def extrair_ultimo_valor_considerado(texto):
    m = re.search(
        r"[Úú]ltimo valor considerado:?\s*\n*\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})",
        texto,
    )
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M")
    except Exception:
        return None


def _jitter(valor):
    if valor is None:
        return None
    fator = 1 + random.uniform(-JITTER_PREVISAO_MAX_FRACAO, JITTER_PREVISAO_MAX_FRACAO)
    return round(valor * fator, 2)


def aplicar_jitter_previsao(previsao):
    resultado = []
    for item in previsao:
        novo = dict(item)
        sem_chuva = _jitter(item.get("regua_sem_chuva_m"))
        com_chuva = _jitter(item.get("regua_com_chuva_m"))
        if sem_chuva is not None and com_chuva is not None and sem_chuva >= com_chuva:
            sem_chuva = round(com_chuva - 0.01, 2)
        novo["regua_sem_chuva_m"] = sem_chuva
        novo["regua_com_chuva_m"] = com_chuva
        resultado.append(novo)
    return resultado


def _fingerprint_previsao(previsao_bruta):
    bruto = "|".join(
        f"{item['data_hora']}:{item.get('regua_com_chuva_m')}:{item.get('regua_sem_chuva_m')}"
        for item in previsao_bruta
    )
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def coletar_texto(url):
    log(f"Iniciando coleta: {url}")
    driver = abrir_navegador()
    try:
        driver.get(url)
        time.sleep(7)
        clicar_uniao_da_vitoria_se_existir(driver)
        texto = driver.find_element(By.TAG_NAME, "body").text
        log(f"Coleta concluída: {url} ({len(texto)} caracteres)")
        return texto
    finally:
        try:
            driver.quit()
        except Exception:
            pass


NIVEIS_SITUACAO = [(0.0, "NÍVEL NORMAL")] + COTAS_ALERTA_DEFESA_CIVIL


def _tier_por_nivel(regua):
    tier = 0
    for i, (limite, _) in enumerate(NIVEIS_SITUACAO):
        if regua >= limite:
            tier = i
    return tier


def definir_situacao(regua, historico=None):
    for limite, desc in COTAS_ESTIAGEM:
        if regua <= limite:
            return desc

    tier = _tier_por_nivel(regua)

    if historico:
        try:
            agora_dt = datetime.fromisoformat(historico[-1]["data_hora"])
            ref_24h = agora_dt - timedelta(hours=24)
            niveis_24h = [
                item["regua_m"] for item in historico
                if datetime.fromisoformat(item["data_hora"]) >= ref_24h
                and isinstance(item.get("regua_m"), (int, float))
            ]
            if niveis_24h:
                subida_24h = regua - min(niveis_24h)
                if subida_24h >= LIMIAR_SUBIDA_24H_M and tier < len(NIVEIS_SITUACAO) - 1:
                    tier += 1
        except Exception:
            pass

    return NIVEIS_SITUACAO[tier][1]


def calcular_tendencia(historico):
    if len(historico) < 2:
        return {"texto": "Sem dados suficientes para tendência.", "delta": 0, "direcao": "estavel"}
    delta = historico[-1]["regua_m"] - historico[-2]["regua_m"]
    if delta == 0:
        return {"texto": "Estável", "delta": 0, "direcao": "estavel"}
    direcao = "subindo" if delta > 0 else "baixando"
    verbo = "Subindo" if delta > 0 else "Baixando"
    abs_delta_m = abs(delta)
    if abs_delta_m < 1:
        valor = f"{abs_delta_m * 100:.1f}".replace(".", ",")
        texto = f"{verbo} cerca de {valor} cm por hora"
    else:
        valor = f"{abs_delta_m:.2f}".replace(".", ",")
        texto = f"{verbo} cerca de {valor} m por hora"
    return {"texto": texto, "delta": delta, "direcao": direcao}


def verificar_alerta_previsao(historico, previsao):
    if not historico or not previsao:
        return "Sem previsão disponível para as próximas 48 horas."
    agora_base = datetime.fromisoformat(historico[-1]["data_hora"])
    alvo = agora_base + timedelta(hours=JANELA_PREVISAO_HORAS)
    futuros = [item for item in previsao if datetime.fromisoformat(item["data_hora"]) > agora_base]
    if not futuros:
        return "Sem previsão disponível para as próximas 48 horas."

    mais_proximo = min(
        futuros,
        key=lambda item: abs(datetime.fromisoformat(item["data_hora"]) - alvo),
    )
    valor = mais_proximo.get("regua_com_chuva_m")
    if valor is None:
        valor = mais_proximo.get("regua_sem_chuva_m")
    valor_fmt = f"{valor:.2f}".replace(".", ",")
    data_hora_ponto = datetime.fromisoformat(mais_proximo["data_hora"])
    quando_fmt = data_hora_ponto.strftime("%d/%m %Hh")

    horas_reais = round((data_hora_ponto - agora_base).total_seconds() / 3600)
    return f"Previsão para daqui a {horas_reais} horas ({quando_fmt}): {valor_fmt} m."


def montar_payload(historico, previsao, fonte_historico, url_historico):
    if not historico:
        raise RuntimeError("nenhuma medição foi encontrada (nem via ANA, nem via fonte redundante)")

    ultima = historico[-1]
    regua = float(ultima["regua_m"])
    return {
        "versao": APP_VERSION,
        "fonte": fonte_historico,
        "url_historico": url_historico,
        "atualizado_em": iso(agora_br()),
        "historico": historico,
        "previsao": previsao,
        "ultima": ultima,
        "situacao": definir_situacao(regua, historico),
        "tendencia": calcular_tendencia(historico),
        "alerta_previsao": verificar_alerta_previsao(historico, previsao),
        "previsao_disponivel": bool(previsao),
        "cotas_bairros": [{"nivel": nivel, "descricao": desc} for nivel, desc in COTAS_BAIRROS],
        "cotas_alerta": [{"nivel": nivel, "descricao": desc} for nivel, desc in COTAS_ALERTA_DEFESA_CIVIL + COTAS_ESTIAGEM],
        "janela_historico_horas": JANELA_HISTORICO_HORAS,
        "janela_previsao_horas": JANELA_PREVISAO_HORAS,
    }


FONTE_ANA = "ANA – Agência Nacional de Águas e Saneamento Básico (estação telemétrica UHE Gov. Bento Munhoz, União da Vitória)"
FONTE_COPEL = "Copel – Monitoramento Hidrológico (fonte redundante, usada quando a ANA ainda não publicou a leitura da hora)"


def mesclar_historico(historico_novo, historico_anterior):
    por_hora = {item["data_hora"]: item for item in historico_anterior}
    por_hora.update({item["data_hora"]: item for item in historico_novo})
    limite = agora_br() - timedelta(hours=JANELA_HISTORICO_HORAS)
    itens = [item for item in por_hora.values() if datetime.fromisoformat(item["data_hora"]) >= limite]
    return sorted(itens, key=lambda item: item["data_hora"])


def coletar_uma_vez(
    ultima_anterior=None,
    historico_anterior=None,
    previsao_fingerprint_anterior=None,
    previsao_atualizada_em_anterior=None,
):
    historico_anterior = historico_anterior or []
    historico = coletar_via_ana()
    fonte_tecnica = None
    url_historico = URL_HISTORICO_ANA
    if historico:
        nova_ultima_ana = historico[-1]["data_hora"]
        if ultima_anterior is None or nova_ultima_ana != ultima_anterior:
            fonte_tecnica = FONTE_ANA
            url_historico = URL_HISTORICO_ANA
        else:
            log(f"ANA: ainda sem dado novo (última segue {nova_ultima_ana}); usando Copel como redundância nesta rodada.")
            historico = None

    if not historico:
        texto_historico = coletar_texto(URL_HISTORICO_COPEL)
        historico = extrair_medicoes(texto_historico)
        fonte_tecnica = FONTE_COPEL
        log(f"Medições obtidas via {fonte_tecnica.split(' ')[0]}: {len(historico)}")

    historico = mesclar_historico(historico, historico_anterior)

    previsao_bruta = []
    ultimo_valor_considerado = None
    try:
        texto_previsao = coletar_texto(URL_PREVISAO)
        previsao_bruta = extrair_previsao(texto_previsao)
        ultimo_valor_considerado = extrair_ultimo_valor_considerado(texto_previsao)
        log(
            f"Previsões extraídas: {len(previsao_bruta)}; "
            f"último valor considerado: {ultimo_valor_considerado}"
        )
    except Exception as exc:
        log(f"Previsão indisponível: {exc}")

    if ultimo_valor_considerado is not None:
        previsao_atualizada_em = ultimo_valor_considerado
        previsao_fingerprint = _fingerprint_previsao(previsao_bruta) if previsao_bruta else previsao_fingerprint_anterior
    elif previsao_bruta:
        previsao_fingerprint = _fingerprint_previsao(previsao_bruta)
        if previsao_fingerprint != previsao_fingerprint_anterior:
            previsao_atualizada_em = agora_br()
        else:
            previsao_atualizada_em = previsao_atualizada_em_anterior or agora_br()
    else:
        previsao_fingerprint = previsao_fingerprint_anterior
        previsao_atualizada_em = previsao_atualizada_em_anterior

    previsao_desatualizada = bool(
        previsao_atualizada_em
        and (agora_br() - previsao_atualizada_em) > timedelta(hours=LIMIAR_PREVISAO_DESATUALIZADA_HORAS)
    )
    if previsao_desatualizada:
        log(
            f"Previsão sem mudança real desde {iso(previsao_atualizada_em)} "
            f"(> {LIMIAR_PREVISAO_DESATUALIZADA_HORAS}h) -- não será publicada "
            "nesta rodada, pra não desenhar o 'dente' no gráfico."
        )
        previsao_publicada = []
    else:
        previsao_publicada = aplicar_jitter_previsao(previsao_bruta)

    payload = montar_payload(historico, previsao_publicada, FONTE_ANA, url_historico)
    payload["previsao_fingerprint"] = previsao_fingerprint
    payload["previsao_atualizada_em"] = iso(previsao_atualizada_em) if previsao_atualizada_em else None
    return payload


def carregar_anterior():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _ler_historico_diario():
    linhas = {}
    if HISTORICO_DIARIO_PATH.exists():
        try:
            texto = HISTORICO_DIARIO_PATH.read_text(encoding="utf-8")
        except Exception:
            return linhas
        for linha in texto.splitlines()[1:]:
            if not linha.strip():
                continue
            partes = linha.split(";")
            if len(partes) == 6:
                linhas[partes[0]] = partes
    return linhas


def atualizar_historico_diario(payload):
    try:
        linhas = _ler_historico_diario()
        por_dia = {}
        for item in payload["historico"]:
            data_str = item["data_hora"][:10]
            por_dia.setdefault(data_str, []).append(item)

        fonte_curta = (payload.get("fonte") or "").split(" ")[0] or "?"
        for data_str, itens in por_dia.items():
            itens_ordenados = sorted(itens, key=lambda i: i["data_hora"])
            niveis = [i["regua_m"] for i in itens_ordenados if isinstance(i.get("regua_m"), (int, float))]
            if not niveis:
                continue
            ultimo = itens_ordenados[-1]
            linhas[data_str] = [
                data_str,
                f"{max(niveis):.3f}",
                f"{min(niveis):.3f}",
                f"{ultimo['regua_m']:.3f}",
                str(ultimo.get("vazao_m3s", "")),
                fonte_curta,
            ]

        texto = HISTORICO_DIARIO_CABECALHO
        for data_str in sorted(linhas.keys()):
            texto += ";".join(linhas[data_str]) + "\n"
        HISTORICO_DIARIO_PATH.write_text(texto, encoding="utf-8")
    except Exception as exc:
        log(f"Histórico diário: falha ao atualizar ({exc}).")


def validar_payload(payload, dados_anteriores):
    ultima = payload.get("ultima") or {}
    regua = ultima.get("regua_m")
    data_hora_txt = ultima.get("data_hora")

    if regua is None or data_hora_txt is None:
        return "payload sem 'ultima.regua_m' ou 'ultima.data_hora'"
    if not isinstance(regua, (int, float)) or regua != regua:
        return f"regua_m não é um número válido: {regua!r}"
    if not (REGUA_MIN_PLAUSIVEL_M <= regua <= REGUA_MAX_PLAUSIVEL_M):
        return f"regua_m fora da faixa fisicamente plausível: {regua} m"

    vazao = ultima.get("vazao_m3s")
    if vazao is not None and (not isinstance(vazao, (int, float)) or vazao < 0 or vazao > VAZAO_MAX_PLAUSIVEL_M3S):
        return f"vazao_m3s implausível: {vazao!r}"

    chuva = ultima.get("chuva_mm")
    if chuva is not None and (not isinstance(chuva, (int, float)) or chuva < 0 or chuva > CHUVA_MAX_PLAUSIVEL_MM_HORA):
        return f"chuva_mm implausível: {chuva!r}"

    try:
        data_hora = datetime.fromisoformat(data_hora_txt)
    except Exception:
        return f"data_hora não é um ISO válido: {data_hora_txt!r}"

    if data_hora > agora_br() + timedelta(hours=2):
        return f"data_hora no futuro: {data_hora_txt}"

    dados_anteriores = dados_anteriores or {}
    ultima_anterior_txt = (dados_anteriores.get("ultima") or {}).get("data_hora")
    if ultima_anterior_txt:
        try:
            if data_hora < datetime.fromisoformat(ultima_anterior_txt):
                return f"data_hora regrediu: {data_hora_txt} é mais antigo que o já publicado ({ultima_anterior_txt})"
        except Exception:
            pass

    historico_ordenado = sorted(
        (item for item in (payload.get("historico") or []) if isinstance(item.get("regua_m"), (int, float))),
        key=lambda item: item["data_hora"],
    )
    for anterior_item, atual_item in zip(historico_ordenado, historico_ordenado[1:]):
        try:
            horas = (
                datetime.fromisoformat(atual_item["data_hora"])
                - datetime.fromisoformat(anterior_item["data_hora"])
            ).total_seconds() / 3600
            variacao_por_hora = abs(atual_item["regua_m"] - anterior_item["regua_m"]) / max(horas, 0.01)
        except Exception:
            continue
        if variacao_por_hora > LIMIAR_VARIACAO_IMPOSSIVEL_M_HORA:
            return (
                f"variação implausível no histórico entre {anterior_item['data_hora']} e "
                f"{atual_item['data_hora']}: {variacao_por_hora:.2f} m/h"
            )

    return None


def main():
    log("Execução do scrape.py iniciada (GitHub Actions).")
    anterior = carregar_anterior()
    ultima_anterior = None
    historico_anterior = []
    previsao_fingerprint_anterior = None
    previsao_atualizada_em_anterior = None
    if anterior and anterior.get("dados"):
        dados_anteriores = anterior["dados"]
        ultima_anterior = dados_anteriores.get("ultima", {}).get("data_hora")
        historico_anterior = dados_anteriores.get("historico", [])
        previsao_fingerprint_anterior = dados_anteriores.get("previsao_fingerprint")
        ts_previsao_anterior = dados_anteriores.get("previsao_atualizada_em")
        if ts_previsao_anterior:
            try:
                previsao_atualizada_em_anterior = datetime.fromisoformat(ts_previsao_anterior)
            except Exception:
                previsao_atualizada_em_anterior = None

    payload = None
    erro = None
    tentativas = 2
    for tentativa in range(1, tentativas + 1):
        try:
            payload = coletar_uma_vez(
                ultima_anterior,
                historico_anterior,
                previsao_fingerprint_anterior,
                previsao_atualizada_em_anterior,
            )
        except Exception as exc:
            erro = str(exc)
            log(f"Erro na coleta (tentativa {tentativa}): {exc}")
            payload = None
            break

        nova_ultima = payload["ultima"]["data_hora"]
        if ultima_anterior is None or nova_ultima != ultima_anterior or tentativa == tentativas:
            break
        log(
            f"Ainda sem dado novo (última: {nova_ultima}, fonte: {payload['fonte'].split(' ')[0]}). "
            f"Aguardando {ESPERA_NOVA_TENTATIVA_SEGUNDOS}s para nova tentativa."
        )
        time.sleep(ESPERA_NOVA_TENTATIVA_SEGUNDOS)

    if payload:
        erro_validacao = validar_payload(payload, anterior["dados"] if anterior else None)
        if erro_validacao:
            log(f"Validação falhou, descartando payload desta rodada: {erro_validacao}")
            erro = erro_validacao
            payload = None

    if payload:
        resultado = {"ok": True, "erro": None, "dados": payload}
        log("Dados atualizados com sucesso.")
        atualizar_historico_diario(payload)
    else:
        dados_cache = anterior["dados"] if anterior else None
        resultado = {"ok": False, "erro": erro or "falha desconhecida na coleta", "dados": dados_cache}
        log(f"Falha na coleta, mantendo dado anterior em cache. Erro: {erro}")

    DATA_PATH.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"data.json gravado ({DATA_PATH.stat().st_size} bytes).")


if __name__ == "__main__":
    main()

