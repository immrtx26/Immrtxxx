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

BOT_TOKEN = '8471631752:AAFnPkZEsUml4aiU3HvzY8TU2QP_Hv34c68' # PEGA TU TOKEN AQUÍ
IMAGE_DIR = 'capturas'
os.makedirs(IMAGE_DIR, exist_ok=True)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

PHONE, CURP, INE_FRONT, INE_BACK, SELFIE = range(5)

def validate_phone(text: str) -> bool:
    return bool(re.match(r'^\+?1?\d{10,15}$', re.sub(r'[\s\-\(\)]', '', text)))

def validate_curp(text: str) -> bool:
    return bool(re.match(r'^[A-Z0-9]{18}$', text.upper()))

async def start(update: Update, context):
    await update.message.reply_text(" **Bienvenido.**\nIngresa tu número de teléfono con lada:")
    return PHONE

async def get_phone(update: Update, context):
    if validate_phone(update.message.text):
        context.user_data['phone'] = update.message.text
        await update.message.reply_text("✅ **Teléfono aceptado.**\nAhora escribe tu CURP (18 caracteres).")
        return CURP
    await update.message.reply_text("❌ Número inválido.")
    return PHONE

async def get_curp(update: Update, context):
    if validate_curp(update.message.text):
        context.user_data['curp'] = update.message.text.upper()
        await update.message.reply_text("✅ **CURP aceptado.**\nAhora envía una foto del **ANTERIOR de tu INE**.")
        return INE_FRONT
    await update.message.reply_text("❌ CURP inválido.")
    return CURP

async def get_ine_front(update: Update, context):
    if update.message.photo:
        context.user_data['ine_front'] = True
        await update.message.reply_text("✅ **Anterior de INE recibido.**\nAhora envía la **TRASERA (FIRMA)** de tu INE.")
        return INE_BACK
    await update.message.reply_text("⚠️ Envía una imagen.")
    return INE_FRONT

async def get_ine_back(update: Update, context):
    if update.message.photo:
        context.user_data['ine_back'] = True
        await update.message.reply_text("✅ **Trasera de INE recibida.**\nPor último, envíame una **SELFIE** 📸")
        return SELFIE
    await update.message.reply_text("⚠️ Envía una imagen.")
    return INE_BACK

async def get_selfie(update: Update, context):
    if update.message.photo:
        context.user_data['selfie'] = True
        await update.message.reply_text(
            f" **¡Registro Completado!**\n"
            f"Teléfono: {context.user_data['phone']}\n"
            f"CURP: {context.user_data['curp']}\n"
            f"Todo listo, tus datos han sido procesados."
        )
        return ConversationHandler.END
    await update.message.reply_text("⚠️ Envía una imagen.")
    return SELFIE

async def cancel(update: Update, context):
    await update.message.reply_text("Registro cancelado. Usa /start para reiniciar.")
    return ConversationHandler.END

def main():
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
    application.run_polling()

if __name__ == '__main__':
    main()
