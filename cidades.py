import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
CIDADES_DIR = PUBLIC_DIR / "cidades"
INDICE_PATH = PUBLIC_DIR / "cidades.json"

ANA_BASE = "https://www.ana.gov.br/hidrowebservice"
ANA_TOKEN_CACHE_PATH = BASE_DIR / ".ana_token_cache.json"
FUSO_BR = ZoneInfo("America/Sao_Paulo")
JANELA_HISTORICO_HORAS = 48
TIMEOUT_SEGUNDOS = 30
INTERVALO_MINIMO_MINUTOS = int(os.environ.get("CIDADES_INTERVALO_MIN", "55"))
PAUSA_ENTRE_ESTACOES_S = 8

FONTE_ANA = "ANA - Agencia Nacional de Aguas e Saneamento Basico"
FONTE_ALERTAS = ("Hidroinfo / Instituto Água e Terra (IAT) - Governo do Paraná, geopr.iat.pr.gov.br")

ATRASO_AVISO_MIN = int(os.environ.get("CIDADES_ATRASO_AVISO_MIN", "150"))

CIDADES = [
    {
        "slug": "porto-vitoria",
        "nome": "Porto Vitória",
        "uf": "PR",
        "codigo_ana": 65365801,
        "lat": -26.1653,
        "lon": -51.2281,
        "zero_regua_m": 739.90,
        "enchentes": [
            {"nivel": 4.14, "descricao": "Enchente de 2023"},
            {"nivel": 3.76, "descricao": "Enchente de 2000"},
            {"nivel": 3.19, "descricao": "Enchente de 2019"},
            {"nivel": 3.13, "descricao": "Enchente de 1999"},
            {"nivel": 3.07, "descricao": "Enchente de 2001"},
            {"nivel": 3.00, "descricao": "Enchente de 2007"},
            {"nivel": 0.01, "descricao": "Menor nível histórico - estiagem de 2020"},
        ],
    },
    {
        "slug": "sao-mateus-do-sul",
        "alertas": [
            {"nivel": 4.00, "descricao": "ALARME"},
            {"nivel": 3.85, "descricao": "ALERTA"},
            {"nivel": 3.60, "descricao": "ATENÇÃO"},
            {"nivel": 0.40, "descricao": "ATENÇÃO (ESTIAGEM)"},
            {"nivel": 0.30, "descricao": "ALERTA (ESTIAGEM)"},
            {"nivel": 0.14, "descricao": "ESCASSEZ HÍDRICA"},
        ],
        "nome": "São Mateus do Sul",
        "uf": "PR",
        "codigo_ana": 65060001,
        "lat": -25.8756,
        "lon": -50.3894,
        "zero_regua_m": None,
        "enchentes": [
            {"nivel": 7.19, "descricao": "Enchente de 1992"},
            {"nivel": 6.72, "descricao": "Enchente de 1983"},
            {"nivel": 6.16, "descricao": "Enchente de 2014"},
            {"nivel": 6.07, "descricao": "Enchente de 1995"},
            {"nivel": 6.02, "descricao": "Enchente de 2010"},
            {"nivel": 5.97, "descricao": "Enchente de 2023"},
            {"nivel": 5.87, "descricao": "Enchente de 1957"},
            {"nivel": 0.04, "descricao": "Menor nível histórico - estiagem de 1963"},
        ],
    },
    {
        "slug": "fluviopolis",
        "alertas": [
            {"nivel": 4.20, "descricao": "ALARME"},
            {"nivel": 4.00, "descricao": "ALERTA"},
            {"nivel": 3.85, "descricao": "ATENÇÃO"},
            {"nivel": 0.75, "descricao": "ATENÇÃO (ESTIAGEM)"},
            {"nivel": 0.63, "descricao": "ALERTA (ESTIAGEM)"},
            {"nivel": 0.48, "descricao": "ESCASSEZ HÍDRICA"},
        ],
        "nome": "Fluviópolis",
        "uf": "PR",
        "codigo_ana": 65220001,
        "lat": -26.0192,
        "lon": -50.5925,
        "zero_regua_m": None,
        "enchentes": [
            {"nivel": 9.66, "descricao": "Enchente de 1983"},
            {"nivel": 9.41, "descricao": "Enchente de 1992"},
            {"nivel": 8.38, "descricao": "Enchente de 2014"},
            {"nivel": 7.85, "descricao": "Enchente de 2023"},
            {"nivel": 6.76, "descricao": "Enchente de 2010"},
            {"nivel": 6.52, "descricao": "Enchente de 1998"},
            {"nivel": 0.30, "descricao": "Menor nível histórico - estiagem de 1963"},
        ],
    },
]


