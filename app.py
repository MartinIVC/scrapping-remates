import os
import re
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request, send_file
import requests
from scraping_remates import (
    buscar_remates,
    obtener_tokens_csrf,
    obtener_ahora_chile,
    DOWNLOAD_URL,
    HEADERS,
)

app = Flask(__name__)

PDF_DIR = os.path.join(os.path.dirname(__file__), "pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

# Estado global de la aplicación
estado_busqueda = {
    "ocupado": False,
    "mensaje": "Listo",
    "ultima_actualizacion": obtener_ahora_chile().strftime("%d/%m/%Y %H:%M"),
    "total_encontrados": 0,
}


def cargar_html_actual():
    """Lee el archivo remates_visual.html generado más recientemente."""
    html_path = os.path.join(os.path.dirname(__file__), "remates_visual.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>No se ha realizado ninguna búsqueda aún. Pulsa 'Buscar Remates'.</h1>"


def tarea_actualizacion_automatica():
    """
    Hilo en segundo plano que actualiza los remates automáticamente todos los días
    a las 09:00 AM (o cada 6 horas) para que el abuelo siempre vea datos frescos
    sin tener que esperar.
    """
    while True:
        try:
            # Esperar 6 horas entre chequeos automáticos
            time.sleep(6 * 3600)
            print("[Auto-Scheduler] Ejecutando búsqueda matutina programada (Todo Chile)...")
            buscar_remates(
                tipo_bienes="muebles",
                filtros_ubicacion=None,
                dias_max_publicacion=40,
                max_publicaciones=500,
            )
            estado_busqueda["ultima_actualizacion"] = obtener_ahora_chile().strftime("%d/%m/%Y %H:%M")
        except Exception as e:
            print(f"[Auto-Scheduler] Error en actualización automática: {e}")


@app.route("/")
def index():
    """Página principal adaptada para smartphone con barra interactiva superior."""
    html_base = cargar_html_actual()

    # Barra de control móvil con diseño moderno y botones táctiles cómodos
    barra_control = f"""
    <!-- Barra de Control Móvil Unificada -->
    <div style="background: #ffffff; border: 2px solid #bae6fd; border-radius: 20px; padding: 22px; margin-bottom: 24px; box-shadow: 0 6px 20px -4px rgba(2,132,199,0.12);">
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;">
        <span style="font-size: 1.2rem; font-weight: 800; color: #0f172a; display: flex; align-items: center; gap: 8px;">
          📱 Panel de Búsqueda y Actualización
        </span>
        <span style="font-size: 0.94rem; color: #64748b; background: #f8fafc; padding: 6px 14px; border-radius: 10px; border: 1px solid #e2e8f0;" id="lbl-actualizado">
          Última actualización: <strong style="color: #0369a1;">{estado_busqueda['ultima_actualizacion']}</strong>
        </span>
      </div>

      <!-- Selector único de tipo de bienes -->
      <div style="margin-bottom: 16px;">
        <label for="sel-tipo" style="display: block; font-size: 0.95rem; font-weight: 800; color: #334155; margin-bottom: 8px;">
          ¿Qué tipo de bienes deseas consultar? (Todo Chile):
        </label>
        <select id="sel-tipo" style="width: 100%; padding: 14px 18px; font-size: 1.1rem; font-weight: 800; font-family: inherit; border: 2px solid #cbd5e1; border-radius: 14px; background: #f8fafc; color: #0f172a; outline: none; cursor: pointer; transition: all 0.2s;">
          <option value="muebles" selected>Bienes muebles</option>
          <option value="inmuebles">Bienes Inmuebles</option>
          <option value="ambos">Todos los Bienes</option>
        </select>
      </div>

      <!-- Botón Gigante de Búsqueda -->
      <button id="btn-buscar-vivo" onclick="lanzarBusquedaEnVivo()" style="width: 100%; padding: 18px 24px; font-size: 1.22rem; font-weight: 900; font-family: inherit; color: white; background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); border: none; border-radius: 14px; cursor: pointer; box-shadow: 0 4px 14px rgba(2,132,199,0.35); display: flex; align-items: center; justify-content: center; gap: 10px; transition: all 0.2s;">
        🔄 ACTUALIZAR REMATES EN VIVO (TODO CHILE)
      </button>

      <!-- Mensaje de estado -->
      <div id="msg-estado" style="display: none; margin-top: 14px; padding: 14px 18px; border-radius: 12px; font-size: 1.05rem; font-weight: 800; text-align: center; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe;">
        ⏳ Buscando en el Boletín Concursal, por favor espera un momento...
      </div>
    </div>

    <script>
      async function lanzarBusquedaEnVivo() {{
        const btn = document.getElementById("btn-buscar-vivo");
        const msg = document.getElementById("msg-estado");
        const tipo = document.getElementById("sel-tipo").value;

        btn.disabled = true;
        btn.style.opacity = "0.7";
        btn.innerHTML = "⏳ CONSULTANDO BOLETÍN OFICIAL...";
        msg.style.display = "block";
        msg.innerHTML = "🔍 Consultando remates vigentes en vivo... esto toma unos 20 segundos.";

        try {{
          const resp = await fetch("/api/buscar", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ tipo: tipo }})
          }});

          const data = await resp.json();
          if (data.ok) {{
            msg.innerHTML = `✅ ¡Listo! Se encontraron ${{data.coincidencias}} remates vigentes. Actualizando catálogo...`;
            setTimeout(() => {{ window.location.reload(); }}, 1200);
          }} else {{
            msg.innerHTML = "⚠️ Hubo un detalle al buscar: " + (data.error || "Intente nuevamente");
            btn.disabled = false;
            btn.style.opacity = "1";
            btn.innerHTML = "🔄 ACTUALIZAR REMATES EN VIVO (TODO CHILE)";
          }}
        }} catch (err) {{
          msg.innerHTML = "⚠️ Error de conexión: " + err.message;
          btn.disabled = false;
          btn.style.opacity = "1";
          btn.innerHTML = "🔄 ACTUALIZAR REMATES EN VIVO (TODO CHILE)";
        }}
      }}
    </script>
    """

    # Inyectar la barra de control tras el encabezado principal para orden visual
    if "</header>" in html_base:
        return html_base.replace("</header>", "</header>" + barra_control, 1)
    elif '<div class="container">' in html_base:
        return html_base.replace('<div class="container">', '<div class="container">' + barra_control, 1)
    else:
        return barra_control + html_base


@app.route("/api/buscar", methods=["POST"])
def api_buscar():
    """Ejecuta el scraper a demanda trayendo todos los remates de Chile según el tipo elegido."""
    if estado_busqueda["ocupado"]:
        return jsonify({"ok": False, "error": "Ya hay una búsqueda en curso. Espera unos segundos."})

    estado_busqueda["ocupado"] = True
    try:
        data = request.get_json() or {}
        tipo = data.get("tipo", "muebles")

        print(f"[API] Nueva búsqueda solicitada desde smartphone: tipo={tipo} (Todo Chile)")
        resultados = buscar_remates(
            tipo_bienes=tipo,
            filtros_ubicacion=None,
            solo_vigentes=True,
            dias_max_publicacion=40,
            max_publicaciones=500,
            generar_archivos=True,
        )

        estado_busqueda["ultima_actualizacion"] = obtener_ahora_chile().strftime("%d/%m/%Y %H:%M")
        estado_busqueda["total_encontrados"] = len(resultados)

        return jsonify({
            "ok": True,
            "coincidencias": len(resultados),
            "actualizado": estado_busqueda["ultima_actualizacion"],
        })

    except Exception as e:
        print(f"[API Error] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        estado_busqueda["ocupado"] = False


@app.route("/pdf/<codigo>")
def ver_pdf_oficial(codigo):
    """
    Descarga o sirve desde caché local el PDF oficial emitido por el Boletín Concursal.
    Permite abrir el documento legal directamente en el navegador de cualquier dispositivo.
    """
    codigo_seguro = re.sub(r"[^A-Za-z0-9\-]", "", codigo).strip()
    if not codigo_seguro:
        return "Código de remate inválido", 400

    pdf_local = os.path.join(PDF_DIR, f"{codigo_seguro}.pdf")
    if os.path.exists(pdf_local) and os.path.getsize(pdf_local) > 500:
        return send_file(pdf_local, mimetype="application/pdf")

    # Si no está en caché, descargarlo en tiempo real desde el portal oficial
    try:
        session = requests.Session()
        csrf_token, _ = obtener_tokens_csrf(session)
        payload = {"_csrf": csrf_token, "codigoValidacion": codigo_seguro}
        resp = session.post(DOWNLOAD_URL, data=payload, headers=HEADERS, timeout=20)
        if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
            with open(pdf_local, "wb") as f:
                f.write(resp.content)
            return send_file(pdf_local, mimetype="application/pdf")
        else:
            return f"No se encontró el documento PDF oficial para el código {codigo_seguro}.", 404
    except Exception as e:
        return f"Error al conectar con el Boletín Concursal: {e}", 500


# Iniciar hilo del scheduler automático (compatible con python app.py y gunicorn)
_scheduler_iniciado = False
def _asegurar_scheduler():
    global _scheduler_iniciado
    if not _scheduler_iniciado:
        _scheduler_iniciado = True
        t = threading.Thread(target=tarea_actualizacion_automatica, daemon=True)
        t.start()

_asegurar_scheduler()


if __name__ == "__main__":
    # Iniciar servidor web accesible en toda la red local
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 70)
    print("       APLICACIÓN MÓVIL DE REMATES EN EJECUCIÓN")
    print("=" * 70)
    print(f"[*] Desde tu computador:      http://localhost:{port}")
    print(f"[*] Desde el celular en casa:  http://192.168.1.89:{port}")
    print("=" * 70 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False)
