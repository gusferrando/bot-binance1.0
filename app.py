from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
from binance.error import ClientError
from dotenv import load_dotenv
import math
import time
import os
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from decimal import Decimal, ROUND_HALF_UP



# Configurar logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Handler para consola (Render lo captura)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)

# Forzar codificación UTF-8 en Windows para evitar UnicodeEncodeError
if sys.platform == "win32":
    import io
    console_handler.stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Handler para archivo con rotación (hasta 1MB, 3 archivos de backup)
file_handler = RotatingFileHandler('logs/bot.log', maxBytes=1_000_000, backupCount=3)
file_handler.setLevel(logging.INFO)

# Formato del log
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# Evitar duplicados si se recarga el script
if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
else:
    logger.handlers[0] = console_handler
    logger.handlers[1:] = [file_handler]




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

# Leer e imprimir con logger
logger.info(f"api_key: {os.getenv('BINANCE_API_KEY')}")
logger.info(f"API SECRET: {os.getenv('BINANCE_API_SECRET')}")
logger.info(f"WEBHOOK_SECRET: {os.getenv('WEBHOOK_SECRET')}")


client = UMFutures(key=api_key, secret=api_secret, base_url="https://testnet.binancefuture.com")
app = Flask(__name__)

def get_position(symbol):
    try:
        positions = client.get_position_risk(symbol=symbol)
        for pos in positions:
            if abs(float(pos['positionAmt'])) > 0:
                return pos
        return None
    except ClientError as e:
        logger.error(f"Error al obtener posición: {e}")
        return None


def get_filled_price(order_id, symbol):
    try:
        response = client.query_order(symbol=symbol, orderId=order_id)
        status = response.get("status")
        executed_qty = float(response.get("executedQty", 0))
        avg_price = float(response.get("avgPrice", 0))

        logger.info(f" Estado de orden: {status}, Ejecutado: {executed_qty}, Precio Promedio: {avg_price}")

        if status in ["FILLED", "PARTIALLY_FILLED"] and executed_qty > 0 and avg_price > 0:
            return avg_price
        else:
            logger.warning(f" La orden no está ejecutada aún. Estado: {status}")
            return None

    except Exception as e:
        logger.error(f" Error al obtener la orden {order_id}: {e}")
        return None


# Obtener mark price (precio de referencia de futuros)
def get_mark_price(symbol):
    try:
        mark = client.mark_price(symbol=symbol)
        return float(mark['markPrice'])
    except Exception as e:
        logger.error(f" Error al obtener markPrice de {symbol}: {e}")
        return None

# Obtener last price (último precio de transacción)
def get_last_price(symbol):
    try:
        ticker = client.ticker_price(symbol=symbol)
        return float(ticker['price'])
    except Exception as e:
        logger.error(f" Error al obtener lastPrice de {symbol}: {e}")
        return None

