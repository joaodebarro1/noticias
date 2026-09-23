"""
Robô de notícias em tempo real — estilo "terminal"
--------------------------------------------------
- Monitora fontes RSS + Google News por tema
- Filtra por palavras-chave com pontuação de relevância
- Remove duplicadas (SQLite)
- Envia alertas no Telegram e/ou WhatsApp e imprime no terminal

Credenciais: NUNCA escreva no código. São lidas só de variáveis de ambiente
(no GitHub: Settings -> Secrets and variables -> Actions):
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID    -> bot criado no @BotFather
    WHATSAPP_FONE, WHATSAPP_APIKEY      -> https://www.callmebot.com/blog/free-api-whatsapp-messages/

Rodar:
    python robo_noticias.py            # loop contínuo no terminal
    python robo_noticias.py --uma-vez  # um ciclo e sai (GitHub Actions / agendador)
    python robo_noticias.py --teste    # manda um alerta de teste e mostra a resposta de cada canal
"""

import os
import sys
import time
import sqlite3
import hashlib
import urllib.parse
from datetime import datetime

import feedparser
import requests

# =========================================================
# 1. CONFIGURAÇÃO — edite aqui
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHATSAPP_FONE = os.getenv("WHATSAPP_FONE", "")
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")

INTERVALO_SEGUNDOS = 60      # de quanto em quanto tempo varre as fontes
PONTUACAO_MINIMA = 2         # nota mínima para gerar alerta

# Cada tema: termos de busca (Google News) + palavras-chave com peso
TEMAS = {
    "JUROS / BC": {
        "buscas": ["Copom Selic", "Banco Central juros", "Federal Reserve rates"],
        "palavras": {"selic": 3, "copom": 3, "fed": 2, "juros": 2, "inflação": 2, "ipca": 2},
    },
    "PETRÓLEO": {
        "buscas": ["petróleo Brent", "Petrobras", "OPEP"],
        "palavras": {"brent": 3, "petrobras": 3, "opep": 3, "opec": 3, "petróleo": 2},
    },
    "CÂMBIO": {
        "buscas": ["dólar real câmbio"],
        "palavras": {"dólar": 2, "câmbio": 2, "real": 1},
    },
}

# Fontes RSS diretas (lidas em todo ciclo e filtradas pelas palavras dos temas)
FEEDS_DIRETOS = [
    "https://www.infomoney.com.br/feed/",
    "https://g1.globo.com/rss/g1/economia/",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]

# Palavras que sempre sobem a urgência
MODO_TESTE = "--teste" in sys.argv

URGENTES = {"urgente": 3, "breaking": 3, "exclusivo": 2, "surpreende": 2, "inesperad": 2}

# =========================================================
# 2. BANCO DE DADOS (evita alertas repetidos)
# =========================================================

ARQUIVO_DB = os.getenv("NOTICIAS_DB", "noticias.db")
banco_novo = not os.path.exists(ARQUIVO_DB)
db = sqlite3.connect(ARQUIVO_DB)
db.execute("""CREATE TABLE IF NOT EXISTS vistas (
    id TEXT PRIMARY KEY, tema TEXT, titulo TEXT, link TEXT, nota INTEGER, visto_em TEXT)""")
db.commit()


def ja_vista(chave: str) -> bool:
    return db.execute("SELECT 1 FROM vistas WHERE id=?", (chave,)).fetchone() is not None


def registrar(chave, tema, titulo, link, nota):
    db.execute("INSERT OR IGNORE INTO vistas VALUES (?,?,?,?,?,?)",
               (chave, tema, titulo, link, nota, datetime.now().isoformat()))
    db.commit()


def chave_noticia(titulo: str) -> str:
    # normaliza o título para pegar a mesma notícia vinda de fontes diferentes
    base = "".join(c for c in titulo.lower() if c.isalnum())[:80]
    return hashlib.md5(base.encode()).hexdigest()

# =========================================================
# 3. COLETA
# =========================================================