def agora_br():
    return datetime.now(FUSO_BR).replace(tzinfo=None)


def log(mensagem):
    print(f"[{agora_br():%Y-%m-%d %H:%M:%S}] {mensagem}", flush=True)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def _ana_query_string(params):
    return "&".join(f"{requests.utils.quote(k)}={requests.utils.quote(str(v))}"
                    for k, v in params.items())


def _ana_token_valido():
    try:
        cache = json.loads(ANA_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        if datetime.fromisoformat(cache["expira_em"]) > datetime.now():
            return cache["token"]
    except Exception:
        return None
    return None


def _ana_autenticar():
    identificador = os.environ.get("ANA_API_LOGIN")
    senha = os.environ.get("ANA_API_SENHA")
    if not identificador or not senha:
        return None
    token = _ana_token_valido()
    if token:
        return token
    resp = requests.get(
        f"{ANA_BASE}/EstacoesTelemetricas/OAUth/v1",
        headers={"Identificador": identificador, "Senha": senha},
        timeout=TIMEOUT_SEGUNDOS,
    )
    resp.raise_for_status()
    token = resp.json()["items"]["tokenautenticacao"]
    try:
        ANA_TOKEN_CACHE_PATH.write_text(
            json.dumps({"token": token,
                        "expira_em": (datetime.now() + timedelta(minutes=8)).isoformat()}),
            encoding="utf-8")
    except Exception:
        pass
    return token


def _parse_data_hora_ana(valor):
    return datetime.strptime(valor[:16], "%Y-%m-%d %H:%M")


def coletar_todas_via_ana(token):
    codigos = ",".join(str(c["codigo_ana"]) for c in CIDADES)
    query = _ana_query_string({
        "Codigos_Estacoes": codigos,
        "Tipo Filtro Data": "DATA_LEITURA",
        "Data de Busca (yyyy-MM-dd)": agora_br().strftime("%Y-%m-%d"),
        "Range Intervalo de busca": "DIAS_2",
    })
    resp = requests.get(
        f"{ANA_BASE}/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v2?{query}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SEGUNDOS,
    )
    resp.raise_for_status()
    payload = resp.json()
    itens = payload.get("items") or []
    if payload.get("code") != 200 or not itens:
        log(f"ANA v2 sem itens ({payload.get('message')})")
        return {}

    por_codigo = {}
    for item in itens:
        codigo = str(item.get("codigoestacao") or "")
        if codigo:
            por_codigo.setdefault(codigo, []).append(item)
    log(f"ANA v2: {len(itens)} registros em 1 chamada, {len(por_codigo)} estacoes")
    return por_codigo


def montar_historico(cidade, itens):
    if not itens:
        return None
    zero = cidade.get("zero_regua_m")
    itens = sorted(itens, key=lambda item: item["Data_Hora_Medicao"])
    historico = []
    for item in itens:
        cota = item.get("Cota_Adotada")
        if cota is None:
            continue
        regua_m = round(float(cota) / 100, 3)
        linha = {
            "data_hora": iso(_parse_data_hora_ana(item["Data_Hora_Medicao"])),
            "regua_m": regua_m,
            "vazao_m3s": (int(float(item["Vazao_Adotada"]))
                          if item.get("Vazao_Adotada") not in (None, "") else None),
            "chuva_mm": round(float(item.get("Chuva_Adotada", 0) or 0), 1),
            "chuva_acumulada_mm": 0.0,
        }
        if zero is not None:
            linha["nivel_agua_m"] = round(regua_m + zero, 3)
        historico.append(linha)
    if not historico:
        return None
    limite = agora_br() - timedelta(hours=JANELA_HISTORICO_HORAS)
    historico = [x for x in historico if datetime.fromisoformat(x["data_hora"]) >= limite]
    log(f"{cidade['slug']}: {len(historico)} medicoes")
    return historico or None


CHUVA_7D_CABECALHOS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def buscar_chuva_7dias(cidade, tentativas=3):
    motivo = None
    for n in range(tentativas):
        if n:
            time.sleep(5 + (n - 1) * 10)
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": cidade["lat"],
                    "longitude": cidade["lon"],
                    "daily": "precipitation_sum,precipitation_probability_max,weather_code",
                    "timezone": "America/Sao_Paulo",
                    "forecast_days": 7,
                    "models": "ecmwf_ifs",
                },
                headers=CHUVA_7D_CABECALHOS,
                timeout=max(TIMEOUT_SEGUNDOS, 30),
            )
            resp.raise_for_status()
            d = resp.json().get("daily") or {}
            datas = d.get("time") or []
            semana = []
            for i, dia in enumerate(datas[:7]):
                semana.append({
                    "data": dia,
                    "chuva_mm": round(float((d.get("precipitation_sum") or [0])[i] or 0), 1),
                    "probabilidade_pct": int((d.get("precipitation_probability_max") or [0])[i] or 0),
                    "codigo_tempo": int((d.get("weather_code") or [0])[i] or 0),
                })
            if semana:
                return semana, None
            motivo = "resposta sem dias"
        except Exception as exc:
            motivo = type(exc).__name__
            codigo = getattr(getattr(exc, "response", None), "status_code", None)
            if codigo:
                motivo = f"{motivo} {codigo}"
        log(f"{cidade['slug']}: chuva 7 dias falhou na tentativa {n + 1} ({motivo})")
    return [], motivo


