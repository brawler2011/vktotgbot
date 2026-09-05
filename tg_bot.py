import io
import re
import html
import logging
from typing import Optional, List, Union
import telegram
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
try:
    from telegram import ReplyParameters
    HAS_REPLY_PARAMETERS = True
except ImportError:
    HAS_REPLY_PARAMETERS = False

from vk_client import VKPost, VKComment, VKClient

logger = logging.getLogger(__name__)

# Max Telegram caption length
MAX_CAPTION_LEN = 1024
# Max Telegram message length
MAX_MESSAGE_LEN = 4096


def format_vk_text(text: str) -> str:
    """
    Форматирует текст из ВК:
    - Преобразует ссылки вида [id123|Имя] в HTML <a href="...">Имя</a>
    - Экранирует спецсимволы HTML
    """
    if not text:
        return ""

    # Функция замены упоминаний [id123|Имя] или [club123|Группа]
    def replace_vk_mention(match):
        raw_target = match.group(1)
        name = html.escape(match.group(2))
        url = f"https://vk.com/{raw_target}"
        return f'<a href="{url}">{name}</a>'

    # 1. Заменяем упоминания ВК
    # Временно экранируем весь текст кроме упоминаний
    pattern = r'\[(id\d+|club\d+|public\d+)\|([^\]]+)\]'
    
    parts = []
    last_idx = 0
    for m in re.finditer(pattern, text):
        # Обычный текст до совпадения - экранируем
        normal_text = text[last_idx:m.start()]
        parts.append(html.escape(normal_text))
        # Упоминание оформляем ссылкой
        parts.append(replace_vk_mention(m))
        last_idx = m.end()

    # Оставшийся хвост
    parts.append(html.escape(text[last_idx:]))
    formatted = "".join(parts)

    return formatted.strip()


