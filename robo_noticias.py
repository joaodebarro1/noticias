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
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
INTERVALO_SEGUNDOS = 1800    # de quanto em quanto tempo varre as fontes (modo terminal)
PONTUACAO_MINIMA = 2         # nota mínima para gerar alerta
PALAVRAS_MINIMAS = 2         # quantas palavras diferentes precisam aparecer (1 = basta uma)
# Cada ciclo manda UMA mensagem com todas as notícias novas (o CallMeBot grátis entrega
# na hora só 16 mensagens a cada 4 horas). O que não couber fica para o próximo ciclo.
MAX_CARACTERES_MENSAGEM = 3500
HORARIO_BRASIL = timezone(timedelta(hours=-3))

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


db.execute("CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT)")
ASSINATURA_TEMAS = hashlib.md5(repr((TEMAS, URGENTES, FEEDS_DIRETOS)).encode()).hexdigest()
_linha = db.execute("SELECT valor FROM meta WHERE chave='temas'").fetchone()
temas_mudaram = _linha is None or _linha[0] != ASSINATURA_TEMAS


def salvar_assinatura_temas():
    db.execute("INSERT OR REPLACE INTO meta VALUES ('temas', ?)", (ASSINATURA_TEMAS,))
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


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def encontrada(palavra: str, texto: str) -> bool:
    # palavra inteira; * aceita continuação (inesperad* -> inesperado, preço* da gasolina -> preços da gasolina)
    palavra = sem_acento(palavra)
    padrao = r"(?<!\w)" + r"\w*".join(re.escape(parte) for parte in palavra.split("*"))
    if not palavra.endswith("*"):
        padrao += r"(?!\w)"
    return re.search(padrao, texto) is not None


def pontuar(texto: str):
    """-> (assunto principal, nota, tags por assunto) ou None se nenhuma palavra bateu."""
    t = sem_acento(texto.lower())
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


def vale_alerta(resultado) -> bool:
    if not resultado or resultado[1] < PONTUACAO_MINIMA:
        return False
    # variações da mesma palavra (combustível/combustíveis, posto/postos) contam uma vez só
    raizes = {sem_acento(p).replace("*", "")[:6] for ps in resultado[2].values() for p in ps}
    return len(raizes) >= PALAVRAS_MINIMAS

# =========================================================
# 5. RESUMO COM IA (OpenAI)
# =========================================================

INSTRUCOES_RESUMO = (
    "Você resume notícias para alertas de WhatsApp de um investidor brasileiro. "
    "Escreva em português do Brasil, em 1 ou 2 frases curtas (no máximo 220 caracteres), "
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


def marcador(nota: int) -> str:
    return "🔴" if nota >= 5 else "🟡" if nota >= 3 else "⚪"


def bloco_noticia(item) -> str:
    """Uma notícia dentro da mensagem agrupada."""
    titulo = sem_marcacao(item["titulo"])
    linhas = [f"{marcador(item['nota'])} *{titulo}*"]
    # o resumo da IA substitui o subtítulo para a mensagem não ficar enorme
    texto = item.get("resumo_ia") or sem_marcacao(item.get("subtitulo", ""))[:220]
    if texto and texto.lower() != titulo.lower():
        linhas.append(texto)
    linhas.append(f"🔗 {item['fonte']} · {item['link']}")
    palavras = list(dict.fromkeys(p.replace("*", "") for ps in item["tags"].values() for p in ps))
    outros = f" · +{len(item['outras_fontes'])} sites" if item.get("outras_fontes") else ""
    linhas.append(f"🏷️ {item['tema']} · {', '.join(palavras)}{outros}")
    return "\n".join(linhas)


def cabecalho(itens, prefixo="") -> str:
    contagem = "  ".join(f"{m} {sum(marcador(i['nota']) == m for i in itens)}" for m in ("🔴", "🟡", "⚪")
                         if any(marcador(i["nota"]) == m for i in itens))
    return f"{prefixo}📰 *Radar de notícias* · {datetime.now(HORARIO_BRASIL):%d/%m %H:%M}\n{contagem}"


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
                timeout=30,
            )
            # a resposta repete a mensagem inteira; tira essa parte e o número para sobrar só o status
            status = re.sub(r"<p>Text to send:.*?(?=<p|$)|<p>Message to:[^<]*", "", r.text, flags=re.S)
            status = texto_limpo(status)[:300]
            # 200 = enviada; 210 = passou de 16 mensagens em 4h e entrou na fila do CallMeBot;
            # outros códigos (ex.: 203 com "APIKey is invalid") = não enviada
            if r.status_code != 200 or "invalid" in status.lower() or MODO_TESTE:
                print(f"[whatsapp] {r.status_code} {status}")
        except Exception as err:
            print(f"[erro whatsapp] {err}")


def completar(item):
    """Só para as notícias que vão ser enviadas: link real, subtítulo e resumo da IA."""
    item["link"] = link_real(item["link"])
    subtitulo, texto = ler_materia(item["link"])
    item["subtitulo"] = subtitulo or item["resumo"]
    item["resumo_ia"] = resumir(item["titulo"], item["subtitulo"], texto or item["resumo"])
    print(f"{datetime.now():%H:%M:%S} {marcador(item['nota'])} [{item['tema']}] {item['titulo']}  ({item['fonte']})")

# =========================================================
# 7. NOTÍCIAS PARECIDAS (mesmo fato publicado por vários sites)
# =========================================================

PALAVRAS_VAZIAS = set("""de da do das dos em no na nos nas um uma uns umas para por pelo pela pelos pelas com sem
sobre entre ate apos que se ao aos as os e o a ou mas mais menos muito ja nao sim seu sua seus suas diz dizem veja
entenda confira contra desde the and for with from its are was has have will""".split())


