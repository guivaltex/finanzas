"""Runtime Render: Telegram -> OpenAI -> Cola_Guivaltex_V1."""
import json
import logging
import os
import sys
import threading

from render_finanzas_api import crear_servidor
from bot_finanzas import (
    AutorizacionTelegram,
    CATEGORIAS_INTERPRETACION,
    CONTEXTO_TRANSCRIPCION,
    campos_explicitos_transcripcion,
    CONTRATO_INTERPRETACION,
    ESFUERZO_INTERPRETACION,
    EventoTelegram,
    IDIOMAS_TRANSCRIPCION,
    MODELO_INTERPRETACION,
    MODELO_TRANSCRIPCION,
    NOMBRES_CATEGORIAS,
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
  y de las palabras empresa/fabrica/hogar. Si tipo o ambito no son null,
  copialos exactamente al JSON:
  solo las excepciones especiales de ambito indicadas abajo pueden cambiarlo.
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
- Cada categoria incluye tipo, ambito, significado y señales. Descarta
  categorias incompatibles con el tipo o ambito ya resuelto.
- Usa los significados de las categorias para inferir por contexto, sin limitarte
  a una lista literal de palabras. otros_gastos solo si no existe categoria especifica.
- Materiales e insumos se unifican en materiales, gasto de empresa. Incluye guantes,
  regleta, Madecanto, ferreteria, portavasos, patas, sonido y compra de caja fuerte.
  Reconoce tambien otros materiales/componentes/consumibles por su significado.
- Acarreo o transporte de sillas es gasto de empresa/transporte salvo ambito explicito.
- Pago de sueldo, costura/tapicero con sueldo, prestamo o anticipo sobre sueldo
  es gasto de empresa/sueldos y clase=movimiento. No exige comprobante de nomina
  ni debe marcarse fuente_canonica_nomina. No conviertas una devolucion recibida
  de un prestamo en gasto ni inventes un pago.
- Cuatro por mil, 4x1000 o GMF es gasto de empresa/impuestos.
- Publicidad, pauta o anuncios es gasto de empresa/publicidad.
- Compra o pago de colchon a proveedor para reventa es gasto de empresa/colchon;
  la venta de colchon es ingreso de empresa/colchon. Distingue la operacion.
- Cerveza, licor y otras bebidas alcoholicas son gasto de hogar/diversion;
  esta y las reglas personales son excepciones especiales de ambito.
- Tienda o tienda Alvaro usa onces/empresa como respaldo; si el audio dice hogar
  usa comida/hogar, y si dice empresa usa onces/empresa. Alimentos concretos
  como almuerzo o chorizos siguen la regla de alimentacion.
- Alcancia o caja fuerte pueden describir una compra, gasto, ingreso externo o
  traslado de fondos propios. Identifica quien paga/recibe y el motivo. Guardar
  o retirar dinero propio no crea ingreso/gasto de la caja agregada. Si solo dice
  hogar alcancia o no queda clara la operacion, requiere revision.
- Jhoana/Joana/Jhohana y variaciones corresponden a hogar/Jhoana.
  Guillermo o Memo corresponden a hogar/Guillermo.
- Transporte puede ser empresa u hogar. Conserva siempre el ambito explicito;
  solo infierelo por contexto cuando campos_explicitos.ambito sea null.
- Con ambito explicito, nunca lo cambies por desayuno, almuerzo, comida, cena,
  onces, refrigerios o transporte; respeta las excepciones personales y de alcohol.
- Sin ambito explicito, desayuno/almuerzo/comida/cena/chorizos usan comida y hogar como
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


def telegram_esta_activo(application):
    updater = application.updater
    return bool(application.running and updater is not None and updater.running)


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
                "categorias_permitidas": CATEGORIAS_INTERPRETACION,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        respuesta = self.client.responses.create(
            model=MODELO_INTERPRETACION,
            instructions=INSTRUCCIONES_INTERPRETACION,
            input=entrada,
            reasoning={"effort": ESFUERZO_INTERPRETACION},
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

    @staticmethod
    def mensaje_resultado(resultado):
        motivos_humanos = {
            "fuente_canonica_nomina":
                "Registrar desde Nómina/comprobante de pago.",
            "posible_abono_canonico":
                "Registrar el pago desde Abonos en Guivaltex.",
            "importe_contradictorio":
                "El importe hablado es contradictorio o ambiguo.",
            "importe_no_coincide_con_evidencia":
                "El importe no coincide con lo pronunciado.",
            "categoria_invalida": "La categoría no es válida.",
            "categoria_tipo_incompatible":
                "La categoría no corresponde al tipo de movimiento.",
            "categoria_ambito_incompatible":
                "La categoría no corresponde al ámbito.",
            "ambito_invalido": "No fue posible determinar el ámbito.",
            "concepto_invalido": "No fue posible determinar el concepto.",
            "contrato_ia_invalido":
                "No fue posible interpretar el audio con seguridad.",
        }
        errores_humanos = {
            "conflicto_identidad":
                "El identificador ya existe con otro audio; requiere revisión.",
            "persistencia_no_disponible":
                "No fue posible confirmar la cola. Reintenta el mismo mensaje.",
            "descarga_fallida":
                "No se pudo descargar el audio. El evento quedó conservado.",
            "transcripcion_fallida":
                "No se pudo transcribir el audio. El evento quedó conservado.",
            "interpretacion_fallida":
                "No se pudo interpretar el audio. El evento quedó conservado.",
        }
        if resultado.estado == "listo":
            encabezado = "✅ Listo para sincronizar"
        elif resultado.estado == "excepcion":
            encabezado = "⚠️ Revisión necesaria"
        elif resultado.estado == "recibido":
            encabezado = "⏳ Conservado; reintento pendiente"
        else:
            encabezado = "❌ No se pudo completar"
        if resultado.codigo == "ya_procesado":
            encabezado = "ℹ️ Mensaje ya procesado\n" + encabezado

        partes = []
        if resultado.tipo:
            partes.append(resultado.tipo.capitalize())
        if resultado.ambito:
            partes.append(resultado.ambito.capitalize())
        if resultado.importe_centavos is not None:
            pesos = resultado.importe_centavos // 100
            partes.append("$" + format(pesos, ",").replace(",", "."))
        if resultado.categoria_codigo:
            partes.append(NOMBRES_CATEGORIAS.get(
                resultado.categoria_codigo,
                resultado.categoria_codigo.replace("_", " ").capitalize(),
            ))

        lineas = [encabezado]
        if partes:
            lineas.append(" · ".join(partes))
        if resultado.concepto:
            lineas.append(resultado.concepto)
        motivos = [
            motivos_humanos.get(
                motivo, motivo.replace("_", " ").capitalize() + "."
            )
            for motivo in resultado.motivos_revision
        ]
        if not motivos and resultado.codigo in errores_humanos:
            motivos.append(errores_humanos[resultado.codigo])
        if motivos:
            lineas.append("Motivo: " + " ".join(motivos))
        lineas.append("ID: " + resultado.external_id)
        return "\n".join(lineas)

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
        await update.effective_message.reply_text(
            self.mensaje_resultado(resultado)
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


def iniciar_servidor_api(cola, secreto, estado_telegram):
    servidor = crear_servidor(
        cola, secreto, estado_telegram=estado_telegram,
        port=int(os.environ.get("PORT", "8080"))
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

    async def post_stop(application):
        LOGGER.info("Polling de Telegram detenido; iniciando cierre.")

    async def post_shutdown(application):
        LOGGER.info("Runtime de Telegram cerrado.")

    application = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(MessageHandler(filters.VOICE, runtime.manejar_audio))
    def telegram_activo():
        return telegram_esta_activo(application)

    servidor = iniciar_servidor_api(
        cola, secreto_sincronizacion, telegram_activo
    )
    LOGGER.info(
        "Iniciando polling transcripcion=%s interpretacion=%s reasoning=%s "
        "cola=Cola_Guivaltex_V1",
        MODELO_TRANSCRIPCION,
        MODELO_INTERPRETACION,
        ESFUERZO_INTERPRETACION,
    )
    try:
        application.run_polling(drop_pending_updates=False)
    except Exception:
        LOGGER.exception("El polling de Telegram terminó por un fallo.")
        raise
    else:
        LOGGER.info("El polling de Telegram terminó normalmente.")
    finally:
        servidor.shutdown()
        servidor.server_close()
        LOGGER.info("Servidor HTTP de Render detenido.")


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
