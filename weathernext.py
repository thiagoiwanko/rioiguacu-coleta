import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJETO = "rioiguacu-mapa"
ASSET = "projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg"
SAIDA = Path(__file__).resolve().parent / "public" / "weathernext.json"
SAIDA_SINOTICA = Path(__file__).resolve().parent / "public" / "weathernext-sinotica.json"
FUSO = timezone(timedelta(hours=-3))
PONTOS = (
    ("65035001", "Porto Amazonas", "porto-amazonas", -25.5481, -49.8894),
    ("65060001", "São Mateus do Sul", "sao-mateus-do-sul", -25.8756, -50.3894),
    ("65100001", "Rio Negro", "rio-negro", -26.1097, -49.8011),
    ("65155001", "São Bento", "sao-bento", -25.9439, -49.7961),
    ("65175001", "Divisa", "divisa", -26.0914, -50.3339),
    ("65220001", "Fluviópolis", "fluviopolis", -26.0192, -50.5925),
    ("65310001", "União da Vitória", "uniao-da-vitoria", -26.2281, -51.0803),
    ("65365801", "Porto Vitória", "porto-vitoria", -26.1653, -51.2281),
    ("65774400", "Barramento Foz do Areia", "barramento-foz-do-areia", -26.0094, -51.6647),
)
VERSOES = (("nativa", "total_precipitation_1hr"), ("imerg", "imerg_tp_1hr"), ("experimental", "experimental_tp_1hr"))
ESTATISTICAS = ("mean", "p10", "p25", "p50", "p75", "p90")
BANDAS = tuple(f"{banda}_{e}" for _, banda in VERSOES for e in ESTATISTICAS)
CAMPOS = tuple(f"{nome}_{e}" for nome, _ in VERSOES for e in ESTATISTICAS)
HORAS_HORARIA = 48
HORAS_SINOTICA = 360
LIMITE_EXTRACOES_DIA = int(os.environ.get("WEATHERNEXT_LIMITE_DIA", "32"))
AUSENTE = -9999.0


def log(texto):
    print(f"[weathernext] {texto}", flush=True)


def mascarar(valor):
    if valor:
        print(f"::add-mask::{valor}", flush=True)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ler_data(texto):
    return datetime.fromisoformat(str(texto).replace("Z", "+00:00")).astimezone(timezone.utc)


def iniciar():
    import ee
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    bruto = os.environ.get("WEATHERNEXT_CREDENCIAL", "")
    if not bruto:
        raise RuntimeError("credencial_ausente")
    dados = json.loads(bruto)
    if isinstance(dados.get("fields"), list):
        dados = {f.get("name"): f.get("value") for f in dados["fields"]}
    for chave in ("refresh_token", "client_secret", "client_id"):
        mascarar(dados.get(chave))
    cred = Credentials.from_authorized_user_info(dados, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    cred.refresh(Request())
    mascarar(cred.token)
    ee.Initialize(credentials=cred, project=PROJETO)
    ee.data.setMaxRetries(0)
    ee.data.setDeadline(90000)
    return ee


def rodadas(ee, agora):
    col = ee.ImageCollection(ASSET).filter(ee.Filter.gte("start_time", iso(agora - timedelta(hours=36))))
    inicios = col.aggregate_array("start_time").getInfo() or []
    horas = col.aggregate_array("forecast_hour").getInfo() or []
    total = {}
    for i, h in zip(inicios, horas):
        total[ler_data(i)] = max(total.get(ler_data(i), 0), int(h))
    horaria = max((i for i, h in total.items() if h >= HORAS_HORARIA), default=None)
    sinotica = max((i for i, h in total.items() if i.hour in (0, 6, 12, 18) and h >= HORAS_SINOTICA), default=None)
    return horaria, sinotica


def extrair(ee, inicio, horas):
    pontos = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([lon, lat]), {"codigo": cod}) for cod, _, _, lat, lon in PONTOS])
    col = (ee.ImageCollection(ASSET)
           .filter(ee.Filter.eq("start_time", iso(inicio)))
           .filter(ee.Filter.gte("forecast_hour", 1))
           .filter(ee.Filter.lte("forecast_hour", horas))
           .select(list(BANDAS)))

    def amostrar(imagem):
        imagem = ee.Image(imagem)
        amostra = imagem.unmask(AUSENTE, False).sampleRegions(collection=pontos, properties=["codigo"], scale=10000, geometries=False)
        return ee.Feature(None, {"fh": imagem.get("forecast_hour"),
                                 "pontos": amostra.toList(len(PONTOS)).map(lambda f: ee.Feature(f).toDictionary())})

    resultado = ee.data.computeFeatures({"expression": col.map(amostrar), "pageSize": 1000})
    if not isinstance(resultado, dict) or resultado.get("nextPageToken"):
        raise RuntimeError("extracao_paginada")
    series = {cod: [] for cod, _, _, _, _ in PONTOS}
    for item in resultado.get("features") or []:
        p = item.get("properties") or {}
        valido = iso(inicio + timedelta(hours=int(p["fh"])))
        for ponto in p.get("pontos") or []:
            linha = {"valido_utc": valido}
            for campo, banda in zip(CAMPOS, BANDAS):
                v = ponto.get(banda)
                linha[campo] = None if v is None or float(v) <= AUSENTE / 2 else round(float(v) * 1000.0, 2)
            series[ponto["codigo"]].append(linha)
    for cod in series:
        series[cod].sort(key=lambda x: x["valido_utc"])
        if len(series[cod]) != horas:
            raise RuntimeError(f"serie_incompleta_{cod}_{len(series[cod])}")
    return series


