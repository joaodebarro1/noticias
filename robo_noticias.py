"""
Robô de notícias em tempo real — estilo "terminal"
--------------------------------------------------
- Monitora fontes RSS + Google News por assunto (definidos em temas.txt)
- Filtra por palavras-chave com pontuação de relevância
- Remove duplicadas (SQLite)
- Busca subtítulo e texto da matéria e gera um resumo com a OpenAI (opcional)
- Envia alertas no Telegram e/ou WhatsApp e imprime no terminal

Credenciais: NUNCA escreva no código. São lidas só de variáveis de ambiente
(no GitHub: Settings -> Secrets and variables -> Actions):
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID    -> bot criado no @BotFather
    WHATSAPP_FONE, WHATSAPP_APIKEY      -> https://www.callmebot.com/blog/free-api-whatsapp-messages/
    OPENAI_API_KEY                      -> https://platform.openai.com/api-keys (resumo com IA)

Rodar:
    python robo_noticias.py            # loop contínuo no terminal
    python robo_noticias.py --uma-vez  # um ciclo e sai (GitHub Actions / agendador)
    python robo_noticias.py --teste    # manda uma notícia real de exemplo e mostra a resposta de cada canal
"""

import os
import re
import sys
import time
import sqlite3
import hashlib
import urllib.parse
from datetime import datetime

import feedparser
import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder

# =========================================================
# 1. CONFIGURAÇÃO (assuntos, palavras e fontes ficam em temas.txt)
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHATSAPP_FONE = os.getenv("WHATSAPP_FONE", "")
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODELO = os.getenv("OPENAI_MODEL") or "gpt-6-luna"