def aproveitar_chuva_anterior(anterior):
    semana = ((anterior or {}).get("dados") or {}).get("chuva_7dias") or []
    hoje = agora_br().date().isoformat()
    restante = [d for d in semana if str(d.get("data") or "") >= hoje]
    return restante if len(restante) >= 3 else []


def calcular_tendencia(historico):
    if len(historico) < 4:
        return {"texto": "Sem dados suficientes", "delta": 0.0, "direcao": "estavel"}
    recentes = historico[-4:]
    delta = (recentes[-1]["regua_m"] - recentes[0]["regua_m"]) / (len(recentes) - 1)
    cm = abs(delta) * 100
    if cm < 0.5:
        return {"texto": "Estavel", "delta": delta, "direcao": "estavel"}
    direcao = "subindo" if delta > 0 else "baixando"
    verbo = "Subindo" if delta > 0 else "Baixando"
    return {"texto": f"{verbo} cerca de {cm:.1f} cm por hora".replace(".", ","),
            "delta": delta, "direcao": direcao}


def montar_payload(cidade, historico, semana, motivo_chuva=None):
    ultima = historico[-1] if historico else None
    return {
        "slug": cidade["slug"],
        "nome": cidade["nome"],
        "uf": cidade["uf"],
        "codigo_estacao": cidade["codigo_ana"],
        "zero_regua_m": cidade.get("zero_regua_m"),
        "fonte": FONTE_ANA,
        "fonte_alertas": FONTE_ALERTAS if cidade.get("alertas") else None,
        "url_historico": "https://www.snirh.gov.br/hidrotelemetria/",
        "atualizado_em": iso(agora_br()),
        "historico": historico,
        "ultima": ultima,
        "tendencia": calcular_tendencia(historico),
        "chuva_7dias": semana,
        "chuva_7dias_diagnostico": motivo_chuva,
        "cotas_bairros": cidade.get("enchentes") or [],
        "cotas_alerta": cidade.get("alertas") or [],
        "previsao": [],
        "previsao_disponivel": False,
        "situacao": None,
        "alerta_previsao": None,
        "janela_historico_horas": JANELA_HISTORICO_HORAS,
        "atraso_aviso_min": ATRASO_AVISO_MIN,
        "tem_enchentes_historicas": bool(cidade.get("enchentes")),
        "tem_bairros": False,
        "tem_alertas": bool(cidade.get("alertas")),
        "tem_previsao": False,
    }


