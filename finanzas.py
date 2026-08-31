"""Nucleo local de Finanzas V1 e integraciones internas, sin HTTP ni interfaz."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from uuid import UUID, NAMESPACE_URL, uuid5


CENTAVO = Decimal('0.01')
MINIMO_COP = Decimal('1000.00')
MAX_CENTAVOS_SQLITE = 9223372036854775807
_SIN_CAMBIO = object()


class ErrorFinanzas(ValueError):
    pass


class ErrorRevisionImporte(ErrorFinanzas):
    """El texto contradice o no permite aplicar con seguridad la regla de miles."""


def preparar_conexion_finanzas(conn):
    """Activa FK antes de abrir la transaccion que escribira Finanzas."""
    if conn.in_transaction:
        raise ErrorFinanzas('Activa las referencias antes de iniciar la transaccion.')
    conn.execute('PRAGMA foreign_keys = ON')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ErrorFinanzas('SQLite no activo las referencias de Finanzas.')


def _exigir_escritura(conn):
    if not conn.in_transaction:
        raise ErrorFinanzas('La escritura financiera requiere una transaccion explicita.')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ErrorFinanzas('La escritura financiera requiere foreign_keys activas.')


def _ahora_utc():
    return datetime.now(timezone.utc).isoformat()


def _texto(valor, nombre):
    texto = str(valor or '').strip()
    if not texto:
        raise ErrorFinanzas('Falta ' + nombre + '.')
    return texto


def _fecha(valor):
    texto = valor.isoformat() if type(valor) is date else str(valor or '').strip()
    try:
        parsed = date.fromisoformat(texto)
    except (TypeError, ValueError):
        raise ErrorFinanzas('La fecha debe usar YYYY-MM-DD.')
    if parsed.isoformat() != texto:
        raise ErrorFinanzas('La fecha debe usar YYYY-MM-DD.')
    return texto


def _decimal(valor):
    if isinstance(valor, bool):
        raise ErrorFinanzas('El importe debe ser numerico.')
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise ErrorFinanzas('El importe debe ser numerico.')
    if not numero.is_finite():
        raise ErrorFinanzas('El importe debe ser finito.')
    return numero


def importe_a_centavos(valor, minimo_movimiento=True):
    """Convierte COP explicitos a entero exacto; no aplica la regla de miles hablados."""
    numero = _decimal(valor)
    if numero < 0:
        raise ErrorFinanzas('El importe no puede ser negativo.')
    centavos_decimal = numero * 100
    if centavos_decimal != centavos_decimal.to_integral_value():
        raise ErrorFinanzas('El importe debe expresarse en centavos, sin redondeo.')
    centavos = int(centavos_decimal)
    if minimo_movimiento and centavos < int(MINIMO_COP * 100):
        raise ErrorFinanzas('Finanzas V1 no admite movimientos inferiores a COP 1.000.')
    if centavos > MAX_CENTAVOS_SQLITE:
        raise ErrorFinanzas('El importe excede el rango disponible.')
    return centavos


def _centavos_validos(valor):
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorFinanzas('El importe normalizado debe ser un entero de centavos.')
    if valor < int(MINIMO_COP * 100):
        raise ErrorFinanzas('Finanzas V1 no admite movimientos inferiores a COP 1.000.')
    if valor > MAX_CENTAVOS_SQLITE:
        raise ErrorFinanzas('El importe excede el rango disponible.')
    return valor


def decimal_desde_centavos(centavos):
    return (Decimal(centavos) / 100).quantize(CENTAVO)


def normalizar_importe_hablado(expresion):
    """Regla Guivaltex: numero desnudo 1..999 significa miles; evidencia explicita manda."""
    original = _texto(expresion, 'la evidencia del importe')
    texto = unicodedata.normalize('NFKD', original).encode('ascii', 'ignore').decode('ascii').lower()
    patron = (r'^\s*\$?\s*'
              r'(?P<numero>(?:\d{1,3}(?P<separador>[\.,])\d{3}'
              r'(?:(?P=separador)\d{3})*|\d+(?:[\.,]\d{1,2})?))'
              r'\s*(?P<unidad>mil(?:es)?(?:\s+(?:pesos?|cop))?|pesos?|cop)?\s*$')
    match = re.fullmatch(patron, texto)
    if not match:
        raise ErrorRevisionImporte('El importe hablado requiere revision: formato ambiguo.')
    numero = match.group('numero')
    if match.group('separador'):
        numero = numero.replace(match.group('separador'), '')
    else:
        numero = numero.replace(',', '.')
    base = _decimal(numero)
    unidad = match.group('unidad') or ''
    if base <= 0:
        raise ErrorRevisionImporte('El importe hablado debe ser positivo.')
    if unidad.startswith('mil'):
        normalizado = base * 1000
    elif unidad in ('peso', 'pesos', 'cop'):
        if base < MINIMO_COP:
            raise ErrorRevisionImporte(
                'La unidad explicita contradice el minimo: no se convierte silenciosamente.'
            )
        normalizado = base
    elif base < MINIMO_COP:
        normalizado = base * 1000
    else:
        normalizado = base
    return decimal_desde_centavos(importe_a_centavos(normalizado))


def _fila(cursor, row):
    if row is None:
        return None
    return dict(zip((col[0] for col in cursor.description), tuple(row)))


def _una(conn, sql, parametros=()):
    cursor = conn.execute(sql, parametros)
    return _fila(cursor, cursor.fetchone())


def _apertura(conn):
    return _una(conn, 'SELECT * FROM apertura_financiera WHERE id=1')


def _apertura_requerida(conn):
    apertura = _apertura(conn)
    if apertura is None:
        raise ErrorFinanzas('Configura la apertura financiera antes de registrar movimientos.')
    return apertura


def configurar_apertura(conn, fecha_inicio, fondos_iniciales_cop):
    _exigir_escritura(conn)
    fecha_inicio = _fecha(fecha_inicio)
    fondos = importe_a_centavos(fondos_iniciales_cop, minimo_movimiento=False)
    previa = _apertura(conn)
    if previa:
        if previa['fecha_inicio'] == fecha_inicio and previa['fondos_iniciales_centavos'] == fondos:
            return previa, True
        raise ErrorFinanzas('La apertura ya existe; usa la correccion con motivo.')
    ahora = _ahora_utc()
    conn.execute(
        """INSERT INTO apertura_financiera(
             id,moneda,fecha_inicio,fondos_originales_centavos,fondos_iniciales_centavos,
             motivo_ultima_correccion,creado_en,actualizado_en
           ) VALUES (1,'COP',?,?,?,?,?,?)""",
        (fecha_inicio, fondos, fondos, None, ahora, ahora),
    )
    return _apertura(conn), False


def corregir_apertura(conn, fondos_iniciales_cop, motivo):
    _exigir_escritura(conn)
    previa = _apertura_requerida(conn)
    fondos = importe_a_centavos(fondos_iniciales_cop, minimo_movimiento=False)
    motivo = _texto(motivo, 'el motivo de correccion de la apertura')
    if fondos == previa['fondos_iniciales_centavos']:
        raise ErrorFinanzas('La correccion no cambia los fondos iniciales.')
    conn.execute(
        """UPDATE apertura_financiera
           SET fondos_iniciales_centavos=?, motivo_ultima_correccion=?, actualizado_en=?
           WHERE id=1""",
        (fondos, motivo, _ahora_utc()),
    )
    return _apertura(conn)


def _categoria(conn, codigo, tipo, ambito):
    codigo = _texto(codigo, 'la categoria')
    categoria = _una(
        conn, 'SELECT * FROM categorias_financieras WHERE codigo=?', (codigo,)
    )
    if categoria is None or not categoria['activa']:
        raise ErrorFinanzas('Categoria financiera inexistente o inactiva.')
    if categoria['tipo'] is not None and categoria['tipo'] != tipo:
        raise ErrorFinanzas('La categoria no corresponde al tipo de movimiento.')
    if categoria['ambito'] is not None and categoria['ambito'] != ambito:
        raise ErrorFinanzas('La categoria no corresponde al ambito del movimiento.')
    return codigo


def _validar_datos(conn, tipo, centavos, fecha_efectiva, concepto, ambito,
                   categoria_codigo, factura_id, motivo_ajuste, origen):
    if tipo not in ('ingreso', 'gasto'):
        raise ErrorFinanzas('Tipo financiero invalido.')
    if ambito not in ('empresa', 'hogar'):
        raise ErrorFinanzas('Ambito financiero invalido.')
    centavos = _centavos_validos(centavos)
    fecha_efectiva = _fecha(fecha_efectiva)
    apertura = _apertura_requerida(conn)
    if fecha_efectiva < apertura['fecha_inicio']:
        raise ErrorFinanzas('La fecha es anterior al inicio financiero configurado.')
    concepto = _texto(concepto, 'el concepto')
    categoria_codigo = _categoria(conn, categoria_codigo, tipo, ambito)
    factura_id = str(factura_id).strip() if factura_id is not None else None
    factura_id = factura_id or None
    if ambito == 'hogar' and factura_id:
        raise ErrorFinanzas('Un gasto de hogar no puede atribuirse a una factura.')
    if factura_id and conn.execute(
        'SELECT 1 FROM facturas WHERE id_factura=?', (factura_id,)
    ).fetchone() is None:
        raise ErrorFinanzas('La factura relacionada no existe.')
    asignacion = 'directo' if factura_id else 'sin_pedido'
    if categoria_codigo == 'ajuste_caja':
        motivo_ajuste = _texto(motivo_ajuste, 'el motivo del ajuste de caja')
    elif motivo_ajuste is not None:
        raise ErrorFinanzas('Solo un ajuste de caja puede tener motivo de ajuste.')
    if origen in ('manual', 'render_telegram'):
        if categoria_codigo in ('abono', 'saldo') or (tipo == 'ingreso' and factura_id):
            raise ErrorFinanzas('Los abonos se registran desde su fuente canonica en Guivaltex.')
        if categoria_codigo == 'sueldos':
            raise ErrorFinanzas('La nomina se registra desde su fuente canonica en Guivaltex.')
    return {
        'tipo': tipo, 'importe_centavos': centavos, 'fecha_efectiva': fecha_efectiva,
        'concepto': concepto, 'ambito': ambito, 'categoria_codigo': categoria_codigo,
        'factura_id': factura_id, 'asignacion_pedido': asignacion,
        'motivo_ajuste': motivo_ajuste,
    }


def _json_canonico(valor):
    try:
        return json.dumps(valor, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError):
        raise ErrorFinanzas('El payload no es JSON valido.')


def _sha(texto):
    return hashlib.sha256(texto.encode('utf-8')).hexdigest()


def _uuid(valor):
    try:
        return str(UUID(str(valor)))
    except (ValueError, TypeError, AttributeError):
        raise ErrorFinanzas('La identidad de operacion debe ser un UUID valido.')


def _movimiento(conn, movimiento_id):
    return _una(conn, 'SELECT * FROM movimientos_financieros WHERE id=?', (movimiento_id,))


def _insertar_movimiento(conn, movimiento_id, datos, origen, huella,
                         abono_id=None, nomina_emision_id=None, recepcion_external_id=None):
    ahora = _ahora_utc()
    conn.execute(
        """INSERT INTO movimientos_financieros(
             id,tipo,origen,fecha_efectiva,importe_centavos,concepto,ambito,
             categoria_codigo,asignacion_pedido,factura_id,abono_id,nomina_emision_id,
             recepcion_external_id,motivo_ajuste,estado,motivo_anulacion,
             huella_alta,creado_en,actualizado_en
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'activo',NULL,?,?,?)""",
        (movimiento_id, datos['tipo'], origen, datos['fecha_efectiva'],
         datos['importe_centavos'], datos['concepto'], datos['ambito'],
         datos['categoria_codigo'], datos['asignacion_pedido'], datos['factura_id'],
         abono_id, nomina_emision_id, recepcion_external_id, datos['motivo_ajuste'],
         huella, ahora, ahora),
    )
    return _movimiento(conn, movimiento_id)


def _fuente_dentro_del_periodo(conn, fecha_efectiva):
    apertura = _apertura(conn)
    return apertura is not None and fecha_efectiva >= apertura['fecha_inicio']


def registrar_movimiento_abono(conn, abono_id, importe_cop):
    """Refleja un abono nuevo; el llamador conserva la transaccion de la fuente."""
    _exigir_escritura(conn)
    if isinstance(abono_id, bool) or not isinstance(abono_id, int) or abono_id <= 0:
        raise ErrorFinanzas('La referencia del abono es invalida.')
    abono = _una(
        conn,
        'SELECT id_abono,factura_id,monto,fecha FROM historial_abonos WHERE id_abono=?',
        (abono_id,),
    )
    if abono is None:
        raise ErrorFinanzas('El abono fuente no existe.')
    fecha_efectiva = _fecha(abono['fecha'])
    centavos = importe_a_centavos(importe_cop, minimo_movimiento=False)
    centavos_guardados = importe_a_centavos(abono['monto'], minimo_movimiento=False)
    if centavos != centavos_guardados:
        raise ErrorFinanzas('El importe financiero no coincide con el abono fuente.')
    if not _fuente_dentro_del_periodo(conn, fecha_efectiva):
        return None, False

    datos = _validar_datos(
        conn, 'ingreso', centavos, fecha_efectiva,
        'Abono de factura ' + abono['factura_id'], 'empresa', 'abono',
        abono['factura_id'], None, 'abono',
    )
    huella = _sha(_json_canonico({
        'abono_id': abono_id,
        'factura_id': abono['factura_id'],
        'importe_centavos': centavos,
        'fecha_efectiva': fecha_efectiva,
    }))
    existente = _una(
        conn, 'SELECT * FROM movimientos_financieros WHERE abono_id=?', (abono_id,)
    )
    if existente:
        if existente['origen'] == 'abono' and existente['huella_alta'] == huella:
            return existente, True
        raise ErrorFinanzas('El abono ya tiene un movimiento financiero incompatible.')
    movimiento_id = str(uuid5(NAMESPACE_URL, 'guivaltex:abono:' + str(abono_id)))
    return _insertar_movimiento(
        conn, movimiento_id, datos, 'abono', huella, abono_id=abono_id
    ), False


def registrar_movimiento_nomina(conn, emision_id, importe_cop, fecha_efectiva,
                                concepto):
    """Refleja el neto de una emision nueva dentro de su transaccion SQLite."""
    _exigir_escritura(conn)
    emision_id = _uuid(emision_id)
    if conn.execute(
        'SELECT 1 FROM comprobantes_nomina WHERE emision_id=?', (emision_id,)
    ).fetchone() is None:
        raise ErrorFinanzas('La emision de nomina fuente no existe.')
    fecha_efectiva = _fecha(fecha_efectiva)
    centavos = importe_a_centavos(importe_cop, minimo_movimiento=False)
    if not _fuente_dentro_del_periodo(conn, fecha_efectiva):
        return None, False

    datos = _validar_datos(
        conn, 'gasto', centavos, fecha_efectiva, concepto, 'empresa',
        'sueldos', None, None, 'nomina',
    )
    huella = _sha(_json_canonico({
        'emision_id': emision_id,
        'importe_centavos': centavos,
        'fecha_efectiva': fecha_efectiva,
    }))
    existente = _una(
        conn, 'SELECT * FROM movimientos_financieros WHERE nomina_emision_id=?',
        (emision_id,),
    )
    if existente:
        if existente['origen'] == 'nomina' and existente['huella_alta'] == huella:
            return existente, True
        raise ErrorFinanzas('La emision ya tiene un movimiento financiero incompatible.')
    movimiento_id = str(uuid5(NAMESPACE_URL, 'guivaltex:nomina:' + emision_id))
    return _insertar_movimiento(
        conn, movimiento_id, datos, 'nomina', huella,
        nomina_emision_id=emision_id,
    ), False


def crear_movimiento_manual(conn, operacion_id, tipo, importe_cop, fecha_efectiva,
                            concepto, ambito, categoria_codigo, factura_id=None,
                            motivo_ajuste=None):
    _exigir_escritura(conn)
    movimiento_id = _uuid(operacion_id)
    datos = _validar_datos(
        conn, tipo, importe_a_centavos(importe_cop), fecha_efectiva, concepto,
        ambito, categoria_codigo, factura_id, motivo_ajuste, 'manual'
    )
    huella = _sha(_json_canonico(datos))
    previa = _movimiento(conn, movimiento_id)
    if previa:
        if previa['origen'] == 'manual' and previa['huella_alta'] == huella:
            return previa, True
        raise ErrorFinanzas('La identidad ya pertenece a otra solicitud financiera.')
    return _insertar_movimiento(conn, movimiento_id, datos, 'manual', huella), False


def registrar_ajuste_caja(conn, operacion_id, sentido, importe_cop,
                          fecha_efectiva, motivo):
    if sentido not in ('positivo', 'negativo'):
        raise ErrorFinanzas('El ajuste debe ser positivo o negativo.')
    motivo = _texto(motivo, 'el motivo del ajuste de caja')
    return crear_movimiento_manual(
        conn, operacion_id, 'ingreso' if sentido == 'positivo' else 'gasto',
        importe_cop, fecha_efectiva, 'Ajuste de caja: ' + motivo,
        'empresa', 'ajuste_caja', motivo_ajuste=motivo,
    )



def corregir_movimiento(conn, movimiento_id, *, tipo=_SIN_CAMBIO,
                         importe_cop=_SIN_CAMBIO, fecha_efectiva=_SIN_CAMBIO,
                         concepto=_SIN_CAMBIO, ambito=_SIN_CAMBIO,
                         categoria_codigo=_SIN_CAMBIO, factura_id=_SIN_CAMBIO,
                         motivo_ajuste=_SIN_CAMBIO):
    _exigir_escritura(conn)
    movimiento_id = _uuid(movimiento_id)
    previo = _movimiento(conn, movimiento_id)
    if previo is None:
        raise ErrorFinanzas('Movimiento financiero inexistente.')
    if previo['origen'] not in ('manual', 'render_telegram'):
        raise ErrorFinanzas('El movimiento automatico solo se corrige desde su fuente canonica.')
    if previo['estado'] != 'activo':
        raise ErrorFinanzas('Un movimiento anulado no se puede corregir.')
    nuevo_tipo = previo['tipo'] if tipo is _SIN_CAMBIO else tipo
    nuevos_centavos = (previo['importe_centavos'] if importe_cop is _SIN_CAMBIO
                       else importe_a_centavos(importe_cop))
    nueva_fecha = previo['fecha_efectiva'] if fecha_efectiva is _SIN_CAMBIO else fecha_efectiva
    nuevo_concepto = previo['concepto'] if concepto is _SIN_CAMBIO else concepto
    nuevo_ambito = previo['ambito'] if ambito is _SIN_CAMBIO else ambito
    nueva_categoria = (previo['categoria_codigo'] if categoria_codigo is _SIN_CAMBIO
                       else categoria_codigo)
    nueva_factura = previo['factura_id'] if factura_id is _SIN_CAMBIO else factura_id
    if nueva_categoria == 'ajuste_caja':
        nuevo_motivo = (previo['motivo_ajuste'] if motivo_ajuste is _SIN_CAMBIO
                        else motivo_ajuste)
    else:
        nuevo_motivo = None
        if motivo_ajuste is not _SIN_CAMBIO and motivo_ajuste is not None:
            raise ErrorFinanzas('Solo un ajuste de caja puede tener motivo de ajuste.')
    datos = _validar_datos(
        conn, nuevo_tipo, nuevos_centavos, nueva_fecha, nuevo_concepto,
        nuevo_ambito, nueva_categoria, nueva_factura, nuevo_motivo, previo['origen']
    )
    conn.execute(
        """UPDATE movimientos_financieros
           SET tipo=?,fecha_efectiva=?,importe_centavos=?,concepto=?,ambito=?,
               categoria_codigo=?,asignacion_pedido=?,factura_id=?,motivo_ajuste=?,
               actualizado_en=?
           WHERE id=?""",
        (datos['tipo'], datos['fecha_efectiva'], datos['importe_centavos'],
         datos['concepto'], datos['ambito'], datos['categoria_codigo'],
         datos['asignacion_pedido'], datos['factura_id'], datos['motivo_ajuste'],
         _ahora_utc(), movimiento_id),
    )
    return _movimiento(conn, movimiento_id)


def anular_movimiento(conn, movimiento_id, motivo):
    _exigir_escritura(conn)
    movimiento_id = _uuid(movimiento_id)
    previo = _movimiento(conn, movimiento_id)
    if previo is None:
        raise ErrorFinanzas('Movimiento financiero inexistente.')
    if previo['origen'] not in ('manual', 'render_telegram'):
        raise ErrorFinanzas('El movimiento automatico solo se anula desde su fuente canonica.')
    motivo = _texto(motivo, 'el motivo de anulacion')
    if previo['estado'] == 'anulado':
        return previo, True
    conn.execute(
        """UPDATE movimientos_financieros
           SET estado='anulado',motivo_anulacion=?,actualizado_en=? WHERE id=?""",
        (motivo, _ahora_utc(), movimiento_id),
    )
    return _movimiento(conn, movimiento_id), False


def _external_id(valor):
    external_id = _texto(valor, 'external_id')
    if len(external_id) > 200 or any(ord(c) < 32 for c in external_id):
        raise ErrorFinanzas('external_id invalido.')
    return external_id


def _recepcion(conn, external_id):
    return _una(conn, 'SELECT * FROM recepciones_render WHERE external_id=?', (external_id,))


def recibir_recepcion_render(conn, external_id, payload, estado_interpretacion,
                             datos_financieros=None, detalle_excepcion=None):
    _exigir_escritura(conn)
    external_id = _external_id(external_id)
    payload_json = _json_canonico(payload)
    payload_sha256 = _sha(payload_json)
    previa = _recepcion(conn, external_id)
    if previa:
        if previa['payload_sha256'] == payload_sha256:
            return previa, True
        raise ErrorFinanzas('external_id repetido con payload diferente.')
    if estado_interpretacion not in ('valido', 'excepcion'):
        raise ErrorFinanzas('Estado de interpretacion externo invalido.')
    if estado_interpretacion == 'valido':
        if not isinstance(datos_financieros, dict):
            raise ErrorFinanzas('Una recepcion valida requiere datos financieros.')
        datos_json = _json_canonico(datos_financieros)
        detalle = None
    else:
        datos_json = _json_canonico(datos_financieros) if datos_financieros is not None else None
        detalle = _texto(detalle_excepcion, 'el detalle de la excepcion')
    conn.execute(
        """INSERT INTO recepciones_render(
             external_id,payload_json,payload_sha256,datos_financieros_json,
             estado_interpretacion,detalle_excepcion,recibido_en,revisado_en,
             convertido_en,estado_acuse,acuse_confirmado_en
           ) VALUES (?,?,?,?,?,?,?,NULL,NULL,'pendiente',NULL)""",
        (external_id, payload_json, payload_sha256, datos_json,
         estado_interpretacion, detalle, _ahora_utc()),
    )
    return _recepcion(conn, external_id), False


def resolver_excepcion_render(conn, external_id, datos_financieros):
    _exigir_escritura(conn)
    external_id = _external_id(external_id)
    previa = _recepcion(conn, external_id)
    if previa is None:
        raise ErrorFinanzas('Recepcion externa inexistente.')
    if previa['convertido_en'] is not None:
        raise ErrorFinanzas('La recepcion ya fue convertida.')
    if previa['estado_interpretacion'] != 'excepcion':
        raise ErrorFinanzas('La recepcion no esta pendiente de revision.')
    if not isinstance(datos_financieros, dict):
        raise ErrorFinanzas('La revision requiere datos financieros.')
    conn.execute(
        """UPDATE recepciones_render
           SET datos_financieros_json=?,estado_interpretacion='valido',
               detalle_excepcion=NULL,revisado_en=?
           WHERE external_id=?""",
        (_json_canonico(datos_financieros), _ahora_utc(), external_id),
    )
    return _recepcion(conn, external_id)


def actualizar_detalle_excepcion_render(conn, external_id, detalle):
    """Conserva un motivo local seguro sin perder identidad ni payload original."""
    _exigir_escritura(conn)
    external_id = _external_id(external_id)
    previa = _recepcion(conn, external_id)
    if previa is None:
        raise ErrorFinanzas('Recepcion externa inexistente.')
    if previa['convertido_en'] is not None:
        raise ErrorFinanzas('La recepcion ya fue convertida.')
    if previa['estado_interpretacion'] != 'excepcion':
        raise ErrorFinanzas('La recepcion no esta pendiente de revision.')
    conn.execute(
        """UPDATE recepciones_render
           SET detalle_excepcion=?,revisado_en=? WHERE external_id=?""",
        (_texto(detalle, 'el detalle de la excepcion'), _ahora_utc(), external_id),
    )
    return _recepcion(conn, external_id)


def convertir_recepcion_render(conn, external_id):
    _exigir_escritura(conn)
    external_id = _external_id(external_id)
    recepcion = _recepcion(conn, external_id)
    if recepcion is None:
        raise ErrorFinanzas('Recepcion externa inexistente.')
    existente = _una(
        conn, 'SELECT * FROM movimientos_financieros WHERE recepcion_external_id=?',
        (external_id,),
    )
    if existente:
        return existente, True
    if recepcion['estado_interpretacion'] != 'valido':
        raise ErrorFinanzas('La recepcion externa requiere revision antes de convertirse.')
    try:
        raw = json.loads(recepcion['datos_financieros_json'])
        datos = _validar_datos(
            conn, raw['tipo'], _centavos_validos(raw['importe_centavos']),
            raw['fecha_efectiva'], raw['concepto'], raw['ambito'],
            raw['categoria_codigo'], raw.get('factura_id'), raw.get('motivo_ajuste'),
            'render_telegram',
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ErrorFinanzas('Los datos financieros externos estan incompletos.') from error
    movimiento_id = str(uuid5(NAMESPACE_URL, 'guivaltex:render:' + external_id))
    movimiento = _insertar_movimiento(
        conn, movimiento_id, datos, 'render_telegram', recepcion['payload_sha256'],
        recepcion_external_id=external_id,
    )
    conn.execute(
        'UPDATE recepciones_render SET convertido_en=? WHERE external_id=?',
        (_ahora_utc(), external_id),
    )
    return movimiento, False


def confirmar_acuse_render(conn, external_id):
    _exigir_escritura(conn)
    external_id = _external_id(external_id)
    previa = _recepcion(conn, external_id)
    if previa is None:
        raise ErrorFinanzas('Recepcion externa inexistente.')
    if previa['estado_acuse'] == 'confirmado':
        return previa, True
    conn.execute(
        """UPDATE recepciones_render
           SET estado_acuse='confirmado',acuse_confirmado_en=? WHERE external_id=?""",
        (_ahora_utc(), external_id),
    )
    return _recepcion(conn, external_id), False


def registrar_estado_sincronizacion(conn, exito, nuevos_recibidos=0, error=None):
    _exigir_escritura(conn)
    if isinstance(nuevos_recibidos, bool) or not isinstance(nuevos_recibidos, int) or nuevos_recibidos < 0:
        raise ErrorFinanzas('Cantidad de recepciones invalida.')
    ahora = _ahora_utc()
    if exito:
        if error is not None:
            raise ErrorFinanzas('Una sincronizacion exitosa no puede conservar error.')
        conn.execute(
            """UPDATE estado_sincronizacion_financiera
               SET ultimo_intento_en=?,ultima_sincronizacion_exitosa_en=?,
                   ultimo_resultado='exito',ultimo_error=NULL,nuevos_recibidos=?
               WHERE id=1""",
            (ahora, ahora, nuevos_recibidos),
        )
    else:
        error = _texto(error, 'el error de sincronizacion')
        conn.execute(
            """UPDATE estado_sincronizacion_financiera
               SET ultimo_intento_en=?,ultimo_resultado='error',ultimo_error=?,
                   nuevos_recibidos=?
               WHERE id=1""",
            (ahora, error, nuevos_recibidos),
        )
    return _una(conn, 'SELECT * FROM estado_sincronizacion_financiera WHERE id=1')



def caja_esperada(conn, hasta):
    hasta = _fecha(hasta)
    apertura = _apertura_requerida(conn)
    if hasta < apertura['fecha_inicio']:
        raise ErrorFinanzas('La fecha consultada es anterior al inicio financiero.')
    filas = conn.execute(
        """SELECT tipo,importe_centavos FROM movimientos_financieros
           WHERE estado='activo' AND fecha_efectiva BETWEEN ? AND ?""",
        (apertura['fecha_inicio'], hasta),
    ).fetchall()
    ingresos = sum(importe for tipo, importe in filas if tipo == 'ingreso')
    gastos = sum(importe for tipo, importe in filas if tipo == 'gasto')
    caja = apertura['fondos_iniciales_centavos'] + ingresos - gastos
    return {
        'fecha_inicio': apertura['fecha_inicio'],
        'hasta': hasta,
        'fondos_iniciales_centavos': apertura['fondos_iniciales_centavos'],
        'ingresos_centavos': ingresos,
        'gastos_centavos': gastos,
        'caja_esperada_centavos': caja,
    }


def consultar_rango(conn, inicio, fin):
    inicio, fin = _fecha(inicio), _fecha(fin)
    if inicio > fin:
        raise ErrorFinanzas('El inicio del periodo no puede superar el fin.')
    apertura = _apertura_requerida(conn)
    if inicio < apertura['fecha_inicio']:
        raise ErrorFinanzas('El periodo inicia antes del inicio financiero.')
    cursor = conn.execute(
        """SELECT * FROM movimientos_financieros
           WHERE fecha_efectiva BETWEEN ? AND ?
           ORDER BY fecha_efectiva,creado_en,id""",
        (inicio, fin),
    )
    movimientos = [_fila(cursor, row) for row in cursor.fetchall()]
    activos = [m for m in movimientos if m['estado'] == 'activo']
    ingresos = sum(m['importe_centavos'] for m in activos if m['tipo'] == 'ingreso')
    gastos = sum(m['importe_centavos'] for m in activos if m['tipo'] == 'gasto')
    return {
        'inicio': inicio,
        'fin': fin,
        'movimientos': movimientos,
        'ingresos_centavos': ingresos,
        'gastos_centavos': gastos,
        'resultado_centavos': ingresos - gastos,
        'caja_final_centavos': caja_esperada(conn, fin)['caja_esperada_centavos'],
    }


def consultar_dia(conn, dia):
    dia = _fecha(dia)
    return consultar_rango(conn, dia, dia)


def consultar_semana(conn, dia):
    referencia = date.fromisoformat(_fecha(dia))
    inicio = referencia - timedelta(days=referencia.weekday())
    fin = inicio + timedelta(days=6)
    return consultar_rango(conn, inicio.isoformat(), fin.isoformat())


def consultar_mes(conn, dia):
    referencia = date.fromisoformat(_fecha(dia))
    inicio = referencia.replace(day=1)
    fin = referencia.replace(day=monthrange(referencia.year, referencia.month)[1])
    return consultar_rango(conn, inicio.isoformat(), fin.isoformat())