ARQUIVO_TEMAS = os.getenv("NOTICIAS_TEMAS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "temas.txt"))
INTERVALO_SEGUNDOS = 60      # de quanto em quanto tempo varre as fontes (modo terminal)
PONTUACAO_MINIMA = 2         # nota mínima para gerar alerta

MODO_TESTE = "--teste" in sys.argv

NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"}


def ler_palavras(texto: str, n: int) -> dict:
    palavras = {}
    for item in texto.split(","):
        item = item.strip().lower()
        if not item:
            continue
        palavra, _, peso = item.partition("=")
        try:
            palavras[palavra.strip()] = int(peso) if peso.strip() else 1
        except ValueError:
            sys.exit(f"[temas.txt linha {n}] peso inválido em '{item}' (use palavra=número)")
    return palavras


def carregar_temas(caminho: str):
    """Lê temas.txt -> (temas, urgentes, fontes). Formato explicado no topo do arquivo."""
    temas, urgentes, fontes = {}, {}, []
    secao = None
    with open(caminho, encoding="utf-8") as f:
        for n, linha in enumerate(f, 1):
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            if linha.startswith("[") and linha.endswith("]"):
                secao = linha[1:-1].strip()
                if secao.upper() not in ("URGENTES", "FONTES"):
                    temas.setdefault(secao, {"buscas": [], "palavras": {}})
                continue
            if secao is None:
                sys.exit(f"[temas.txt linha {n}] escreva o [NOME DO ASSUNTO] antes desta linha")
            if secao.upper() == "FONTES":
                fontes.append(linha)
                continue
            chave, _, valor = linha.partition(":")
            chave = chave.strip().lower()
            if chave == "palavras":
                destino = urgentes if secao.upper() == "URGENTES" else temas[secao]["palavras"]
                destino.update(ler_palavras(valor, n))
            elif chave == "buscar" and secao.upper() != "URGENTES":
                temas[secao]["buscas"] += [b.strip() for b in valor.split(";") if b.strip()]
            else:
                sys.exit(f"[temas.txt linha {n}] não entendi: '{linha}' (use 'buscar:' ou 'palavras:')")
    return temas, urgentes, fontes


TEMAS, URGENTES, FEEDS_DIRETOS = carregar_temas(ARQUIVO_TEMAS)

# =========================================================
# 2. BANCO DE DADOS (evita alertas repetidos)
# =========================================================

ARQUIVO_DB = os.getenv("NOTICIAS_DB", "noticias.db")
banco_novo = not os.path.exists(ARQUIVO_DB)
db = sqlite3.connect(ARQUIVO_DB)
db.execute("""CREATE TABLE IF NOT EXISTS vistas (
    id TEXT PRIMARY KEY, tema TEXT, titulo TEXT, link TEXT, nota INTEGER, visto_em TEXT)""")
db.commit()


def ja_vista(noticia) -> bool:
    # confere também o título "bruto" (com " - Fonte" do Google News), usado nas versões antigas
    chaves = {chave_noticia(noticia["titulo"]), chave_noticia(noticia["titulo_bruto"])}
    return any(db.execute("SELECT 1 FROM vistas WHERE id=?", (c,)).fetchone() for c in chaves)


def registrar(noticia, tema, nota):
    db.execute("INSERT OR IGNORE INTO vistas VALUES (?,?,?,?,?,?)",
               (chave_noticia(noticia["titulo"]), tema, noticia["titulo"], noticia["link"], nota,
                datetime.now().isoformat()))
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


def texto_limpo(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def ler_feed(url: str):
    try:
        feed = feedparser.parse(url, request_headers=NAVEGADOR)
        for e in feed.entries[:30]:
            titulo = e.get("title", "").strip()
            fonte = e.get("source", {}).get("title") or feed.feed.get("title", url)
            bruto = titulo
            if titulo.endswith(f" - {fonte}"):  # Google News põe " - Fonte" no fim do título
                titulo = titulo[: -len(f" - {fonte}")].strip()
            resumo = "" if "news.google.com" in url else texto_limpo(e.get("summary", ""))[:500]
            yield {"titulo": titulo, "titulo_bruto": bruto, "link": e.get("link", ""),
                   "resumo": resumo, "fonte": fonte}
    except Exception as err:
        print(f"[erro] {url}: {err}")


def link_real(link: str) -> str:
    """Troca o link de redirecionamento do Google News pelo link do site da matéria."""
    if "news.google.com" not in link:
        return link
    try:
        return gnewsdecoder(link, interval=1).get("decoded_url") or link
    except Exception as err:
        print(f"[erro link google] {err}")
        return link


def ler_materia(link: str):
    """Abre a matéria -> (subtítulo, texto). Vazio se o site bloquear ou não responder."""
    try:
        r = requests.get(link, headers=NAVEGADOR, timeout=15)
        pagina = BeautifulSoup(r.text, "html.parser")
        subtitulo = ""
        for nome in ("og:description", "description", "twitter:description"):
            tag = pagina.find("meta", attrs={"property": nome}) or pagina.find("meta", attrs={"name": nome})
            if tag and tag.get("content"):
                subtitulo = tag["content"].strip()
                break
        paragrafos = [p.get_text(" ", strip=True) for p in pagina.find_all("p")]
        texto = " ".join(p for p in paragrafos if len(p) > 80)[:6000]
        return subtitulo, texto
    except Exception as err:
        print(f"[erro matéria] {link}: {err}")
        return "", ""

# =========================================================
# 4. PONTUAÇÃO
# =========================================================


def encontrada(palavra: str, texto: str) -> bool:
    # palavra inteira; com * no fim aceita continuação (inesperad* -> inesperado)
    if palavra.endswith("*"):
        padrao = r"(?<!\w)" + re.escape(palavra[:-1])
    else:
        padrao = r"(?<!\w)" + re.escape(palavra) + r"(?!\w)"
    return re.search(padrao, texto) is not None


def pontuar(texto: str):
    """-> (assunto principal, nota, tags por assunto) ou None se nenhuma palavra bateu."""
    t = texto.lower()
    tags, notas = {}, {}
    for tema, cfg in TEMAS.items():
        achadas = [p for p in cfg["palavras"] if encontrada(p, t)]
        if achadas:
            tags[tema] = achadas
            notas[tema] = sum(cfg["palavras"][p] for p in achadas)
    if not notas:
        return None
    tema = max(notas, key=notas.get)
    urgentes = [p for p in URGENTES if encontrada(p, t)]
    if urgentes:
        tags["URGENTE"] = urgentes
    return tema, notas[tema] + sum(URGENTES[p] for p in urgentes), tags

# =========================================================
# 5. RESUMO COM IA (OpenAI)
# =========================================================

INSTRUCOES_RESUMO = (
    "Você resume notícias para alertas de WhatsApp de um investidor brasileiro. "
    "Escreva em português do Brasil, em 2 ou 3 frases curtas (no máximo 350 caracteres), "
    "usando apenas fatos presentes no texto e destacando números relevantes. "
    "Não repita o título. Não use markdown, emojis nem aspas."
)


def resumir(titulo: str, subtitulo: str, texto: str) -> str:
    if not OPENAI_API_KEY or len(texto) < 200:
        return ""
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": OPENAI_MODELO, "instructions": INSTRUCOES_RESUMO,
                  "input": f"Título: {titulo}\nSubtítulo: {subtitulo}\n\nTexto:\n{texto}",
                  "max_output_tokens": 1000},
            timeout=60,
        )
        if not r.ok:
            print(f"[erro openai] {r.status_code} {r.text[:300]}")
            return ""
        partes = [c.get("text", "") for item in r.json().get("output", []) if item.get("type") == "message"
                  for c in item.get("content", []) if c.get("type") == "output_text"]
        return " ".join(partes).strip()
    except Exception as err:
        print(f"[erro openai] {err}")
        return ""

# =========================================================
# 6. ALERTA
# =========================================================


def sem_marcacao(texto: str) -> str:
    # * e _ viram negrito/itálico no WhatsApp/Telegram e bagunçariam a mensagem
    return texto.replace("*", "").replace("_", " ").strip()


