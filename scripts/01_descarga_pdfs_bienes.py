"""
Paso 2 del proyecto: localizar y descargar los PDFs de "Declaración de Bienes y Rentas"
de los diputados activos de la XV Legislatura.

Lógica:
1. Cargar el CSV de diputados activos (data/raw/DiputadosActivos.csv) para saber
   qué 350 nombres nos interesan.
2. Iterar `codParlamentario` en un rango acotado para idLegislatura=XV, cargando
   la ficha pública de cada uno CON SELENIUM (el contenido se inyecta con
   JavaScript, confirmado: el HTML crudo de `requests` siempre devuelve la
   misma plantilla vacía de 1977 caracteres, sin los datos de la ficha).
3. Parsear de cada ficha ya renderizada: el nombre del diputado y los enlaces a
   "Declaración de Bienes y Rentas" (puede haber varias, una por fecha).
4. Cruzar por nombre normalizado con el CSV de activos, quedándonos solo con esos 350.
5. Guardar un CSV puente (codigos_diputados.csv) con nombre, codParlamentario y
   URL del PDF más reciente.
6. Descargar cada PDF con `requests` (los PDFs sí son enlaces directos, sin JS).

IMPORTANTE:
- Necesitas Google Chrome instalado y el paquete `selenium` (Selenium Manager,
  incluido desde Selenium 4.6+, descarga el chromedriver automáticamente).
- Cargar ~450 páginas con un navegador real es mucho más lento que con
  `requests` (varios segundos por ficha). Prueba siempre primero con el rango
  reducido antes de lanzar el completo.
"""

import re
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# --- Configuración ---
BASE_DIR = Path(__file__).resolve().parent.parent
CSV_ACTIVOS = BASE_DIR / "data" / "raw" / "DiputadosActivos.csv"
OUT_CODIGOS = BASE_DIR / "data" / "raw" / "codigos_diputados.csv"
PDFS_DIR = BASE_DIR / "data" / "pdfs_bienes"

FICHA_URL = (
    "https://www.congreso.es/es/web/guest/busqueda-de-diputados"
    "?p_p_id=diputadomodule&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
    "&_diputadomodule_mostrarFicha=true"
    "&codParlamentario={cod}&idLegislatura=XV"
)

RANGO_COD_PARLAMENTARIO = range(315, 320)  # cambiar a range(1, 451) cuando funcione bien
PAUSA_ENTRE_PETICIONES = 0.3  # cortesía con el servidor, entre cargas de Selenium
TIMEOUT_CARGA_FICHA = 10  # segundos que espera Selenium a que aparezca el contenido
TIMEOUT_DESCARGA_PDF = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (proyecto académico - análisis de datos abiertos del Congreso)"
}


def normalizar_nombre(nombre: str) -> str:
    """
    Quita acentos, comas y mayúsculas, y devuelve las palabras ordenadas
    alfabéticamente unidas por espacio. Así "Abascal Conde, Santiago" y
    "Santiago Abascal Conde" (orden distinto, típico de título vs. CSV)
    se normalizan al mismo valor y se pueden comparar por igualdad.
    """
    if not isinstance(nombre, str):
        return ""
    nombre = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode("utf-8")
    nombre = nombre.lower().replace(",", " ")
    palabras = re.findall(r"[a-z]+", nombre)
    return " ".join(sorted(palabras))


def cargar_activos() -> set:
    df = pd.read_csv(CSV_ACTIVOS, sep=";", encoding="utf-8-sig")
    return {normalizar_nombre(n) for n in df["NOMBRE"]}


def crear_driver():
    """Crea un driver de Chrome headless (sin ventana visible)."""
    opciones = Options()
    opciones.add_argument("--headless=new")
    opciones.add_argument("--disable-gpu")
    opciones.add_argument("--no-sandbox")
    opciones.add_argument(f"user-agent={HEADERS['User-Agent']}")
    return webdriver.Chrome(options=opciones)


def obtener_html_renderizado(driver, url: str) -> str:
    """
    Carga la URL y espera a que aparezca el bloque de datos de la ficha
    (clase 'dip-intro') antes de devolver el HTML ya renderizado. Si no
    aparece a tiempo, devuelve igualmente lo que haya cargado (normalmente
    significa que ese codParlamentario no corresponde a ningún diputado).
    """
    driver.get(url)
    try:
        WebDriverWait(driver, TIMEOUT_CARGA_FICHA).until(
            EC.presence_of_element_located((By.CLASS_NAME, "dip-intro"))
        )
    except TimeoutException:
        pass
    return driver.page_source