def url_google_news(termo: str) -> str:
    q = urllib.parse.quote(f"{termo} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def ler_feed(url: str):
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        for e in feed.entries[:30]:
            yield {
                "titulo": e.get("title", "").strip(),
                "link": e.get("link", ""),
                "resumo": e.get("summary", "")[:500],
                "fonte": feed.feed.get("title", url),
            }
    except Exception as err:
        print(f"[erro] {url}: {err}")

# =========================================================
# 4. PONTUAÇÃO
# =========================================================


def pontuar(texto: str, palavras: dict) -> int:
    t = texto.lower()
    nota = sum(peso for p, peso in palavras.items() if p in t)
    nota += sum(peso for p, peso in URGENTES.items() if p in t)
    return nota

# =========================================================
# 5. ALERTA
# =========================================================


def alertar(tema, noticia, nota):
    marcador = "🔴" if nota >= 5 else "🟡" if nota >= 3 else "⚪"
    hora = datetime.now().strftime("%H:%M:%S")
    print(f"{hora} {marcador} [{tema}] {noticia['titulo']}  ({noticia['fonte']})")

    msg = f"{marcador} *{tema}*\n{noticia['titulo']}\n_{noticia['fonte']}_\n{noticia['link']}"

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.ok:  # ex.: título com "_" ou "*" quebra o Markdown -> reenvia sem formatação
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": msg.replace("*", "").replace("_", ""),
                          "disable_web_page_preview": True},
                    timeout=10,
                )
            if not r.ok or MODO_TESTE:
                print(f"[telegram] {r.status_code} {r.text[:300]}")
        except Exception as err:
            print(f"[erro telegram] {err}")

    if WHATSAPP_FONE and WHATSAPP_APIKEY:
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": WHATSAPP_FONE, "text": msg, "apikey": WHATSAPP_APIKEY},
                timeout=20,
            )
            if not r.ok or MODO_TESTE:
                print(f"[whatsapp] {r.status_code} {r.text[:300]}")
        except Exception as err:
            print(f"[erro whatsapp] {err}")

# =========================================================
# 6. CICLO PRINCIPAL
# =========================================================


def processar(noticia, tema, palavras):
    if not noticia["titulo"]:
        return
    chave = chave_noticia(noticia["titulo"])
    if ja_vista(chave):
        return
    nota = pontuar(noticia["titulo"] + " " + noticia["resumo"], palavras)
    if nota >= PONTUACAO_MINIMA:
        registrar(chave, tema, noticia["titulo"], noticia["link"], nota)
        alertar(tema, noticia, nota)


def ciclo(primeira_vez=False):
    # Google News por tema
    for tema, cfg in TEMAS.items():
        for termo in cfg["buscas"]:
            for n in ler_feed(url_google_news(termo)):
                if primeira_vez:  # na 1ª rodada só memoriza, sem inundar de alertas
                    registrar(chave_noticia(n["titulo"]), tema, n["titulo"], n["link"], 0)
                else:
                    processar(n, tema, cfg["palavras"])

    # Feeds diretos, testados contra todos os temas
    for url in FEEDS_DIRETOS:
        for n in ler_feed(url):
            for tema, cfg in TEMAS.items():
                if primeira_vez:
                    registrar(chave_noticia(n["titulo"]), tema, n["titulo"], n["link"], 0)
                else:
                    processar(n, tema, cfg["palavras"])


if __name__ == "__main__":
    if MODO_TESTE:
        print("Telegram:", "configurado" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "NÃO configurado")
        print("WhatsApp:", "configurado" if WHATSAPP_FONE and WHATSAPP_APIKEY else "NÃO configurado")
        alertar("TESTE", {"titulo": "Mensagem de teste do robô de notícias",
                          "fonte": "robo_noticias.py", "link": "https://github.com"}, 5)
        sys.exit(0)

    if "--uma-vez" in sys.argv:
        # Modo agendado: um ciclo e sai. Sem histórico salvo, só memoriza (não inunda de alertas).
        ciclo(primeira_vez=banco_novo)
        print("Histórico criado." if banco_novo else "Ciclo concluído.")
        sys.exit(0)

    print("Robô de notícias iniciado. Carregando histórico...")
    ciclo(primeira_vez=True)
    print(f"Monitorando {len(TEMAS)} temas a cada {INTERVALO_SEGUNDOS}s. Ctrl+C para parar.\n")
    while True:
        try:
            ciclo()
        except Exception as err:
            print(f"[erro no ciclo] {err}")
        time.sleep(INTERVALO_SEGUNDOS)
