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
# Borra lo que tenías y pon esto (con TUS códigos reales):
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "PEGAR_AQUI_TU_TOKEN_REAL_SOLO_SI_VAS_A_PROBAR_LOCAL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "PEGAR_AQUI_TU_KEY_REAL_SOLO_SI_VAS_A_PROBAR_LOCAL")

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

# --- LISTAS DE CATEGORÍAS (Para referencia en el Prompt) ---
# Nota: He simplificado las listas para que el modelo las lea mejor
CAT_INGRESOS = "Base cama, Espaldar, Sofa, Mesas de noche, Colchon, Cama multifuncional, Silleteria, Refaccion"
CAT_GASTOS_HOGAR = "Comida, Transporte, Diversion, Dulce, Salidas, Salud, Vivienda"
CAT_GASTOS_FABRICA = "Materiales, Onces, Sueldos, Arriendo, Servicios, Deudas, Herramientas"

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
    Eres un contador experto e inteligente. Tu trabajo es clasificar gastos e ingresos.
    
    TEXTO DEL USUARIO: "{texto_transcrito}"
    
    TUS HERRAMIENTAS (CATEGORÍAS PERMITIDAS):
    - SI ES INGRESO, ELIGE DE: [{CAT_INGRESOS}, Otros]
    - SI ES GASTO (HOGAR), ELIGE DE: [{CAT_GASTOS_HOGAR}, Otros]
    - SI ES GASTO (FABRICA), ELIGE DE: [{CAT_GASTOS_FABRICA}, Otros]
    
    INSTRUCCIONES DE RAZONAMIENTO (IMPORTANTE):
    1. NO uses "Otros" si existe una categoría relacionada. Haz un esfuerzo por clasificar.
       - Ejemplo: "Compré tornillos y telas" -> Contexto: Fabrica, Categoria: Materiales (NO Otros).
       - Ejemplo: "Pagué el recibo de la luz" -> Categoria: Servicios.
       - Ejemplo: "Me comí una hamburguesa" -> Categoria: Comida.
    2. Identifica el contexto (Hogar vs Fabrica) basado en las palabras clave.
       - "Telas, madera, pegante, nomina" -> Fabrica.
       - "Mercado, cine, medicina" -> Hogar.
    
    FORMATO DE RESPUESTA OBLIGATORIO:
    TRANSACCION|TIPO|CONTEXTO|CATEGORIA|MONTO|DESCRIPCION
    (Si es solo una nota: NOTA|CONTENIDO)
    """
    
    response = client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1 # Subimos un poquitico la temperatura para darle creatividad de asociación
    )
    
    return response.choices[0].message.content.strip().replace('"', '')

async def manejar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    archivo_local = f"audio_{user_id}.ogg"

    try:
        await update.message.reply_text("🎧 Escuchando...") # Feedback inmediato
        
        # 1. Descargar
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        await voice_file.download_to_drive(archivo_local)
        print(f"--- Audio descargado: {archivo_local} ---")

        # 2. Transcribir
        with open(archivo_local, "rb") as audio:
            transcription = client_openai.audio.transcriptions.create(
                model="whisper-1", 
                file=audio
            )
        texto_usuario = transcription.text
        
        # --- DEPURACIÓN: VER QUÉ ESCUCHÓ ---
        print(f"👂 WHISPER ESCUCHÓ: '{texto_usuario}'")
        
        if not texto_usuario or len(texto_usuario) < 2:
            await update.message.reply_text("⚠️ No escuché nada en el audio. Intenta hablar más fuerte.")
            return

        # 3. Inteligencia
        respuesta_ia = procesar_inteligencia(texto_usuario)
        
        # --- DEPURACIÓN: VER QUÉ PENSÓ GPT ---
        print(f"🧠 GPT RESPONDIÓ: '{respuesta_ia}'")

        partes = respuesta_ia.split('|')
        
        # Validación extra: Si GPT devuelve las palabras del ejemplo, es un error
        if len(partes) > 4 and "MONTO" in partes[4]:
             await update.message.reply_text("⚠️ Error: La IA no entendió los datos. Intenta ser más claro.")
             return

        if partes[0] == "NOTA":
            guardar_google("Notas", [datetime.now().strftime("%Y-%m-%d"), partes[1]])
            await update.message.reply_text(f"📝 Nota guardada.")
            
        elif partes[0] == "TRANSACCION":
            datos = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                normalizar_texto(partes[1]), 
                normalizar_texto(partes[2]), 
                normalizar_texto(partes[3]), 
                partes[4],                   
                normalizar_texto(partes[5])  
            ]
            exito = guardar_google("Registros", datos)
            
            if exito:
                # Usamos los datos limpios de la lista 'datos'
                # datos[1]=Tipo, datos[2]=Contexto, datos[3]=Categoria, datos[4]=Monto
                await update.message.reply_text(
                    f"✅ **{datos[1].upper()}**\n"
                    f"Contexto: {datos[2]}\n"
                    f"Cat: {datos[3]}\n"
                    f"Valor: ${datos[4]}\n"
                    f"Desc: {datos[5]}"
                )
            else:
                await update.message.reply_text(f"❌ Error de conexión con Google Sheets.")
        
    except Exception as e:
        print(f"❌ ERROR CRÍTICO: {e}") # Ver error en terminal
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