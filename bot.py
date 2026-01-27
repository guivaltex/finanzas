import os
import logging
import threading
import json
import unicodedata
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask

# --- CONFIGURACIÓN ---
# Render usará Variables de Entorno, pero para pruebas locales busca el archivo
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI_SI_PRUEBAS_LOCAL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "TU_KEY_AQUI_SI_PRUEBAS_LOCAL")

# Nombre exacto de tu hoja en Google Drive
NOMBRE_HOJA_CALCULO = "FinanzasBot"

# Configuración de Logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
client_openai = OpenAI(api_key=OPENAI_API_KEY)

# --- TRUCO PARA RENDER (SERVIDOR WEB FALSO) ---
# Render necesita un puerto abierto o cerrará la app. Usamos Flask.
app = Flask('')

@app.route('/')
def home():
    return "Bot Funcionando"

def run_flask():
    # Render asigna un puerto en la variable PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.start()

# --- CONEXIÓN GOOGLE SHEETS ---
def conectar_google():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # En Render, guardaremos el JSON en una variable secreta
    # En Local, busca el archivo
    if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name('credenciales_google.json', scope)
        
    client_gs = gspread.authorize(creds)
    return client_gs.open(NOMBRE_HOJA_CALCULO)

# --- LÓGICA DEL NEGOCIO (IGUAL QUE ANTES) ---

CAT_INGRESOS = "Base cama, Espaldar, Sofa, Mesas de noche, Colchon, Cama multifuncional, Silleteria, Refaccion, Otros"
CAT_GASTOS_HOGAR = "Comida, Transporte, Diversion, Dulce, Salidas, Salud, Vivienda, Otros"
CAT_GASTOS_FABRICA = "Materiales, Onces, Sueldos, Arriendo, Servicios, Deudas, Herramientas, Otros"

def normalizar_texto(texto):
    if not texto: return ""
    texto = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('ASCII')
    return texto.lower().strip()

def guardar_google(hoja_nombre, datos_lista):
    """Guarda en la pestaña especificada de Google Sheets"""
    try:
        sheet = conectar_google()
        # Selecciona la pestaña (worksheet). Si no existe, usa la primera (0)
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
    Eres un asistente contable. 
    REGLAS:
    1. Si es "Nota" o corrección -> TIPO: NOTA.
    2. Si es Ingreso -> Categoria de: [{CAT_INGRESOS}].
    3. Si es Gasto -> Contexto Hogar: [{CAT_GASTOS_HOGAR}] o Fabrica: [{CAT_GASTOS_FABRICA}].
    
    OUTPUT FORMATO:
    TRANSACCION|TIPO|CONTEXTO|CATEGORIA|MONTO|DESCRIPCION
    NOTA|CONTENIDO
    
    Texto: "{texto_transcrito}"
    """
    response = client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    return response.choices[0].message.content.strip().replace('"', '')

async def manejar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    archivo_local = f"audio_{user_id}.ogg"

    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        await voice_file.download_to_drive(archivo_local)

        with open(archivo_local, "rb") as audio:
            texto_usuario = client_openai.audio.transcriptions.create(model="whisper-1", file=audio).text
        
        respuesta_ia = procesar_inteligencia(texto_usuario)
        partes = respuesta_ia.split('|')
        
        if partes[0] == "NOTA":
            # Guardar en pestaña 'Notas' (Debes crearla en el Sheet o usará la principal)
            guardar_google("Notas", [datetime.now().strftime("%Y-%m-%d"), partes[1]])
            await update.message.reply_text(f"📝 Nota guardada en la nube.")
            
        elif partes[0] == "TRANSACCION":
            # Datos: Fecha, Tipo, Contexto, Categoria, Monto, Descripcion
            datos = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                normalizar_texto(partes[1]), # Tipo
                normalizar_texto(partes[2]), # Contexto
                normalizar_texto(partes[3]), # Categoria
                partes[4],                   # Monto
                normalizar_texto(partes[5])  # Descripcion
            ]
            # Guardar en pestaña 'Registros'
            exito = guardar_google("Registros", datos)
            
            if exito:
                await update.message.reply_text(f"✅ Guardado en Drive: ${partes[4]} ({partes[3]})")
            else:
                await update.message.reply_text(f"❌ Error conectando con Google Sheets.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(archivo_local):
            os.remove(archivo_local)

if __name__ == '__main__':
    keep_alive() # Inicia el servidor falso para Render
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.VOICE, manejar_audio))
    print("🤖 BOT NUBE LISTO")
    application.run_polling()