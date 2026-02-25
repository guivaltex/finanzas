import os
import logging
import threading
import json
import unicodedata
from datetime import datetime
import pytz # Librería para la hora de Colombia
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask

# --- CONFIGURACIÓN ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

NOMBRE_HOJA_CALCULO = "FinanzasBot" # Asegúrate que coincida con tu Drive

# Configuración de Logs (Menos ruidoso)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

client_openai = OpenAI(api_key=OPENAI_API_KEY)

# --- SERVIDOR WEB (KEEP ALIVE) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot Contable Activo 24/7"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.start()

# --- CONEXIÓN GOOGLE SHEETS ---
def conectar_google():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        # Fallback local
        creds = ServiceAccountCredentials.from_json_keyfile_name('credenciales_google.json', scope)
        
    client_gs = gspread.authorize(creds)
    return client_gs.open(NOMBRE_HOJA_CALCULO)

# --- LÓGICA DE NEGOCIO ---

# Listas de referencia (Solo para contexto, el prompt hace el trabajo duro)
CAT_INGRESOS = "Venta, Abono, Saldo, Base cama, Espaldar, Sofa, Mesas, Colchon, Silleteria, Refaccion"
CAT_GASTOS_HOGAR = "Comida, Transporte, Diversion, Dulce, Salidas, Salud, Vivienda, Servicios, Celular, Educacion, Mercado"
CAT_GASTOS_FABRICA = "Materiales, Onces, Sueldos, Arriendo, Servicios, Deudas, Herramientas, Insumos"

def obtener_fecha_colombia():
    """Retorna la fecha y hora actual en zona horaria Bogotá"""
    bogota = pytz.timezone('America/Bogota')
    return datetime.now(bogota).strftime("%Y-%m-%d %H:%M:%S")

def normalizar_texto(texto):
    if not texto or texto == "NA": return ""
    texto = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('ASCII')
    return texto.lower().strip()

def guardar_google(hoja_nombre, datos_lista):
    try:
        sheet = conectar_google()
        try:
            worksheet = sheet.worksheet(hoja_nombre)
        except:
            worksheet = sheet.get_worksheet(0)
            
        worksheet.append_row(datos_lista)
        return True
    except Exception as e:
        logging.error(f"Error guardando en Google: {e}")
        return False

def procesar_inteligencia(texto_transcrito):
    prompt = f"""
    Actúa como un asistente contable experto en Colombia.
    Analiza el texto del usuario y extrae una transacción financiera.
    
    TEXTO: "{texto_transcrito}"

    REGLAS CRÍTICAS DE INTERPRETACIÓN:
    1. **MONEDA Y NÚMEROS:**
       - Estamos en Colombia. Si el monto es pequeño (ej: "280", "31") y el contexto es muebles, madera o compras grandes, INTERPRETA MILES (280 -> 280000).
       - Si el contexto es comida barata o dulces, mantén el valor bajo (ej: "Dulce 2000" -> 2000).
       - El monto final debe ser un NÚMERO ENTERO SIN PUNTOS NI COMAS (Ej: 280000, no 280.000).
    
    2. **FACTURAS:**
       - Si escuchas números sueltos asociados a "factura" (ej: "tres uno cinco siete"), ÚNELOS (3157).
       - Extrae el número de la factura en su propio campo. Si no hay, pon "NA".

    3. **CLASIFICACIÓN (PROHIBIDO USAR NOTA):**
       - Palabras clave como: "Pago", "Compra", "Gasto", "Abono", "Saldo", "Venta", "Cobro" -> SON SIEMPRE TRANSACCIONES (Ingreso o Gasto). JAMÁS las marques como 'NOTA'.
       - "Abono" o "Saldo" -> TIPO: Ingreso.
       - "Pago celular" -> GASTO, Hogar, Servicios.
       - "Matrícula" -> GASTO, Hogar, Educación.

    4. **CATEGORÍAS:**
       - Ingresos: [{CAT_INGRESOS}]
       - Gastos Hogar: [{CAT_GASTOS_HOGAR}]
       - Gastos Fabrica: [{CAT_GASTOS_FABRICA}]
       - Si no encaja, busca la más lógica. NO uses "Otros" si puedes evitarlo.

    FORMATO DE RESPUESTA OBLIGATORIO (Separado por |):
    TIPO|CONTEXTO|CATEGORIA|MONTO_ENTERO|DESCRIPCION|NUMERO_FACTURA

    Ejemplos de entrenamiento:
    Input: "Compra de madera por 280 factura tres uno cinco siete"
    Output: GASTO|FABRICA|MATERIALES|280000|compra de madera|3157

    Input: "Abono de la factura 20"
    Output: INGRESO|FABRICA|ABONO|0|abono factura 20 (Monto 0 si no se dice valor)|20

    Input: "Pago matrícula sofia 3500"
    Output: GASTO|HOGAR|EDUCACION|3500000|pago matricula sofia|NA
    
    Input: "Nota corregir el valor anterior"
    Output: NOTA|corregir el valor anterior
    """
    
    response = client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0 # Temperatura 0 para máxima precisión y menos creatividad
    )
    
    return response.choices[0].message.content.strip().replace('"', '')

async def manejar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    archivo_local = f"audio_{user_id}.ogg"

    try:
        await update.message.reply_text("🎧 Procesando...")
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        await voice_file.download_to_drive(archivo_local)

        # 1. Transcribir
        with open(archivo_local, "rb") as audio:
            transcription = client_openai.audio.transcriptions.create(
                model="whisper-1", file=audio, language="es"
            )
        texto_usuario = transcription.text
        print(f"Texto escuchado: {texto_usuario}")

        # 2. Interpretar
        respuesta_ia = procesar_inteligencia(texto_usuario)
        print(f"Respuesta IA: {respuesta_ia}")
        
        partes = respuesta_ia.split('|')

        # Manejo de NOTAS
        if partes[0] == "NOTA":
            guardar_google("Notas", [obtener_fecha_colombia(), partes[1]])
            await update.message.reply_text(f"📝 Nota guardada:\n{partes[1]}")
            return

        # Manejo de TRANSACCIONES
        # Formato esperado: TIPO|CONTEXTO|CATEGORIA|MONTO|DESCRIPCION|FACTURA
        if len(partes) >= 6:
            tipo = normalizar_texto(partes[0]).upper()
            contexto = normalizar_texto(partes[1])
            categoria = normalizar_texto(partes[2])
            monto = partes[3] # Debería ser solo números gracias al prompt
            descripcion = normalizar_texto(partes[4])
            factura = partes[5] if partes[5] != "NA" else ""

            datos = [
                obtener_fecha_colombia(),
                tipo,
                contexto,
                categoria,
                monto,
                descripcion,
                factura
            ]
            
            exito = guardar_google("Registros", datos)
            
            if exito:
                msj_factura = f"\n📄 Factura: {factura}" if factura else ""
                await update.message.reply_text(
                    f"✅ **{tipo} REGISTRADO**\n"
                    f"💰 ${monto}\n"
                    f"📂 {contexto} - {categoria}\n"
                    f"📝 {descripcion}"
                    f"{msj_factura}"
                )
            else:
                await update.message.reply_text("❌ Error guardando en Drive.")
        else:
            await update.message.reply_text(f"⚠️ La IA no pudo estructurar el dato: {respuesta_ia}")

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(archivo_local):
            os.remove(archivo_local)

if __name__ == '__main__':
    keep_alive()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.VOICE, manejar_audio))
    print("🤖 BOT COLOMBIA ACTIVO")
    application.run_polling()
