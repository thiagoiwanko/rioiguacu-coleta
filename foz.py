import json
import os
import re
import html as htmlmod
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import inmet

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
SAIDA = PUBLIC_DIR / "foz-do-iguacu.json"
ANA_BASE = "https://www.ana.gov.br/hidrowebservice"
TOKEN_CACHE = BASE_DIR / ".ana_token_cache.json"
FUSO_BR = ZoneInfo("America/Sao_Paulo")
CODIGO = "65992500"
URBIA_API = "https://cataratasdoiguacu.com.br/wp-json/wp/v2/posts?search=passarela&per_page=20&_fields=id,date,link,title,excerpt"
URBIA_SITE = "https://cataratasdoiguacu.com.br/"
FONTE_ANA = "ANA - Agencia Nacional de Aguas e Saneamento Basico, estacao telemetrica 65992500 (Hotel Cataratas), operada pela Itaipu"
FONTE_URBIA = "Urbia+Cataratas, concessionaria do Parque Nacional do Iguacu, com o ICMBio"
INTERVALO_MIN = int(os.environ.get("FOZ_INTERVALO_MIN", "55"))
TIMEOUT = 30
JANELA_H = 48
UA = {"User-Agent": "rioiguacu.com (monitoramento hidrologico; contato no site)"}


