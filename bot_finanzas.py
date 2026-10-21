"""Dominio determinista y cola durable para Telegram -> IA -> Google Sheets."""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata

from finanzas import (
    ErrorFinanzas, ErrorRevisionImporte, importe_a_centavos,
    normalizar_importe_hablado,
)

SCHEMA_VERSION = "1"
NOMBRE_COLA = "Cola_Guivaltex_V1"
MODELO_TRANSCRIPCION = "gpt-transcribe"
MODELO_INTERPRETACION = "gpt-5.6-terra"
ESFUERZO_INTERPRETACION = "medium"
IDIOMAS_TRANSCRIPCION = ("es",)
PALABRAS_CLAVE_TRANSCRIPCION = (
    "Guivaltex", "muebles", "botón", "botones", "Cherosky", "tela",
    "madera", "espuma", "materiales", "transporte", "acarreo", "sillas",
    "tintos", "desayunos", "onces", "refrigerios", "base cama",
    "espaldar", "sofá", "colchón", "silletería", "melamina",
)
CONTEXTO_TRANSCRIPCION = (
    "Audio breve en español de Colombia para registrar movimientos de "
    "Guivaltex, una fábrica de muebles. Transcribe únicamente lo dicho, "
    "respetando nombres propios y vocabulario del negocio; no completes ni "
    "inventes palabras."
)
ESTADOS_TERMINALES = ("listo", "excepcion")
ESTADOS_REANUDABLES = ("recibido", "procesando")

CATEGORIAS_FINANCIERAS_V1 = {
    "venta": ("ingreso", "empresa"), "abono": ("ingreso", "empresa"),
    "saldo": ("ingreso", "empresa"), "base_cama": ("ingreso", "empresa"),
    "espaldar": ("ingreso", "empresa"), "sofa": ("ingreso", "empresa"),
    "mesas": ("ingreso", "empresa"), "colchon": (None, "empresa"),
    "silleteria": ("ingreso", "empresa"), "refaccion": ("ingreso", "empresa"),
    "comida": ("gasto", "hogar"), "transporte": ("gasto", None),
    "diversion": ("gasto", "hogar"), "dulce": ("gasto", "hogar"),
    "salidas": ("gasto", "hogar"), "salud": ("gasto", "hogar"),
    "vivienda": ("gasto", "hogar"), "servicios_hogar": ("gasto", "hogar"),
    "celular": ("gasto", "hogar"), "educacion": ("gasto", "hogar"),
    "mercado": ("gasto", "hogar"), "materiales": ("gasto", "empresa"),
    "onces": ("gasto", "empresa"), "sueldos": ("gasto", "empresa"),
    "arriendo": ("gasto", "empresa"), "servicios_empresa": ("gasto", "empresa"),
    "deudas": ("gasto", "empresa"), "herramientas": ("gasto", "empresa"),
    "impuestos": ("gasto", "empresa"), "publicidad": ("gasto", "empresa"),
    "otros_ingresos": ("ingreso", None),
    "otros_gastos": ("gasto", None), "ajuste_caja": (None, None),
    "jhoana": ("gasto", "hogar"), "guillermo": ("gasto", "hogar"),
}
CATEGORIAS_RESERVADAS = frozenset(("abono", "saldo", "ajuste_caja"))
# Compatibilidad de respuestas anteriores; nunca emitir Insumos como categoria nueva.
ALIASES_CATEGORIAS = {"insumos": "materiales"}
NOMBRES_CATEGORIAS = {
    "base_cama": "Base cama", "servicios_hogar": "Servicios del hogar",
    "servicios_empresa": "Servicios de empresa",
    "otros_ingresos": "Otros ingresos", "otros_gastos": "Otros gastos",
    "ajuste_caja": "Ajuste de caja", "jhoana": "Jhoana",
    "guillermo": "Guillermo", "colchon": "Colchon",
    "impuestos": "Impuestos", "publicidad": "Publicidad",
}
SENALES_CATEGORIAS = {
    "materiales": ("materiales", "insumos", "guantes", "regleta", "Madecanto",
                   "ferreteria", "portavasos", "patas", "sonido", "caja fuerte"),
    "sueldos": ("sueldo", "salario", "nomina", "prestamo sobre sueldo",
                "anticipo de sueldo", "pago tapicero sueldo"),
    "impuestos": ("impuesto", "cuatro por mil", "4 por mil", "4x1000", "GMF"),
    "publicidad": ("publicidad", "pauta", "anuncio", "Facebook verificado"),
    "colchon": ("compra de colchon para reventa", "pago colchon", "venta colchon"),
    "diversion": ("cerveza", "licor", "aguardiente", "ron", "whisky"),
    "transporte": ("acarreo", "transporte"),
    "onces": ("tinto", "tintos", "onces", "refrigerio"),
    "comida": ("desayuno", "almuerzo", "comida", "cena", "chorizos", "tienda hogar"),
    "jhoana": ("Jhoana", "Joana", "Jhohana", "Johana", "Johanna"),
    "guillermo": ("Guillermo", "Memo"),
}
SIGNIFICADOS_INTERPRETACION = {
    "materiales": "Gasto de empresa: materiales, componentes y consumibles de muebles. "
                 "Incluye lo antes llamado insumos. Inferir por el objeto comprado, no "
                 "solo por estas senales; no exige enumerar cada material. Una compra "
                 "de colchon para reventa usa colchon. Ahorros o traslados no son materiales.",
    "sueldos": "Gasto de empresa: pago de trabajo, salario, nomina o prestamo/anticipo "
               "sobre sueldo. Permitir como movimiento externo sin exigir comprobante. "
               "No reclasificar una devolucion recibida como gasto ni inventar un pago.",
    "colchon": "Ingreso de empresa por venta de colchon, o gasto de empresa por compra "
               "de colchon a un proveedor para reventa. El tipo depende de la operacion.",
    "impuestos": "Gasto de empresa: impuestos, cuatro por mil / 4x1000 / GMF.",
    "publicidad": "Gasto de empresa: anuncios, pauta, publicidad o Facebook verificado.",
    "onces": "Gasto de empresa: refrigerios, tintos y compras de tienda para la fabrica.",
    "comida": "Gasto de hogar: alimentacion, almuerzo, desayuno, cena, chorizos y tienda "
              "cuando el audio dice hogar. Si dice empresa, alimentacion usa onces.",
    "diversion": "Gasto de hogar: cerveza, licor y otras bebidas alcoholicas.",
    "otros_ingresos": "Ingreso externo sin categoria mas especifica. Mencionar alcancia "
                      "no basta: identificar quien entrega dinero y por que. Retirar "
                      "fondos propios de una alcancia no crea ingreso agregado.",
    "otros_gastos": "Gasto externo sin categoria mas especifica. No usar como escape "
                    "para sueldos, materiales, transporte, comida o publicidad. Alcancia "
                    "sin operacion clara requiere revision; ahorrar o mover fondos propios "
                    "no es por si mismo gasto de la caja agregada.",
}
CATEGORIAS_INTERPRETACION = tuple(
    {
        "codigo": codigo,
        "tipo_permitido": regla[0],
        "ambito_permitido": regla[1],
        "significado": SIGNIFICADOS_INTERPRETACION.get(
            codigo, NOMBRES_CATEGORIAS.get(
                codigo, codigo.replace("_", " ").capitalize()
            )
        ),
        "aliases_senales": list(SENALES_CATEGORIAS.get(codigo, ())),
    }
    for codigo, regla in sorted(CATEGORIAS_FINANCIERAS_V1.items())
)
MOTIVOS_REVISION_PERMITIDOS = frozenset((
    "ambito_ambiguo", "ambito_invalido", "ambito_omitido",
    "categoria_ambigua", "categoria_ambito_incompatible",
    "categoria_invalida", "categoria_reservada_fuente_canonica",
    "categoria_tipo_incompatible", "concepto_invalido",
    "contradiccion_datos", "evidencia_importe_ausente",
    "factura_hogar_incompatible", "factura_invalida",
    "fecha_efectiva_ausente", "fecha_efectiva_invalida",
    "fuente_canonica_nomina", "ia_requiere_revision",
    "importe_ausente", "importe_contradictorio", "importe_invalido",
    "importe_no_coincide_con_evidencia", "ingreso_factura_requiere_abonos",
    "nota_no_financiera", "posible_abono_canonico",
    "tipo_invalido", "transcripcion_insuficiente",
))
CAMPOS_INTERPRETACION = (
    "clase", "tipo", "importe", "fecha_efectiva", "concepto", "ambito",
    "categoria", "factura_id", "requiere_revision", "motivos_revision",
    "evidencia_importe",
)
CONTRATO_INTERPRETACION = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "clase": {"type": "string", "enum": ["movimiento", "posible_abono", "nota"]},
        "tipo": {"type": ["string", "null"], "enum": ["ingreso", "gasto", None]},
        "importe": {"type": ["string", "null"]},
        "fecha_efectiva": {"type": ["string", "null"]},
        "concepto": {"type": ["string", "null"]},
        "ambito": {"type": ["string", "null"], "enum": ["empresa", "hogar", None]},
        "categoria": {
            "type": ["string", "null"],
            "enum": sorted(CATEGORIAS_FINANCIERAS_V1) + [None],
        },
        "factura_id": {"type": ["string", "null"]},
        "requiere_revision": {"type": "boolean"},
        "motivos_revision": {
            "type": "array",
            "items": {
                "type": "string", "enum": sorted(MOTIVOS_REVISION_PERMITIDOS),
            },
        },
        "evidencia_importe": {"type": ["string", "null"]},
    },
    "required": list(CAMPOS_INTERPRETACION),
}
COLUMNAS_COLA = (
    "external_id", "fecha_mensaje", "recuperacion_json",
    "transcripcion_original", "payload_json", "payload_sha256", "tipo",
    "importe_cop", "fecha_efectiva", "concepto", "ambito",
    "categoria_codigo", "factura_id", "estado", "motivo_revision",
    "sincronizacion_estado", "sincronizado_en",
)