def montar_mensagem(noticia, nota, tags) -> str:
    marcador = "🔴" if nota >= 5 else "🟡" if nota >= 3 else "⚪"
    titulo = sem_marcacao(noticia["titulo"])
    partes = [f"{marcador} *{titulo}*"]
    subtitulo = sem_marcacao(noticia.get("subtitulo", ""))
    if subtitulo and subtitulo.lower() != titulo.lower():
        partes[0] += f"\n_{subtitulo[:300]}_"
    if noticia.get("resumo_ia"):
        partes.append(f"📝 {noticia['resumo_ia']}")
    partes.append(f"🔗 {noticia['fonte']}\n{noticia['link']}")
    etiquetas = " · ".join(f"{tema}: {', '.join(p.rstrip('*') for p in ps)}" for tema, ps in tags.items())
    partes.append(f"🏷️ {etiquetas} (nota {nota})")
    return "\n\n".join(partes)


def enviar(msg: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            dados = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={**dados, "parse_mode": "Markdown"}, timeout=10)
            if not r.ok:  # ex.: "_" dentro do link quebra o Markdown -> reenvia sem formatação
                r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data=dados, timeout=10)
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
            # CallMeBot responde 203 (não 4xx) quando a apikey/número é inválido
            if r.status_code != 200 or "invalid" in r.text.lower() or MODO_TESTE:
                print(f"[whatsapp] {r.status_code} {r.text[:300]}")
        except Exception as err:
            print(f"[erro whatsapp] {err}")


def alertar(tema, noticia, nota, tags, prefixo=""):
    # só as notícias que vão ser enviadas passam por aqui: abre a matéria e resume
    noticia["link"] = link_real(noticia["link"])
    subtitulo, texto = ler_materia(noticia["link"])
    noticia["subtitulo"] = subtitulo or noticia["resumo"]
    noticia["resumo_ia"] = resumir(noticia["titulo"], noticia["subtitulo"], texto or noticia["resumo"])

    marcador = "🔴" if nota >= 5 else "🟡" if nota >= 3 else "⚪"
    print(f"{datetime.now():%H:%M:%S} {marcador} [{tema}] {noticia['titulo']}  ({noticia['fonte']})")
    msg = prefixo + montar_mensagem(noticia, nota, tags)
    if MODO_TESTE:
        print(f"\n----- mensagem -----\n{msg}\n--------------------\n")
    enviar(msg)

# =========================================================
# 7. CICLO PRINCIPAL
# =========================================================


def coletar():
    for cfg in TEMAS.values():
        for termo in cfg["buscas"]:
            yield from ler_feed(url_google_news(termo))
    for url in FEEDS_DIRETOS:
        yield from ler_feed(url)


def ciclo(primeira_vez=False):
    for noticia in coletar():
        if not noticia["titulo"] or ja_vista(noticia):
            continue
        if primeira_vez:  # na 1ª rodada só memoriza, sem inundar de alertas
            registrar(noticia, "", 0)
            continue
        resultado = pontuar(noticia["titulo"] + " " + noticia["resumo"])
        if resultado and resultado[1] >= PONTUACAO_MINIMA:
            tema, nota, tags = resultado
            registrar(noticia, tema, nota)
            alertar(tema, noticia, nota, tags)


def teste():
    print("Telegram:", "configurado" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "NÃO configurado")
    print("WhatsApp:", "configurado" if WHATSAPP_FONE and WHATSAPP_APIKEY else "NÃO configurado")
    print("OpenAI:  ", f"configurado ({OPENAI_MODELO})" if OPENAI_API_KEY else "NÃO configurado (sem resumo)")
    print(f"Assuntos: {', '.join(TEMAS)} | fontes diretas: {len(FEEDS_DIRETOS)}")
    # manda a primeira notícia real que bater com algum assunto, no formato final
    for noticia in coletar():
        resultado = pontuar(noticia["titulo"] + " " + noticia["resumo"])
        if resultado:
            tema, nota, tags = resultado
            alertar(tema, noticia, nota, tags, prefixo="🧪 TESTE (notícia real de exemplo)\n\n")
            return
    print("Nenhuma notícia bateu com as palavras de temas.txt agora.")


if __name__ == "__main__":
    if MODO_TESTE:
        teste()
        sys.exit(0)

    if "--uma-vez" in sys.argv:
        # Modo agendado: um ciclo e sai. Sem histórico salvo, só memoriza (não inunda de alertas).
        ciclo(primeira_vez=banco_novo)
        print("Histórico criado." if banco_novo else "Ciclo concluído.")
        sys.exit(0)

    print("Robô de notícias iniciado. Carregando histórico...")
    ciclo(primeira_vez=True)
    print(f"Monitorando {len(TEMAS)} assuntos a cada {INTERVALO_SEGUNDOS}s. Ctrl+C para parar.\n")
    while True:
        try:
            ciclo()
        except Exception as err:
            print(f"[erro no ciclo] {err}")
        time.sleep(INTERVALO_SEGUNDOS)