def agora_br():
    return datetime.now(FUSO_BR).replace(second=0, microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def log(m):
    print("[foz]", m, flush=True)


def _qs(params):
    from urllib.parse import quote
    return "&".join(quote(k, safe="") + "=" + quote(str(v), safe="") for k, v in params.items())


def _token():
    try:
        d = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        if datetime.fromisoformat(d["expira_em"]) > datetime.now():
            return d["token"]
    except Exception:
        pass
    login, senha = os.environ.get("ANA_API_LOGIN"), os.environ.get("ANA_API_SENHA")
    if not login or not senha:
        return None
    r = requests.get(ANA_BASE + "/EstacoesTelemetricas/OAUth/v1",
                     headers={"Identificador": login, "Senha": senha}, timeout=TIMEOUT)
    r.raise_for_status()
    token = r.json()["items"]["tokenautenticacao"]
    TOKEN_CACHE.write_text(json.dumps({"token": token, "expira_em": (datetime.now() + timedelta(minutes=55)).isoformat()}),
                           encoding="utf-8")
    return token


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def coletar_ana(token):
    q = _qs({"Código da Estação": CODIGO, "Tipo Filtro Data": "DATA_LEITURA",
             "Data de Busca (yyyy-MM-dd)": agora_br().strftime("%Y-%m-%d"), "Range Intervalo de busca": "DIAS_2"})
    r = requests.get(ANA_BASE + "/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v1?" + q,
                     headers={"Authorization": "Bearer " + token}, timeout=TIMEOUT)
    r.raise_for_status()
    itens = (r.json() or {}).get("items") or []
    saida = []
    for i in itens:
        try:
            dh = datetime.strptime(str(i.get("Data_Hora_Medicao")).split(".")[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        cota, vaz = _num(i.get("Cota_Adotada")), _num(i.get("Vazao_Adotada"))
        if vaz is None and cota is None:
            continue
        saida.append({"data_hora": iso(dh), "vazao_m3s": round(vaz, 1) if vaz is not None else None,
                      "nivel_m": round(cota / 100.0, 2) if cota is not None else None})
    saida.sort(key=lambda x: x["data_hora"])
    return saida


def classificar(titulo):
    t = titulo.lower()
    if re.search(r"reabr|reabert|liberad|permanece aberta|segue aberta|esta aberta|está aberta", t):
        return "aberta"
    if re.search(r"interdit|fechad|fecha ", t):
        return "fechada"
    return None


def coletar_passarela():
    r = requests.get(URBIA_API, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    posts = r.json()
    eventos = []
    for p in posts:
        titulo = htmlmod.unescape(re.sub("<[^>]+>", "", p["title"]["rendered"])).strip()
        estado = classificar(titulo)
        if not estado:
            continue
        resumo = htmlmod.unescape(re.sub("<[^>]+>", "", (p.get("excerpt") or {}).get("rendered") or "")).strip()
        m = re.search(r"(\d+[.,]?\d*)\s*milh(?:ão|ões|oes|ao)", resumo.lower())
        vazao = None
        if m:
            vazao = round(float(m.group(1).replace(",", ".")) * 1000)
        eventos.append({"data": p["date"][:16], "estado": estado, "titulo": titulo, "url": p["link"],
                        "vazao_citada_m3s": vazao})
    eventos.sort(key=lambda e: e["data"], reverse=True)
    return eventos


def situacao_passarela(eventos, ultima_vazao):
    atual = eventos[0] if eventos else None
    limites = {"fecha_acima_m3s": 8500, "reabre_entre_m3s": [7500, 8000], "avaliacao_a_partir_m3s": 8000}
    if atual:
        return {"estado": atual["estado"], "desde": atual["data"], "titulo": atual["titulo"], "url": atual["url"],
                "vazao_citada_m3s": atual["vazao_citada_m3s"], "fonte": FONTE_URBIA, "site_oficial": URBIA_SITE,
                "limites": limites, "eventos": eventos[:12]}
    return {"estado": None, "desde": None, "titulo": None, "url": URBIA_SITE, "vazao_citada_m3s": None,
            "fonte": FONTE_URBIA, "site_oficial": URBIA_SITE, "limites": limites, "eventos": []}


def anterior():
    try:
        return json.loads(SAIDA.read_text(encoding="utf-8"))
    except Exception:
        return None


def precisa(ant):
    try:
        q = datetime.fromisoformat(ant["dados"]["atualizado_em"]).replace(tzinfo=FUSO_BR)
    except Exception:
        return True
    return (agora_br() - q) >= timedelta(minutes=INTERVALO_MIN)


def mesclar(novo, velho):
    por = {x["data_hora"]: x for x in (velho or [])}
    por.update({x["data_hora"]: x for x in novo})
    lim = agora_br().replace(tzinfo=None) - timedelta(hours=JANELA_H)
    itens = [x for x in por.values() if datetime.fromisoformat(x["data_hora"]) >= lim]
    itens.sort(key=lambda x: x["data_hora"])
    return itens


def main():
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ant = anterior()
    if ant and not precisa(ant):
        log("recente, mantido")
        return
    dados_ant = (ant or {}).get("dados") or {}
    historico = dados_ant.get("historico") or []
    try:
        token = _token()
        novo = coletar_ana(token) if token else []
        if novo:
            historico = mesclar(novo, historico)
            log(f"ANA: {len(novo)} leituras, ultima {historico[-1]['data_hora']}")
        else:
            log("ANA sem itens, mantido o historico anterior")
    except Exception as exc:
        log(f"ANA falhou ({type(exc).__name__}), mantido o historico anterior")
    try:
        eventos = coletar_passarela()
        log(f"Urbia: {len(eventos)} eventos, ultimo {eventos[0]['data'] if eventos else '-'} {eventos[0]['estado'] if eventos else ''}")
    except Exception as exc:
        log(f"Urbia falhou ({type(exc).__name__}), mantida a situacao anterior")
        eventos = (dados_ant.get("passarela") or {}).get("eventos") or []
    ultima = historico[-1] if historico else None
    payload = {"ok": True, "erro": None, "dados": {
        "slug": "foz-do-iguacu", "nome": "Foz do Iguaçu", "uf": "PR", "codigo_estacao": CODIGO,
        "fonte": FONTE_ANA, "url_fonte": "https://www.snirh.gov.br/hidrotelemetria/",
        "atualizado_em": iso(agora_br()), "historico": historico, "ultima": ultima,
        "passarela": situacao_passarela(eventos, ultima and ultima.get("vazao_m3s")),
        "referencias": [
            {"vazao_m3s": 28395, "descricao": "Maior vazão registrada — 11/07/1983", "fonte": "ANA/HidroWeb, estação 65993000 Salto Cataratas"},
            {"vazao_m3s": 8500, "descricao": "Passarela das Cataratas interditada", "fonte": FONTE_URBIA},
            {"vazao_m3s": 1477, "descricao": "Vazão mediana histórica (1982–2019)", "fonte": "ANA/HidroWeb, estação 65993000 Salto Cataratas"},
        ],
        "historico_serie": {"estacao": "65993000 Salto Cataratas", "periodo": "1942–2020 (nível), 1982–2019 (vazão)",
                            "dias_acima_8500": 187, "dias_total": 13540, "fonte": "ANA/HidroWeb, séries históricas"},
        "janela_historico_horas": JANELA_H,
        "avisos_inmet": inmet.bloco_para(("4108304",), dados_ant.get("avisos_inmet")),
    }}
    SAIDA.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("gravado")


if __name__ == "__main__":
    main()