def assinatura(titulo: str) -> set:
    palavras = re.findall(r"\w+", sem_acento(titulo.lower()))
    return {p[:5] for p in palavras if len(p) >= 3 and p not in PALAVRAS_VAZIAS}


def parecida(a: set, b: set) -> bool:
    # calibrado com notícias reais: >= 4 palavras em comum e >= 60% do título menor
    comum = len(a & b)
    return comum >= 4 and comum / min(len(a), len(b)) >= 0.6


def enviadas_recentes():
    desde = (datetime.now() - timedelta(hours=24)).isoformat()
    return [assinatura(t) for (t,) in db.execute(
        "SELECT titulo FROM vistas WHERE nota > 0 AND visto_em >= ?", (desde,))]

# =========================================================
# 8. CICLO PRINCIPAL
# =========================================================


def coletar():
    urls = [url_google_news(t) for cfg in TEMAS.values() for t in cfg["buscas"]] + FEEDS_DIRETOS
    # várias buscas ao mesmo tempo (centenas de buscas em sequência levariam minutos)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for noticias in pool.map(lambda u: list(ler_feed(u)), urls):
            yield from noticias


def candidatas(ignorar_historico=False):
    """Notícias novas que passaram no filtro, sem repetidas, das mais importantes para as menos."""
    ja_enviadas = [] if ignorar_historico else enviadas_recentes()
    escolhidas = []
    for noticia in coletar():
        if not noticia["titulo"] or (not ignorar_historico and ja_vista(noticia)):
            continue
        resultado = pontuar(noticia["titulo"] + " " + noticia["resumo"])
        if not vale_alerta(resultado):
            continue
        noticia["tema"], noticia["nota"], noticia["tags"] = resultado
        sig = assinatura(noticia["titulo"])
        igual = next((e for e in escolhidas if parecida(sig, e["assinatura"])), None)
        if igual:  # mesma notícia de outro site neste ciclo
            if noticia["fonte"] != igual["fonte"]:
                igual["outras_fontes"].add(noticia["fonte"])
            if not ignorar_historico:
                registrar(noticia, "PARECIDA", -1)
            continue
        if any(parecida(sig, e) for e in ja_enviadas):  # já foi enviada nas últimas 24h
            if not ignorar_historico:
                registrar(noticia, "PARECIDA", -1)
            continue
        noticia["assinatura"], noticia["outras_fontes"] = sig, set()
        escolhidas.append(noticia)
    return sorted(escolhidas, key=lambda n: -n["nota"])


def montar_e_enviar(escolhidas, prefixo="", registrar_enviadas=True):
    """Monta UMA mensagem com o que couber; o resto fica para o próximo ciclo."""
    incluidas, blocos = [], []
    for item in escolhidas:
        completar(item)
        bloco = bloco_noticia(item)
        tamanho = len(cabecalho(incluidas + [item], prefixo)) + sum(len(b) + 2 for b in blocos + [bloco])
        if incluidas and tamanho > MAX_CARACTERES_MENSAGEM:
            break
        incluidas.append(item)
        blocos.append(bloco)
    if not incluidas:
        return 0
    msg = cabecalho(incluidas, prefixo) + "\n\n" + "\n\n".join(blocos)
    if MODO_TESTE:
        print(f"\n----- mensagem ({len(msg)} caracteres) -----\n{msg}\n--------------------\n")
    enviar(msg)
    if registrar_enviadas:
        for item in incluidas:
            registrar(item, item["tema"], item["nota"])
    return len(incluidas)


def ciclo(primeira_vez=False):
    if primeira_vez:  # na 1ª rodada só memoriza, sem inundar de alertas
        for noticia in coletar():
            if noticia["titulo"] and not ja_vista(noticia):
                registrar(noticia, "", 0)
        salvar_assinatura_temas()
        return
    escolhidas = candidatas()
    enviadas = montar_e_enviar(escolhidas)
    print(f"{len(escolhidas)} notícias novas; {enviadas} enviadas"
          + (f"; {len(escolhidas) - enviadas} ficaram para o próximo ciclo" if len(escolhidas) > enviadas else ""))


def teste():
    print("Telegram:", "configurado" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "NÃO configurado")
    print("WhatsApp:", "configurado" if WHATSAPP_FONE and WHATSAPP_APIKEY else "NÃO configurado")
    print("OpenAI:  ", f"configurado ({OPENAI_MODELO})" if OPENAI_API_KEY else "NÃO configurado (sem resumo)")
    print(f"Assuntos: {len(TEMAS)} | fontes diretas: {len(FEEDS_DIRETOS)}")
    # manda as 3 notícias reais mais fortes do momento, no formato final (sem mexer no histórico)
    escolhidas = candidatas(ignorar_historico=True)[:3]
    if not montar_e_enviar(escolhidas, prefixo="🧪 TESTE\n", registrar_enviadas=False):
        print("Nenhuma notícia bateu com as palavras de temas.txt agora.")


if __name__ == "__main__":
    if MODO_TESTE:
        teste()
        sys.exit(0)

    if "--uma-vez" in sys.argv:
        # Modo agendado: um ciclo e sai. Sem histórico salvo, ou com temas.txt alterado,
        # só memoriza o que já existe (não inunda de alertas).
        memorizar = banco_novo or temas_mudaram
        ciclo(primeira_vez=memorizar)
        print("Histórico criado/atualizado para os assuntos atuais." if memorizar else "Ciclo concluído.")
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