def resumo(estado, agora):
    hoje = agora.astimezone(FUSO).date()
    fim_hoje = datetime(hoje.year, hoje.month, hoje.day, tzinfo=FUSO) + timedelta(days=1)
    horaria = estado.get("rodada_horaria") or {}
    sinotica = estado.get("rodada_sinotica") or {}
    saida = {}
    for cod, nome, slug, _, _ in PONTOS:
        por_hora = {}
        for linha in (sinotica.get("pontos") or {}).get(cod, []):
            por_hora[linha["valido_utc"]] = linha.get("experimental_mean")
        for linha in (horaria.get("pontos") or {}).get(cod, []):
            por_hora[linha["valido_utc"]] = linha.get("experimental_mean")
        dias = {}
        restante = 0.0
        for valido, mm in por_hora.items():
            if mm is None:
                continue
            t = ler_data(valido)
            if t <= agora:
                continue
            dia = (t.astimezone(FUSO) - timedelta(seconds=1)).date()
            dias.setdefault(dia, [0.0, 0])
            dias[dia][0] += mm
            dias[dia][1] += 1
            if t <= fim_hoje:
                restante += mm
        semana = []
        for dia in sorted(dias):
            soma, n = dias[dia]
            if dia == hoje:
                semana.append({"data": dia.isoformat(), "restante_mm": round(restante, 1)})
            elif n == 24:
                semana.append({"data": dia.isoformat(), "chuva_mm": round(soma, 1)})
        saida[slug] = {"codigo": cod, "nome": nome, "dias": semana[:7]}
    return saida


def ler(caminho):
    if caminho.exists():
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def carregar():
    estado = ler(SAIDA)
    sinotica = ler(SAIDA_SINOTICA).get("rodada_sinotica")
    if sinotica:
        estado["rodada_sinotica"] = sinotica
    return estado


def disponivel(estado, agora):
    inicio = (estado.get("rodada_horaria") or {}).get("inicio_utc")
    return bool(inicio) and agora - ler_data(inicio) <= timedelta(hours=20)


def semana_para(slug, historico, agora=None):
    agora = agora or datetime.now(timezone.utc)
    estado = carregar()
    if not disponivel(estado, agora):
        return None
    cidade = resumo(estado, agora).get(slug)
    if not cidade or len(cidade["dias"]) < 7:
        return None
    hoje = agora.astimezone(FUSO).date().isoformat()
    caiu = round(sum(float(x.get("chuva_mm") or 0) for x in (historico or [])
                     if str(x.get("data_hora", "")).startswith(hoje)), 1)
    semana = []
    for dia in cidade["dias"]:
        if dia["data"] == hoje:
            restante = dia["restante_mm"]
            semana.append({"data": hoje, "chuva_mm": round(caiu + restante, 1), "chuva_caida_mm": caiu,
                           "chuva_restante_mm": restante, "fonte": "WeatherNext 3"})
        else:
            semana.append({"data": dia["data"], "chuva_mm": dia["chuva_mm"], "fonte": "WeatherNext 3"})
    return semana[:7]


