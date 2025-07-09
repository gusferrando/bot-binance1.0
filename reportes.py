from telegram_bot import enviar_telegram
from datetime import datetime, timezone, timedelta
from binance.um_futures import UMFutures
import pytz
import logging
import sys

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

# Obtener PnL por timestamp
def obtener_pnl_por_timestamp(client: UMFutures, symbol: str, timestamp: int):
    try:
        fills = client.get_account_trades(symbol=symbol)  # ✅ CORRECTO PARA UMFutures
        pnl_total = 0.0
        comision_total = 0.0

        for fill in fills:
            if int(fill['time']) >= timestamp:
                realized_pnl = float(fill.get('realizedPnl', 0))
                commission = float(fill.get('commission', 0))
                pnl_total += realized_pnl
                comision_total += commission

        return pnl_total, comision_total
    except Exception as e:
        logger.error(f"❌ Error al obtener PnL por timestamp: {e}")
        return None, None


# Verificar si el SL fue ejecutado por timestamp (cuando no se tiene order_id)

def verificar_si_sl_fue_ejecutado(client: UMFutures, symbol: str, timestamp: int, side_entrada: str):
    try:
        opposite_side = "SELL" if side_entrada == "BUY" else "BUY"
        fills = client.get_account_trades(symbol=symbol)  # <- FIXED

        for fill in fills:
            if int(fill['time']) >= timestamp and fill['side'] == opposite_side:
                return True

        return False
    except Exception as e:
        logger.error(f"❌ Error al verificar ejecución del SL: {e}")
        return False


def obtener_balance_usdt(client: UMFutures):
    try:
        balances = client.balance()  # ✅ UMFutures compatible
        for b in balances:
            if b['asset'] == 'USDT':
                return float(b['balance'])
    except Exception as e:
        logger.error(f"❌ Error al obtener balance: {e}")
        return 0.0

def obtener_pnl_por_order_id(client: UMFutures, symbol: str, order_id: int):
    try:
        trades = client.get_account_trades(symbol=symbol)  # ✅ Método correcto
        pnl_total = 0.0
        comision_total = 0.0

        for trade in trades:
            if int(trade['orderId']) == order_id:
                realized_pnl = float(trade.get('realizedPnl', 0))
                commission = float(trade.get('commission', 0))
                pnl_total += realized_pnl
                comision_total += commission

        return pnl_total, comision_total
    except Exception as e:
        logger.error(f"❌ Error al obtener PnL por order_id: {e}")
        return None, None





def verificar_salida_programada(client, estado_orden, scheduler=None, job_id=None):
    """
    Verifica si se ejecutó el TP (por order_id) o el SL (por timestamp).
    Si se detecta salida, envía mensaje con PnL, comisiones y balance, y opcionalmente cancela el job del scheduler.

    Args:
        client: Cliente Binance.
        estado_orden: dict con claves necesarias: timestamp_inicio, side, tp_order_id, symbol.
        scheduler: (opcional) instancia de BackgroundScheduler.
        job_id: (opcional) id del job para eliminarlo tras completarse.
    """
    symbol = estado_orden["symbol"]
    tp_order_id = estado_orden["tp_order_id"]
    timestamp_inicio = estado_orden["timestamp_inicio"]
    side_entrada = estado_orden["side"]

    # 1. Verificamos si se ejecutó el TP (por order_id)
    try:
        tp_info = client.query_order(symbol=symbol, orderId=tp_order_id)  # ✅ CORRECTO PARA UMFutures
        tp_status = tp_info.get("status")

        if tp_status == "FILLED":
            pnl, comision = obtener_pnl_por_order_id(client, symbol, tp_order_id)
            tipo = "TP"
        else:
            # 2. Si no se ejecutó el TP, verificamos el SL (por timestamp)
            sl_ok = verificar_si_sl_fue_ejecutado(client, symbol, timestamp_inicio, side_entrada)
            if sl_ok:
                pnl, comision = obtener_pnl_por_timestamp(client, symbol, timestamp_inicio)
                tipo = "SL"
            else:
                # Ninguna salida detectada aún
                return
    except Exception as e:
        logger.error(f"❌ Error al consultar TP/SL: {e}")
        return

   
    # 3. Si se ejecutó alguna salida, preparamos el mensaje
    balance = obtener_balance_usdt(client)
    fecha = obtener_fecha_hora_arg()
    icono = "🟢" if pnl > 0 else "🔴"
    tipo_texto = "Take Profit" if tipo == "TP" else "Stop Loss"

    mensaje = (
        f"{icono} *{tipo_texto} ejecutado*\n"
        f"🕒 Fecha: `{fecha}`\n"
        f"💰 PnL Realizado: `${pnl:.2f}`\n"
        f"💸 Comisión total: `${comision:.4f}`\n"
        f"🏦 Balance actual: `${balance:.2f}`"
    )
    enviar_telegram(mensaje)

    mensaje_limpio = (
    f"{icono} {tipo_texto} ejecutado\n"
    f"🕒 Fecha: {fecha}\n"
    f"💰 PnL Realizado: ${pnl:.2f}\n"
    f"💸 Comisión total: ${comision:.4f}\n"
    f"🏦 Balance actual: ${balance:.2f}"
    )

    logger.info(mensaje_limpio)

    # 4. Cancelamos el scheduler si corresponde
    if scheduler and job_id:
        try:
            scheduler.remove_job(job_id)
            logger.info(f"🛑 Scheduler '{job_id}' detenido tras detectar salida.")
        except Exception as e:
            logger.info(f"⚠️ No se pudo detener el scheduler '{job_id}': {e}")