class TelegramBridge:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        topic_id: Optional[int] = None,
        admin_id: Optional[int] = None,
        **kwargs
    ):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id or kwargs.get("channel_id", "")
        self.topic_id = topic_id
        self.admin_id = admin_id

    async def send_admin(self, text: str) -> None:
        """Отправляет уведомление или лог администратору."""
        if not self.admin_id:
            return
        try:
            await self.bot.send_message(
                chat_id=self.admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление администратору ({self.admin_id}): {e}")

    async def init(self) -> None:
        """Проверяет права бота в супергруппе и топике."""
        try:
            me = await self.bot.get_me()
            chat = await self.bot.get_chat(chat_id=self.chat_id)
            member = await self.bot.get_chat_member(chat_id=self.chat_id, user_id=me.id)
            topic_info = f", топик (thread): {self.topic_id}" if self.topic_id else ""
            logger.info(
                f"✅ Подключение к чату '{chat.title or self.chat_id}' "
                f"(тип: {chat.type}, статус бота: {member.status}{topic_info})"
            )
            if member.status not in ("administrator", "creator"):
                logger.warning(
                    f"⚠️ Бот не является администратором в чате {self.chat_id} (статус: {member.status}). "
                    "Убедитесь, что у бота есть права на публикацию сообщений в супергруппе и топике!"
                )
        except Exception as e:
            logger.error(f"Не удалось проверить статус бота в чате {self.chat_id}: {e}")

    @property
    def channel_id(self) -> str:
        """Алиас для обратной совместимости."""
        return self.chat_id

    async def send_post(self, post: VKPost, vk_client: VKClient) -> Optional[int]:
        """
        Публикует пост ВК в супергруппу/топик Telegram.
        Возвращает message_id отправленного сообщения.
        """
        formatted_text = format_vk_text(post.text)

        # Собираем дополнительные ссылки (видео, опросы и т.д.)
        extra_parts = []
        if post.extra_links:
            extra_parts.append("\n".join(post.extra_links))

        # Ссылка на оригинал ВК
        orig_link = f'🔗 <a href="{post.vk_url}">Оригинал ВКонтакте</a>'
        
        full_content = formatted_text
        if extra_parts:
            full_content = f"{full_content}\n\n" + "\n\n".join(extra_parts) if full_content else "\n\n".join(extra_parts)

        tg_post_msg_id: Optional[int] = None
        post_kwargs = {}
        if self.topic_id:
            post_kwargs["message_thread_id"] = self.topic_id

        try:
            # Сценарий 1: Есть фотографии
            if post.photos:
                # Если 1 фото
                if len(post.photos) == 1:
                    photo_url = post.photos[0].url
                    # Если текст помещается в caption (до 1024 символов)
                    caption = f"{full_content}\n\n{orig_link}".strip()
                    if len(caption) <= MAX_CAPTION_LEN:
                        msg = await self.bot.send_photo(
                            chat_id=self.chat_id,
                            photo=photo_url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        tg_post_msg_id = msg.message_id
                    else:
                        # Текст слишком длинный: сначала фото, потом текст
                        msg_photo = await self.bot.send_photo(
                            chat_id=self.chat_id,
                            photo=photo_url,
                            **post_kwargs
                        )
                        tg_post_msg_id = msg_photo.message_id
                        msg_text = await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"{full_content}\n\n{orig_link}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        # Запоминаем ID текста, так как он обычно замыкает пост
                        tg_post_msg_id = msg_text.message_id

                # Если альбом фото (2 и более, до 10)
                else:
                    media_photos = post.photos[:10]
                    media_group = []
                    caption = f"{full_content}\n\n{orig_link}".strip()
                    
                    for idx, p in enumerate(media_photos):
                        if idx == 0 and len(caption) <= MAX_CAPTION_LEN:
                            media_group.append(InputMediaPhoto(media=p.url, caption=caption, parse_mode=ParseMode.HTML))
                        else:
                            media_group.append(InputMediaPhoto(media=p.url))

                    msgs = await self.bot.send_media_group(
                        chat_id=self.chat_id,
                        media=media_group,
                        **post_kwargs
                    )
                    tg_post_msg_id = msgs[0].message_id

                    # Если текст был длинным и не влез в caption первого фото
                    if len(caption) > MAX_CAPTION_LEN:
                        msg_text = await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"{full_content}\n\n{orig_link}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        tg_post_msg_id = msg_text.message_id

            # Сценарий 2: Только текст (без фото)
            else:
                text_to_send = f"{full_content}\n\n{orig_link}".strip() if full_content else orig_link
                # Если текст > 4096, делим на части
                if len(text_to_send) > MAX_MESSAGE_LEN:
                    chunks = [text_to_send[i:i + 4000] for i in range(0, len(text_to_send), 4000)]
                    for chunk in chunks:
                        msg = await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=chunk,
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        tg_post_msg_id = msg.message_id
                else:
                    msg = await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=text_to_send,
                        parse_mode=ParseMode.HTML,
                        **post_kwargs
                    )
                    tg_post_msg_id = msg.message_id

            # Сценарий 3: Документы (PDF, DOCX и т.д.)
            if post.docs:
                for doc in post.docs:
                    try:
                        file_bytes = await vk_client.download_file(doc.url)
                        file_io = io.BytesIO(file_bytes)
                        file_io.name = doc.title
                        await self.bot.send_document(
                            chat_id=self.chat_id,
                            document=file_io,
                            caption=f"📎 {html.escape(doc.title)}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить документ {doc.title}: {e}")
                        # Если не удалось скачать/отправить как файл, даём ссылку
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"📎 <a href='{doc.url}'>{html.escape(doc.title)}</a>",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )

            logger.info(f"Пост VK {post.id} успешно опубликован (message_id={tg_post_msg_id})")
            return tg_post_msg_id

        except Exception as e:
            logger.error(f"Ошибка при публикации поста VK {post.id} в Telegram: {e}", exc_info=True)
            return None

    async def send_comment(self, comment: VKComment, post_msg_id: int, vk_client: VKClient) -> Optional[int]:
        """
        Публикует комментарий из ВК в ветку поста в супергруппе/топике.
        """
        if not self.chat_id:
            logger.error("Пропуск комментария: chat_id не задан")
            return None

        formatted_text = format_vk_text(comment.text)
        author_link = f'<a href="{comment.author_url}"><b>{html.escape(comment.author_name)}</b></a>'
        
        lines = [f"💬 {author_link}:"]
        if formatted_text:
            lines.append(formatted_text)
        if comment.extra_links:
            lines.extend(comment.extra_links)

        comment_body = "\n".join(lines)

        reply_kwargs = {}
        if HAS_REPLY_PARAMETERS:
            reply_kwargs["reply_parameters"] = ReplyParameters(
                message_id=post_msg_id,
                allow_sending_without_reply=True
            )
        else:
            reply_kwargs["reply_to_message_id"] = post_msg_id

        if self.topic_id:
            reply_kwargs["message_thread_id"] = self.topic_id

        try:
            # Отправка текста комментария
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=comment_body,
                parse_mode=ParseMode.HTML,
                **reply_kwargs
            )
            sent_msg_id = msg.message_id

            # Если в комментарии были фото
            if comment.photos:
                for p in comment.photos[:5]:
                    try:
                        await self.bot.send_photo(
                            chat_id=self.chat_id,
                            photo=p.url,
                            **reply_kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить фото комментария: {e}")

            # Если в комментарии были документы
            if comment.docs:
                for doc in comment.docs:
                    try:
                        file_bytes = await vk_client.download_file(doc.url)
                        file_io = io.BytesIO(file_bytes)
                        file_io.name = doc.title
                        await self.bot.send_document(
                            chat_id=self.chat_id,
                            document=file_io,
                            caption=f"📎 {html.escape(doc.title)}",
                            parse_mode=ParseMode.HTML,
                            **reply_kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить документ комментария: {e}")

            logger.info(f"Комментарий {comment.id} к посту {comment.post_id} опубликован (ответом на msg_id={post_msg_id})")
            return sent_msg_id

        except Exception as e:
            logger.error(f"Ошибка при отправке комментария {comment.id} в Telegram: {e}", exc_info=True)
            return None