class ErrorBotFinanzas(ValueError):
    pass


class ErrorContrato(ErrorBotFinanzas):
    pass


class ErrorPersistencia(ErrorBotFinanzas):
    pass


class ConflictoIdentidad(ErrorBotFinanzas):
    pass


def ahora_utc():
    return datetime.now(timezone.utc).isoformat()


def json_canonico(valor):
    try:
        return json.dumps(valor, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ErrorContrato("El contenido no es JSON valido.")


def sha256_texto(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def entero_identidad(valor, nombre):
    if isinstance(valor, bool):
        raise ErrorBotFinanzas(nombre + " invalido.")
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise ErrorBotFinanzas(nombre + " invalido.")
    if str(numero) != str(valor).strip():
        raise ErrorBotFinanzas(nombre + " invalido.")
    return numero


def crear_external_id(chat_id, message_id):
    return "telegram:{0}:{1}".format(
        entero_identidad(chat_id, "chat_id"),
        entero_identidad(message_id, "message_id"),
    )


def fecha_mensaje_iso(valor):
    if not isinstance(valor, datetime):
        raise ErrorBotFinanzas("La fecha del mensaje es invalida.")
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class EventoTelegram:
    chat_id: int
    message_id: int
    user_id: int
    file_id: str
    file_unique_id: str
    fecha_mensaje: str

    @property
    def external_id(self):
        return crear_external_id(self.chat_id, self.message_id)

    @property
    def recuperacion_json(self):
        return json_canonico({
            "user_id": self.user_id,
            "file_id": self.file_id,
            "file_unique_id": self.file_unique_id,
        })

    @classmethod
    def crear(cls, chat_id, message_id, user_id, file_id, file_unique_id,
              fecha_mensaje):
        chat_id = entero_identidad(chat_id, "chat_id")
        message_id = entero_identidad(message_id, "message_id")
        user_id = entero_identidad(user_id, "user_id")
        file_id, file_unique_id = str(file_id or "").strip(), str(file_unique_id or "").strip()
        if not file_id or not file_unique_id:
            raise ErrorBotFinanzas("El audio no contiene identidad de archivo.")
        if max(len(file_id), len(file_unique_id)) > 500:
            raise ErrorBotFinanzas("La identidad del audio excede el limite.")
        return cls(chat_id, message_id, user_id, file_id, file_unique_id,
                   fecha_mensaje_iso(fecha_mensaje))

    @classmethod
    def desde_fila(cls, fila):
        try:
            fecha = datetime.fromisoformat(fila["fecha_mensaje"])
            recuperacion = json.loads(fila["recuperacion_json"])
            if (not isinstance(recuperacion, dict)
                    or set(recuperacion) != {
                        "user_id", "file_id", "file_unique_id"
                    }):
                raise ValueError
            identidad = re.fullmatch(
                r"telegram:(-?\d+):(\d+)", str(fila["external_id"])
            )
            if identidad is None:
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ErrorPersistencia("La fila pendiente no permite recuperacion.")
        return cls.crear(
            identidad.group(1), identidad.group(2),
            recuperacion["user_id"], recuperacion["file_id"],
            recuperacion["file_unique_id"], fecha,
        )


class AutorizacionTelegram:
    def __init__(self, chat_ids, user_ids):
        self.chat_ids, self.user_ids = frozenset(chat_ids), frozenset(user_ids)
        if not self.chat_ids or not self.user_ids:
            raise ErrorBotFinanzas("Configura listas explicitas de chats y usuarios autorizados.")

    @staticmethod
    def lista(valor, nombre):
        partes = [p.strip() for p in str(valor or "").split(",") if p.strip()]
        if not partes:
            raise ErrorBotFinanzas("Falta " + nombre + ".")
        return {entero_identidad(parte, nombre) for parte in partes}

    @classmethod
    def desde_entorno(cls, entorno=None):
        entorno = entorno or os.environ
        return cls(
            cls.lista(entorno.get("GUIVALTEX_TELEGRAM_ALLOWED_CHAT_IDS"),
                      "GUIVALTEX_TELEGRAM_ALLOWED_CHAT_IDS"),
            cls.lista(entorno.get("GUIVALTEX_TELEGRAM_ALLOWED_USER_IDS"),
                      "GUIVALTEX_TELEGRAM_ALLOWED_USER_IDS"),
        )

    def permite(self, chat_id, user_id):
        try:
            chat_id, user_id = entero_identidad(chat_id, "chat_id"), entero_identidad(user_id, "user_id")
        except ErrorBotFinanzas:
            return False
        return chat_id in self.chat_ids and user_id in self.user_ids


@dataclass(frozen=True)
class ResultadoInterpretacion:
    estado: str
    payload_json: str
    payload_sha256: str
    clase: str
    tipo: object
    importe_centavos: object
    fecha_efectiva: object
    concepto: object
    ambito: object
    categoria_codigo: object
    factura_id: object
    requiere_revision: bool
    motivos_revision: tuple
    evidencia_importe: object
    error_codigo: str
    error_detalle: str


@dataclass(frozen=True)
class ResultadoProceso:
    external_id: str
    estado: str
    codigo: str
    repetido: bool = False
    tipo: object = None
    importe_centavos: object = None
    concepto: object = None
    ambito: object = None
    categoria_codigo: object = None
    motivos_revision: tuple = ()


def crear_resultado_proceso(external_id, estado, codigo, repetido=False,
                            interpretacion=None, fila=None):
    valores = {
        "tipo": None, "importe_centavos": None, "concepto": None,
        "ambito": None, "categoria_codigo": None, "motivos_revision": (),
    }
    if interpretacion is not None:
        valores.update({
            "tipo": interpretacion.tipo,
            "importe_centavos": interpretacion.importe_centavos,
            "concepto": interpretacion.concepto,
            "ambito": interpretacion.ambito,
            "categoria_codigo": interpretacion.categoria_codigo,
            "motivos_revision": interpretacion.motivos_revision,
        })
    elif fila is not None:
        importe_cop = texto_opcional(fila.get("importe_cop"))
        importe_centavos = None
        if importe_cop:
            try:
                importe_centavos = importe_a_centavos(Decimal(importe_cop))
            except ErrorFinanzas:
                pass
        valores.update({
            "tipo": texto_opcional(fila.get("tipo")),
            "importe_centavos": importe_centavos,
            "concepto": texto_opcional(fila.get("concepto")),
            "ambito": texto_opcional(fila.get("ambito")),
            "categoria_codigo": texto_opcional(fila.get("categoria_codigo")),
            "motivos_revision": tuple(
                parte.strip()
                for parte in str(fila.get("motivo_revision") or "").split(",")
                if parte.strip()
            ),
        })
    return ResultadoProceso(
        external_id, estado, codigo, repetido=repetido, **valores
    )


def texto_opcional(valor):
    texto = None if valor is None else str(valor).strip()
    return texto or None


def validar_contrato(datos):
    if not isinstance(datos, dict) or set(datos) != set(CAMPOS_INTERPRETACION):
        raise ErrorContrato("La respuesta no coincide con el contrato exacto.")
    if datos["clase"] not in ("movimiento", "posible_abono", "nota"):
        raise ErrorContrato("Clase externa invalida.")
    for campo in ("tipo", "importe", "fecha_efectiva", "concepto", "ambito",
                  "categoria", "factura_id", "evidencia_importe"):
        if datos[campo] is not None and not isinstance(datos[campo], str):
            raise ErrorContrato("Tipo invalido para " + campo + ".")
    if type(datos["requiere_revision"]) is not bool:
        raise ErrorContrato("requiere_revision debe ser booleano.")
    if not isinstance(datos["motivos_revision"], list) or any(
            not isinstance(m, str) for m in datos["motivos_revision"]):
        raise ErrorContrato("motivos_revision debe ser una lista de textos.")
    return datos


def agregar_motivo(motivos, motivo):
    motivo = re.sub(
        r"[^a-z0-9]+", "_", _texto_sin_tildes(motivo)
    ).strip("_")
    if motivo not in MOTIVOS_REVISION_PERMITIDOS:
        motivo = "ia_requiere_revision"
    if motivo and motivo not in motivos:
        motivos.append(motivo)


_NUMEROS_HASTA_29 = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20,
    "veintiun": 21, "veintiuno": 21, "veintiuna": 21,
    "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
    "veintiocho": 28, "veintinueve": 29,
}
_DECENAS = {
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
    "setenta": 70, "ochenta": 80, "noventa": 90,
}
_CENTENAS = {
    "cien": 100, "ciento": 100, "doscientos": 200,
    "trescientos": 300, "cuatrocientos": 400, "quinientos": 500,
    "seiscientos": 600, "setecientos": 700, "ochocientos": 800,
    "novecientos": 900,
}


def _texto_sin_tildes(valor):
    normalizado = unicodedata.normalize("NFKD", str(valor or "").lower())
    return " ".join(
        "".join(c for c in normalizado if not unicodedata.combining(c)).split()
    )


def campos_explicitos_transcripcion(transcripcion):
    """Reconoce tipo y ámbitos pronunciados, incluida «fábrica»."""
    tokens = re.findall(r"[a-z0-9]+", _texto_sin_tildes(transcripcion))
    tipo = tokens[0] if tokens and tokens[0] in ("ingreso", "gasto") else None
    posicion = 1 if tipo is not None else 0
    inmediato = tokens[posicion] if len(tokens) > posicion else None
    if inmediato in ("empresa", "fabrica", "hogar"):
        ambito = "empresa" if inmediato in ("empresa", "fabrica") else "hogar"
    elif any(token in ("empresa", "fabrica") for token in tokens):
        ambito = "empresa"
    elif "hogar" in tokens:
        ambito = "hogar"
    else:
        ambito = None
    return {"tipo": tipo, "ambito": ambito}


def _numero_espanol_hasta_999(texto):
    partes = [p for p in _texto_sin_tildes(texto).split() if p != "y"]
    if not partes:
        return None
    total = 0
    if partes[0] in _CENTENAS:
        centena = partes.pop(0)
        if centena == "cien" and partes:
            return None
        total += _CENTENAS[centena]
    if not partes:
        return total or None
    if len(partes) == 1 and partes[0] in _NUMEROS_HASTA_29:
        total += _NUMEROS_HASTA_29[partes[0]]
    elif (len(partes) == 1 and partes[0] in _DECENAS):
        total += _DECENAS[partes[0]]
    elif (len(partes) == 2 and partes[0] in _DECENAS
          and partes[1] in _NUMEROS_HASTA_29
          and _NUMEROS_HASTA_29[partes[1]] < 10):
        total += _DECENAS[partes[0]] + _NUMEROS_HASTA_29[partes[1]]
    else:
        return None
    return total if 0 < total <= 999 else None


def normalizar_evidencia_importe(evidencia):
    try:
        return normalizar_importe_hablado(evidencia)
    except ErrorRevisionImporte as error_original:
        texto = _texto_sin_tildes(evidencia)
        texto = re.sub(r" (?:peso|pesos|cop)$", "", texto)
        coincidencia = re.fullmatch(
            r"(?P<miles>[a-z ]+?) mil"
            r"(?: (?P<resto>[a-z ]+?))?",
            texto,
        )
        if coincidencia is None:
            raise error_original
        miles = _numero_espanol_hasta_999(coincidencia.group("miles"))
        resto_texto = coincidencia.group("resto")
        resto = _numero_espanol_hasta_999(resto_texto) if resto_texto else 0
        if miles is None or (resto_texto and resto is None):
            raise error_original
        return Decimal(miles * 1000 + resto)


_REFRIGERIO_OPERATIVO = re.compile(
    r"\b(?:tinto|tintos|onces|refrigerio|refrigerios)\b"
)
_ALIMENTACION_HOGAR = re.compile(
    r"\b(?:desayuno|desayunos|almuerzo|almuerzos|comida|comidas|cena|cenas|chorizos?)\b"
)
_CATEGORIAS_ALIMENTACION = frozenset(("comida", "onces", "otros_gastos"))
_NOMBRE_JHOANA = re.compile(r"\b(?:j|y)h?oh?a?n{1,2}a\b")
_NOMBRE_GUILLERMO = re.compile(r"\b(?:guillermo|memo)\b")
_GUANTES = re.compile(r"\bguantes?\b")
_ACARREO = re.compile(r"\bacarreos?\b")
_PRESTAMO_NOMINA = re.compile(
    r"\b(?:prestamo|prestamos|anticipo|anticipos)\b"
)
_SUELDO_NOMINA = re.compile(
    r"\b(?:sueldo|sueldos|salario|salarios|nomina)\b"
)


_TIENDA = re.compile(r"\btiendas?\b")
_TRANSPORTE = re.compile(r"\b(?:transporte|acarreos?)\b")
_ALCOHOL = re.compile(
    r"\b(?:cervezas?|licor(?:es)?|aguardiente|ron|whisk(?:e)?y|vodka|tequila)\b"
)
_IMPUESTOS = re.compile(
    r"\b(?:impuestos?|gmf)\b|\b(?:cuatro|4)\s*(?:por|x)\s*(?:mil|1000)\b"
)
_PUBLICIDAD = re.compile(
    r"\b(?:publicidad|pauta|anuncios?)\b|\bfacebook\s+verificad[oa]\b"
)
_MATERIALES = re.compile(
    r"\b(?:material(?:es)?|insumos?|guantes?|regletas?|madecanto|ferreteria|"
    r"portavasos|patas?|sonido|terlenca|satin|pranatex|madera|espuma|telas?|"
    r"melamina|tiner|thinner)\b|\bcaja\s+fuerte\b"
)
_COLCHON = re.compile(r"\bcolchon(?:es)?\b")
_ALCANCIA = re.compile(r"\balcancias?\b")
_DEPOSITO_FONDOS = re.compile(r"\b(?:alcancias?|caja\s+(?:fuerte|ahorro))\b")
_TRASLADO_FONDOS = re.compile(
    r"\b(?:ahorro|ahorrar|ahorre|deposito|depositar|deposite|guardar|guarde|"
    r"retirar|retiro|retire|sacar|saque|sacaron|sacamos|coger|cogi|cogieron|traslado|transferencia)\b"
)
_OPERACION_EXTERNA = re.compile(
    r"\b(?:pague|pagamos|pagaron|pago|compre|compramos|compra|vendi|vendimos|venta|"
    r"recibi|recibimos|regalo|regalaron|donacion|"
    r"reparacion|arreglo|fabricacion|sonido|transporte)\b"
)


def es_traslado_fondos(concepto, transcripcion=None):
    texto = _texto_sin_tildes(transcripcion or concepto)
    return bool(_DEPOSITO_FONDOS.search(texto)
                and _TRASLADO_FONDOS.search(texto)
                and not _OPERACION_EXTERNA.search(texto))


def normalizar_reglas_negocio(tipo, categoria, ambito, concepto,
                              transcripcion=None, ambito_explicito=False):
    """Aplica reglas confirmadas; no cambia importes, fuente ni fechas."""
    categoria = ALIASES_CATEGORIAS.get(categoria, categoria)
    texto = _texto_sin_tildes(transcripcion or concepto)
    empresa = ambito if ambito_explicito else "empresa"
    if tipo == "gasto" and _SUELDO_NOMINA.search(texto):
        return "sueldos", empresa, True
    if _NOMBRE_JHOANA.search(texto):
        return "jhoana", "hogar", True
    if _NOMBRE_GUILLERMO.search(texto):
        return "guillermo", "hogar", True
    if tipo != "gasto":
        return categoria, ambito, False
    # Evita que caja fuerte vuelva materiales un ahorro/traslado de dinero.
    if es_traslado_fondos(concepto, transcripcion):
        return "otros_gastos", ambito, True
    if _ALCOHOL.search(texto):
        return "diversion", "hogar", True
    if _IMPUESTOS.search(texto):
        return "impuestos", empresa, True
    if _PUBLICIDAD.search(texto):
        return "publicidad", empresa, True
    if _TRANSPORTE.search(texto):
        return "transporte", empresa, True
    if _COLCHON.search(texto):
        return "colchon", empresa, True
    if _MATERIALES.search(texto):
        return "materiales", empresa, True
    if (_REFRIGERIO_OPERATIVO.search(texto)
            or _ALIMENTACION_HOGAR.search(texto)):
        return normalizar_alimentacion(
            tipo, "otros_gastos", ambito, concepto, ambito_explicito,
            transcripcion
        ) + (True,)
    if _TIENDA.search(texto):
        destino = ambito if ambito_explicito else "empresa"
        return ("comida" if destino == "hogar" else "onces"), destino, True
    return categoria, ambito, False


def es_fuente_canonica_nomina(concepto, transcripcion=None):
    texto = _texto_sin_tildes(
        " ".join(filter(None, (transcripcion, concepto)))
    )
    return bool(_PRESTAMO_NOMINA.search(texto)
                and _SUELDO_NOMINA.search(texto))


def normalizar_alimentacion(tipo, categoria, ambito, concepto,
                           ambito_explicito, transcripcion=None):
    """Aplica ámbito explícito primero y reglas sobre el audio original."""
    if tipo != "gasto" or categoria not in _CATEGORIAS_ALIMENTACION:
        return categoria, ambito
    texto = _texto_sin_tildes(transcripcion or concepto)
    es_refrigerio = _REFRIGERIO_OPERATIVO.search(texto) is not None
    es_comida = _ALIMENTACION_HOGAR.search(texto) is not None
    if not (es_refrigerio or es_comida):
        return categoria, ambito
    if ambito_explicito:
        return ("onces" if ambito == "empresa" else "comida"), ambito
    if es_refrigerio:
        return "onces", "empresa"
    return "comida", "hogar"


def validar_interpretacion(external_id, respuesta, transcripcion=None):
    if isinstance(respuesta, str):
        try:
            respuesta = json.loads(respuesta)
        except (TypeError, ValueError):
            raise ErrorContrato("La IA no devolvio JSON valido.")
    datos = validar_contrato(respuesta)
    clase = datos["clase"]
    tipo, importe = texto_opcional(datos["tipo"]), texto_opcional(datos["importe"])
    fecha_efectiva = texto_opcional(datos["fecha_efectiva"])
    concepto, ambito = texto_opcional(datos["concepto"]), texto_opcional(datos["ambito"])
    categoria, factura_id = texto_opcional(datos["categoria"]), texto_opcional(datos["factura_id"])
    evidencia = texto_opcional(datos["evidencia_importe"])
    explicitos = campos_explicitos_transcripcion(transcripcion)
    tipo_explicito = explicitos["tipo"] is not None
    ambito_explicito = explicitos["ambito"] is not None
    if tipo_explicito:
        tipo = explicitos["tipo"]
    if ambito_explicito:
        ambito = explicitos["ambito"]

    categoria_original, ambito_previo = categoria, ambito
    categoria, ambito, regla_fuerte = normalizar_reglas_negocio(
        tipo, categoria, ambito, concepto, transcripcion, ambito_explicito
    )
    categoria, ambito = normalizar_alimentacion(
        tipo, categoria, ambito, concepto, ambito_explicito, transcripcion
    )
    regla_categoria = CATEGORIAS_FINANCIERAS_V1.get(categoria)
    categoria_incompatible_ia = (
        regla_categoria is not None and regla_categoria[0] is not None
        and tipo in ("ingreso", "gasto") and regla_categoria[0] != tipo
    )
    if (tipo == "gasto" and not regla_fuerte
            and (categoria is None or categoria_incompatible_ia)):
        categoria = "otros_gastos"
    categoria_inferida = categoria != categoria_original
    ambito_inferido_regla = ambito != ambito_previo
    regla_categoria = CATEGORIAS_FINANCIERAS_V1.get(categoria)
    ambito_inferido_categoria = (
        ambito is None and regla_categoria is not None
        and regla_categoria[1] in ("empresa", "hogar")
    )
    if ambito_inferido_categoria:
        ambito = regla_categoria[1]
    ambito_resuelto = (ambito_explicito or ambito_inferido_regla
                       or ambito_inferido_categoria)
    fallback_otros_gastos = (
        tipo == "gasto" and categoria == "otros_gastos"
        and ambito in ("empresa", "hogar") and bool(concepto)
    )
    categoria_resuelta = (
        categoria_inferida or regla_fuerte or fallback_otros_gastos
    )

    motivos = []
    if (categoria_original is not None
            and categoria_original not in CATEGORIAS_FINANCIERAS_V1
            and categoria_original not in ALIASES_CATEGORIAS):
        agregar_motivo(motivos, "categoria_invalida")
    if datos["requiere_revision"]:
        for motivo in datos["motivos_revision"]:
            motivo_normalizado = _texto_sin_tildes(motivo)
            if (tipo == "gasto" and categoria == "sueldos"
                    and motivo_normalizado == "fuente_canonica_nomina"):
                continue
            if ((tipo_explicito and "tipo" in motivo_normalizado)
                    or (ambito_resuelto and "ambito" in motivo_normalizado)
                    or (categoria_resuelta
                        and "categoria" in motivo_normalizado)):
                continue
            agregar_motivo(motivos, motivo)
        if not motivos and not (tipo_explicito or ambito_resuelto
                                or categoria_resuelta):
            agregar_motivo(motivos, "ia_requiere_revision")
    if clase == "nota":
        agregar_motivo(motivos, "nota_no_financiera")
    if clase == "posible_abono":
        agregar_motivo(motivos, "posible_abono_canonico")
    if es_traslado_fondos(concepto, transcripcion):
        agregar_motivo(motivos, "contradiccion_datos")
    elif (_ALCANCIA.search(_texto_sin_tildes(transcripcion or concepto))
            and categoria in (None, "otros_gastos", "otros_ingresos")
            and not _OPERACION_EXTERNA.search(
                _texto_sin_tildes(transcripcion or concepto))):
        agregar_motivo(motivos, "transcripcion_insuficiente")

    if tipo not in ("ingreso", "gasto"):
        agregar_motivo(motivos, "tipo_invalido")
    if ambito not in ("empresa", "hogar"):
        agregar_motivo(motivos, "ambito_invalido")
    if not concepto or len(concepto) > 500:
        agregar_motivo(motivos, "concepto_invalido")

    if regla_categoria is None:
        agregar_motivo(motivos, "categoria_invalida")
    else:
        tipo_categoria, ambito_categoria = regla_categoria
        if tipo_categoria is not None and tipo != tipo_categoria:
            agregar_motivo(motivos, "categoria_tipo_incompatible")
        if ambito_categoria is not None and ambito != ambito_categoria:
            agregar_motivo(motivos, "categoria_ambito_incompatible")
        if categoria in CATEGORIAS_RESERVADAS:
            agregar_motivo(motivos, "categoria_reservada_fuente_canonica")

    if factura_id is not None:
        if len(factura_id) > 100 or any(ord(c) < 32 for c in factura_id):
            agregar_motivo(motivos, "factura_invalida")
        if ambito == "hogar":
            agregar_motivo(motivos, "factura_hogar_incompatible")
        if tipo == "ingreso":
            agregar_motivo(motivos, "ingreso_factura_requiere_abonos")

    importe_centavos = None
    if not importe:
        agregar_motivo(motivos, "importe_ausente")
    if not evidencia:
        agregar_motivo(motivos, "evidencia_importe_ausente")
    if importe and evidencia:
        try:
            centavos_evidencia = importe_a_centavos(normalizar_evidencia_importe(evidencia))
            propuesto = Decimal(importe)
            if not propuesto.is_finite():
                raise InvalidOperation
            centavos_propuestos = importe_a_centavos(propuesto)
            if centavos_propuestos != centavos_evidencia:
                agregar_motivo(motivos, "importe_no_coincide_con_evidencia")
            else:
                importe_centavos = centavos_evidencia
        except ErrorRevisionImporte:
            agregar_motivo(motivos, "importe_contradictorio")
        except (ErrorFinanzas, InvalidOperation, ValueError):
            agregar_motivo(motivos, "importe_invalido")

    if fecha_efectiva:
        try:
            if date.fromisoformat(fecha_efectiva).isoformat() != fecha_efectiva:
                raise ValueError
        except (TypeError, ValueError):
            agregar_motivo(motivos, "fecha_efectiva_invalida")
    else:
        agregar_motivo(motivos, "fecha_efectiva_ausente")

    texto_abono = " ".join(filter(None, (concepto, categoria, clase))).lower()
    if tipo == "ingreso" and re.search(r"\b(abono|saldo|cobro)\b", texto_abono):
        agregar_motivo(motivos, "posible_abono_canonico")

    datos_financieros = None
    if (tipo in ("ingreso", "gasto") and ambito in ("empresa", "hogar")
            and categoria in CATEGORIAS_FINANCIERAS_V1
            and importe_centavos is not None and fecha_efectiva and concepto):
        datos_financieros = {
            "tipo": tipo, "importe_centavos": importe_centavos,
            "fecha_efectiva": fecha_efectiva, "concepto": concepto,
            "ambito": ambito, "categoria_codigo": categoria,
            "factura_id": factura_id,
        }

    requiere_revision = bool(motivos)
    payload = {
        "schema_version": SCHEMA_VERSION, "external_id": external_id,
        "interpretacion": datos, "datos_financieros": datos_financieros,
        "requiere_revision": requiere_revision, "motivos_revision": motivos,
    }
    payload_json = json_canonico(payload)
    estado = "excepcion" if requiere_revision else "listo"
    return ResultadoInterpretacion(
        estado, payload_json, sha256_texto(payload_json), clase, tipo,
        importe_centavos, fecha_efectiva, concepto, ambito, categoria,
        factura_id, requiere_revision, tuple(motivos), evidencia,
        "" if estado == "listo" else "requiere_revision",
        "" if estado == "listo" else "El evento requiere revision.",
    )


def excepcion_contrato(external_id, respuesta):
    try:
        texto = respuesta if isinstance(respuesta, str) else json_canonico(respuesta)
    except ErrorContrato:
        texto = repr(type(respuesta))
    payload = {
        "schema_version": SCHEMA_VERSION, "external_id": external_id,
        "interpretacion": None, "datos_financieros": None,
        "requiere_revision": True, "motivos_revision": ["contrato_ia_invalido"],
        "respuesta_ia_sha256": sha256_texto(texto),
    }
    payload_json = json_canonico(payload)
    return ResultadoInterpretacion(
        "excepcion", payload_json, sha256_texto(payload_json), "", None, None,
        None, None, None, None, None, True, ("contrato_ia_invalido",), None,
        "contrato_ia_invalido",
        "La respuesta de IA no cumple el contrato estructurado.",
    )


def fila_vacia():
    return {columna: "" for columna in COLUMNAS_COLA}


def valor_celda(valor):
    if valor is None:
        return ""
    if type(valor) is bool:
        return "true" if valor else "false"
    return str(valor)


def importe_cop_celda(centavos):
    if centavos is None:
        return ""
    centavos = int(centavos)
    pesos, fraccion = divmod(centavos, 100)
    return str(pesos) if fraccion == 0 else "{0}.{1:02d}".format(pesos, fraccion)

def columna_a1(numero):
    resultado = ""
    while numero:
        numero, residuo = divmod(numero - 1, 26)
        resultado = chr(65 + residuo) + resultado
    return resultado


class GoogleSheetsQueue:
    """Una sola fila mutable por external_id; nunca elimina ni usa otra pestana."""

    def __init__(self, worksheet):
        self.worksheet, self._lock = worksheet, threading.RLock()

    def verificar_encabezado(self):
        if tuple(self.worksheet.row_values(1)) != COLUMNAS_COLA:
            raise ErrorPersistencia("Cola_Guivaltex_V1 tiene encabezado incompatible.")

    def todas_las_filas(self):
        valores = self.worksheet.get_all_values()
        if not valores or tuple(valores[0]) != COLUMNAS_COLA:
            raise ErrorPersistencia("La cola tiene una estructura incompatible.")
        filas = []
        for numero, valores_fila in enumerate(valores[1:], 2):
            valores_fila = list(valores_fila) + [""] * (len(COLUMNAS_COLA) - len(valores_fila))
            fila = dict(zip(COLUMNAS_COLA, valores_fila[:len(COLUMNAS_COLA)]))
            fila["_row_number"] = numero
            filas.append(fila)
        return filas

    def buscar(self, external_id):
        filas = [f for f in self.todas_las_filas() if f["external_id"] == external_id]
        if len(filas) > 1:
            raise ConflictoIdentidad("La cola contiene external_id duplicado.")
        return filas[0] if filas else None

    def guardar(self, fila):
        numero = fila.get("_row_number")
        if not numero:
            raise ErrorPersistencia("No se conoce la fila que debe actualizarse.")
        rango = "A{0}:{1}{0}".format(numero, columna_a1(len(COLUMNAS_COLA)))
        self.worksheet.update(
            [[valor_celda(fila.get(c)) for c in COLUMNAS_COLA]],
            range_name=rango, value_input_option="RAW",
        )
        return self.buscar(fila["external_id"])

    def recibir(self, evento):
        with self._lock:
            self.verificar_encabezado()
            previa = self.buscar(evento.external_id)
            if previa:
                if (previa["fecha_mensaje"] != evento.fecha_mensaje
                        or previa["recuperacion_json"] != evento.recuperacion_json):
                    raise ConflictoIdentidad(
                        "external_id repetido con contenido incompatible."
                    )
                return previa, True
            fila = fila_vacia()
            fila.update({
                "external_id": evento.external_id,
                "fecha_mensaje": evento.fecha_mensaje,
                "recuperacion_json": evento.recuperacion_json,
                "estado": "recibido",
                "sincronizacion_estado": "pendiente",
            })
            self.worksheet.append_row(
                [valor_celda(fila[c]) for c in COLUMNAS_COLA],
                value_input_option="RAW",
            )
            guardada = self.buscar(evento.external_id)
            if guardada is None:
                raise ErrorPersistencia("Sheets no devolvio la recepcion persistida.")
            return guardada, False

    def iniciar(self, external_id):
        with self._lock:
            fila = self.buscar(external_id)
            if fila is None:
                raise ErrorPersistencia("La recepcion no existe.")
            if fila["estado"] in ESTADOS_TERMINALES:
                return fila
            fila.update({"estado": "procesando", "motivo_revision": ""})
            return self.guardar(fila)

    def guardar_transcripcion(self, external_id, transcripcion):
        with self._lock:
            fila = self.buscar(external_id)
            if fila is None:
                raise ErrorPersistencia("La recepcion no existe.")
            fila["transcripcion_original"] = transcripcion
            return self.guardar(fila)

    def finalizar(self, external_id, resultado):
        with self._lock:
            fila = self.buscar(external_id)
            if fila is None:
                raise ErrorPersistencia("La recepcion no existe.")
            fila.update({
                "payload_json": resultado.payload_json,
                "payload_sha256": resultado.payload_sha256,
                "tipo": resultado.tipo,
                "importe_cop": importe_cop_celda(resultado.importe_centavos),
                "fecha_efectiva": resultado.fecha_efectiva,
                "concepto": resultado.concepto,
                "ambito": resultado.ambito,
                "categoria_codigo": resultado.categoria_codigo,
                "factura_id": resultado.factura_id,
                "estado": resultado.estado,
                "motivo_revision": ", ".join(resultado.motivos_revision),
            })
            return self.guardar(fila)

    def marcar_reintentable(self, external_id, codigo, detalle):
        with self._lock:
            fila = self.buscar(external_id)
            if fila is None:
                raise ErrorPersistencia("La recepcion no existe.")
            fila.update({
                "estado": "recibido",
                "motivo_revision": str(codigo or "").strip(),
            })
            return self.guardar(fila)

    def listar_incompletos(self):
        with self._lock:
            self.verificar_encabezado()
            return [f for f in self.todas_las_filas()
                    if f["estado"] in ESTADOS_REANUDABLES]

    def entrega_sincronizacion(self):
        """Entrega terminales pendientes; una fila corrupta no bloquea las demas."""
        with self._lock:
            self.verificar_encabezado()
            registros, errores = [], []
            for fila in self.todas_las_filas():
                if (fila["estado"] not in ESTADOS_TERMINALES
                        or fila["sincronizacion_estado"] != "pendiente"):
                    continue
                try:
                    payload_json = fila["payload_json"]
                    if (not payload_json or
                            sha256_texto(payload_json) != fila["payload_sha256"]):
                        raise ErrorPersistencia("Huella de payload incompatible.")
                    payload = json.loads(payload_json)
                    if not isinstance(payload, dict):
                        raise ErrorPersistencia("Payload externo invalido.")
                    motivos = payload.get("motivos_revision")
                    codigo = (motivos[0] if isinstance(motivos, list)
                              and motivos and isinstance(motivos[0], str)
                              else None)
                    registros.append({
                        "external_id": fila["external_id"],
                        "payload": payload,
                        "payload_sha256": fila["payload_sha256"],
                        "estado": fila["estado"],
                        "error_codigo": codigo,
                        "transcripcion_original":
                            fila["transcripcion_original"] or None,
                    })
                except (ErrorPersistencia, TypeError, ValueError, json.JSONDecodeError):
                    errores.append({
                        "external_id": fila["external_id"],
                        "codigo": "fila_cola_incompatible",
                    })
            return {"schema_version": SCHEMA_VERSION,
                    "registros": registros, "errores": errores}

    def confirmar_sincronizacion(self, acuses):
        """Marca filas sin borrarlas; cada acuse se valida y aplica por separado."""
        if not isinstance(acuses, list) or len(acuses) > 500:
            raise ErrorPersistencia("La lista de acuses es invalida.")
        resultados = []
        with self._lock:
            self.verificar_encabezado()
            for acuse in acuses:
                if (not isinstance(acuse, dict)
                        or set(acuse) != {"external_id", "payload_sha256"}):
                    resultados.append({
                        "external_id": None, "estado": "error",
                        "codigo": "acuse_invalido",
                    })
                    continue
                external_id = str(acuse["external_id"] or "").strip()
                payload_sha256 = str(acuse["payload_sha256"] or "").strip()
                try:
                    fila = self.buscar(external_id)
                    if fila is None:
                        raise ErrorPersistencia("Recepcion inexistente.")
                    if fila["payload_sha256"] != payload_sha256:
                        resultados.append({
                            "external_id": external_id, "estado": "conflicto",
                            "codigo": "huella_incompatible",
                        })
                        continue
                    if fila["estado"] not in ESTADOS_TERMINALES:
                        resultados.append({
                            "external_id": external_id, "estado": "error",
                            "codigo": "recepcion_no_terminal",
                        })
                        continue
                    if fila["sincronizacion_estado"] == "sincronizado":
                        resultados.append({
                            "external_id": external_id,
                            "estado": "ya_sincronizado", "codigo": "",
                        })
                        continue
                    fila.update({
                        "sincronizacion_estado": "sincronizado",
                        "sincronizado_en": ahora_utc(),
                    })
                    self.guardar(fila)
                    resultados.append({
                        "external_id": external_id,
                        "estado": "sincronizado", "codigo": "",
                    })
                except Exception:
                    resultados.append({
                        "external_id": external_id, "estado": "error",
                        "codigo": "persistencia_no_disponible",
                    })
        return {"schema_version": SCHEMA_VERSION, "resultados": resultados}


def credenciales_google(readonly, entorno=None):
    entorno = entorno or os.environ
    credenciales_json = entorno.get("GOOGLE_CREDENTIALS_JSON")
    spreadsheet_id = str(entorno.get("GOOGLE_SHEET_ID") or "").strip()
    if not credenciales_json or not spreadsheet_id:
        raise ErrorPersistencia("Faltan GOOGLE_CREDENTIALS_JSON o GOOGLE_SHEET_ID.")
    try:
        info = json.loads(credenciales_json)
    except (TypeError, ValueError):
        raise ErrorPersistencia("GOOGLE_CREDENTIALS_JSON no contiene JSON valido.")
    scope = ("https://www.googleapis.com/auth/spreadsheets.readonly" if readonly
             else "https://www.googleapis.com/auth/spreadsheets")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(info, scopes=[scope])
        return gspread.authorize(creds).open_by_key(spreadsheet_id)
    except Exception as error:
        raise ErrorPersistencia("No se pudo abrir la hoja configurada.") from error


def inspeccionar_spreadsheet(entorno=None):
    """Lee titulos/dimensiones y solo el encabezado de la cola."""
    spreadsheet = credenciales_google(True, entorno)
    resultado = {"spreadsheet_id": spreadsheet.id, "pestanas": [],
                 "cola_existe": False, "encabezado_cola": None}
    for worksheet in spreadsheet.worksheets():
        resultado["pestanas"].append({
            "titulo": worksheet.title, "filas": worksheet.row_count,
            "columnas": worksheet.col_count,
        })
        if worksheet.title == NOMBRE_COLA:
            resultado["cola_existe"] = True
            resultado["encabezado_cola"] = worksheet.row_values(1)
    return resultado


def preparar_spreadsheet(entorno=None):
    """Crea solo la cola o valida su encabezado; no toca pestanas legacy."""
    spreadsheet = credenciales_google(False, entorno)
    coincidencias = [w for w in spreadsheet.worksheets() if w.title == NOMBRE_COLA]
    if len(coincidencias) > 1:
        raise ErrorPersistencia("Existe mas de una pestana Cola_Guivaltex_V1.")
    if coincidencias:
        worksheet, creada = coincidencias[0], False
        encabezado = tuple(worksheet.row_values(1))
        if not encabezado:
            worksheet.update([list(COLUMNAS_COLA)],
                range_name="A1:{0}1".format(columna_a1(len(COLUMNAS_COLA))),
                value_input_option="RAW")
        elif encabezado != COLUMNAS_COLA:
            raise ErrorPersistencia("La cola existente tiene encabezado incompatible.")
    else:
        worksheet = spreadsheet.add_worksheet(
            title=NOMBRE_COLA, rows=1000, cols=len(COLUMNAS_COLA))
        worksheet.update([list(COLUMNAS_COLA)],
            range_name="A1:{0}1".format(columna_a1(len(COLUMNAS_COLA))),
            value_input_option="RAW")
        creada = True
    GoogleSheetsQueue(worksheet).verificar_encabezado()
    return {"creada": creada, "titulo": worksheet.title,
            "columnas": len(COLUMNAS_COLA)}


def conectar_cola(entorno=None):
    spreadsheet = credenciales_google(False, entorno)
    coincidencias = [w for w in spreadsheet.worksheets() if w.title == NOMBRE_COLA]
    if len(coincidencias) != 1:
        raise ErrorPersistencia("Prepara Cola_Guivaltex_V1 antes de iniciar el bot.")
    cola = GoogleSheetsQueue(coincidencias[0])
    cola.verificar_encabezado()
    return cola


async def en_hilo(funcion, *args):
    bucle = asyncio.get_running_loop()
    return await bucle.run_in_executor(None, lambda: funcion(*args))


class ProcesadorCola:
    def __init__(self, cola, transcribir, interpretar):
        self.cola, self.transcribir, self.interpretar = cola, transcribir, interpretar
        self._locks = {}

    def lock(self, external_id):
        if external_id not in self._locks:
            self._locks[external_id] = asyncio.Lock()
        return self._locks[external_id]

    async def marcar_reintentable(self, external_id, codigo, detalle):
        try:
            await en_hilo(self.cola.marcar_reintentable, external_id, codigo, detalle)
            return True
        except Exception:
            return False

    async def procesar(self, evento, descargar_audio):
        async with self.lock(evento.external_id):
            try:
                fila, repetido = await en_hilo(self.cola.recibir, evento)
            except ConflictoIdentidad:
                return ResultadoProceso(evento.external_id, "error",
                                        "conflicto_identidad", True)
            except Exception:
                return ResultadoProceso(evento.external_id, "error",
                                        "persistencia_no_disponible")
            if fila["estado"] in ESTADOS_TERMINALES:
                return crear_resultado_proceso(
                    evento.external_id, fila["estado"], "ya_procesado", True,
                    fila=fila,
                )
            try:
                fila = await en_hilo(self.cola.iniciar, evento.external_id)
            except Exception:
                return ResultadoProceso(evento.external_id, "error",
                                        "persistencia_no_disponible", repetido)

            transcripcion = str(fila.get("transcripcion_original") or "").strip()
            if not transcripcion:
                with tempfile.TemporaryDirectory(prefix="guivaltex_telegram_") as directorio:
                    ruta = os.path.join(directorio, "audio.ogg")
                    try:
                        await descargar_audio(ruta)
                    except Exception:
                        await self.marcar_reintentable(
                            evento.external_id, "descarga_fallida",
                            "No se pudo descargar; el evento puede reintentarse.")
                        return ResultadoProceso(evento.external_id, "recibido",
                                                "descarga_fallida", repetido)
                    try:
                        transcripcion = await en_hilo(self.transcribir, ruta)
                        transcripcion = str(transcripcion or "").strip()
                        if not transcripcion or len(transcripcion) > 20000:
                            raise ErrorBotFinanzas("Transcripcion vacia o excesiva.")
                    except Exception:
                        await self.marcar_reintentable(
                            evento.external_id, "transcripcion_fallida",
                            "No se pudo transcribir; el evento puede reintentarse.")
                        return ResultadoProceso(evento.external_id, "recibido",
                                                "transcripcion_fallida", repetido)
                try:
                    fila = await en_hilo(self.cola.guardar_transcripcion,
                                         evento.external_id, transcripcion)
                except Exception:
                    return ResultadoProceso(evento.external_id, "error",
                                            "persistencia_no_disponible", repetido)
            try:
                respuesta = await en_hilo(self.interpretar, transcripcion,
                                          evento.fecha_mensaje)
            except Exception:
                await self.marcar_reintentable(
                    evento.external_id, "interpretacion_fallida",
                    "No se pudo interpretar; el evento puede reintentarse.")
                return ResultadoProceso(evento.external_id, "recibido",
                                        "interpretacion_fallida", repetido)
            try:
                resultado = validar_interpretacion(
                    evento.external_id, respuesta, transcripcion
                )
            except ErrorContrato:
                resultado = excepcion_contrato(evento.external_id, respuesta)
            try:
                await en_hilo(self.cola.finalizar, evento.external_id, resultado)
            except Exception:
                return crear_resultado_proceso(
                    evento.external_id, "error", "persistencia_no_disponible",
                    repetido, interpretacion=resultado,
                )
            return crear_resultado_proceso(
                evento.external_id, resultado.estado, "procesado", repetido,
                interpretacion=resultado,
            )

    async def recuperar_pendientes(self, autorizacion, descargar_factory):
        try:
            pendientes = await en_hilo(self.cola.listar_incompletos)
        except Exception:
            return []
        resultados = []
        for fila in pendientes:
            try:
                evento = EventoTelegram.desde_fila(fila)
            except ErrorBotFinanzas:
                continue
            if autorizacion.permite(evento.chat_id, evento.user_id):
                resultados.append(await self.procesar(
                    evento, descargar_factory(evento)))
        return resultados
