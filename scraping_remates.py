import csv
import io
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

def obtener_ahora_chile() -> datetime:
    """Devuelve la fecha y hora oficial de Chile (Continental) sin tzinfo para comparaciones directas."""
    try:
        return datetime.now(ZoneInfo("America/Santiago")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


BASE_URL = "https://www.boletinconcursal.cl"
PORTAL_URL = f"{BASE_URL}/boletin/remates"
DOWNLOAD_URL = f"{BASE_URL}/boletin/downloadDocumentoByCodigo"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def normalizar_texto(texto: str) -> str:
    """Elimina tildes y convierte a minúsculas para comparaciones robustas."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def parsear_fecha_remate(texto_fecha: str) -> Optional[datetime]:
    """
    Convierte la cadena de fecha del PDF a un objeto datetime de Python.
    Formatos comunes: '09/09/2026 12:00', '10/09/2026 15:30', '09/09/2026'.
    """
    if not texto_fecha or texto_fecha == "No informado":
        return None
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}):(\d{2}))?", texto_fecha)
    if m:
        dia, mes, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hora = int(m.group(4)) if m.group(4) else 23
        minuto = int(m.group(5)) if m.group(5) else 59
        try:
            return datetime(anio, mes, dia, hora, minuto)
        except ValueError:
            return None
    return None


def detectar_modalidad(direccion: str, detalle: str) -> str:
    """
    Determina si el remate es ONLINE, PRESENCIAL o MIXTO (Presencial + Online)
    a partir del texto de la dirección y del detalle de la ficha.
    """
    texto = f"{direccion} {detalle}".lower()
    dir_lower = direccion.lower()

    es_online = (
        bool(re.search(r"\b(online|zoom|virtual|rematadas?\.cl|cgrchile|plataforma|portal)\b|www\.", dir_lower))
        or "modalidad online" in texto
        or "vía zoom" in texto
    )
    es_presencial = (
        bool(re.search(r"\b(presencial|calle|avenida|avda|av\.|pasaje|notar[ií]a|juzgado|oficina|aldunate|norte|sur|n[°º])\b|\d{3,}", dir_lower))
    )

    if es_online and es_presencial:
        return "MIXTO (Presencial + Online)"
    elif es_online:
        return "ONLINE"
    elif es_presencial:
        return "PRESENCIAL"
    return "PRESENCIAL"


def obtener_tokens_csrf(session: requests.Session) -> Tuple[str, str]:
    """
    Obtiene el token CSRF y el nombre del header desde las etiquetas meta
    de la página de remates del Boletín Concursal.
    """
    resp = session.get(PORTAL_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    csrf_meta = soup.find("meta", {"name": "_csrf"})
    csrf_header_meta = soup.find("meta", {"name": "_csrf_header"})

    if not csrf_meta or not csrf_meta.get("content"):
        raise ValueError("No se encontró la etiqueta meta '_csrf' en el Boletín Concursal.")

    csrf_token = csrf_meta["content"]
    csrf_header = csrf_header_meta["content"] if csrf_header_meta else "X-CSRF-TOKEN"

    return csrf_token, csrf_header


def obtener_listado_remates(
    session: requests.Session,
    csrf_token: str,
    csrf_header: str,
    tipo: str = "muebles",
    inicio: int = 0,
    cantidad: int = 100,
) -> Tuple[List[dict], int]:
    """
    Consulta el endpoint AJAX de DataTables con paginación:
    - tipo='muebles'   -> /boletin/getRMP/
    - tipo='inmuebles' -> /boletin/getRIP/
    Retorna (lista_de_publicaciones, total_registros_en_servidor).
    """
    endpoint = f"{BASE_URL}/boletin/getRMP/" if tipo == "muebles" else f"{BASE_URL}/boletin/getRIP/"

    ajax_headers = HEADERS.copy()
    ajax_headers[csrf_header] = csrf_token
    ajax_headers["X-Requested-With"] = "XMLHttpRequest"

    payload = {
        "draw": "1",
        "start": str(inicio),
        "length": str(cantidad),
    }

    resp = session.post(endpoint, data=payload, headers=ajax_headers, timeout=25)
    resp.raise_for_status()

    data = resp.json()
    registros = data.get("data", [])
    total = data.get("recordsTotal", len(registros))
    return registros, total


def descargar_y_extraer_pdf(
    session: requests.Session, csrf_token: str, codigo_validacion: str
) -> str:
    """Descarga el PDF oficial del remate y extrae su texto plano, utilizando caché local."""
    pdf_dir = os.path.join(os.path.dirname(__file__), "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"{codigo_validacion}.pdf")

    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 500:
        with open(pdf_path, "rb") as f:
            contenido = f.read()
    else:
        payload = {
            "_csrf": csrf_token,
            "codigoValidacion": codigo_validacion,
        }
        resp = session.post(DOWNLOAD_URL, data=payload, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        contenido = resp.content
        with open(pdf_path, "wb") as f:
            f.write(contenido)

    reader = PdfReader(io.BytesIO(contenido))
    texto_completo = []
    for page in reader.pages:
        txt = page.extract_text()
        if txt:
            texto_completo.append(txt)

    return "\n".join(texto_completo)


def parsear_datos_remate(texto: str) -> Dict[str, str]:
    """Extrae y normaliza los campos estructurados del texto del PDF oficial."""
    def match(pattern: str, default: str = "No informado") -> str:
        m = re.search(pattern, texto, re.IGNORECASE)
        return m.group(1).strip() if m else default

    # Extracción de campos clave con tolerancia a tildes
    region_match = re.search(r"Regi[oó]n:\s*(.*?)(?=\s*Comuna:|$)", texto, re.IGNORECASE)
    comuna_match = re.search(r"Comuna:\s*(.*?)(?=\s*Direcci[oó]n:|$|\n)", texto, re.IGNORECASE)
    direccion_match = re.search(r"Direcci[oó]n:\s*(.*?)(?=\s*Detalle:|$|\n\s*Detalle)", texto, re.IGNORECASE | re.DOTALL)
    detalle_match = re.search(r"Detalle\s*\n(.*?)(?=\nTipo Bienes|\nValor M[ií]nimo|$)", texto, re.IGNORECASE | re.DOTALL)

    direccion_remate = " ".join((direccion_match.group(1).strip() if direccion_match else "No informado").split())
    detalle_bien = " ".join((detalle_match.group(1).strip() if detalle_match else "").split())

    # 1. Corrección de Valor Mínimo:
    # Solo buscar caracteres en la misma línea para evitar capturar la siguiente (ej. Comisión)
    valor_min_match = re.search(r"Valor M[ií]nimo[ \t]*\([^\)]+\):[ \t]*([^\n\r]*)", texto, re.IGNORECASE)
    val_raw = valor_min_match.group(1).strip() if valor_min_match else ""
    if not val_raw or not re.search(r"[1-9]", val_raw):
        valor_min_limpio = "Sin mínimo ($ 0)"
    else:
        valor_min_limpio = val_raw

    # 2. Corrección de Comisión:
    # Normaliza doble '%' o comas huérfanas al inicio (ej: ",00 %" -> "0,00 %")
    comision_match = re.search(r"Comisi[oó]n:\s*([^\n\r]+)", texto, re.IGNORECASE)
    comision_raw = comision_match.group(1).strip() if comision_match else "No informado"
    if comision_raw != "No informado":
        comision_limpia = re.sub(r"%\s*%", "%", comision_raw).strip()
        if re.match(r"^\s*,\s*\d+", comision_limpia):
            comision_limpia = f"0{comision_limpia.lstrip()}"
        elif comision_limpia == "%" or not comision_limpia:
            comision_limpia = "No informado"
    else:
        comision_limpia = "No informado"

    # 3. Corrección de Web Martillero:
    # Ignorar la URL del pie de página de 'boletinconcursal.cl'
    todas_webs = re.findall(
        r"(?:https?://)?(?:www\.)?([a-zA-Z0-9\-_]+(?:\.[a-zA-Z0-9\-_]+)*\.(?:cl|com|org|net)(?:/[^\s]*)?)",
        texto,
        re.IGNORECASE,
    )
    martillero_webs = [w for w in todas_webs if "boletinconcursal" not in w.lower()]
    web_martillero = martillero_webs[0] if martillero_webs else "No informado"

    # 4. Clasificación de modalidad (Online, Presencial o Mixto)
    modalidad = detectar_modalidad(direccion_remate, detalle_bien)

    # 5. Tipo de Bienes oficial según PDF
    tipo_bienes_match = re.search(r"Tipo Bienes\s*\n([^\n\r]+)", texto, re.IGNORECASE)
    tipo_bienes_pdf = tipo_bienes_match.group(1).strip() if tipo_bienes_match else "No informado"

    fecha_remate_raw = match(r"Fecha del Remate:\s*([^\n\r]+)")

    return {
        "region_remate": region_match.group(1).strip() if region_match else "No informado",
        "comuna_remate": comuna_match.group(1).strip() if comuna_match else "No informado",
        "direccion_remate": direccion_remate,
        "modalidad": modalidad,
        "fecha_remate": fecha_remate_raw,
        "tipo_bienes_pdf": tipo_bienes_pdf,
        "tipo_procedimiento": match(r"Tipo Procedimiento:\s*([^\n\r]+)"),
        "rol_causa": match(r"Rol Causa:\s*([^\n\r]+)"),
        "tribunal": match(r"Tribunal:\s*([^\n\r]+)"),
        "deudor": match(r"Deudor:\s*([^\n\r]+)"),
        "rut_deudor": match(r"Deudor Rut:\s*([^\n\r]+)"),
        "liquidador": match(r"Liquidador:\s*([^\n\r]+)"),
        "valor_minimo": valor_min_limpio,
        "comision": comision_limpia,
        "martillero_web": web_martillero,
        "detalle": detalle_bien,
    }


def coincide_filtro(
    datos: Dict[str, str], palabras_clave: List[str], solo_ubicacion_oficial: bool = False
) -> Tuple[bool, Optional[str]]:
    """Comprueba si alguna de las palabras clave coincide respetando límites de palabra (\b)."""
    filtros_norm = [normalizar_texto(k) for k in palabras_clave if k.strip()]
    if not filtros_norm:
        return True, "Sin filtro geográfico (Todo Chile)"

    # 1. Comprobar en ubicación del remate (Región y Comuna)
    ubicacion_remate = normalizar_texto(f"{datos['region_remate']} {datos['comuna_remate']}")
    for k in filtros_norm:
        if re.search(rf"\b{re.escape(k)}\b", ubicacion_remate):
            return True, f"Ubicación del remate ({datos['region_remate']} - {datos['comuna_remate']})"

    if solo_ubicacion_oficial:
        return False, None

    # 2. Comprobar en el Tribunal
    tribunal_norm = normalizar_texto(datos["tribunal"])
    for k in filtros_norm:
        if re.search(rf"\b{re.escape(k)}\b", tribunal_norm):
            return True, f"Tribunal de la causa ({datos['tribunal']})"

    # 3. Comprobar en la descripción del bien
    detalle_norm = normalizar_texto(datos["detalle"])
    for k in filtros_norm:
        if re.search(rf"\b{re.escape(k)}\b", detalle_norm):
            return True, f"Descripción del bien ('{k}')"

    return False, None


def exportar_csv(resultados: List[Dict[str, str]], filename: str = "remates_muebles_vigentes.csv"):
    """Guarda los resultados filtrados en un archivo CSV."""
    if not resultados:
        return
    campos = [
        "codigo_ficha",
        "tipo_bien",
        "modalidad",
        "estado_vigencia",
        "fecha_remate",
        "fecha_publicacion",
        "region_remate",
        "comuna_remate",
        "direccion_remate",
        "valor_minimo",
        "comision",
        "tribunal",
        "rol_causa",
        "deudor",
        "rut_deudor",
        "martillero_web",
        "coincidencia_filtro",
        "detalle",
    ]
    with open(filename, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        for r in resultados:
            writer.writerow(r)
    print(f"\n[+] Archivo CSV generado con éxito: {filename}")


def desglosar_articulos(detalle: str) -> List[str]:
    """
    Desglosa la descripción de bienes en una lista limpia de artículos individuales,
    eliminando disclaimers legales e instrucciones de postores y evitando cortar
    especificaciones de modelos o marcas.
    """
    if not detalle:
        return []

    # 1. Separar instrucciones de postor / disclaimers de remate para que no aparezcan como artículos
    m_disc = re.search(
        r"\s*(?:El\s+remate\s+se\s+efectuar[aá]|Para\s+(?:mayor\s+informaci[oó]n|participar)|Los\s+interesados\s+deber[aá]n|Bases\s+(?:del\s+remate|en\s+el\s+tribunal)|Garant[ií]a\s+seriedad|Inscripci[oó]n\s+en\s+p[aá]gina|Todo\s+postor).*",
        detalle,
        re.IGNORECASE,
    )
    if m_disc:
        detalle = detalle[:m_disc.start()].strip()

    # 2. Separar por delimitadores explícitos en orden de prioridad
    if "/" in detalle:
        partes = [p.strip() for p in detalle.split("/") if p.strip()]
    elif "*" in detalle:
        partes = [p.strip() for p in detalle.split("*") if p.strip()]
    elif ";" in detalle:
        partes = [p.strip() for p in detalle.split(";") if p.strip()]
    elif re.search(r"\b0\d\s+[A-ZÁÉÍÓÚ]", detalle):
        # Separar por contadores con dos dígitos (01 ..., 02 ...) sin romper especificaciones
        partes = [p.strip() for p in re.split(r"(?<=[A-Za-z0-9\.\)])\s+(?=0\d\s+[A-ZÁÉÍÓÚ])", detalle) if p.strip()]
    elif "," in detalle and len(detalle.split(",")) >= 3:
        partes = [p.strip() for p in detalle.split(",") if p.strip()]
    else:
        partes = [detalle]

    limpios = []
    for p in partes:
        p_clean = re.sub(
            r"^(?:BIEN(?:ES)?\s+(?:MUEBLE(?:S)?\s+)?(?:A\s+SUBASTAR\s+)?(?:SIN|CON)?\s*(?:POSTURA\s+M[IÍ]NIMA)?[:\s]*|"
            r"VEH[IÍ]CULO(?:S)?\s+(?:A\s+SUBASTAR\s+)?(?:SIN|CON)?\s*(?:POSTURA\s+M[IÍ]NIMA)?[:\s]*|"
            r"BIEN(?:ES)?\s+A\s+SUBASTAR[:\s]*|"
            r"(?:SIN|CON)\s+POSTURA\s+M[IÍ]NIMA[:\s]*|"
            r"POSTURA\s+M[IÍ]NIMA[:\s]*|"
            r"LOTE\s+(?:N[°º\.]*\s*)?\d+[:\s]*|"
            r"\d+[\.\-\)]\s*)",
            "",
            p,
            flags=re.IGNORECASE,
        ).strip(" ,;.-")
        if p_clean and len(p_clean) > 2:
            limpios.append(p_clean)

    return limpios


def extraer_sitios_web(martillero_web: str = "", direccion: str = "", detalle: str = "") -> List[Dict[str, str]]:
    """Extrae todas las URLs o páginas web donde se realiza el remate."""
    texto_busqueda = f"{martillero_web} {direccion} {detalle}"
    encontrados = re.findall(
        r"(?:https?://)?(?:www\.)?([a-zA-Z0-9\-_]+(?:\.[a-zA-Z0-9\-_]+)*\.(?:cl|com|org|net)(?:/[^\s,\)]*)?)",
        texto_busqueda,
        re.IGNORECASE,
    )
    sitios = []
    ignorar = ["boletinconcursal", "pjud", "gmail", "hotmail", "yahoo", "outlook", "google"]
    vistos = set()
    for c in encontrados:
        clean = c.strip(".,;:()[]{}\"'")
        low = clean.lower()
        if any(ign in low for ign in ignorar) or len(clean) < 4 or low in vistos:
            continue
        vistos.add(low)
        nombre = clean.lower()
        if not nombre.startswith("www.") and not nombre.startswith("http"):
            nombre_mostrar = f"www.{nombre}"
        else:
            nombre_mostrar = nombre.replace("https://", "").replace("http://", "")
        url = clean if clean.startswith("http://") or clean.startswith("https://") else f"https://{clean}"
        sitios.append({"nombre": nombre_mostrar, "url": url})
    return sitios


def extraer_vehiculos(detalle_texto: str, valor_minimo_remate: str = "") -> List[Dict[str, Any]]:
    """
    Analiza la descripción de un remate y extrae los vehículos con sus detalles estructurados
    sin generar falsos positivos en enseres domésticos o tecnología:
    - tipo: Automóvil, Camioneta, Station Wagon / SUV, Moto, Furgón, Camión, Semirremolque, Excavadora, etc.
    - titulo: Nombre o modelo descriptivo limpio
    - patente: Placa Patente Única (PPU) normalizada
    - anio: Año del modelo
    - minimo: Postura mínima individual o del lote si es unitario
    - color: Color de la carrocería
    - transmision: Automática o Manual / Mecánica
    - traccion: 4x4, 4x2, AWD
    - combustible: Diésel, Bencina, Híbrido, Eléctrico
    - kilometraje: Kilometraje si está especificado
    - afecto_iva: Si está afecto a IVA (+19%)
    - zona_franca: Si tiene limitación de dominio Zona Franca
    - ubicacion_fisica: Ciudad o lugar físico donde está custodiado el vehículo
    - estado_mecanico: Observación de estado (a la vista, en el estado en que se encuentra, etc.)
    - motor: Número de motor si viene
    - chasis: Número de chasis si viene
    - bienes_anexos: Bienes menores adicionales incluidos con el vehículo
    """
    if not detalle_texto:
        return []

    iva_global = bool(re.search(r'\bAFECT[AO]\s+A\s+IVA\b|\+\s*IVA', detalle_texto, re.IGNORECASE))
    zf_global = bool(re.search(r'\bZONA\s+FRANCA\b', detalle_texto, re.IGNORECASE))

    # Ubicación física especial en todo el texto
    ubic_global = ""
    m_ub = re.search(r'(?:UBICAD[OA]S?|DOMICILIO).*?(?:EN\s+(?:LA\s+CIUDAD\s+DE|COMUNA\s+DE|LOCALIDAD\s+DE)\s+([^\.,\n]+))', detalle_texto, re.IGNORECASE)
    if m_ub:
        ubic_global = m_ub.group(1).strip(".,;: ")
    else:
        m_ub_gen = re.search(r'(?:UBICAD[OA]S?|DOMICILIO)[^,\.]*?(?:EN\s+(?:EL\s+DOMICILIO[^\.]*?))', detalle_texto, re.IGNORECASE)
        if m_ub_gen:
            ubic_global = "En domicilio del deudor"

    # Proteger slashes dentro de paréntesis ej: (SINIESTRADAS / NO OPERATIVAS)
    def proteger_parentesis(match):
        return match.group(0).replace('/', '---SLASH---')
    texto_prep = re.sub(r'\([^\)]+\)', proteger_parentesis, detalle_texto)

    # Segmentación por posibles vehículos enumerados o delimitados
    if re.search(r'\b\d+\)\s+[A-Za-z]', texto_prep):
        partes = re.split(r'(?=\b\d+\)\s+)', texto_prep)
    elif '/' in texto_prep:
        partes = re.split(r'\s*/\s*', texto_prep)
    elif ';' in texto_prep and re.search(r'PATENTE|PPU|PLACA|AÑO', texto_prep, re.IGNORECASE):
        partes = texto_prep.split(';')
    elif re.search(r'(?<=[A-Za-z0-9\.\)])\s+(?=0\d\s+[A-ZÁÉÍÓÚ])', texto_prep):
        partes = re.split(r'(?<=[A-Za-z0-9\.\)])\s+(?=0\d\s+[A-ZÁÉÍÓÚ])', texto_prep)
    else:
        partes = re.split(
            r'(?<=[A-Za-z0-9\.\-\)])\s+(?=(?:\d+\s+)?(?:UN\s+|UNA\s+)?(?:VEH[IÍ]CULOS?|AUTOM[OÓ]VILES?|AUTOS?|CAMIONETAS?|STATION\s+WAGONS?|MOTOS?(?:CICLETAS?)?|FURGONES?|FURG[OÓ]NES?|CAMIONES?|SEMIREMOLQUES?|REMOLQUES?|TRACTORES?|EXCAVADORAS?|RETROEXCAVADORAS?|MINIBUSES?|BUSES?)\b)',
            texto_prep,
            flags=re.IGNORECASE
        )

    segmentos_raw = [p.replace('---SLASH---', '/').strip() for p in partes if p.strip()]

    # Si algún segmento aún contiene "01 CAMION... 02 ...", subdividirlo
    segmentos = []
    for s in segmentos_raw:
        subpartes = re.split(r'(?<=[A-Za-z0-9\.\)])\s+(?=0\d\s+[A-ZÁÉÍÓÚ])', s)
        for sp in subpartes:
            if sp.strip():
                segmentos.append(sp.strip())

    patron_tipo_especifico = re.compile(
        r'\b(STATION\s+WAGON|AUTOM[OÓ]VIL(?:ES)?|AUTO(?:S)?|CAMIONETA(?:S)?|MOTOCICLETA(?:S)?|MOTO(?:S)?|FURG[OÓ]N(?:ES)?|CAMI[OÓ]N(?:ES)?|SEMIREMOLQUE(?:S)?|REMOLQUE(?:S)?|TRACTOR(?:ES)?|EXCAVADORA(?:S)?|RETROEXCAVADORA(?:S)?|MINIB[UÚ]S(?:ES)?|BUS(?:ES)?)\b',
        re.IGNORECASE
    )

    vehiculos = []
    for seg in segmentos:
        # 1. Detección de Patente Chilena (PPU)
        ppu_match = re.search(
            r'(?:PATENTE|PPU|PLACA(?: PATENTE)?|INSCRIPCI[OÓ]N\s+(?:RNVM|N[°º]))[:\s]*([A-Z]{2,4}[\.\-\s]*\d{2,4}(?:[\.\-][0-9Kk])?)',
            seg,
            re.IGNORECASE
        )
        patente = ""
        if ppu_match:
            cand = re.sub(r'[^A-Z0-9\.\-]', '', ppu_match.group(1).upper().strip())
            # Validar formato de patente chilena: 2 a 4 letras + 2 a 4 números
            if re.match(r'^[A-Z]{2,4}[\.\-]?[0-9]{2,4}(?:[\.\-]?[0-9K])?$', cand) and not cand.endswith('.5') and len(cand) >= 5:
                patente = cand
        else:
            alt_ppu = re.search(r'\b([A-Z]{4}[\.\-\s]*\d{2}(?:[\.\-][0-9Kk])?|[A-Z]{2}[\.\-\s]*\d{4}(?:[\.\-][0-9Kk])?)\b', seg)
            if alt_ppu and any(w in seg.upper() for w in ['AÑO', 'MARCA', 'MODELO', 'COLOR', 'MOTOR', 'CHASIS']):
                cand = re.sub(r'[^A-Z0-9\.\-]', '', alt_ppu.group(1).upper().strip())
                if not cand.endswith('.5') and len(cand) >= 5:
                    patente = cand

        anio_match = re.search(r'\bAÑO[:\s]*([12]\d{3})\b', seg, re.IGNORECASE)
        anio = anio_match.group(1) if anio_match else ""

        tipo_esp = patron_tipo_especifico.search(seg)

        # FILTRO ESTRICTO CONTRA FALSOS POSITIVOS:
        if not patente and not tipo_esp:
            continue
        if not patente and not anio and not tipo_esp:
            continue
        if not patente and not anio and not any(w in seg.upper() for w in ['MOTOR', 'CHASIS', 'KILOMETRAJE', 'CILINDRADA', 'TRANSMISIÓN', 'CARROCERÍA', 'EXCAVADORA']):
            continue

        # Definir tipo legible
        if tipo_esp:
            tipo_raw = tipo_esp.group(0).upper().strip()
            if "STATION" in tipo_raw:
                tipo_vehiculo = "STATION WAGON / SUV"
            elif "AUTOM" in tipo_raw or "AUTO" in tipo_raw:
                tipo_vehiculo = "AUTOMÓVIL"
            elif "MOTO" in tipo_raw:
                tipo_vehiculo = "MOTOCICLETA"
            elif "CAMIONETA" in tipo_raw:
                tipo_vehiculo = "CAMIONETA"
            elif "FURG" in tipo_raw:
                tipo_vehiculo = "FURGÓN"
            elif "CAMI" in tipo_raw:
                tipo_vehiculo = "CAMIÓN"
            elif "SEMIREMOLQUE" in tipo_raw:
                tipo_vehiculo = "SEMIREMOLQUE"
            elif "REMOLQUE" in tipo_raw:
                tipo_vehiculo = "REMOLQUE"
            elif "EXCAVADORA" in tipo_raw:
                tipo_vehiculo = "EXCAVADORA"
            else:
                tipo_vehiculo = tipo_raw
        else:
            tipo_vehiculo = "AUTOMÓVIL" if patente else "VEHÍCULO"

        # 2. Color del vehículo
        color = ""
        m_col = re.search(r'\bCOLOR[:\s]+([A-ZÁÉÍÓÚ\s]+?)(?=,\s*PLACA|,\s*PATENTE|,\s*PPU|\s+PLACA|\s+A\s+LA\s+VISTA|\s+EN\s+EL\s+ESTADO|\s+AÑO|\s+POSTURA|\s+M[IÍ]NIM|\.|$)', seg, re.IGNORECASE)
        if m_col:
            raw_col = m_col.group(1).strip()
            if 3 <= len(raw_col) <= 25 and not any(w in raw_col.upper() for w in ['MARCA', 'MODELO', 'AUTOMOVIL', 'POSTURA', 'MINIMO']):
                color = raw_col.title()

        # 3. Transmisión y Tracción
        transmision = ""
        if re.search(r'\b(?:AUTOM[AÁ]TIC[OA]|AUT|AT)\b', seg, re.IGNORECASE):
            transmision = "Automática"
        elif re.search(r'\b(?:MEC[AÁ]NIC[OA]|MANUAL|MT)\b', seg, re.IGNORECASE):
            transmision = "Manual"

        traccion = ""
        if re.search(r'\b(?:4X4|4WD|AWD)\b', seg, re.IGNORECASE):
            traccion = "4x4"
        elif re.search(r'\b(?:4X2|2WD)\b', seg, re.IGNORECASE):
            traccion = "4x2"

        # 4. Combustible
        combustible = ""
        if re.search(r'\b(?:DI[EÉ]SEL|PETR[OÓ]LEO)\b', seg, re.IGNORECASE):
            combustible = "Diésel"
        elif re.search(r'\b(?:BENCIN[AERO]|GASOLINA)\b', seg, re.IGNORECASE):
            combustible = "Bencina"
        elif re.search(r'\b(?:H[IÍ]BRID[OA])\b', seg, re.IGNORECASE):
            combustible = "Híbrido"
        elif re.search(r'\b(?:VEH[IÍ]CULO|AUTO|MOTO(?:CICL)?|MOTOR)?\s*EL[EÉ]CTRIC[OA]\b|\b100%\s*EL[EÉ]CTRIC[OA]\b', seg, re.IGNORECASE) and not re.search(r'\b(?:HERVIDOR|ESTUFA|HORNO|TALADRO|PLANCHA)\s+EL[EÉ]CTRIC[OA]\b', seg, re.IGNORECASE):
            combustible = "Eléctrico"

        # 5. Kilometraje
        km = ""
        m_km = re.search(r'\b([0-9\.\,]+)\s*(?:MIL\s+)?KM[S]?\b', seg, re.IGNORECASE)
        if m_km:
            km = f"{m_km.group(1)} km"

        afecto_iva = iva_global or bool(re.search(r'\bAFECT[AO]\s+A\s+IVA\b|\+\s*IVA', seg, re.IGNORECASE))
        zona_franca = zf_global or bool(re.search(r'\bZONA\s+FRANCA\b', seg, re.IGNORECASE))

        m_ub_seg = re.search(r'(?:UBICAD[OA]S?|DOMICILIO).*?(?:EN\s+(?:LA\s+CIUDAD\s+DE|COMUNA\s+DE|LOCALIDAD\s+DE)\s+([^\.,\n]+))', seg, re.IGNORECASE)
        ubic_auto = m_ub_seg.group(1).strip(".,;: ") if m_ub_seg else ubic_global

        # 6. Observaciones de estado mecánico
        obs = []
        if re.search(r'\bA\s+LA\s+VISTA\b', seg, re.IGNORECASE):
            obs.append("A la vista")
        if re.search(r'\bEN\s+EL\s+ESTADO\s+EN\s+QUE\s+SE\s+ENCUENTRA\b', seg, re.IGNORECASE):
            obs.append("En el estado en que se encuentra")
        if re.search(r'\bSINIESTRAD[OA]\b', seg, re.IGNORECASE):
            obs.append("Siniestrado")
        if re.search(r'\bNO\s+OPERATIV[OA]\b', seg, re.IGNORECASE):
            obs.append("No operativo")
        if re.search(r'\bDESARME\b', seg, re.IGNORECASE):
            obs.append("Para desarme")
        if re.search(r'\bSIN\s+BALDE\b', seg, re.IGNORECASE):
            obs.append("Sin balde")
        if re.search(r'\bMAL\s+ESTADO\b', seg, re.IGNORECASE):
            obs.append("Mal estado")
        estado_mecanico = ", ".join(obs) if obs else ""

        # 7. Postura mínima (Soporte masculino y femenino: MÍNIMO o MÍNIMA)
        min_match = re.search(r'(?:POSTURA\s+)?M[IÍ]NIM[OA][:.\s]*\$?\s*([0-9\.\,]+)', seg, re.IGNORECASE)
        minimo_auto = ""
        if min_match:
            val = min_match.group(1).strip().rstrip(".")
            if len(val) >= 4:
                minimo_auto = f"$ {val}"

        motor_match = re.search(r'MOTOR\s+(?:N[°º\.]*\s*)?([A-Z0-9\-]+)', seg, re.IGNORECASE)
        chasis_match = re.search(r'CHASIS\s+(?:N[°º\.]*\s*)?([A-Z0-9\-]+)', seg, re.IGNORECASE)
        motor = motor_match.group(1).strip() if motor_match else ""
        chasis = chasis_match.group(1).strip() if chasis_match else ""

        # Limpiar título del vehículo removiendo jerigonza judicial
        texto_limpio = seg
        texto_limpio = re.sub(
            r'^(?:BIEN(?:ES)?\s+(?:MUEBLE(?:S)?\s+)?A\s+SUBASTAR(?:\s+CON\s+POSTURA\s+M[IÍ]NIMA)?[:\s]*|'
            r'VEH[IÍ]CULO(?:S)?\s+A\s+SUBASTAR(?:\s+CON\s+POSTURA\s+M[IÍ]NIMA)?[:\s]*|'
            r'BIEN(?:ES)?\s+A\s+SUBASTAR(?:\s+SIN\s+POSTURA\s+M[IÍ]NIMA)?[:\s]*|'
            r'POSTURA\s+M[IÍ]NIMA[:\s]*|'
            r'LOTE\s+(?:N[°º\.]*\s*)?\d+[:\s]*|'
            r'\d+[\)\.\-]\s*|'
            r'(?:UN|UNA)\s+VEH[IÍ]CULO\s+|'
            r'(?:UN|UNA)\s+)',
            '',
            texto_limpio,
            flags=re.IGNORECASE,
        ).strip()

        corte_match = re.search(
            r'(?:,\s*PATENTE|,\s*PPU|\s+PPU:|\s+PATENTE:|\s+PLACA\b|\s+LLEVAR[AÁ]\s+COMO|\s+M[IÍ]NIM[OA]|\s+EL\s+ADJUDICATARIO|\.\s+EL\s+REMATE|,\s*EN\s+EL\s+ESTADO|,\s*A\s+LA\s+VISTA)',
            texto_limpio,
            re.IGNORECASE,
        )
        if corte_match:
            titulo_vehiculo = texto_limpio[:corte_match.start()].strip(" ,;.-")
        else:
            titulo_vehiculo = texto_limpio.split(".")[0].strip(" ,;.-")

        anexos = []
        anexos_match = re.search(r'(?:ADJUDICACI[OÓ]N\s+DE\s+LOS\s+SIGUIENTES\s+BIENES|SIGUIENTES\s+ENSERES)[:\s]*(.*?)(?=\.\s*BIENES|\.$|$)', seg, re.IGNORECASE)
        if anexos_match:
            raw_anexos = anexos_match.group(1).strip()
            anexos = [p.strip() for p in re.split(r'\d+\s+', raw_anexos) if len(p.strip()) > 3]

        vehiculos.append({
            "tipo": tipo_vehiculo,
            "titulo": titulo_vehiculo,
            "patente": patente,
            "anio": anio,
            "minimo": minimo_auto,
            "color": color,
            "transmision": transmision,
            "traccion": traccion,
            "combustible": combustible,
            "kilometraje": km,
            "afecto_iva": afecto_iva,
            "zona_franca": zona_franca,
            "ubicacion_fisica": ubic_auto,
            "estado_mecanico": estado_mecanico,
            "motor": motor,
            "chasis": chasis,
            "bienes_anexos": anexos,
        })

    # Si hay 1 solo vehículo y no tenía mínimo individual, heredar el valor mínimo del remate si existe
    if len(vehiculos) == 1 and not vehiculos[0]["minimo"] and valor_minimo_remate:
        min_clean = valor_minimo_remate.strip()
        if min_clean and "sin mínimo" not in min_clean.lower() and re.search(r'[1-9]', min_clean):
            vehiculos[0]["minimo"] = min_clean

    return vehiculos


def es_articulo_vehiculo(art: str, vehs: List[Dict[str, Any]]) -> bool:
    """
    Determina si un artículo desglosado corresponde en realidad a un vehículo ya identificado
    o a cláusulas residuales/legales del vehículo para evitar duplicaciones en el catálogo y WhatsApp.
    """
    if not vehs or not art:
        return False
    art_strip = art.strip()
    art_low = art_strip.lower()

    # Cláusulas legales / técnicas o de estado que a menudo quedan separadas por comas en lotes de vehículos
    if re.search(r'^(?:AFECT[AO]\s+A\s+IVA|UBICAD[OA]S?|EN\s+(?:LA\s+CIUDAD|EL\s+DOMICILIO)|LIMITACI[OÓ]N\s+AL\s+DOMINIO|EN\s+EL\s+ESTADO|A\s+LA\s+VISTA|ZONA\s+FRANCA|PLACA|PATENTE|PPU)', art_strip, re.IGNORECASE):
        return True

    for v in vehs:
        # 1. Coincidencia por patente
        if v.get("patente") and v.get("patente").lower() in art_low:
            return True
        # 2. Coincidencia por título completo o inclusión mutua
        v_titulo = v.get("titulo", "").lower()
        if v_titulo and (v_titulo in art_low or art_low in v_titulo):
            return True
        # 3. Coincidencia de múltiples palabras clave identificadoras del vehículo
        palabras = [
            w for w in v_titulo.split()
            if len(w) > 3 and w not in ["marca", "modelo", "color", "para", "este", "esta", "subasta", "remate", "minimo", "postura"]
        ]
        if palabras:
            coincidencias = sum(1 for w in palabras if w in art_low)
            if coincidencias >= 2 or (len(palabras) == 1 and palabras[0] in art_low and len(palabras[0]) > 4):
                return True
    return False


def exportar_whatsapp_txt(resultados: List[Dict[str, str]], filename: str = "resumen_whatsapp.txt"):
    """
    Genera un archivo de texto con formato enriquecido para WhatsApp (negritas, viñetas y emojis)
    ideal para copiar y enviar en un mensaje al abuelo o familiares en su smartphone.
    """
    if not resultados:
        return

    lineas = [
        "🔔 *CATÁLOGO DE REMATES VIGENTES* 🔔",
        f"📅 Actualizado: {obtener_ahora_chile().strftime('%d/%m/%Y %H:%M')}\n",
    ]

    for i, r in enumerate(resultados, start=1):
        comuna_raw = r.get("comuna_remate", "No informado").strip()
        comuna = comuna_raw.title() if comuna_raw.lower() != "no informado" else "Chile (Sin especificar)"
        fecha = r.get("fecha_remate", "")
        direccion = r.get("direccion_remate", "")
        modalidad = r.get("modalidad", "PRESENCIAL")
        minimo = r.get("valor_minimo", "Sin mínimo ($ 0)")
        comision = r.get("comision", "No informado")
        if comision.startswith(","):
            comision = f"0{comision}"
        articulos = desglosar_articulos(r.get("detalle", ""))
        webs = extraer_sitios_web(r.get("martillero_web", ""), direccion, r.get("detalle", ""))
        vehs = extraer_vehiculos(r.get("detalle", ""), minimo)

        lineas.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lineas.append(f"🏢 *OPCIÓN #{i} - {comuna.upper()}*")
        lineas.append(f"📅 *Cuándo:* {fecha} hrs" if fecha else "📅 *Cuándo:* Por confirmar")
        lineas.append(f"📍 *Dónde:* {direccion}")
        lineas.append(f"🏷️ *Modalidad:* {modalidad}")
        if webs:
            urls_str = " | ".join(w["url"] for w in webs)
            lineas.append(f"🌐 *Sitio Web:* {urls_str}")
        elif modalidad == "PRESENCIAL" and direccion and not direccion.lower().startswith("online") and direccion.lower() != "no informado":
            import urllib.parse
            maps_q = urllib.parse.quote(f"{direccion}, {comuna}, Chile")
            lineas.append(f"🗺️ *Mapa:* https://www.google.com/maps/search/?api=1&query={maps_q}")
        elif modalidad in ["ONLINE", "MIXTO"]:
            lineas.append(f"🏛️ *Portal:* https://www.boletinconcursal.cl/boletin/verificacion (Cód: {r.get('codigo_ficha') or r.get(chr(65279) + 'codigo_ficha', '')})")
        lineas.append(f"💰 *Mínimo:* {minimo} (Comisión: {comision})")

        if vehs:
            lineas.append(f"🚗 *Vehículos en subasta ({len(vehs)}):*")
            for v in vehs:
                ppu_txt = f" | Patente: *{v['patente']}*" if v.get('patente') else ""
                anio_txt = f" | Año: *{v['anio']}*" if v.get('anio') else ""
                min_auto_txt = f" | Mínimo: *{v['minimo']}*" if v.get('minimo') else ""
                col_txt = f" | Color: *{v['color']}*" if v.get('color') else ""
                trans_txt = f" | Caja: *{v['transmision']}*" if v.get('transmision') else ""
                trac_txt = f" | Tracción: *{v['traccion']}*" if v.get('traccion') else ""
                comb_txt = f" | Combustible: *{v['combustible']}*" if v.get('combustible') else ""
                km_txt = f" | Km: *{v['kilometraje']}*" if v.get('kilometraje') else ""
                lineas.append(f"  • 🚘 {v['titulo']}{anio_txt}{ppu_txt}{min_auto_txt}{col_txt}{trans_txt}{trac_txt}{comb_txt}{km_txt}")
                if v.get('afecto_iva'):
                    lineas.append("    ⚠️ *AFECTO A IVA (+19%)*")
                if v.get('zona_franca'):
                    lineas.append("    ⚠️ *RESTRICCIÓN ZONA FRANCA*")
                if v.get('ubicacion_fisica'):
                    lineas.append(f"    📍 *Ubicación del auto:* {v['ubicacion_fisica']}")
                if v.get('estado_mecanico'):
                    lineas.append(f"    🔧 *Estado:* {v['estado_mecanico']}")

        otros_articulos = [a for a in articulos if not es_articulo_vehiculo(a, vehs)]
        if otros_articulos:
            lineas.append("📦 *Artículos adicionales:*" if vehs else "📦 *Artículos a rematar:*")
            for art in otros_articulos[:6]:
                lineas.append(f"  • {art}")
            if len(otros_articulos) > 6:
                lineas.append(f"  • _(+{len(otros_articulos)-6} cosas más)_")
        lineas.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas))
    print(f"[+] Resumen para WhatsApp generado con éxito: {filename}")


def exportar_html(resultados: List[Dict[str, str]], filename: str = "remates_visual.html"):
    """
    Genera una página web visual y accesible (HTML) de alta estética y rendimiento:
    - Tipografía moderna (Outfit & Inter) con nitidez visual en cualquier pantalla.
    - Zoom proporcional real para personas mayores vía :root.
    - Tarjetas claras con iconos, fechas naturales y viñetas de artículos sin duplicados.
    - Tarjetas de vehículos destacadas con Patente Chilena (PPU), año, alertas y ubicación física.
    - Botones directos con enlaces a los sitios web oficiales del remate.
    - Buscador interactivo instantáneo con botón de limpieza rápida.
    - Diseño 100% responsive optimizado para smartphones y PC.
    """
    if not resultados:
        return

    import json

    remates_data = []
    for r in resultados:
        # Limpieza de valor mínimo
        minimo_raw = r.get("valor_minimo", "Sin mínimo").strip()
        if not re.search(r"[1-9]", minimo_raw):
            minimo_raw = "Sin mínimo ($ 0)"

        webs = extraer_sitios_web(r.get("martillero_web", ""), r.get("direccion_remate", ""), r.get("detalle", ""))
        vehs = extraer_vehiculos(r.get("detalle", ""), minimo_raw)
        todos_articulos = desglosar_articulos(r.get("detalle", ""))

        # Filtrar artículos que ya sean los vehículos detallados para evitar duplicación
        articulos_filtrados = [art for art in todos_articulos if not es_articulo_vehiculo(art, vehs)]

        # Limpieza de comuna y región
        comuna_raw = r.get("comuna_remate", "No informado").strip()
        comuna_limpia = comuna_raw.title() if comuna_raw.lower() != "no informado" else "Chile (Sin especificar)"
        region_raw = r.get("region_remate", "").strip()

        # Limpieza de comisión
        comision_raw = r.get("comision", "No informado").strip()
        if comision_raw.startswith(","):
            comision_raw = f"0{comision_raw}"
        if comision_raw == "0,00 %":
            comision_raw = "0% (Sin comisión adicional)"

        codigo_val = r.get("codigo_ficha") or r.get("\ufeffcodigo_ficha", "")
        remates_data.append({
            "codigo": codigo_val,
            "comuna": comuna_limpia,
            "region": region_raw,
            "direccion": r.get("direccion_remate", "No informado"),
            "fecha": r.get("fecha_remate", ""),
            "modalidad": r.get("modalidad", "PRESENCIAL"),
            "valor_minimo": minimo_raw,
            "comision": comision_raw,
            "tribunal": r.get("tribunal", ""),
            "rol": r.get("rol_causa", ""),
            "deudor": r.get("deudor", ""),
            "detalle": r.get("detalle", ""),
            "webs": webs,
            "vehiculos": vehs,
            "articulos": articulos_filtrados,
        })

    remates_json = json.dumps(remates_data, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Catálogo de Remates Vigentes</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary-dark: #0f172a;
      --primary-blue: #1e3a8a;
      --accent: #0284c7;
      --accent-hover: #0369a1;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #1e293b;
      --text-muted: #64748b;
      --green-bg: #dcfce7;
      --green-text: #15803d;
      --border: #e2e8f0;
      --border-focus: #38bdf8;
      --font-scale: 1;
      font-size: calc(16px * var(--font-scale));
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.6;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 1120px; margin: 0 auto; }}
    header {{
      background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 55%, #0369a1 100%);
      color: white;
      padding: 32px 36px;
      border-radius: 24px;
      margin-bottom: 24px;
      box-shadow: 0 12px 30px -8px rgba(15, 23, 42, 0.25);
      position: relative;
      overflow: hidden;
    }}
    header::after {{
      content: '';
      position: absolute;
      top: -50%;
      right: -20%;
      width: 400px;
      height: 400px;
      background: radial-gradient(circle, rgba(56, 189, 248, 0.15) 0%, rgba(255,255,255,0) 70%);
      pointer-events: none;
    }}
    .header-top {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; position: relative; z-index: 1; }}
    h1 {{ font-size: 2.1rem; font-weight: 800; letter-spacing: -0.5px; }}
    .subtitulo {{ font-size: 1.12rem; color: #bae6fd; margin-top: 10px; max-width: 820px; font-weight: 400; position: relative; z-index: 1; }}
    .contador-badge {{
      background: rgba(255, 255, 255, 0.95);
      color: #0369a1;
      font-weight: 800;
      padding: 8px 20px;
      border-radius: 9999px;
      font-size: 1.05rem;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      background: white;
      padding: 18px 24px;
      border-radius: 18px;
      border: 1px solid var(--border);
      margin-bottom: 28px;
      box-shadow: 0 4px 16px -2px rgba(15, 23, 42, 0.05);
    }}
    .search-box {{ flex: 1; min-width: 280px; position: relative; display: flex; align-items: center; }}
    .search-icon {{ position: absolute; left: 16px; font-size: 1.25rem; color: #94a3b8; pointer-events: none; }}
    .search-box input {{
      width: 100%;
      padding: 14px 44px 14px 48px;
      font-size: 1.08rem;
      font-family: inherit;
      border: 2px solid var(--border);
      border-radius: 14px;
      outline: none;
      background: #f8fafc;
      color: var(--text);
      transition: all 0.2s ease;
    }}
    .search-box input:focus {{
      border-color: var(--accent);
      background: #ffffff;
      box-shadow: 0 0 0 4px rgba(2, 132, 199, 0.12);
    }}
    .btn-clear-search {{
      position: absolute;
      right: 14px;
      background: #e2e8f0;
      border: none;
      width: 26px;
      height: 26px;
      border-radius: 50%;
      display: none;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: bold;
      color: #475569;
      transition: all 0.15s;
    }}
    .btn-clear-search:hover {{ background: #cbd5e1; color: #0f172a; }}
    .actions-group {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .btn-print {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 12px 20px;
      font-size: 1rem;
      font-weight: 700;
      font-family: inherit;
      border-radius: 12px;
      cursor: pointer;
      border: none;
      background: linear-gradient(135deg, #059669 0%, #047857 100%);
      color: white;
      box-shadow: 0 3px 10px rgba(5, 150, 105, 0.25);
      transition: all 0.2s ease;
    }}
    .btn-print:hover {{ transform: translateY(-1px); box-shadow: 0 5px 14px rgba(5, 150, 105, 0.35); }}
    .font-controls {{
      display: inline-flex;
      align-items: center;
      background: #f1f5f9;
      padding: 4px;
      border-radius: 12px;
      border: 1px solid var(--border);
    }}
    .font-btn {{
      background: white;
      border: 1px solid #cbd5e1;
      padding: 7px 14px;
      font-size: 0.98rem;
      font-weight: 800;
      font-family: inherit;
      border-radius: 8px;
      cursor: pointer;
      margin: 0 2px;
      color: #334155;
      transition: all 0.15s ease;
    }}
    .font-btn:hover {{ background: #e2e8f0; color: #0f172a; }}
    .grid-remates {{ display: flex; flex-direction: column; gap: 26px; }}
    .card {{
      background: var(--card-bg);
      border-radius: 20px;
      border: 2px solid var(--border);
      padding: 28px;
      box-shadow: 0 6px 18px -4px rgba(15, 23, 42, 0.05);
      transition: all 0.25s ease;
    }}
    .card:hover {{
      border-color: #cbd5e1;
      box-shadow: 0 12px 28px -6px rgba(15, 23, 42, 0.09);
      transform: translateY(-2px);
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 14px;
      border-bottom: 2px dashed #f1f5f9;
      padding-bottom: 18px;
      margin-bottom: 20px;
    }}
    .card-title {{ font-size: 1.45rem; font-weight: 800; color: var(--primary-dark); display: flex; align-items: center; gap: 8px; }}
    .badges {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .badge {{ font-size: 0.95rem; font-weight: 700; padding: 6px 14px; border-radius: 10px; display: inline-flex; align-items: center; gap: 6px; }}
    .badge-vigente {{ background-color: var(--green-bg); color: var(--green-text); border: 1px solid #bbf7d0; }}
    .badge-modalidad {{ background-color: #fef08a; color: #854d0e; border: 1px solid #fde047; }}
    .badge-precio {{ background-color: #e0e7ff; color: #3730a3; border: 1px solid #c7d2fe; }}
    .badge-precio-cero {{ background-color: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }}
    .datos-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 18px;
      margin-bottom: 22px;
      background-color: #f8fafc;
      padding: 20px;
      border-radius: 16px;
      border: 1px solid #f1f5f9;
    }}
    .dato-item {{ display: flex; flex-direction: column; }}
    .dato-label {{ font-size: 0.88rem; font-weight: 800; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
    .dato-valor {{ font-size: 1.15rem; font-weight: 700; color: #0f172a; word-break: break-word; }}
    .dato-valor.resaltado {{ color: #1d4ed8; font-size: 1.25rem; font-weight: 800; }}
    .btn-sitio-web {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%);
      color: white !important;
      text-decoration: none;
      padding: 7px 16px;
      border-radius: 10px;
      font-size: 0.96rem;
      font-weight: 800;
      box-shadow: 0 3px 8px rgba(2, 132, 199, 0.3);
      margin: 4px 6px 4px 0;
      transition: all 0.2s ease;
    }}
    .btn-sitio-web:hover {{
      background: #0284c7;
      transform: translateY(-1px);
      box-shadow: 0 6px 14px rgba(2, 132, 199, 0.4);
    }}
    .badge-pdf-link {{
      background: #0f172a;
      color: white !important;
      text-decoration: none;
      font-size: 0.92rem;
      font-weight: 800;
      padding: 6px 14px;
      border-radius: 10px;
      border: 1px solid #334155;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      box-shadow: 0 3px 8px rgba(15, 23, 42, 0.2);
      transition: all 0.2s ease;
    }}
    .badge-pdf-link:hover {{
      background: #1e3a8a;
      border-color: #38bdf8;
      transform: translateY(-1px);
      box-shadow: 0 5px 12px rgba(30, 58, 138, 0.3);
    }}
    .btn-sitio-web.btn-mapa {{
      background: linear-gradient(135deg, #059669 0%, #047857 100%);
      box-shadow: 0 3px 8px rgba(5, 150, 105, 0.3);
    }}
    .btn-sitio-web.btn-mapa:hover {{
      background: #059669;
      box-shadow: 0 6px 14px rgba(5, 150, 105, 0.4);
    }}
    .btn-sitio-web.btn-verificacion {{
      background: linear-gradient(135deg, #475569 0%, #334155 100%);
      box-shadow: 0 3px 8px rgba(71, 85, 105, 0.3);
    }}
    .btn-sitio-web.btn-verificacion:hover {{
      background: #334155;
      box-shadow: 0 6px 14px rgba(71, 85, 105, 0.4);
    }}
    .link-mapa {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: #0369a1;
      font-weight: 800;
      font-size: 0.92rem;
      text-decoration: none;
      margin-left: 8px;
      padding: 3px 10px;
      background: #e0f2fe;
      border: 1px solid #bae6fd;
      border-radius: 8px;
      transition: all 0.15s ease;
    }}
    .link-mapa:hover {{
      background: #bae6fd;
      color: #0284c7;
    }}
    .btn-descarga-pdf {{
      background: #0284c7;
      color: white !important;
      text-decoration: none;
      font-size: 0.88rem;
      font-weight: 800;
      padding: 5px 12px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      box-shadow: 0 2px 6px rgba(2, 132, 199, 0.25);
      transition: all 0.15s ease;
    }}
    .btn-descarga-pdf:hover {{
      background: #0369a1;
      transform: translateY(-1px);
    }}
    /* Vehículos */
    .seccion-vehiculos {{
      margin-top: 18px;
      margin-bottom: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .seccion-vehiculos-titulo {{
      font-size: 1.2rem;
      font-weight: 800;
      color: var(--primary-dark);
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 2px;
    }}
    .card-vehiculo {{
      background: linear-gradient(145deg, #ffffff 0%, #f0f9ff 100%);
      border: 2px solid #bae6fd;
      border-left: 6px solid #0284c7;
      border-radius: 16px;
      padding: 20px 22px;
      box-shadow: 0 4px 12px rgba(2, 132, 199, 0.08);
    }}
    .vehiculo-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 14px;
      border-bottom: 1px dashed #cbd5e1;
      padding-bottom: 12px;
    }}
    .vehiculo-titulo {{
      font-size: 1.25rem;
      font-weight: 900;
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 8px;
      line-height: 1.4;
    }}
    .placa-patente-box {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: #ffffff;
      border: 2.5px solid #0f172a;
      border-radius: 10px;
      padding: 5px 14px;
      box-shadow: 0 3px 7px rgba(0,0,0,0.12);
    }}
    .placa-patente-texto {{
      font-family: 'Courier New', Courier, monospace;
      font-size: 1.35rem;
      font-weight: 900;
      letter-spacing: 2px;
      color: #0f172a;
    }}
    .btn-copiar-patente {{
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 5px 12px;
      font-size: 0.88rem;
      font-weight: 800;
      cursor: pointer;
      color: #334155;
      transition: all 0.15s ease;
    }}
    .btn-copiar-patente:hover {{ background: #e2e8f0; color: #0f172a; }}
    .vehiculo-datos-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .badge-auto-anio {{ background: #e0f2fe; color: #0369a1; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #bae6fd; }}
    .badge-auto-minimo {{ background: #dcfce7; color: #15803d; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #bbf7d0; }}
    .badge-auto-color {{ background: #fef3c7; color: #92400e; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #fde68a; }}
    .badge-auto-detalle {{ background: #f8fafc; color: #475569; font-weight: 700; font-size: 0.92rem; padding: 5px 10px; border-radius: 8px; border: 1px solid #e2e8f0; }}
    .alerta-iva {{ background: #fee2e2; color: #b91c1c; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #fca5a5; display: inline-flex; align-items: center; gap: 6px; }}
    .alerta-zona-franca {{ background: #f3e8ff; color: #6b21a8; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #d8b4fe; display: inline-flex; align-items: center; gap: 6px; }}
    .alerta-ubicacion-auto {{ background: #fffbeb; color: #92400e; font-weight: 800; font-size: 1.02rem; padding: 10px 16px; border-radius: 10px; border: 1px solid #fde68a; margin-top: 8px; display: flex; align-items: center; gap: 10px; }}
    .alerta-estado-auto {{ background: #ffedd5; color: #9a3412; font-weight: 800; font-size: 0.96rem; padding: 6px 12px; border-radius: 8px; border: 1px solid #fed7aa; }}
    .vehiculo-anexos {{ margin-top: 8px; padding: 10px 16px; background: #ffffff; border-radius: 10px; font-size: 0.98rem; color: #475569; border: 1px solid #e2e8f0; }}
    /* Artículos */
    .seccion-articulos {{ margin-top: 14px; }}
    .seccion-articulos h4 {{ font-size: 1.15rem; font-weight: 800; color: var(--primary-dark); margin-bottom: 12px; }}
    .lista-articulos {{ list-style: none; display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; }}
    .articulo-item {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      padding: 11px 16px;
      border-radius: 12px;
      font-size: 1rem;
      display: flex;
      gap: 10px;
      align-items: flex-start;
      box-shadow: 0 2px 5px rgba(0,0,0,0.02);
    }}
    .articulo-icono {{ color: #10b981; font-weight: 900; font-size: 1.1rem; flex-shrink: 0; line-height: 1.4; }}
    .articulo-texto {{ font-weight: 700; color: #1e293b; line-height: 1.4; }}
    /* Acordeón de Descripción Oficial del PDF */
    .detalle-acordeon {{
      margin-top: 18px;
      border: 1.5px solid #e2e8f0;
      border-radius: 14px;
      background: #f8fafc;
      overflow: hidden;
      transition: all 0.2s ease;
    }}
    .detalle-acordeon[open] {{
      border-color: #93c5fd;
      background: #ffffff;
      box-shadow: 0 4px 14px rgba(2, 132, 199, 0.08);
    }}
    .detalle-summary {{
      padding: 12px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: pointer;
      font-size: 0.98rem;
      font-weight: 700;
      color: #334155;
      user-select: none;
      background: #f1f5f9;
      transition: background 0.15s ease;
    }}
    .detalle-summary:hover {{
      background: #e2e8f0;
      color: #0f172a;
    }}
    .detalle-summary::-webkit-details-marker {{
      display: none;
    }}
    .detalle-summary-title {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .detalle-summary-toggle {{
      font-size: 0.85rem;
      color: #0284c7;
      background: #e0f2fe;
      padding: 4px 10px;
      border-radius: 8px;
      font-weight: 800;
    }}
    .detalle-contenido-box {{
      padding: 16px 20px;
      border-top: 1px solid #e2e8f0;
      font-size: 0.96rem;
      line-height: 1.6;
      color: #334155;
      background: #fafafa;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .card-footer {{
      margin-top: 20px;
      padding-top: 16px;
      border-top: 1px solid #f1f5f9;
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      font-size: 0.94rem;
      color: var(--text-muted);
    }}
    /* Responsive Breakpoints */
    @media (max-width: 768px) {{
      body {{ padding: 12px 10px; }}
      header {{ padding: 22px 18px; border-radius: 18px; margin-bottom: 16px; }}
      h1 {{ font-size: 1.6rem; }}
      .subtitulo {{ font-size: 1rem; }}
      .toolbar {{ padding: 14px 16px; gap: 12px; border-radius: 14px; margin-bottom: 20px; }}
      .search-box {{ min-width: 100%; }}
      .actions-group {{ width: 100%; justify-content: space-between; }}
      .card {{ padding: 18px 16px; border-radius: 16px; }}
      .card-title {{ font-size: 1.25rem; }}
      .datos-grid {{ grid-template-columns: 1fr; padding: 14px; gap: 12px; }}
      .card-vehiculo {{ padding: 16px 14px; border-radius: 14px; }}
      .vehiculo-header {{ flex-direction: column; align-items: flex-start; gap: 10px; }}
      .placa-patente-box {{ width: 100%; justify-content: space-between; }}
      .lista-articulos {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 480px) {{
      body {{ padding: 8px 6px; }}
      header {{ padding: 18px 14px; border-radius: 14px; }}
      h1 {{ font-size: 1.4rem; }}
      .card {{ padding: 16px 12px; }}
      .card-header {{ gap: 10px; margin-bottom: 14px; padding-bottom: 14px; }}
      .badges {{ gap: 6px; }}
      .badge {{ font-size: 0.88rem; padding: 5px 10px; }}
      .btn-print {{ width: 100%; justify-content: center; }}
      .actions-group {{ flex-direction: column; align-items: stretch; }}
      .font-controls {{ justify-content: center; }}
    }}
    @media print {{
      body {{ background: white; color: black; font-size: 12pt; padding: 0; }}
      .toolbar, .font-controls, .btn-print, .search-box, #btn-buscar-vivo, #sel-tipo {{ display: none !important; }}
      header {{ background: none !important; color: black !important; border-bottom: 2px solid black; padding: 0 0 15px 0 !important; }}
      .subtitulo {{ color: #333 !important; }}
      .card {{ border: 1px solid #444 !important; break-inside: avoid; margin-bottom: 20px; box-shadow: none !important; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="header-top">
        <h1>📋 Catálogo de Remates Vigentes</h1>
        <span class="contador-badge" id="contador">✨ {len(remates_data)} encontrados</span>
      </div>
      <p class="subtitulo">Listado oficial de bienes muebles y oportunidades activas (electrodomésticos, herramientas, tecnología y vehículos en subasta concursal).</p>
    </header>

    <div class="toolbar">
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="buscador" placeholder="Buscar por ciudad, patente, marca o artículo (ej: Talca, Ford, televisor)..." oninput="filtrarTarjetas()">
        <button id="btn-limpiar" class="btn-clear-search" onclick="limpiarBuscador()" title="Limpiar búsqueda">✕</button>
      </div>
      <div class="actions-group">
        <div class="font-controls" title="Ajustar tamaño de letra para lectura cómoda">
          <button class="font-btn" onclick="cambiarTamano(-0.1)" title="Reducir letra">A -</button>
          <button class="font-btn" onclick="cambiarTamano(0)" title="Tamaño normal">A</button>
          <button class="font-btn" onclick="cambiarTamano(0.15)" title="Aumentar letra">A +</button>
        </div>
        <button class="btn-print" onclick="window.print()">🖨️ Imprimir</button>
      </div>
    </div>

    <div class="grid-remates" id="contenedor-remates"></div>
  </div>

  <script>
    const remates = {remates_json};

    function formatFecha(fechaStr) {{
      if (!fechaStr) return "Por confirmar";
      return `${{fechaStr}} hrs`;
    }}

    function renderizarRemates(lista) {{
      const contenedor = document.getElementById("contenedor-remates");
      const contador = document.getElementById("contador");
      contador.textContent = `✨ ${{lista.length}} remate${{lista.length === 1 ? '' : 's'}} disponible${{lista.length === 1 ? '' : 's'}}`;

      if (lista.length === 0) {{
        contenedor.innerHTML = `
          <div style="background: white; padding: 48px 24px; border-radius: 20px; text-align: center; border: 2px dashed #cbd5e1; margin-top: 10px;">
            <p style="font-size: 1.35rem; font-weight: 800; color: #1e293b; margin-bottom: 8px;">No se encontraron remates con ese criterio.</p>
            <p style="color: #64748b; font-size: 1.05rem; margin-bottom: 16px;">Prueba buscando por otra ciudad, tipo de bien o borra el texto de búsqueda.</p>
            <button onclick="limpiarBuscador()" style="padding: 10px 20px; background: #0284c7; color: white; border: none; border-radius: 10px; font-weight: 800; cursor: pointer;">
              🔄 Ver Todos los Remates
            </button>
          </div>
        `;
        return;
      }}

      contenedor.innerHTML = lista.map(r => {{
        const webs = r.webs || [];
        const vehs = r.vehiculos || [];
        const arts = r.articulos || [];
        const tieneMinimoCero = r.valor_minimo.includes("$ 0") || r.valor_minimo.toLowerCase().includes("sin mínimo");

        return `
        <article class="card">
          <div class="card-header">
            <h2 class="card-title">🏢 Remate en ${{r.comuna}}${{r.region ? ' (' + r.region + ')' : ''}}</h2>
            <div class="badges">
              <span class="badge badge-vigente">🟢 VIGENTE</span>
              <span class="badge badge-modalidad">📍 ${{r.modalidad}}</span>
              <span class="badge ${{tieneMinimoCero ? 'badge-precio-cero' : 'badge-precio'}}">
                ${{tieneMinimoCero ? '🟢 Sin Mínimo ($ 0)' : '💰 ' + r.valor_minimo}}
              </span>
              ${{r.codigo ? `<a href="/pdf/${{r.codigo}}" target="_blank" class="badge badge-pdf-link" title="Abrir documento PDF oficial del Boletín Concursal">📥 PDF Oficial ↗</a>` : ''}}
            </div>
          </div>
          <div class="datos-grid">
            <div class="dato-item">
              <span class="dato-label">📅 ¿Cuándo es el Remate?</span>
              <span class="dato-valor resaltado">${{formatFecha(r.fecha)}}</span>
            </div>
            <div class="dato-item">
              <span class="dato-label">📍 ¿Dónde se Realiza?</span>
              <span class="dato-valor">
                ${{r.direccion}}
                ${{(!r.direccion.toLowerCase().startsWith('online') && r.direccion.toLowerCase() !== 'no informado' && r.direccion.length > 3) ? `
                  <a href="https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(r.direccion + ', ' + r.comuna + ', Chile')}}" target="_blank" rel="noopener noreferrer" class="link-mapa" title="Abrir ubicación en Google Maps">🗺️ Mapa ↗</a>
                ` : ''}}
              </span>
            </div>
            <div class="dato-item">
              <span class="dato-label">🌐 Portal Oficial / Remate Online</span>
              <div class="dato-valor">
                ${{webs.length > 0
                  ? webs.map(w => `<a href="${{w.url}}" target="_blank" rel="noopener noreferrer" class="btn-sitio-web" title="Ir al portal oficial de remates">🔗 ${{w.nombre}} ↗</a>`).join(' ')
                  : (r.modalidad === 'PRESENCIAL'
                      ? `<a href="https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(r.direccion + ', ' + r.comuna + ', Chile')}}" target="_blank" rel="noopener noreferrer" class="btn-sitio-web btn-mapa" title="Ver dirección presencial en Google Maps">🏛️ Presencial (Ver en Mapa ↗)</a>`
                      : `<a href="https://www.boletinconcursal.cl/boletin/verificacion" target="_blank" rel="noopener noreferrer" class="btn-sitio-web btn-verificacion" title="Verificar publicación en el portal oficial del Boletín Concursal">🏛️ Verificación Boletín (Cód: ${{r.codigo}}) ↗</a>`
                    )
                }}
              </div>
            </div>
            <div class="dato-item">
              <span class="dato-label">⚖️ Tribunal / Causa</span>
              <span class="dato-valor">${{r.tribunal || 'No informado'}}${{r.rol ? ' (' + r.rol + ')' : ''}}</span>
            </div>
            <div class="dato-item">
              <span class="dato-label">💵 Comisión</span>
              <span class="dato-valor">${{r.comision}}</span>
            </div>
          </div>

          ${{vehs.length > 0 ? `
          <div class="seccion-vehiculos">
            <h4 class="seccion-vehiculos-titulo">🚗 Vehículo${{vehs.length > 1 ? 's' : ''}} en este Remate (${{vehs.length}}):</h4>
            ${{vehs.map(v => `
              <div class="card-vehiculo">
                <div class="vehiculo-header">
                  <div class="vehiculo-titulo">
                    ${{v.tipo === 'MOTOCICLETA' ? '🏍️' : v.tipo === 'CAMIONETA' ? '🛻' : v.tipo === 'FURGÓN' ? '🚐' : v.tipo === 'CAMIÓN' ? '🚛' : '🚗'}}
                    ${{v.titulo}}
                  </div>
                  ${{v.patente ? `
                  <div class="placa-patente-box">
                    <span style="font-size: 1.15rem;">🇨🇱</span>
                    <span class="placa-patente-texto">${{v.patente}}</span>
                    <button class="btn-copiar-patente" onclick="copiarPatente('${{v.patente}}', this)" title="Copiar patente al portapapeles">📋 Copiar</button>
                  </div>
                  ` : ''}}
                </div>

                <div class="vehiculo-datos-row">
                  ${{v.anio ? `<span class="badge-auto-anio">📅 Año ${{v.anio}}</span>` : ''}}
                  ${{v.minimo ? `<span class="badge-auto-minimo">💰 Mínimo: ${{v.minimo}}</span>` : ''}}
                  ${{v.color ? `<span class="badge-auto-color">🎨 Color: ${{v.color}}</span>` : ''}}
                  ${{v.transmision ? `<span class="badge-auto-detalle">🕹️ ${{v.transmision}}</span>` : ''}}
                  ${{v.traccion ? `<span class="badge-auto-detalle">🛞 ${{v.traccion}}</span>` : ''}}
                  ${{v.combustible ? `<span class="badge-auto-detalle">⛽ ${{v.combustible}}</span>` : ''}}
                  ${{v.kilometraje ? `<span class="badge-auto-detalle">🛣️ ${{v.kilometraje}}</span>` : ''}}
                  ${{v.afecto_iva ? `<span class="alerta-iva">⚠️ AFECTO A IVA (+19%)</span>` : ''}}
                  ${{v.zona_franca ? `<span class="alerta-zona-franca">⚠️ RESTRICCIÓN ZONA FRANCA</span>` : ''}}
                  ${{v.estado_mecanico ? `<span class="alerta-estado-auto">🔧 ${{v.estado_mecanico}}</span>` : ''}}
                  ${{v.motor ? `<span class="badge-auto-detalle">Motor: ${{v.motor}}</span>` : ''}}
                  ${{v.chasis ? `<span class="badge-auto-detalle">Chasis: ${{v.chasis}}</span>` : ''}}
                </div>

                ${{v.ubicacion_fisica ? `
                <div class="alerta-ubicacion-auto">
                  <span>📍</span>
                  <span><strong>Ubicación física del vehículo:</strong> ${{v.ubicacion_fisica}}</span>
                </div>
                ` : ''}}

                ${{v.bienes_anexos && v.bienes_anexos.length > 0 ? `
                <div class="vehiculo-anexos">
                  <strong>📦 Incluye además con este lote:</strong> ${{v.bienes_anexos.join(', ')}}
                </div>
                ` : ''}}
              </div>
            `).join('')}}
          </div>
          ` : ''}}

          ${{arts.length > 0 ? `
          <div class="seccion-articulos">
            <h4>${{vehs.length > 0 ? '📦 Bienes y artículos adicionales en este remate:' : '📦 Cosas y Artículos a Rematar:'}}</h4>
            <ul class="lista-articulos">
              ${{arts.map(art => `
                <li class="articulo-item">
                  <span class="articulo-icono">✔</span>
                  <span class="articulo-texto">${{art}}</span>
                </li>
              `).join('')}}
            </ul>
          </div>
          ` : ''}}

          <details class="detalle-acordeon">
            <summary class="detalle-summary">
              <span class="detalle-summary-title">📄 Ver descripción oficial completa del remate / PDF</span>
              <span class="detalle-summary-toggle">Ver texto judicial ▾</span>
            </summary>
            <div class="detalle-contenido-box">
              <p class="detalle-texto">${{r.detalle ? r.detalle : 'No se registraron notas adicionales en la publicación judicial.'}}</p>
            </div>
          </details>

          <div class="card-footer">
            ${{r.deudor && r.deudor.toLowerCase() !== 'no informado' ? `<span>Deudor: <strong>${{r.deudor}}</strong></span>` : '<span></span>'}}
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
              <span>Código Oficial: <strong>${{r.codigo}}</strong></span>
              ${{r.codigo ? `<a href="/pdf/${{r.codigo}}" target="_blank" class="btn-descarga-pdf" title="Descargar o ver el documento legal oficial emitido por el Estado">📥 Descargar PDF ↗</a>` : ''}}
            </div>
          </div>
        </article>
      `}}).join('');
    }}

    function copiarPatente(patente, btn) {{
      navigator.clipboard.writeText(patente).then(() => {{
        const orig = btn.innerHTML;
        btn.innerHTML = "✅ Copiada";
        btn.style.background = "#dcfce7";
        btn.style.color = "#15803d";
        btn.style.borderColor = "#86efac";
        setTimeout(() => {{
          btn.innerHTML = orig;
          btn.style.background = "";
          btn.style.color = "";
          btn.style.borderColor = "";
        }}, 1800);
      }}).catch(() => {{
        prompt("Copia la patente:", patente);
      }});
    }}

    function filtrarTarjetas() {{
      const q = document.getElementById("buscador").value.toLowerCase().trim();
      const btnClear = document.getElementById("btn-limpiar");
      btnClear.style.display = q ? "flex" : "none";

      if (!q) {{ renderizarRemates(remates); return; }}
      const terminos = q.split(/\\s+/).filter(t => t.length > 0);

      const filtrados = remates.filter(r => {{
        let texto = (r.comuna + " " + r.region + " " + r.direccion + " " + r.tribunal + " " + r.rol + " " + r.codigo + " " + (r.deudor || "") + " " + (r.detalle || "") + " " + r.articulos.join(" ")).toLowerCase();
        if (r.vehiculos && r.vehiculos.length > 0) {{
          for (const v of r.vehiculos) {{
            texto += " " + (v.patente || "") + " " + (v.titulo || "") + " " + (v.anio || "") + " " + (v.color || "") + " " + (v.transmision || "") + " " + (v.traccion || "") + " " + (v.combustible || "") + " " + (v.ubicacion_fisica || "") + " " + (v.tipo || "") + " " + (v.estado_mecanico || "");
          }}
        }}
        return terminos.every(term => texto.includes(term));
      }});
      renderizarRemates(filtrados);
    }}

    function limpiarBuscador() {{
      const b = document.getElementById("buscador");
      b.value = "";
      filtrarTarjetas();
      b.focus();
    }}

    let escala = 1.0;
    try {{
      const savedEscala = localStorage.getItem('remates_font_scale');
      if (savedEscala) {{
        escala = parseFloat(savedEscala) || 1.0;
        document.documentElement.style.setProperty('--font-scale', escala);
      }}
    }} catch (e) {{}}

    function cambiarTamano(delta) {{
      escala = delta === 0 ? 1.0 : Math.max(0.85, Math.min(1.45, Math.round((escala + delta) * 100) / 100));
      document.documentElement.style.setProperty('--font-scale', escala);
      try {{ localStorage.setItem('remates_font_scale', escala); }} catch(e) {{}}
    }}

    renderizarRemates(remates);
  </script>
</body>
</html>"""

    with open(filename, mode="w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[+] Catálogo visual accesible generado con éxito: {filename}")


def buscar_remates(
    tipo_bienes: str = "muebles",
    filtros_ubicacion: Optional[List[str]] = None,
    solo_vigentes: bool = True,
    dias_max_publicacion: int = 40,
    max_publicaciones: int = 600,
    solo_ubicacion_oficial: bool = False,
    detener_al_vencer_consecutivos: int = 80,
    archivo_csv: str = "remates_muebles_vigentes.csv",
    generar_archivos: bool = True,
) -> List[Dict[str, str]]:
    """
    Ejecuta el rastreo de remates vigentes en el Boletín Concursal.
    Puede ser invocada tanto desde la terminal como desde la aplicación web o móvil.
    """
    if filtros_ubicacion is None:
        filtros_ubicacion = []

    ahora = obtener_ahora_chile()
    fecha_limite_publicacion = ahora - timedelta(days=dias_max_publicacion)
    session = requests.Session()

    print("=" * 70)
    print("      BUSCADOR DE REMATES - BOLETÍN CONCURSAL DE CHILE")
    print("=" * 70)
    print(f"[+] Fecha y hora actual (Chile): {ahora.strftime('%d/%m/%Y %H:%M')}")
    print(f"[+] Tipo de bienes:           {tipo_bienes.upper()}")
    print(f"[+] Solo remates vigentes:    {'SÍ (Descarta fechas pasadas)' if solo_vigentes else 'NO'}")
    print(f"[+] Ventana de publicación:   Últimos {dias_max_publicacion} días (Desde {fecha_limite_publicacion.strftime('%d/%m/%Y')})")
    print(f"[+] Filtro geográfico:        {filtros_ubicacion if filtros_ubicacion else 'TODO CHILE (Sin restricción)'}")
    print(f"[+] Modo de búsqueda:         {'Solo ubicación oficial' if solo_ubicacion_oficial else 'Ubicación remate + Tribunal + Detalle del bien'}\n")

    try:
        csrf_token, csrf_header = obtener_tokens_csrf(session)
    except Exception as e:
        print(f"[-] Error al conectar con el Boletín Concursal: {e}")
        return

    # Descarga de publicaciones en bloques de 100
    publicaciones = []
    bloque_tam = 100
    tipos_a_consultar = ["muebles", "inmuebles"] if tipo_bienes == "ambos" else [tipo_bienes]

    print("[+] Obteniendo listado de remates desde el portal...")
    for tb in tipos_a_consultar:
        inicio = 0
        while inicio < max_publicaciones:
            cant_a_pedir = min(bloque_tam, max_publicaciones - inicio)
            try:
                items, total_disponibles = obtener_listado_remates(
                    session, csrf_token, csrf_header, tipo=tb, inicio=inicio, cantidad=cant_a_pedir
                )
                if not items:
                    break
                for it in items:
                    it["_tipo_bien"] = tb
                publicaciones.extend(items)
                inicio += len(items)
                if len(items) < cant_a_pedir:
                    break

                # Si el último ítem de este bloque ya supera la ventana de días, paramos de pedir más páginas
                fch_ultimo_str = items[-1].get("fchPublicacion")
                if fch_ultimo_str:
                    try:
                        fch_ultimo_dt = datetime.strptime(fch_ultimo_str, "%Y-%m-%d")
                        if fch_ultimo_dt < fecha_limite_publicacion:
                            break
                    except Exception:
                        pass

            except Exception as e:
                print(f"[-] Error al obtener listado ({tb}) en offset {inicio}: {e}")
                break

    print(f"[+] Se obtuvieron {len(publicaciones)} publicaciones dentro de la ventana de fechas.")
    print("[+] Analizando fichas técnicas y fechas de remate...\n")

    coincidencias = 0
    consecutivos_vencidos = 0
    total_vigentes = 0
    total_vencidos = 0
    resultados_csv = []

    for idx, pub in enumerate(publicaciones, start=1):
        codigo = pub.get("codigoValidacion")
        if not codigo:
            continue

        fch_pub_str = pub.get("fchPublicacion")
        if fch_pub_str:
            try:
                fch_pub_dt = datetime.strptime(fch_pub_str, "%Y-%m-%d")
                if fch_pub_dt < fecha_limite_publicacion:
                    print(f"\n[!] Se alcanzó el límite de fecha de publicación ({fch_pub_str} < {fecha_limite_publicacion.strftime('%Y-%m-%d')}). Finalizando búsqueda.")
                    break
            except Exception:
                pass

        try:
            texto_pdf = descargar_y_extraer_pdf(session, csrf_token, codigo)
            datos = parsear_datos_remate(texto_pdf)
            fecha_dt = parsear_fecha_remate(datos["fecha_remate"])

            # Evaluación de vigencia
            if fecha_dt:
                es_vigente = (fecha_dt >= ahora)
            else:
                es_vigente = False

            if es_vigente:
                total_vigentes += 1
                consecutivos_vencidos = 0
                tiempo_restante = fecha_dt - ahora
                dias = tiempo_restante.days
                horas = int(tiempo_restante.seconds / 3600)
                estado_str = f"VIGENTE (En {dias}d {horas}h)"
            else:
                total_vencidos += 1
                consecutivos_vencidos += 1
                estado_str = "VENCIDO"

            # Auto-detención solo si se alcanza un umbral alto de vencidos consecutivos (80)
            if solo_vigentes and consecutivos_vencidos >= detener_al_vencer_consecutivos:
                print("\n" + "*" * 70)
                print(f"[!] Se detectaron {consecutivos_vencidos} remates vencidos consecutivos.")
                print(f"[!] Se ha alcanzado el historial anterior; finalizando búsqueda anticipadamente.")
                print("*" * 70)
                break

            # Si se piden solo vigentes y este está vencido, se omite
            if solo_vigentes and not es_vigente:
                time.sleep(0.1)
                continue

            # Evaluación de filtro geográfico
            cumple_geo, razon_geo = coincide_filtro(
                datos, filtros_ubicacion, solo_ubicacion_oficial=solo_ubicacion_oficial
            )

            if cumple_geo:
                coincidencias += 1
                print("-" * 70)
                print(f"[MATCH #{coincidencias}] Publicación #{idx} ({pub.get('_tipo_bien', '').upper()})")
                print(f"  Estado:           {estado_str}")
                print(f"  Modalidad:        {datos['modalidad']}")
                print(f"  Fecha de Remate:  {datos['fecha_remate']}")
                print(f"  Publicado en:     {pub.get('fchPublicacion', 'N/A')}")
                print(f"  Región / Comuna:  {datos['region_remate']} | {datos['comuna_remate']}")
                print(f"  Dirección Remate: {datos['direccion_remate']}")
                print(f"  Valor Mínimo:     {datos['valor_minimo']} (Comisión: {datos['comision']})")
                print(f"  Tribunal / Rol:   {datos['tribunal']} ({datos['rol_causa']})")
                print(f"  Deudor:           {datos['deudor']} (RUT: {datos['rut_deudor']})")
                if datos["martillero_web"] != "No informado":
                    print(f"  Web Martillero:   {datos['martillero_web']}")
                print(f"  Código Ficha:     {codigo}")
                detalle_corto = (datos['detalle'][:180] + "...") if len(datos['detalle']) > 180 else datos['detalle']
                print(f"  Detalle del Bien: {detalle_corto}")

                if generar_archivos:
                    resultados_csv.append({
                        "codigo_ficha": codigo,
                        "tipo_bien": pub.get("_tipo_bien", ""),
                        "modalidad": datos["modalidad"],
                        "estado_vigencia": "VIGENTE" if es_vigente else "VENCIDO",
                        "fecha_remate": datos["fecha_remate"],
                        "fecha_publicacion": pub.get("fchPublicacion", ""),
                        "region_remate": datos["region_remate"],
                        "comuna_remate": datos["comuna_remate"],
                        "direccion_remate": datos["direccion_remate"],
                        "valor_minimo": datos["valor_minimo"],
                        "comision": datos["comision"],
                        "tribunal": datos["tribunal"],
                        "rol_causa": datos["rol_causa"],
                        "deudor": datos["deudor"],
                        "rut_deudor": datos["rut_deudor"],
                        "martillero_web": datos["martillero_web"],
                        "coincidencia_filtro": razon_geo,
                        "detalle": datos["detalle"],
                    })

            # Pausa breve de cortesía con el servidor
            time.sleep(0.15)

        except Exception as e:
            print(f"[-] Error al procesar ficha {codigo}: {e}")

    print("\n" + "=" * 70)
    print("                    RESUMEN DE RESULTADOS")
    print("=" * 70)
    print(f"[+] Total analizados:       {total_vigentes + total_vencidos}")
    print(f"[+] Remates vigentes:       {total_vigentes}")
    print(f"[+] Remates vencidos:       {total_vencidos}")
    print(f"[+] Coincidencias mostradas: {coincidencias}")
    print("=" * 70)

    if generar_archivos and resultados_csv:
        exportar_csv(resultados_csv, filename=archivo_csv)
        exportar_html(resultados_csv, filename="remates_visual.html")
        exportar_whatsapp_txt(resultados_csv, filename="resumen_whatsapp.txt")

    return resultados_csv


def main():
    buscar_remates(
        tipo_bienes="muebles",
        filtros_ubicacion=None,
        dias_max_publicacion=40,
        max_publicaciones=600,
    )


if __name__ == "__main__":
    main()