def parsear_ficha(html: str):
    """
    Devuelve (nombre, lista_de_enlaces_bienes) a partir del HTML renderizado de una ficha.
    lista_de_enlaces_bienes es una lista de tuplas (fecha_str, url_pdf).

    Confirmado con HTML real:
    - El nombre viene en el <title> de la página, formato
      "Nombre Apellidos - XV Legislatura - Congreso de los Diputados".
    - Los enlaces de bienes están en <div class="declaraciones-dip"> junto con
      OTROS documentos (Declaración de Actividades, Intereses Económicos) que
      comparten la misma clase, así que filtramos por el TEXTO del enlace,
      no por la clase del div.
    """
    soup = BeautifulSoup(html, "html.parser")

    nombre = None
    if soup.title and soup.title.string:
        nombre = soup.title.string.split(" - ")[0].strip()

    enlaces_bienes = []
    for a in soup.find_all("a", href=True):
        texto = a.get_text(strip=True)
        if "Declaración de Bienes y Rentas" in texto or "Declaracion de Bienes y Rentas" in texto:
            match_fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
            fecha = match_fecha.group(1) if match_fecha else "01/01/1900"
            url_pdf = a["href"]
            if url_pdf.startswith("/"):
                url_pdf = "https://www.congreso.es" + url_pdf
            enlaces_bienes.append((fecha, url_pdf))

    return nombre, enlaces_bienes


def mas_reciente(enlaces_bienes):
    """De la lista de (fecha, url), devuelve la URL con fecha más reciente."""
    if not enlaces_bienes:
        return None
    def parse_fecha(f):
        d, m, a = f.split("/")
        return (int(a), int(m), int(d))
    return sorted(enlaces_bienes, key=lambda x: parse_fecha(x[0]))[-1][1]


def recolectar_fichas():
    nombres_activos = cargar_activos()
    resultados = []

    driver = crear_driver()
    try:
        for cod in RANGO_COD_PARLAMENTARIO:
            url = FICHA_URL.format(cod=cod)
            try:
                html = obtener_html_renderizado(driver, url)
            except Exception as e:
                print(f"[cod={cod}] error cargando página: {e}")
                time.sleep(PAUSA_ENTRE_PETICIONES)
                continue

            nombre, enlaces_bienes = parsear_ficha(html)

            if nombre is None:
                print(f"[cod={cod}] sin ficha válida, se salta")
                time.sleep(PAUSA_ENTRE_PETICIONES)
                continue

            nombre_norm = normalizar_nombre(nombre)
            if nombre_norm not in nombres_activos:
                print(f"[cod={cod}] '{nombre}' no está en los 350 activos, se salta")
                time.sleep(PAUSA_ENTRE_PETICIONES)
                continue

            url_pdf_reciente = mas_reciente(enlaces_bienes)
            resultados.append({
                "codParlamentario": cod,
                "nombre": nombre,
                "url_pdf_bienes": url_pdf_reciente,
                "num_declaraciones_encontradas": len(enlaces_bienes),
            })
            print(f"[cod={cod}] OK: {nombre} ({len(enlaces_bienes)} declaraciones)")

            time.sleep(PAUSA_ENTRE_PETICIONES)
    finally:
        driver.quit()

    return pd.DataFrame(resultados)


def descargar_pdfs(df: pd.DataFrame):
    PDFS_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    for _, fila in df.iterrows():
        if not fila["url_pdf_bienes"]:
            print(f"[cod={fila['codParlamentario']}] sin PDF de bienes, se salta")
            continue

        destino = PDFS_DIR / f"{fila['codParlamentario']}.pdf"
        if destino.exists():
            continue  # ya descargado, no repetir

        try:
            resp = session.get(fila["url_pdf_bienes"], timeout=TIMEOUT_DESCARGA_PDF)
            resp.raise_for_status()
            destino.write_bytes(resp.content)
            print(f"[cod={fila['codParlamentario']}] PDF descargado: {destino.name}")
        except requests.RequestException as e:
            print(f"[cod={fila['codParlamentario']}] error descargando PDF: {e}")

        time.sleep(PAUSA_ENTRE_PETICIONES)


if __name__ == "__main__":
    print("Recolectando fichas y localizando PDFs de bienes...")
    df_codigos = recolectar_fichas()
    df_codigos.to_csv(OUT_CODIGOS, index=False, encoding="utf-8-sig")
    print(f"\nGuardado: {OUT_CODIGOS} ({len(df_codigos)} diputados encontrados de 350 esperados)")

    print("\nDescargando PDFs...")
    descargar_pdfs(df_codigos)
    print("\nProceso completado.")