@app.route('/ping', methods=['GET'])
def ping_binance():
    try:
        response = client.ping()
        return jsonify({"status": "ok", "binance_response": response}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/balance', methods=['GET'])
def get_balance():
    try:
        account_info = client.balance()
        usdt_balance = next((item for item in account_info if item["asset"] == "USDT"), None)
        if usdt_balance:
            return jsonify({
                "status": "ok",
                "USDT_wallet_balance": usdt_balance["walletBalance"],
                "USDT_available_balance": usdt_balance["availableBalance"]
            }), 200
        else:
            return jsonify({"status": "error", "message": "USDT balance not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if webhook_secret and data.get("secret") != webhook_secret:
        return jsonify({"error": " Webhook no autorizado"}), 403

    symbol = data.get("symbol", "BTCUSDT")
    side = data.get("side", "").upper()  # BUY, SELL, CLOSE
    entry = float(data.get("entry", 0))
    sl_distance = float(data.get("sl_distance", 0))
    tp_factor = float(data.get("tp_factor", 2.0))
    risk_percent = float(data.get("risk_percent", 1.0))
    limit_offset = float(data.get("limit_offset", 80))

    pos = get_position(symbol)
    if pos:
        if side != "CLOSE":
            return jsonify({"message": " Ya existe una posición abierta. Solo se permite 'CLOSE'."}), 200
        else:
            try:
                direction = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                qty = abs(float(pos["positionAmt"]))
                client.new_order(symbol=symbol, side=direction, type="MARKET", quantity=qty)
                return jsonify({"message": f" Posición cerrada ({direction})", "qty": qty})
            except ClientError as e:
                return jsonify({"error": str(e)}), 500
    else:
        if side == "CLOSE":
            return jsonify({"message": " No hay posición abierta para cerrar"}), 200

        try:
            # Obtener balance USDT
            balances = client.balance()
            usdt = next(float(b["availableBalance"]) for b in balances if b["asset"] == "USDT")
            risk_amount = usdt * (risk_percent / 100)

            qty = round(risk_amount / sl_distance, 3)
            position_value = entry * qty
            leverage = min(math.ceil(position_value / usdt), 125)  # Binance limita a 125x
            client.change_leverage(symbol=symbol, leverage=leverage)

            max_attempts = 3
            filled_price = None
            for attempt in range(max_attempts):
                limit_price = round(entry + limit_offset if side == "BUY" else entry - limit_offset, 2)
                order = client.new_order(
                    symbol=symbol,
                    side=side,
                    type="LIMIT",
                    price=limit_price,
                    quantity=qty,
                    timeInForce="GTC"
                )
                order_id = order['orderId']
                logger.info(f" Intento {attempt+1}: esperando ejecución a {limit_price}")
                time.sleep(2)

                filled_price = get_filled_price(order_id, symbol)
                if filled_price:
                    logger.info(f" Orden LIMIT ejecutada a {filled_price}")
                    break
                else:
                    logger.warning(f" Orden no ejecutada. Cancelando intento {attempt+1}")
                    client.cancel_order(symbol=symbol, orderId=order_id)
                    limit_offset += 5

            if not filled_price:
                logger.warning(" No se logró ejecutar por LIMIT. Enviando orden MARKET...")
                order = client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
                time.sleep(5)
                filled_price = get_filled_price(order['orderId'], symbol)
                logger.info(f"Precio llenado orden MARKET: {filled_price}")

            # Asegurarse de que sea float y mayor a 0
            if filled_price:
                try:
                    filled_price = float(filled_price)
                    if filled_price <= 0:
                        logger.warning(" filled_price <= 0, descartando.")
                        filled_price = None
                except ValueError:
                    logger.warning(" filled_price no se pudo convertir a float.")
                    filled_price = None


            if not filled_price:
                logger.warning(" No se pudo obtener precio ejecutado. Usando markPrice...")
                filled_price = get_mark_price(symbol)

            if not filled_price:
                logger.warning(" No se pudo obtener markPrice. Usando lastPrice...")
                filled_price = get_last_price(symbol)

            if not filled_price:
                logger.warning(" No se pudo obtener ningún precio del mercado. Usando entry recibido por JSON...")
                filled_price = entry  # este valor debe estar en el JSON

            # Reconfirmación por seguridad extrema
            if not filled_price:
                return jsonify({"error": " No se pudo obtener ningún precio válido para SL/TP"}), 500                


            opposite = "SELL" if side == "BUY" else "BUY"
            sl_price = round(filled_price - sl_distance if side == "BUY" else filled_price + sl_distance, 2)
            tp_price = round(filled_price + sl_distance * tp_factor if side == "BUY" else filled_price - sl_distance * tp_factor, 2)

            # Ajustar precisión antes de enviar las órdenes
            qty = ajustar_precision(qty, '0.001')
            tp_price = ajustar_precision(tp_price, '0.10')
            sl_price = ajustar_precision(sl_price, '0.10')

            # Conversión a string (para evitar errores de precisión en órdenes LIMIT)
            sl_price_str = str(sl_price)
            tp_price_str = str(tp_price)

            # STOP LOSS
            sl_order = client.new_order(
                symbol=symbol,
                side=opposite,
                type="STOP_MARKET",
                stopPrice=str(sl_price),
                closePosition=True
            )
            logger.info(f"SL Order response: {sl_order}")


            # TP
            logger.info(f" TP price: {tp_price} ({type(tp_price)})")
            logger.info(f" Qty: {qty} ({type(qty)})")

            try:
                tp_order = client.new_order(
                    symbol=symbol,
                    side=opposite,
                    type="LIMIT",
                    quantity=str(qty),
                    price=str(tp_price),  # Asegúrate de que sea str
                    timeInForce="GTC",
                    reduceOnly=True
                )
                logger.info(f"TP Order response: {tp_order}")
            except Exception as e:
                logger.error(f" Error al colocar TP: {e}")

            return jsonify({
                "message": f" Orden {side} ejecutada",
                "qty": qty,
                "entry": filled_price,
                "tp": tp_price,
                "sl": sl_price,
                "leverage": leverage
            })

        except ClientError as e:
            return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
