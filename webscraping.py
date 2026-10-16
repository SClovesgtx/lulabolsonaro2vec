import os
import re
import time
import requests
from urllib.parse import urljoin, urlparse, parse_qs
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ============ CONFIGURAÇÕES ============
BASE_URL = "https://www.gov.br/planalto/pt-br/conheca-a-presidencia/biblioteca-da-pr/ex-presidentes/luiz-inacio-lula-da-silva-2/discursos-presidenciais"
ANO_INICIO = 2003
ANO_FIM = 2010
DOWNLOAD_DIR = "discursos_lula"
CHROMEDRIVER_PATH = None  # Ubuntu: já está no PATH se instalou via apt
ITENS_POR_PAGINA = 20     # Conforme visto no HTML (?b_start:int=20)
PAUSA_ENTRE_REQUESTS = 1.5


def configurar_driver():
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless')  # Descomente após testar
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--lang=pt-BR')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    prefs = {
        "download.default_directory": os.path.abspath(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    if CHROMEDRIVER_PATH:
        return webdriver.Chrome(executable_path=CHROMEDRIVER_PATH, options=options)
    return webdriver.Chrome(options=options)


def tratar_cookies(driver):
    """Fecha o popup de cookies do gov.br (botão 'Rejeitar' ou 'Aceitar')."""
    try:
        # O HTML mostra: <button class="br-button secondary small reject-all" ...>Rejeitar</button>
        botao = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button.reject-all, button.btn-accept, button.btn-manage")
            )
        )
        driver.execute_script("arguments[0].click();", botao)
        print("[OK] Popup de cookies fechado.")
        time.sleep(1.5)
    except TimeoutException:
        print("[INFO] Nenhum popup de cookies visível.")
    except Exception as e:
        print(f"[AVISO] Erro no popup de cookies: {e}")


