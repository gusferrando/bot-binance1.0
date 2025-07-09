from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
import time
import pytz
from datetime import datetime
from binance.um_futures import UMFutures
from dotenv import load_dotenv
import os
import math
from binance.error import ClientError
from decimal import Decimal, ROUND_DOWN
from decimal import Decimal, ROUND_HALF_UP
from telegram_bot import enviar_telegram
from reportes import verificar_salida_programada
import logging
import sys

app = Flask(__name__)

estado_orden = {
    "activa": False,
    "order_id": None,
    "timestamp_inicio": None,
    "symbol": None,
    "side": None,
    "qty": None,
    "sl_distance": None,
    "tp_factor": None
}
# Silenciar logs de apscheduler
logging.getLogger('apscheduler').setLevel(logging.WARNING)

scheduler = BackgroundScheduler()



# === Funciones auxiliares ===
logging.basicConfig(
    level=logging.INFO,  # nivel mínimo de logs a mostrar (INFO y superiores)
    format='%(asctime)s - %(levelname)s - %(message)s',  # formato con fecha, nivel y mensaje
    handlers=[logging.StreamHandler(sys.stdout)]  # salida a consola, que Render captura
)

logger = logging.getLogger()


def obtener_fecha_hora_arg():
    zona_ar = pytz.timezone("America/Argentina/Buenos_Aires")
    return datetime.now(zona_ar).strftime("%Y-%m-%d %H:%M:%S")


