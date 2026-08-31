"""API HTTP minima de Render para entregar y confirmar la cola financiera."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlsplit

MAX_BODY_BYTES = 1024 * 1024


class ErrorApiRender(ValueError):
    pass


def validar_secreto(secreto):
    secreto = str(secreto or "")
    if len(secreto) < 32:
        raise ErrorApiRender(
            "GUIVALTEX_SYNC_SHARED_SECRET debe tener al menos 32 caracteres."
        )
    return secreto


def _autorizado(encabezado, secreto):
    prefijo = "Bearer "
    if not isinstance(encabezado, str) or not encabezado.startswith(prefijo):
        return False
    return hmac.compare_digest(encabezado[len(prefijo):], secreto)


def crear_manejador(cola, secreto):
    secreto = validar_secreto(secreto)

    class ManejadorFinanzas(BaseHTTPRequestHandler):
        server_version = "GuivaltexRender/1"

        def log_message(self, formato, *args):
            return

        def responder(self, estado, contenido):
            cuerpo = json.dumps(
                contenido, ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(estado)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def autenticar(self):
            if _autorizado(self.headers.get("Authorization"), secreto):
                return True
            self.responder(401, {"status": "error", "codigo": "no_autorizado"})
            return False

        def leer_json(self):
            try:
                longitud = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ErrorApiRender("Longitud invalida.")
            if longitud <= 0 or longitud > MAX_BODY_BYTES:
                raise ErrorApiRender("Cuerpo invalido.")
            cuerpo = self.rfile.read(longitud)
            try:
                datos = json.loads(cuerpo.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ErrorApiRender("JSON invalido.")
            if not isinstance(datos, dict):
                raise ErrorApiRender("El cuerpo debe ser un objeto.")
            return datos

        def do_GET(self):
            ruta = urlsplit(self.path).path
            if ruta in ("/", "/salud"):
                self.responder(200, {"status": "ok"})
                return
            if ruta != "/api/v1/movimientos/pendientes":
                self.responder(404, {"status": "error", "codigo": "no_encontrado"})
                return
            if not self.autenticar():
                return
            try:
                self.responder(200, cola.entrega_sincronizacion())
            except Exception:
                self.responder(
                    503,
                    {"status": "error", "codigo": "cola_no_disponible"},
                )

        def do_POST(self):
            ruta = urlsplit(self.path).path
            if ruta != "/api/v1/movimientos/acuse":
                self.responder(404, {"status": "error", "codigo": "no_encontrado"})
                return
            if not self.autenticar():
                return
            try:
                datos = self.leer_json()
                if set(datos) != {"schema_version", "acuses"}:
                    raise ErrorApiRender("Contrato de acuse invalido.")
                if str(datos["schema_version"]) != "1":
                    raise ErrorApiRender("Version de acuse incompatible.")
                resultado = cola.confirmar_sincronizacion(datos["acuses"])
                self.responder(200, resultado)
            except ErrorApiRender:
                self.responder(
                    400, {"status": "error", "codigo": "solicitud_invalida"}
                )
            except Exception:
                self.responder(
                    503, {"status": "error", "codigo": "cola_no_disponible"}
                )

    return ManejadorFinanzas


def crear_servidor(cola, secreto, host="0.0.0.0", port=8080):
    return ThreadingHTTPServer(
        (host, int(port)),
        crear_manejador(cola, secreto),
    )
