import requests
import logging
import sys
from dotenv import load_dotenv
import os

# === Funciones auxiliares ===
logging.basicConfig(
    level=logging.INFO,  # nivel mínimo de logs a mostrar (INFO y superiores)
    format='%(asctime)s - %(levelname)s - %(message)s',  # formato con fecha, nivel y mensaje
    handlers=[logging.StreamHandler(sys.stdout)]  # salida a consola, que Render captura
)

logger = logging.getLogger()

# Cargar las variables de entorno desde el archivo .env
load_dotenv()

# Obtener las variables de entorno
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def enviar_telegram(mensaje: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, data=payload)
        logger.info(f"✅ Telegram status: {response.status_code}")
        logger.info(f"📨 Telegram response: {response.text}")
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.info(f"[Telegram Error] {e}")

    