def ajustar_precision(valor, tick_size_str):
    tick = Decimal(tick_size_str)
    return float((Decimal(str(valor)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)


# Ruta al .env
dotenv_path = os.path.join(os.getcwd(), ".env")

# Cargar variables
load_dotenv(dotenv_path)

# Variables de entorno o define directamente aquí
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
webhook_secret = os.getenv("WEBHOOK_SECRET")
base_url = os.getenv("BINANCE_BASE_URL")

# Leer e imprimir
if os.getenv("BINANCE_API_KEY"):
    logger.info("🔑 BINANCE_API_KEY cargada correctamente.")
else:
    logger.error("❌ BINANCE_API_KEY no encontrada.")

if os.getenv("BINANCE_API_SECRET"):
    logger.info("🔐 BINANCE_API_SECRET cargada correctamente.")
else:
    logger.error("❌ BINANCE_API_SECRET no encontrada.")

if os.getenv("WEBHOOK_SECRET"):
    logger.info("📩 WEBHOOK_SECRET cargado correctamente.")
else:
    logger.warning("⚠️ WEBHOOK_SECRET no definido.")

logger.info("⚠️.... SISTEMA REINICIADO CORRECTAMENTE....")


# 📦 Crear cliente Binance con variables de entorno
client = UMFutures(key=api_key, secret=api_secret, base_url=base_url)


#client = UMFutures(key=api_key, secret=api_secret, base_url="https://testnet.binancefuture.com")


# Funcion para colocar una orden STOP LIMIT en BINANCE intentandolo 3 veces con 1 seg de delay

def colocar_orden_stop_limit(symbol, side, qty, stop_price, limit_price, intentos=3, espera_segundos=1):
    logger.info("✨ Intentando colocar orden STOP LIMIT...")
    logger.info(f"Symbol: {symbol}, Side: {side}, Qty: {qty}, Stop: {stop_price}, Limit: {limit_price}")

    qty_str = str(ajustar_precision(qty, '0.001'))
    stop_str = str(ajustar_precision(stop_price, '0.10'))
    limit_str = str(ajustar_precision(limit_price, '0.10'))

    for intento in range(1, intentos + 1):
        try:
            logger.info(f"🔄 Intento {intento}/{intentos}")
            orden = client.new_order(
                symbol=symbol,
                side=side,
                type="STOP",
                timeInForce="GTC",
                quantity=qty_str,
                stopPrice=stop_str,
                price=limit_str
            )

            logger.info(f"✅ Orden colocada: {orden}")

            if 'orderId' not in orden or orden.get('orderId') is None:
                msg = f"❌ Binance no devolvió un orderId. Respuesta: {orden}"
                logger.error(msg)
                enviar_telegram(msg)
                return None

            return orden['orderId']

        except ClientError as e:
            msg = f"❌ ClientError Binance (intento {intento}): {e.error_code} - {e.error_message}"
            logger.error(msg)
            enviar_telegram(msg)

        except Exception as e:
            msg = f"❌ Error inesperado al colocar orden (intento {intento}): {str(e)}"
            logger.exception(msg)
            enviar_telegram(msg)

        # Esperar antes del próximo intento, si no es el último
        if intento < intentos:
            logger.info(f"⏳ Esperando {espera_segundos} segundo(s) antes de reintentar...")
            time.sleep(espera_segundos)

    logger.error("❌ No se pudo colocar la orden después de múltiples intentos.")
    return None

def verificar_estado_orden(symbol, order_id):
    orden = client.query_order(symbol=symbol, orderId=order_id)
    return orden['status']

def cancelar_orden(symbol, order_id):
    client.cancel_order(symbol=symbol, orderId=order_id)
    logger.info(f"❌ Orden cancelada ({order_id})")

def han_pasado_5_velas(timestamp_inicio):
    ahora = int(time.time() * 1000)
    diferencia = ahora - timestamp_inicio
    minutos_pasados = diferencia // (60 * 1000)
    minutos_faltantes = 60 - minutos_pasados

    if minutos_faltantes > 0:
        #logger.info(f"⌛️ Aún no pasaron 5 velas. Faltan {minutos_faltantes} min.")
        return False
    else:
        logger.info("⏱ Han pasado 6 velas (60 min).")
        return True


def get_position(symbol):
    try:
        positions = client.get_position_risk(symbol=symbol)
        for pos in positions:
            if abs(float(pos['positionAmt'])) > 0:
                return pos
        return None
    except ClientError as e:
        return None

def cerrar_si_sin_sl(symbol: str):
    try:
        # Cancelar todas las órdenes abiertas antes de una nueva entrada
        client.cancel_open_orders(symbol="BTCUSDT")
        logger.info(f"🚫 Todas las órdenes abiertas en {symbol} fueron canceladas")

        # 1. Obtener posición actual
        posiciones = client.get_position_risk(symbol=symbol)
        posicion = next((p for p in posiciones if float(p["positionAmt"]) != 0), None)

        if not posicion:
            logger.info(f"ℹ️ No hay posición abierta en {symbol}.")
            return


        # 2. Cerrar la posición por seguridad
        cantidad = abs(float(posicion["positionAmt"]))
        lado = "SELL" if float(posicion["positionAmt"]) > 0 else "BUY"

        mensaje = (
            f"⚠️ *¡Alerta de seguridad!*\n\n"
            f"Se detectó una posición abierta en `{symbol}`\n"
            f"Se forzó el cierre de la posición: `{lado}` {cantidad} {symbol}"
        )

        logger.warning(mensaje)
        enviar_telegram(mensaje)

        cierre = client.new_order(
            symbol=symbol,
            side=lado,
            type="MARKET",
            quantity=round(cantidad, 3)
        )

        logger.info(f"✅ Posición cerrada por seguridad: {cierre}")

    except Exception as e:
        error_msg = f"❌ Error al cerrar posición en {symbol}: {e}"
        logger.error(error_msg)
        enviar_telegram(error_msg)



# === Lógica principal: ciclo automático ===

def ciclo_bot():
    if estado_orden["activa"]:
        symbol = estado_orden["symbol"]
        order_id = estado_orden["order_id"]

        estado = verificar_estado_orden(symbol, order_id)

        if estado == 'FILLED':
            logger.info("✅ Orden ejecutada.")
            estado_orden["activa"] = False

            try:
                orden_ejecutada = client.query_order(symbol=symbol, orderId=order_id)
                filled_price = float(orden_ejecutada["avgPrice"])
                side = orden_ejecutada["side"]
                qty = float(orden_ejecutada["origQty"])

                sl_distance = estado_orden["sl_distance"]
                tp_factor = estado_orden["tp_factor"]

                opposite = "SELL" if side == "BUY" else "BUY"
                sl_price = round(filled_price - sl_distance if side == "BUY" else filled_price + sl_distance, 2)
                tp_price = round(filled_price + sl_distance * tp_factor if side == "BUY" else filled_price - sl_distance * tp_factor, 2)


                scheduler.remove_job("ciclo_bot")

                # STOP LOSS
                sl_order = client.new_order(
                    symbol=symbol,
                    side=opposite,
                    type="STOP_MARKET",
                    stopPrice=str(ajustar_precision(sl_price, '0.10')),
                    closePosition=True
                )
                logger.info(f"SL colocado: {sl_price}")

                #time.sleep(2)

                # TAKE PROFIT
                tp_order = client.new_order(
                    symbol=symbol,
                    side=opposite,
                    type="LIMIT",
                    quantity=str(ajustar_precision(qty, '0.001')),
                    price=str(ajustar_precision(tp_price, '0.10')),
                    timeInForce="GTC",
                    reduceOnly=True
                )
                logger.info(f"TP colocado: {tp_price}")
                logger.info(f"Tamaño: {qty}")
                logger.info(f"Apalancamiento: {estado_orden.get('apalancamiento', 'N/A')}x")
                logger.info(f"Riesgo: {estado_orden.get('risk_percent', 'N/A')}%")


                # Cuando se completa una entrada se envia un mensaje de TELEGRAM

                fecha = obtener_fecha_hora_arg()
                mensaje = (
                    f"✅ *Orden ejecutada*\n"
                    f"🕒 Fecha: `{fecha}`\n"
                    f"📉 Tipo: *{side}*\n"
                    f"💰 Entrada: `${filled_price:.2f}`\n"
                    f"🎯 TP: `${tp_price:.2f}`\n"
                    f"⚠️ SL: `${sl_price:.2f}`\n"
                    f"📊 Tamaño: `{qty}`\n"
                    f"🎯 Apalancamiento: `{estado_orden.get('apalancamiento', 'N/A')}x`\n"
                    f"📈 Riesgo: `{estado_orden.get('risk_percent', 'N/A')}%`"
                )

                # ✅ Actualizamos el estado_orden con ejecución real
                estado_orden["timestamp_inicio"] = int(time.time() * 1000)
                estado_orden["tp_order_id"] = tp_order["orderId"]

                enviar_telegram(mensaje)
                #scheduler.remove_job("ciclo_bot")
                
                scheduler.add_job(lambda: verificar_salida_programada(client, estado_orden, scheduler, job_id="verif_salida"), 'interval', seconds=60, id="verif_salida")

            except Exception as e:
                logger.error(f"❌ Error al colocar SL/TP: {e}")
                cerrar_si_sin_sl("BTCUSDT")

                fecha = obtener_fecha_hora_arg()

                mensaje = (
                    f"🕒 Fecha: `{fecha}`\n"
                    f"🛑 Error al colocar SL/TP"
                )

                enviar_telegram(mensaje)





        elif han_pasado_5_velas(estado_orden["timestamp_inicio"]):
            cancelar_orden(symbol, order_id)
            logger.info("❌ Orden cancelada por tiempo.")
            estado_orden["activa"] = False
            # Cuando se cancela la Orden STOP LIMIT por tiempo envia un mensaje a TELEGRAM

            fecha = obtener_fecha_hora_arg()

            mensaje = (
                f"❌ *STOP LIMIT cancelada*\n"
                f"🕒 Fecha: `{fecha}`\n"
                f"🛑 Por vencimiento del tiempo"
            )

            enviar_telegram(mensaje)
            scheduler.remove_job("ciclo_bot")

# === Scheduler que ejecuta 5 segundos ===
if not scheduler.get_job("ciclo_bot"):
    scheduler.add_job(ciclo_bot, 'interval', seconds=5, id="ciclo_bot")
    logger.info("⏱ Tarea 'ciclo_bot' programada.")

#Inicia funcion de proteccion ante REINICIO INESPERADO DE RENDER

cerrar_si_sin_sl("BTCUSDT")


# === Webhook para control de estado ===


@app.route('/ping', methods=['GET'])
def ping_binance():
    try:
        return jsonify({
                "status": "ok",
                "message": "Bot en línea",
                "timestamp": int(time.time())
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# === Webhook para recibir señales ===

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    if webhook_secret and data.get("secret") != webhook_secret:
        return jsonify({"error": "❌ Webhook no autorizado"}), 403

    symbol = data.get("symbol", "BTCUSDT")
    side = data.get("side", "").upper()
    entry = float(data.get("entry", 0))
    sl_distance = float(data.get("sl_distance", 0))
    tp_factor = float(data.get("tp_factor", 2.0))
    risk_percent = float(data.get("risk_percent", 1.0))
    limit_offset = float(data.get("limit_offset", 80))

    pos = get_position(symbol)
    if pos:
        if side != "CLOSE":
            return jsonify({"message": "⛔ Ya existe una posición abierta. Solo se permite 'CLOSE'."}), 200
        else:
            try:
                direction = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                qty = abs(float(pos["positionAmt"]))
                client.new_order(symbol=symbol, side=direction, type="MARKET", quantity=qty)
                return jsonify({"message": f"✅ Posición cerrada ({direction})", "qty": qty})
            except ClientError as e:
                return jsonify({"error": str(e)}), 500
    else:
        if side == "CLOSE":
            return jsonify({"message": "ℹ️ No hay posición abierta para cerrar"}), 200

        try:
            # Cancelar todas las órdenes abiertas antes de una nueva entrada
            client.cancel_open_orders(symbol="BTCUSDT")
            #client.cancel_all_open_orders(symbol=symbol)
            logger.info(f"🚫 Todas las órdenes abiertas en {symbol} fueron canceladas")

            balances = client.balance()
            usdt = next(float(b["availableBalance"]) for b in balances if b["asset"] == "USDT")
            risk_amount = usdt * (risk_percent / 100)

            qty = round(risk_amount / sl_distance, 3)
            position_value = entry * qty
            leverage = min(math.ceil(position_value / usdt), 125)
            client.change_leverage(symbol=symbol, leverage=leverage)

            offset = float(data.get("limit_offset", 0))

            # Dividimos el offset recibido
            offset_stop = offset * 0.3
            offset_limit = offset * 0.7

            # Calculamos stop_price (30% del offset)
            stop_price = round(entry + offset_stop if side == "BUY" else entry - offset_stop, 2)

            # Calculamos limit_price (70% adicional sobre stop_price)
            limit_price = round(stop_price + offset_limit if side == "BUY" else stop_price - offset_limit, 2)

            # ✅ Intentamos colocar la orden con reintentos internos
            order_id = colocar_orden_stop_limit(symbol, side, qty, stop_price, limit_price, intentos=3, espera_segundos=1)

            # ❌ Si falla, respondemos con error
            if order_id is None:
                return jsonify({"error": "❌ No se pudo colocar la orden STOP LIMIT"}), 400


            fecha = obtener_fecha_hora_arg()

            mensaje = (
                    f"✅ *Orden STOP LIMIT colocada*\n"
                    f"🕒 Fecha: `{fecha}`\n"
                    f"📉 Tipo: *{side}*\n"
            )

            enviar_telegram(mensaje)
            try:
                if not scheduler.running:
                    scheduler.start()
                    logger.info("🚀 Scheduler iniciado.")

                if not scheduler.get_job("ciclo_bot"):
                    scheduler.add_job(ciclo_bot, 'interval', seconds=5, id="ciclo_bot")
                    logger.info("⏱ Tarea 'ciclo_bot' programada.")
            except JobLookupError as e:
                    logger.error(f"⚠️ Error al buscar job: {e}")
            except Exception as e:
                    logger.error(f"❌ Error al iniciar el scheduler o agregar el job: {e}")
                    


            estado_orden.update({
                "activa": True,
                "order_id": order_id,
                "timestamp_inicio": int(time.time() * 1000),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "sl_distance": sl_distance,
                "tp_factor": tp_factor,
                "risk_percent": risk_percent,
                "apalancamiento": leverage
            })

            return jsonify({
                "msg": "🟢 Orden STOP_LIMIT colocada",
                "order_id": order_id,
                "stop_price": stop_price,
            })


        except Exception as e:
            logger.error(f"❌ Error general en webhook: {e}")
            enviar_telegram(f"❌ Error general en webhook: {e}")
            return jsonify({"msg": "⚠️ Ocurrió un error interno, la orden no se ejecutó", "detalle": str(e)}), 200

