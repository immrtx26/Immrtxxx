import logging
import os
import re
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler
)

# --- CONFIGURACIÓN ---
# Reemplaza con tu token real de @BotFather
BOT_TOKEN = 'TU_TOKEN_AQUI'

# Carpeta donde se guardarán las imágenes (Windows manejará la ruta automáticamente)
IMAGE_DIR = os.path.join(os.getcwd(), "capturas")
if not os.path.exists(IMAGE_DIR):
    os.makedirs(IMAGE_DIR)

# Configuración de logs para ver errores en la consola de Windows
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Estados
PHONE, CURP, INE_FRONT, INE_BACK, SELFIE = range(5)

# --- Validaciones ---
def validate_phone(text: str) -> bool:
    return bool(re.match(r'^\+?1?\d{10,15}$', re.sub(r'[\s\-\(\)]', '', text)))

def validate_curp(text: str) -> bool:
    return bool(re.match(r'^[A-Z0-9]{18}$', text.upper()))

# --- Funciones de Respuesta ---
async def start(update: Update, context):
    await update.message.reply_text("👋 **Bienvenido al Sistema de Registro.**\nPor favor, introduce tu número de teléfono con lada:")
    return PHONE

async def get_phone(update: Update, context):
    if validate_phone(update.message.text):
        context.user_data['phone'] = update.message.text
        await update.message.reply_text("✅ **Teléfono guardado.**\nAhora escribe tu CURP (18 caracteres).")
        return CURP
    await update.message.reply_text("❌ Error: Por favor usa solo números y el código de país si es necesario.")
    return PHONE

async def get_curp(update: Update, context):
    if validate_curp(update.message.text):
        context.user_data['curp'] = update.message.text.upper()
        await update.message.reply_text("✅ **CURP guardado.**\nAhora envía una foto del **ANTERIOR de tu INE**.")
        return INE_FRONT
    await update.message.reply_text("❌ Error: El CURP debe tener 18 caracteres alfanuméricos.")
    return CURP

async def get_ine_front(update: Update, context):
    if update.message.photo:
        context.user_data['ine_front'] = True
        await update.message.reply_text("✅ **Anterior recibido.**\nAhora envía la **TRASERA de tu INE** (con firma y código).")
        return INE_BACK
    await update.message.reply_text("⚠️ Por favor, envía una foto del documento.")
    return INE_FRONT

async def get_ine_back(update: Update, context):
    if update.message.photo:
        context.user_data['ine_back'] = True
        await update.message.reply_text("✅ **Trasera recibida.**\nFinalmente, envía una **SELFIE** clara.")
        return SELFIE
    await update.message.reply_text("⚠️ Por favor, envía una foto.")
    return INE_BACK

async def get_selfie(update: Update, context):
    if update.message.photo:
        context.user_data['selfie'] = True
        await update.message.reply_text(
            f"✅ **¡Registro Completado con éxito!**\n\n"
            f"📱 Teléfono: {context.user_data['phone']}\n"
            f"🆔 CURP: {context.user_data['curp']}"
        )
        return ConversationHandler.END
    await update.message.reply_text("⚠️ Por favor, envía una foto de tu rostro.")
    return SELFIE

async def cancel(update: Update, context):
    await update.message.reply_text("Proceso cancelado. Usa /start para reiniciar.")
    return ConversationHandler.END

def main():
    # En Windows, a veces es necesario asegurar que el loop sea correcto
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            CURP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_curp)],
            INE_FRONT: [MessageHandler(filters.PHOTO, get_ine_front)],
            INE_BACK: [MessageHandler(filters.PHOTO, get_ine_back)],
            SELFIE: [MessageHandler(filters.PHOTO, get_selfie)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(conv_handler)
    print("--- Bot en ejecución (Presiona Ctrl+C para detener) ---")
    application.run_polling()

if __name__ == '__main__':
    main()