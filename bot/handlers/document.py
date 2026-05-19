import asyncio
import tempfile
from pathlib import Path

from aiogram import Router, F
from aiogram.types import FSInputFile, Message, Document, PhotoSize
from aiogram.exceptions import TelegramRetryAfter

from bot.services.file_service import download_file
from bot.agente_mestre import process
from bot.agents.state_manager import TaskCancelledError
from bot.utils.logger import logger
from bot.utils.validators import validate_file
from bot.utils.status_tracker import StatusTracker
from bot.exporters.txt_exporter import export_txt
from bot.exporters.docx_exporter import export_docx
from bot.exporters.pdf_exporter import export_pdf
from config.settings import settings

router = Router()

OUTPUT_DIR = settings.temp_dir / "output"

user_modes: dict[int, str] = {}


async def _send_with_retry(bot, chat_id: int, msg: str, max_retries: int = 3) -> None:
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id, msg)
            return
        except TelegramRetryAfter as e:
            wait = e.retry_after + attempt * 5
            logger.warning("Telegram rate limit, aguardando {}s: {}", wait, msg[:50])
            await asyncio.sleep(wait)
    logger.error("Falha apos {} tentativas para enviar mensagem", max_retries)


async def _send_doc_with_retry(message: Message, out_path: Path, caption: str, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            await message.answer_document(
                document=FSInputFile(out_path),
                caption=caption,
            )
            return True
        except TelegramRetryAfter as e:
            wait = e.retry_after + attempt * 5
            logger.warning("Telegram rate limit no envio de {}, aguardando {}s", out_path.name, wait)
            await asyncio.sleep(wait)
    logger.error("Falha apos {} tentativas para enviar {}", max_retries, out_path.name)
    return False


@router.message(F.document)
async def handle_document(message: Message) -> None:
    document: Document | None = message.document
    if document is None:
        return

    filename = document.file_name or "documento"
    file_size = document.file_size or 0

    valid, error_msg = validate_file(filename, file_size)
    if not valid:
        await message.answer(error_msg)
        return

    mode = user_modes.pop(message.chat.id, "normal")
    await message.answer("📄 Arquivo recebido!")
    await process_file(message, document.file_id, filename, mode=mode)


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    photo: PhotoSize | None = message.photo[-1] if message.photo else None
    if photo is None:
        return

    mode = user_modes.pop(message.chat.id, "normal")
    await message.answer("📷 Foto recebida!")
    await process_file(message, photo.file_id, "imagem.png", mode=mode)


async def process_file(
    message: Message,
    file_id: str,
    filename: str,
    mode: str = "normal",
) -> None:
    tracker = StatusTracker(message.bot, message.chat.id, filename)

    with tempfile.TemporaryDirectory(dir=settings.temp_dir) as tmpdir:
        try:
            input_path = Path(tmpdir) / filename

            await tracker("Baixando arquivo...")
            await download_file(message.bot, file_id, input_path)

            extracted_text = await process(input_path, status_callback=tracker, mode=mode)

            await tracker("Conteudo extraido com sucesso! Preparando exportacao...")

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            base_name = Path(filename).stem
            txt_path = OUTPUT_DIR / f"{base_name}.txt"
            docx_path = OUTPUT_DIR / f"{base_name}.docx"
            pdf_path = OUTPUT_DIR / f"{base_name}.pdf"

            export_txt(extracted_text, txt_path)
            export_docx(extracted_text, docx_path, filename)
            export_pdf(extracted_text, pdf_path, filename)

            await tracker("Enviando arquivos...")

            caption = "Versao acessivel gerada."

            out_paths = [txt_path, docx_path, pdf_path]
            for i, out_path in enumerate(out_paths):
                if out_path.exists():
                    await _send_doc_with_retry(message, out_path, caption)
                    if i < len(out_paths) - 1:
                        await asyncio.sleep(1.5)

            await tracker.finish(success=True)

        except TaskCancelledError:
            await tracker("Processamento cancelado.")
            await tracker.finish(success=False)
        except Exception:
            logger.exception("Erro ao processar arquivo")
            await tracker.finish(success=False)