def aguardar_tabela(driver, timeout=15):
    """Espera a tabela de listagem dos discursos aparecer."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.listing tbody tr"))
        )
        return True
    except TimeoutException:
        return False


def coletar_links_discursos(driver):
    """
    Coleta os links das linhas da tabela de discursos.
    Baseado no XPath: //*[@id="content-core"]/div/div/div[1]/a
    Estrutura real: table.listing > tbody > tr > td:first-child > a
    """
    links = []
    try:
        # Espera a tabela aparecer
        if not aguardar_tabela(driver):
            return links
        
        # Pega todas as linhas da tabela
        linhas = driver.find_elements(By.CSS_SELECTOR, "table.listing tbody tr")
        
        for linha in linhas:
            try:
                # O primeiro <td> de cada linha tem o <a> do título
                a = linha.find_element(By.CSS_SELECTOR, "td:first-child a")
                href = a.get_attribute("href")
                titulo = a.text.strip()
                if href:
                    links.append((href, titulo))
            except NoSuchElementException:
                continue
    except Exception as e:
        print(f"   [AVISO] Erro ao coletar links: {e}")
    
    # Remove duplicatas preservando ordem
    vistos = set()
    unicos = []
    for href, titulo in links:
        if href not in vistos:
            vistos.add(href)
            unicos.append((href, titulo))
    
    return unicos


def descobrir_paginas_do_ano(driver):
    """
    Lê a paginação da página do ano e retorna a lista de URLs de todas as páginas.
    Ex: .../2003, .../2003?b_start:int=20, .../2003?b_start:int=40
    """
    urls = [driver.current_url]
    
    try:
        # Os links de paginação têm a classe "proximo" ou são <a> com b_start:int=
        links_pag = driver.find_elements(
            By.CSS_SELECTOR, "ul.paginacao a[href*='b_start:int=']"
        )
        for link in links_pag:
            href = link.get_attribute("href")
            if href and href not in urls:
                urls.append(href)
        
        # Também capturamos o link "última página" (pagina class="pagina")
        links_ultima = driver.find_elements(By.CSS_SELECTOR, "ul.paginacao a.pagina")
        for link in links_ultima:
            href = link.get_attribute("href")
            if href and href not in urls:
                urls.append(href)
    except Exception as e:
        print(f"   [AVISO] Erro ao ler paginação: {e}")
    
    # Ordena pela query string b_start:int
    def chave_ordenacao(url):
        q = parse_qs(urlparse(url).query)
        try:
            return int(q.get("b_start:int", ["0"])[0])
        except (ValueError, IndexError):
            return 0
    
    return sorted(set(urls), key=chave_ordenacao)


def encontrar_pdf_na_pagina_view(driver, url_view):
    """
    Abre uma página `.pdf/view` e localiza o link real para o PDF.
    No Plone, geralmente há um <a> com href terminando em `.pdf` ou
    um link para `/@@download/file` ou similar.
    """
    driver.get(url_view)
    time.sleep(1.5)
    
    # Estratégia 1: link direto para arquivo .pdf
    candidatos = []
    
    # Procura qualquer <a> cujo href termine em .pdf ou contenha "/download"
    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = a.get_attribute("href") or ""
        if href.lower().endswith(".pdf"):
            candidatos.append(href)
        elif "/@@download/" in href or "/download/file" in href:
            candidatos.append(href)
    
    # Estratégia 2: meta tag de download (Plone às vezes usa)
    if not candidatos:
        try:
            meta = driver.find_element(By.CSS_SELECTOR, "meta[name='download']")
            href = meta.get_attribute("content")
            if href:
                candidatos.append(href)
        except NoSuchElementException:
            pass
    
    # Estratégia 3: link dentro de #content-core
    if not candidatos:
        try:
            a = driver.find_element(By.CSS_SELECTOR, "#content-core a[href*='.pdf'], #content-core a[href*='download']")
            href = a.get_attribute("href")
            if href:
                candidatos.append(href)
        except NoSuchElementException:
            pass
    
    # Retorna o primeiro candidato, ou None
    return candidatos[0] if candidatos else None


def nome_arquivo_de_url(url, titulo):
    """Gera um nome de arquivo seguro a partir do título ou URL."""
    if titulo:
        nome = titulo
    else:
        nome = url.split("/")[-2]  # penúltimo segmento costuma ser o slug
    
    # Remove extensão .pdf duplicada se houver
    if nome.lower().endswith(".pdf"):
        nome = nome[:-4]
    
    # Sanitiza
    nome = re.sub(r'[^\w\s\-\.]', '_', nome, flags=re.UNICODE)
    nome = re.sub(r'\s+', ' ', nome).strip()
    nome = nome[:150]  # Limita tamanho
    
    return f"{nome}.pdf"


def baixar_pdf(url_pdf, pasta_destino, titulo=None):
    if not url_pdf:
        return False
    
    nome_arquivo = nome_arquivo_de_url(url_pdf, titulo)
    caminho = os.path.join(pasta_destino, nome_arquivo)
    
    if os.path.exists(caminho):
        print(f"   [SKIP] Já existe: {nome_arquivo}")
        return True
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
        "Referer": "https://www.gov.br/",
    }
    
    try:
        r = requests.get(url_pdf, headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        
        # Confere se realmente é um PDF
        content_type = r.headers.get("Content-Type", "").lower()
        if "pdf" not in content_type and not url_pdf.lower().endswith(".pdf"):
            print(f"   [AVISO] Resposta não é PDF ({content_type}): {url_pdf}")
            return False
        
        with open(caminho, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        tamanho = os.path.getsize(caminho) / 1024
        print(f"   [OK] {nome_arquivo} ({tamanho:.0f} KB)")
        return True
    except Exception as e:
        print(f"   [ERRO] Falha ao baixar {url_pdf}: {e}")
        if os.path.exists(caminho):
            os.remove(caminho)
        return False


def processar_ano(driver, ano):
    print(f"\n========== ANO {ano} ==========")
    
    pasta_ano = os.path.join(DOWNLOAD_DIR, str(ano))
    os.makedirs(pasta_ano, exist_ok=True)
    
    # 1. Vai direto para a URL do ano (não precisa clicar em nada)
    url_ano = f"{BASE_URL}/{ano}"
    driver.get(url_ano)
    time.sleep(2)
    tratar_cookies(driver)
    
    if not aguardar_tabela(driver):
        print(f"[AVISO] Nenhuma tabela encontrada para {ano}.")
        return 0
    
    # 2. Descobre todas as páginas de paginação
    paginas = descobrir_paginas_do_ano(driver)
    print(f"[INFO] {len(paginas)} página(s) de listagem encontradas.")
    
    # 3. Para cada página, coleta os links dos discursos
    todos_discursos = []
    for i, url_pag in enumerate(paginas, 1):
        if i > 1:
            driver.get(url_pag)
            time.sleep(1.5)
            tratar_cookies(driver)
        
        if not aguardar_tabela(driver):
            print(f"   [AVISO] Página {i} sem tabela: {url_pag}")
            continue
        
        discursos = coletar_links_discursos(driver)
        print(f"   Página {i}: {len(discursos)} discurso(s).")
        todos_discursos.extend(discursos)
    
    # Remove duplicatas
    todos_discursos = list(dict.fromkeys(todos_discursos))
    print(f"[INFO] Total: {len(todos_discursos)} discursos únicos em {ano}.")
    
    # 4. Para cada discurso, entra na página .pdf/view e baixa o PDF
    baixados = 0
    for idx, (url_view, titulo) in enumerate(todos_discursos, 1):
        print(f"   [{idx}/{len(todos_discursos)}] {titulo[:80]}...")
        
        try:
            url_pdf = encontrar_pdf_na_pagina_view(driver, url_view)
            
            if not url_pdf:
                print(f"      [ERRO] PDF não encontrado em {url_view}")
                continue
            
            if baixar_pdf(url_pdf, pasta_ano, titulo):
                baixados += 1
            
            time.sleep(PAUSA_ENTRE_REQUESTS)
        except Exception as e:
            print(f"      [ERRO] {e}")
    
    print(f"[RESUMO] {ano}: {baixados}/{len(todos_discursos)} PDFs baixados.")
    return baixados


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    driver = configurar_driver()
    driver.maximize_window()
    
    total_geral = 0
    try:
        for ano in range(ANO_INICIO, ANO_FIM + 1):
            try:
                baixados = processar_ano(driver, ano)
                total_geral += baixados
            except Exception as e:
                print(f"[ERRO] Falha no ano {ano}: {e}")
    finally:
        driver.quit()
        print(f"\n========== CONCLUÍDO ==========")
        print(f"Total de PDFs baixados: {total_geral}")
        print(f"Pasta: {os.path.abspath(DOWNLOAD_DIR)}")


if __name__ == "__main__":
    main()