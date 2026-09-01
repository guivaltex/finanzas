"""Runtime Render: Telegram -> OpenAI -> Cola_Guivaltex_V1."""
import json
import logging
import os
import sys
import threading

from render_finanzas_api import crear_servidor
from bot_finanzas import (
    AutorizacionTelegram,
    CATEGORIAS_FINANCIERAS_V1,
    CONTEXTO_TRANSCRIPCION,
    campos_explicitos_transcripcion,
    CONTRATO_INTERPRETACION,
    EventoTelegram,
    IDIOMAS_TRANSCRIPCION,
    MODELO_INTERPRETACION,
    MODELO_TRANSCRIPCION,
    PALABRAS_CLAVE_TRANSCRIPCION,
    ProcesadorCola,
    conectar_cola,
    inspeccionar_spreadsheet,
    preparar_spreadsheet,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger("guivaltex.bot")

INSTRUCCIONES_INTERPRETACION = """
Interpreta una transcripcion financiera de Guivaltex en Colombia.
Devuelve exclusivamente el objeto del JSON Schema estricto.

Reglas:
- Jerarquia obligatoria: primero campos pronunciados explicitamente; despues
  reglas deterministas; luego inferencia semantica; finalmente revision.
- La entrada incluye campos_explicitos detectados del prefijo habitual
  tipo -> ambito. Si tipo o ambito no son null, copialos exactamente al JSON:
  ninguna palabra del concepto puede sobrescribirlos.
- clase=movimiento para gastos/ingresos externos normales.
- clase=posible_abono para abonos, saldos o cobros que deben registrarse
  canonicamente desde el modulo Abonos de Guivaltex.
- clase=nota para una nota no financiera; debe requerir revision.
- importe es el valor COP normalizado como texto decimal, sin separadores de miles.
- evidencia_importe copia literalmente solo la expresion del importe.
- Un numero desnudo entre 1 y 999 significa miles: 180 significa 180000.
- '180 mil', '180.000' y '180000' significan 180000.
- Una unidad explicita contradictoria como '180 pesos' requiere revision y no
  debe convertirse silenciosamente.
- No hay movimientos finales validos inferiores a COP 1000.
- fecha_efectiva usa YYYY-MM-DD. Resuelve fechas relativas usando fecha_mensaje.
- categoria debe ser exactamente una de las categorias entregadas.
- Transporte puede ser empresa u hogar. Conserva siempre el ambito explicito;
  solo infierelo por contexto cuando campos_explicitos.ambito sea null.
- Con ambito explicito, nunca lo cambies por desayuno, almuerzo, comida, cena,
  onces, refrigerios, transporte ni ninguna otra palabra del concepto.
- Sin ambito explicito, desayuno/almuerzo/comida/cena usan comida y hogar como
  respaldo. Onces/refrigerios/tintos operativos usan onces y empresa.
- No inventes factura_id. Conserva el identificador solo si fue mencionado.
- Si falta evidencia, hay ambiguedad o la categoria no es clara, marca
  requiere_revision=true y explica motivos breves mediante codigos estables.
""".strip()


def _variable_requerida(nombre):
    valor = str(os.environ.get(nombre) or "").strip()
    if not valor:
        raise RuntimeError("Falta la variable de entorno " + nombre + ".")
    return valor


class ServiciosOpenAI:
    def __init__(self, api_key):
        from openai import OpenAI
        self.client = OpenAI(
            api_key=api_key,
            timeout=60.0,
            max_retries=2,
        )

    def transcribir(self, ruta_audio):
        with open(ruta_audio, "rb") as audio:
            respuesta = self.client.audio.transcriptions.create(
                model=MODELO_TRANSCRIPCION,
                file=audio,
                prompt=CONTEXTO_TRANSCRIPCION,
                extra_body={
                    "keywords": list(PALABRAS_CLAVE_TRANSCRIPCION),
                    "languages": list(IDIOMAS_TRANSCRIPCION),
                },
                response_format="json",
            )
        return respuesta.text

    def interpretar(self, transcripcion, fecha_mensaje):
        entrada = json.dumps(
            {
                "fecha_mensaje": fecha_mensaje,
                "transcripcion": transcripcion,
                "campos_explicitos": campos_explicitos_transcripcion(
                    transcripcion
                ),
                "categorias_permitidas": sorted(CATEGORIAS_FINANCIERAS_V1),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        respuesta = self.client.responses.create(
            model=MODELO_INTERPRETACION,
            instructions=INSTRUCCIONES_INTERPRETACION,
            input=entrada,
            reasoning={"effort": "low"},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "movimiento_financiero_guivaltex_v1",
                    "description": "Interpretacion financiera estricta para Guivaltex.",
                    "strict": True,
                    "schema": CONTRATO_INTERPRETACION,
                }
            },
            max_output_tokens=1500,
            store=False,
        )
        return respuesta.output_text


class RuntimeTelegram:
    def __init__(self, autorizacion, procesador):
        self.autorizacion = autorizacion
        self.procesador = procesador

    @staticmethod
    def evento(update):
        return EventoTelegram.crear(
            update.effective_chat.id,
            update.effective_message.message_id,
            update.effective_user.id,
            update.effective_message.voice.file_id,
            update.effective_message.voice.file_unique_id,
            update.effective_message.date,
        )

    @staticmethod
    def descargar_factory(bot, evento):
        async def descargar(ruta):
            archivo = await bot.get_file(evento.file_id)
            await archivo.download_to_drive(ruta)
        return descargar

    async def manejar_audio(self, update, context):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        if not self.autorizacion.permite(chat_id, user_id):
            await update.effective_message.reply_text("⛔ Usuario o chat no autorizado.")
            return
        try:
            evento = self.evento(update)
        except Exception:
            await update.effective_message.reply_text("❌ El audio no tiene una identidad válida.")
            return

        resultado = await self.procesador.procesar(
            evento,
            self.descargar_factory(context.bot, evento),
        )
        LOGGER.info(
            "evento=%s estado=%s codigo=%s repetido=%s",
            resultado.external_id,
            resultado.estado,
            resultado.codigo,
            resultado.repetido,
        )
        mensajes = {
            "listo": "✅ Movimiento conservado y listo para sincronizar.",
            "excepcion": "⚠️ Evento conservado; requiere revisión antes de sincronizar.",
            "recibido": "⏳ Evento conservado. El procesamiento podrá reintentarse.",
        }
        if resultado.codigo == "conflicto_identidad":
            texto = "❌ Conflicto de identidad del mensaje. Requiere revisión."
        elif resultado.estado == "error":
            texto = "❌ No fue posible confirmar el avance en la cola. Reintenta el mismo mensaje."
        elif resultado.codigo == "ya_procesado":
            texto = "ℹ️ Este mensaje ya estaba registrado; no se creó otra fila."
        else:
            texto = mensajes.get(resultado.estado, "Evento recibido.")
        await update.effective_message.reply_text(
            texto + "\nID: " + resultado.external_id
        )

    async def recuperar(self, application):
        def factory(evento):
            return self.descargar_factory(application.bot, evento)

        resultados = await self.procesador.recuperar_pendientes(
            self.autorizacion,
            factory,
        )
        for resultado in resultados:
            LOGGER.info(
                "recuperacion evento=%s estado=%s codigo=%s",
                resultado.external_id,
                resultado.estado,
                resultado.codigo,
            )


def iniciar_servidor_api(cola, secreto):
    servidor = crear_servidor(
        cola, secreto, port=int(os.environ.get("PORT", "8080"))
    )
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def inspeccionar_cli():
    resultado = inspeccionar_spreadsheet()
    print(json.dumps(resultado, ensure_ascii=False, indent=2, sort_keys=True))


def preparar_cli():
    resultado = preparar_spreadsheet()
    print(json.dumps(resultado, ensure_ascii=False, indent=2, sort_keys=True))


def ejecutar_bot():
    from telegram.ext import ApplicationBuilder, MessageHandler, filters

    token = _variable_requerida("TELEGRAM_TOKEN")
    api_key = _variable_requerida("OPENAI_API_KEY")
    secreto_sincronizacion = _variable_requerida(
        "GUIVALTEX_SYNC_SHARED_SECRET"
    )
    autorizacion = AutorizacionTelegram.desde_entorno()
    cola = conectar_cola()
    servicios = ServiciosOpenAI(api_key)
    runtime = RuntimeTelegram(
        autorizacion,
        ProcesadorCola(cola, servicios.transcribir, servicios.interpretar),
    )

    async def post_init(application):
        await runtime.recuperar(application)

    application = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )
    application.add_handler(MessageHandler(filters.VOICE, runtime.manejar_audio))
    iniciar_servidor_api(cola, secreto_sincronizacion)
    LOGGER.info(
        "Bot iniciado transcripcion=%s interpretacion=%s cola=Cola_Guivaltex_V1",
        MODELO_TRANSCRIPCION,
        MODELO_INTERPRETACION,
    )
    application.run_polling(drop_pending_updates=False)


def main():
    comando = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if comando == "inspeccionar-sheet":
        inspeccionar_cli()
    elif comando == "preparar-sheet":
        preparar_cli()
    elif comando == "bot":
        ejecutar_bot()
    else:
        raise SystemExit(
            "Uso: python bot.py [bot|inspeccionar-sheet|preparar-sheet]"
        )


if __name__ == "__main__":
    main()
