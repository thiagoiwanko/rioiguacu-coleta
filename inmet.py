"""Avisos meteorologicos oficiais do INMET, filtrados por municipio (codigo IBGE).

Fonte: API publica do INMET (sem chave). Uma unica consulta por execucao; cada
cidade recebe so os avisos que citam o seu codigo IBGE. O texto dos riscos e das
instrucoes e repassado como esta, sem reescrever. Se a consulta falhar, o bloco
anterior e mantido com a marca de erro, para o site nunca inventar nem apagar
um aviso por instabilidade da API.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

URL_API = "https://apiprevmet3.inmet.gov.br/avisos/ativos"
URL_PUBLICA = "https://portal.inmet.gov.br/"
FONTE = "INMET - Instituto Nacional de Meteorologia"
CABECALHOS = {"User-Agent": "Mozilla/5.0 (rioiguacu.com; avisos oficiais)", "Accept": "application/json"}
FUSO_BR = ZoneInfo("America/Sao_Paulo")

NIVEL_POR_COR = {"#FFFE00": "amarelo", "#F96602": "laranja", "#F80703": "vermelho"}
NIVEL_POR_SEVERIDADE = {"Perigo Potencial": "amarelo", "Perigo": "laranja", "Grande Perigo": "vermelho"}

_cache = None


def _agora():
    return datetime.now(FUSO_BR).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")


def _iso(texto):
    """'2026-09-21 09:16' -> '2026-09-21T09:16'."""
    if not texto:
        return None
    return str(texto).strip().replace(" ", "T")[:16]


def _codigos(aviso):
    return {c.strip() for c in str(aviso.get("geocodes") or "").split(",") if c.strip()}


def _normalizar(aviso):
    cor = str(aviso.get("aviso_cor") or "").upper()
    nivel = NIVEL_POR_COR.get(cor) or NIVEL_POR_SEVERIDADE.get(aviso.get("severidade") or "")
    riscos = aviso.get("riscos") or []
    instrucoes = aviso.get("instrucoes") or []
    return {
        "id": aviso.get("id"),
        "nivel": nivel or "amarelo",
        "grau": aviso.get("severidade"),
        "tipo": aviso.get("descricao"),
        "inicio": _iso(aviso.get("inicio")),
        "fim": _iso(aviso.get("fim")),
        "riscos": " ".join(r.strip() for r in riscos) if isinstance(riscos, list) else str(riscos).strip(),
        "instrucoes": [i.strip() for i in instrucoes if str(i).strip()] if isinstance(instrucoes, list) else [str(instrucoes).strip()],
    }


def buscar_avisos_ativos():
    """Uma consulta por execucao; devolve a lista bruta ou levanta excecao."""
    global _cache
    if _cache is None:
        resp = requests.get(URL_API, headers=CABECALHOS, timeout=20)
        resp.raise_for_status()
        dados = resp.json()
        if isinstance(dados, dict):
            dados = (dados.get("hoje") or []) + (dados.get("futuro") or [])
        _cache = dados
    return _cache


def bloco_para(codigos_ibge, anterior=None):
    """Bloco `avisos_inmet` de uma cidade, ou None se a API do INMET falhar
    (`anterior` e ignorado: sem INMET, sem aviso)."""
    alvo = {str(c) for c in codigos_ibge}
    try:
        brutos = buscar_avisos_ativos()
    except Exception as exc:
        # Decisao do dono (21/09/2026): sem INMET, sem aviso. O bloco some do JSON e o site
        # nao mostra nada - nem aviso antigo, nem mensagem de erro. Fica so no log da rodada.
        print(f"[inmet] consulta falhou ({type(exc).__name__}); bloco de avisos omitido nesta rodada", flush=True)
        return None
    ativos = [_normalizar(a) for a in brutos if _codigos(a) & alvo and not a.get("encerrado")]
    ordem = {"vermelho": 3, "laranja": 2, "amarelo": 1}
    ativos.sort(key=lambda a: (-ordem.get(a["nivel"], 0), a["fim"] or ""))
    return {"consultado_em": _agora(), "fonte": FONTE, "url": URL_PUBLICA, "erro": None, "ativos": ativos}