def ler_anterior(caminho):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return None


def mesclar_historico(novo, anterior):
    por_hora = {x["data_hora"]: x for x in (anterior or [])}
    por_hora.update({x["data_hora"]: x for x in novo})
    limite = agora_br() - timedelta(hours=JANELA_HISTORICO_HORAS)
    itens = [x for x in por_hora.values()
             if datetime.fromisoformat(x["data_hora"]) >= limite]
    itens.sort(key=lambda x: x["data_hora"])
    acumulada = 0.0
    for x in itens:
        acumulada = round(acumulada + (x.get("chuva_mm") or 0.0), 1)
        x["chuva_acumulada_mm"] = acumulada
    return itens


def precisa_atualizar(anterior):
    if not anterior:
        return True
    try:
        quando = datetime.fromisoformat((anterior.get("dados") or {}).get("atualizado_em"))
    except Exception:
        return True
    return (agora_br() - quando) >= timedelta(minutes=INTERVALO_MINIMO_MINUTOS)


def main():
    CIDADES_DIR.mkdir(parents=True, exist_ok=True)
    indice = []
    algum_pendente = any(
        precisa_atualizar(ler_anterior(CIDADES_DIR / f"{c['slug']}.json")) for c in CIDADES)
    por_codigo = {}
    if algum_pendente:
        try:
            token = _ana_autenticar()
        except Exception as exc:
            log(f"ANA: autenticacao falhou ({type(exc).__name__})")
            token = None
        if token:
            try:
                por_codigo = coletar_todas_via_ana(token)
            except Exception as exc:
                log(f"ANA v2 falhou ({type(exc).__name__})")
                por_codigo = {}
        else:
            log("ANA: sem token")
    for i, cidade in enumerate(CIDADES):
        caminho = CIDADES_DIR / f"{cidade['slug']}.json"
        anterior = ler_anterior(caminho)
        indice.append({"slug": cidade["slug"], "nome": cidade["nome"], "uf": cidade["uf"],
                       "lat": cidade["lat"], "lon": cidade["lon"],
                       "codigo_estacao": cidade["codigo_ana"],
                       "arquivo": f"cidades/{cidade['slug']}.json"})

        if not precisa_atualizar(anterior):
            log(f"{cidade['slug']}: recente, mantido")
            continue

        if not por_codigo:
            log(f"{cidade['slug']}: sem dado da ANA, mantido o anterior")
            continue
        historico = montar_historico(cidade, por_codigo.get(str(cidade["codigo_ana"])))

        if not historico:
            log(f"{cidade['slug']}: mantido o anterior")
            continue

        anterior_hist = ((anterior or {}).get("dados") or {}).get("historico") or []
        historico = mesclar_historico(historico, anterior_hist)
        semana, motivo_chuva = buscar_chuva_7dias(cidade)
        if not semana:
            semana = aproveitar_chuva_anterior(anterior)
            if semana:
                motivo_chuva = f"{motivo_chuva or 'sem resposta'}; mantida a ultima previsao valida"
        payload = {"ok": True, "erro": None,
                   "dados": montar_payload(cidade, historico, semana, motivo_chuva)}
        caminho.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"{cidade['slug']}: gravado ({payload['dados']['ultima']['data_hora']})")

    INDICE_PATH.write_text(
        json.dumps({"cidades": indice, "atualizado_em": iso(agora_br())},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    log(f"indice com {len(indice)} cidades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