def _media_checada(valores):
    v = sorted(x for x in valores if x is not None and x >= 0)
    if len(v) >= 3 and v[-2] > 1 and v[-1] > 3 * v[-2]:
        v = v[:-1]
    return sum(v) / len(v) if v else None


def bacia_3dias(agora=None):
    import urllib.request
    agora = agora or datetime.now(timezone.utc)
    estado = carregar()
    if not disponivel(estado, agora):
        return None
    cidades = resumo(estado, agora)
    montante = [p for p in PONTOS if p[0] in ("65035001", "65060001", "65100001", "65155001", "65175001", "65220001", "65310001")]
    hoje = agora.astimezone(FUSO).date()
    alvos = [(hoje + timedelta(days=n)).isoformat() for n in (1, 2, 3)]
    modelos = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless", "jma_seamless",
               "meteofrance_seamless", "ukmo_seamless", "cma_grapes_global")
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(str(p[3]) for p in montante)
           + "&longitude=" + ",".join(str(p[4]) for p in montante)
           + "&daily=precipitation_sum&timezone=America%2FSao_Paulo&forecast_days=5&models=" + ",".join(modelos))
    with urllib.request.urlopen(url, timeout=45) as resp:
        locais = json.loads(resp.read().decode("utf-8"))
    if isinstance(locais, dict):
        locais = [locais]
    total = 0.0
    for p, local in zip(montante, locais):
        diario = local.get("daily") or {}
        datas = diario.get("time") or []
        wn = {d["data"]: d.get("chuva_mm") for d in cidades[p[2]]["dias"]}
        for alvo in alvos:
            if alvo not in datas or wn.get(alvo) is None:
                return None
            i = datas.index(alvo)
            om = _media_checada([(diario.get("precipitation_sum_" + m) or [None] * (i + 1))[i] for m in modelos])
            if om is None:
                return None
            total += (om + wn[alvo]) / 2 / len(montante)
    return {"mm": round(total, 1), "de": alvos[0], "ate": alvos[-1], "fonte": "Open-Meteo + WeatherNext 3"}


def main():
    agora = datetime.now(timezone.utc)
    estado = ler(SAIDA)
    sinotica = ler(SAIDA_SINOTICA).get("rodada_sinotica")
    if sinotica:
        estado["rodada_sinotica"] = sinotica
    contagem = estado.get("extracoes") or {}
    if contagem.get("dia") != agora.date().isoformat():
        contagem = {"dia": agora.date().isoformat(), "n": 0}
    try:
        ee = iniciar()
        horaria, sinotica = rodadas(ee, agora)
        for chave, inicio, horas in (("rodada_horaria", horaria, HORAS_HORARIA), ("rodada_sinotica", sinotica, HORAS_SINOTICA)):
            if inicio is None:
                log(f"{chave}: nenhuma disponível")
                continue
            if (estado.get(chave) or {}).get("inicio_utc") == iso(inicio):
                continue
            if contagem["n"] >= LIMITE_EXTRACOES_DIA:
                log(f"{chave}: limite diário de extrações atingido")
                continue
            contagem["n"] += 1
            estado[chave] = {"inicio_utc": iso(inicio), "horas": horas, "extraido_utc": iso(agora), "pontos": extrair(ee, inicio, horas)}
            log(f"{chave}: {iso(inicio)} extraída")
    except Exception as erro:
        log(f"falha: {type(erro).__name__}")
        estado["falha_utc"] = iso(agora)
    estado["extracoes"] = contagem
    estado["fonte"] = "WeatherNext 3"
    estado["atualizado_utc"] = iso(agora)
    estado["cidades"] = resumo(estado, agora)
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    sinotica = estado.pop("rodada_sinotica", None)
    if sinotica:
        SAIDA_SINOTICA.write_text(json.dumps({"fonte": "WeatherNext 3", "rodada_sinotica": sinotica}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    SAIDA.write_text(json.dumps(estado, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
    sys.exit(0)
