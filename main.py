import os
import time
import json
import tempfile
import requests
import sys
from functools import wraps
from threading import Thread

import vk_api
import telebot
from telebot import types
from vk_api.longpoll import VkLongPoll
from vk_api.upload import VkUpload
from loguru import logger

logger.configure(handlers=[
    {"sink": sys.stderr, "format": "{time} {level} {function} {message}"}
])

# ─── Config ───────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_data():
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = load_json("data.json")
chats = load_json("chats.json")

def get_secret(env: str, key: str | None = None, required: bool = False) -> str:
    val = os.getenv(env) or (data.get(key) if key else None)
    if required and not val:
        raise RuntimeError(f"Отсутствует секрет {env}")
    return val or ""

# Initialize data structure
if not isinstance(data.get("tg_ids"), list):
    data["tg_ids"] = []

if not isinstance(data.get("user_states"), dict):
    data["user_states"] = {}

TG_TOKEN    = get_secret("TG_TOKEN",    "tg_token", required=True)
VK_TOKEN    = get_secret("VK_TOKEN",    "vk_token", required=True)
BOT_PASSWORD = get_secret("BOT_PASSWORD", "password")

# Don't store secrets in file if they came from env
for env, key in (("TG_TOKEN", "tg_token"), ("VK_TOKEN", "vk_token"), ("BOT_PASSWORD", "password")):
    if os.getenv(env):
        data.pop(key, None)

tg = telebot.TeleBot(TG_TOKEN)
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk_session._auth_token()
vk = vk_session.get_api()
upload = VkUpload(vk_session)

# ─── User state ───────────────────────────────────────────────────────────────

CHAT_PEER_OFFSET = 2_000_000_000


def is_chat_peer(peer_id: int) -> bool:
    return peer_id > CHAT_PEER_OFFSET


def peer_kind_label(peer_id: int) -> str:
    return "беседа" if is_chat_peer(peer_id) else "личка"


def parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    return default

def get_tg_ids() -> list:
    return data["tg_ids"]

def get_user_state(user_id: int) -> dict:
    raw = data["user_states"].get(str(user_id), {})
    try:
        current_chat = int(raw.get("current_chat", 0) or 0)
    except (TypeError, ValueError):
        current_chat = 0
    default_is_chat = is_chat_peer(current_chat) if current_chat else False
    return {
        "current_chat": current_chat,
        "isChat": parse_bool(raw.get("isChat", default_is_chat), default=default_is_chat),
    }

def set_user_state(user_id: int, current_chat: int, is_chat: bool):
    data["user_states"][str(user_id)] = {
        "current_chat": int(current_chat),
        "isChat": bool(is_chat),
    }

def is_authorized(user_id: int) -> bool:
    return user_id in get_tg_ids()

def is_admin(user_id: int) -> bool:
    ids = get_tg_ids()
    return bool(ids) and ids[0] == user_id

# Sync orphaned user_states into tg_ids
_known = set(get_tg_ids())
for _uid_str in list(data["user_states"]):
    try:
        _uid = int(_uid_str)
        if _uid not in _known:
            data["tg_ids"].append(_uid)
            _known.add(_uid)
    except ValueError:
        pass
save_data()

# ─── Chat helpers ─────────────────────────────────────────────────────────────

def resolve_chat_target(key: str) -> tuple[int, bool]:
    value = int(chats[key])
    return value, is_chat_peer(value)

def get_allowed_peer_ids() -> set[int]:
    allowed = set()
    for key in chats:
        try:
            peer_id, _ = resolve_chat_target(key)
            allowed.add(peer_id)
            logger.info(f"✅ '{key}': peer_id={peer_id} ({peer_kind_label(peer_id)})")
        except Exception as e:
            logger.error(f"❌ Не удалось разрешить '{key}': {e}")
    return allowed

def reload_chats_and_allowed() -> tuple[bool, str]:
    global chats, ALLOWED_PEER_IDS
    try:
        chats = load_json("chats.json")
        ALLOWED_PEER_IDS = get_allowed_peer_ids()
        return True, "✅ chats.json успешно перезагружен"
    except Exception as e:
        return False, f"❌ Не удалось перезагрузить chats.json: {e}"

def get_current_chat_name(user_id: int) -> str:
    current = get_user_state(user_id)["current_chat"]
    if not current:
        return "не выбран"
    for name in chats:
        try:
            peer_id, _ = resolve_chat_target(name)
            if peer_id == current:
                return name + f" ({peer_kind_label(peer_id)})"
        except Exception:
            pass
    return str(current)

def get_vk_send_kwargs(user_id: int) -> dict | None:
    state = get_user_state(user_id)
    if not state["current_chat"]:
        return None
    return {"peer_id": state["current_chat"]}

ALLOWED_PEER_IDS = get_allowed_peer_ids()

# ─── Broadcast ────────────────────────────────────────────────────────────────

def broadcast(text: str, **kwargs):
    for tid in get_tg_ids():
        try:
            tg.send_message(tid, text, **kwargs)
        except Exception as e:
            logger.error(f"broadcast → {tid}: {e}")

def broadcast_media(method: str, *args, **kwargs):
    fn = getattr(tg, method)
    for tid in get_tg_ids():
        try:
            fn(tid, *args, **kwargs)
        except Exception as e:
            logger.error(f"broadcast_media({method}) → {tid}: {e}")

# ─── TG utils ─────────────────────────────────────────────────────────────────

def download_tg_file(file_id: str) -> str:
    info = tg.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{info.file_path}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    ext = info.file_path.rsplit(".", 1)[-1] if "." in info.file_path else "bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp.write(r.content)
    tmp.close()
    return tmp.name

def auth_required(fn):
    @wraps(fn)
    def wrapper(message):
        if not is_authorized(message.chat.id):
            tg.send_message(message.chat.id, "⛔ Нет доступа. Используй /start")
            return
        return fn(message)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(message):
        if not is_admin(message.chat.id):
            tg.send_message(message.chat.id, "⛔ Только для администратора")
            return
        return fn(message)
    return wrapper

# ─── Commands ─────────────────────────────────────────────────────────────────

@tg.message_handler(commands=["start"])
def cmd_start(message):
    try:
        parts = message.text.split()
        if BOT_PASSWORD and (len(parts) < 2 or parts[1] != BOT_PASSWORD):
            tg.send_message(message.chat.id, "⛔ Неверный пароль")
            return

        uid = message.chat.id
        if uid in get_tg_ids():
            tg.send_message(uid, "ℹ️ Вы уже авторизованы")
            return

        data["tg_ids"].append(uid)
        ids = get_tg_ids()
        base_state = get_user_state(ids[0]) if len(ids) > 1 else {"current_chat": 0, "isChat": False}
        set_user_state(uid, base_state["current_chat"], base_state["isChat"])
        save_data()

        role = "👑 Администратор" if len(ids) == 1 else "👤 Пользователь"
        tg.send_message(uid, f"✅ Авторизован как {role}\n`ID: {uid}`", parse_mode="Markdown")
        logger.info(f"Новый пользователь: {uid}")
    except Exception as e:
        logger.error(f"cmd_start: {e}")


@tg.message_handler(commands=["help"])
@auth_required
def cmd_help(message):
    tg.send_message(message.chat.id, (
        "*Команды бота:*\n\n"
        "/chats — список доступных чатов\n"
        "/switch <имя> — переключить активный чат\n"
        "/mychat — текущий чат\n"
        "/clear\\_chat — сбросить текущий чат\n"
        "/status — статус и статистика\n"
        "/whoami — ваш TG ID и роль\n"
        "/allowed — разрешённые VK-чаты\n"
        "/users — авторизованные пользователи\n"
        "/kick <id> — удалить пользователя _(только админ)_\n"
        "/reload\\_chats — перечитать chats.json _(только админ)_\n"
    ), parse_mode="Markdown")


@tg.message_handler(commands=["whoami"])
@auth_required
def cmd_whoami(message):
    role = "👑 Администратор" if is_admin(message.chat.id) else "👤 Пользователь"
    tg.send_message(message.chat.id,
                    f"Ваш TG ID: `{message.chat.id}`\nРоль: {role}",
                    parse_mode="Markdown")


@tg.message_handler(commands=["status"])
@auth_required
def cmd_status(message):
    state = get_user_state(message.chat.id)
    role = "👑 Администратор" if is_admin(message.chat.id) else "👤 Пользователь"
    tg.send_message(message.chat.id, (
        f"*Статус бота*\n\n"
        f"💬 Активный чат: `{get_current_chat_name(message.chat.id)}`\n"
        f"🔗 Тип: {peer_kind_label(state['current_chat']).capitalize()}\n"
        f"👥 Пользователей: {len(get_tg_ids())}\n"
        f"🎭 Ваша роль: {role}"
    ), parse_mode="Markdown")


@tg.message_handler(commands=["mychat"])
@auth_required
def cmd_mychat(message):
    state = get_user_state(message.chat.id)
    tg.send_message(message.chat.id, (
        f"*Ваш текущий чат*\n\n"
        f"💬 Имя: `{get_current_chat_name(message.chat.id)}`\n"
        f"🆔 peer/user id: `{state['current_chat']}`\n"
        f"🔗 Тип: {peer_kind_label(state['current_chat']).capitalize()}"
    ), parse_mode="Markdown")


@tg.message_handler(commands=["clear_chat"])
@auth_required
def cmd_clear_chat(message):
    set_user_state(message.chat.id, 0, False)
    save_data()
    tg.send_message(message.chat.id, "✅ Активный чат сброшен. Выберите новый через /chats")


@tg.message_handler(commands=["allowed"])
@auth_required
def cmd_allowed(message):
    if not chats:
        tg.send_message(message.chat.id, "Список чатов пуст")
        return
    lines = []
    for key in chats:
        try:
            peer_id, _ = resolve_chat_target(key)
            lines.append(f"- {key}: {peer_id} ({peer_kind_label(peer_id)})")
        except Exception as e:
            lines.append(f"- {key}: ошибка ({e})")
    tg.send_message(message.chat.id, "Разрешённые VK-чаты:\n" + "\n".join(lines))


@tg.message_handler(commands=["users"])
@auth_required
def cmd_users(message):
    ids = get_tg_ids()
    if not ids:
        tg.send_message(message.chat.id, "Нет авторизованных пользователей")
        return
    lines = [f"{'👑' if i == 0 else '👤'} `{uid}`" for i, uid in enumerate(ids)]
    tg.send_message(message.chat.id,
                    "*Авторизованные пользователи:*\n" + "\n".join(lines),
                    parse_mode="Markdown")


@tg.message_handler(commands=["kick"])
@admin_required
def cmd_kick(message):
    try:
        target_id = int(message.text.split()[1])
        if target_id == message.chat.id:
            tg.send_message(message.chat.id, "❌ Нельзя удалить самого себя")
            return
        ids = get_tg_ids()
        if target_id not in ids:
            tg.send_message(message.chat.id, "❌ Пользователь не найден")
            return
        ids.remove(target_id)
        data["tg_ids"] = ids
        data["user_states"].pop(str(target_id), None)
        save_data()
        tg.send_message(message.chat.id, f"✅ Пользователь `{target_id}` удалён", parse_mode="Markdown")
    except (IndexError, ValueError):
        tg.send_message(message.chat.id, "Использование: `/kick <id>`", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"cmd_kick: {e}")


@tg.message_handler(commands=["chats"])
@auth_required
def cmd_chats(message):
    try:
        kb = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=True)
        kb.add(*[f"/switch {k}" for k in chats])
        tg.send_message(message.chat.id, "Выберите чат:", reply_markup=kb)
    except Exception as e:
        logger.error(f"cmd_chats: {e}")
        tg.send_message(message.chat.id, "Ошибка. Проверьте chats.json")


@tg.message_handler(commands=["reload_chats"])
@auth_required
@admin_required
def cmd_reload_chats(message):
    ok, text = reload_chats_and_allowed()
    tg.send_message(message.chat.id, text)
    if ok:
        logger.info("chats.json перезагружен администратором")


@tg.message_handler(commands=["switch"])
@auth_required
def cmd_switch(message):
    try:
        key = message.text.split()[1]
        if key not in chats:
            tg.send_message(message.chat.id, "❌ Такого чата нет в базе")
            return
        current_chat, is_chat = resolve_chat_target(key)
        set_user_state(message.chat.id, current_chat, is_chat)
        save_data()
        tg.send_message(message.chat.id, f"✅ Чат сменён на *{key}*", parse_mode="Markdown")
        logger.info(f"Чат сменён: {key} ({current_chat})")
    except (IndexError, KeyError):
        tg.send_message(message.chat.id, "Использование: `/switch <имя>`", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"cmd_switch: {e}")
        tg.send_message(message.chat.id, "Ошибка при переключении чата")

# ─── TG → VK ─────────────────────────────────────────────────────────────────

def _vk_send_guard(user_id: int) -> dict | None:
    """Returns send kwargs or notifies the user and returns None."""
    send_kwargs = get_vk_send_kwargs(user_id)
    if not send_kwargs:
        tg.send_message(user_id, "Сначала выберите чат: /chats")
    return send_kwargs


def _send_doc_to_vk(message, file_id: str, title: str = "file", caption: str = ""):
    """Downloads a TG file and uploads it as a VK document."""
    uid = message.chat.id
    if not is_authorized(uid):
        return
    send_kwargs = _vk_send_guard(uid)
    if not send_kwargs:
        return
    path = None
    try:
        path = download_tg_file(file_id)
        peer_id = get_user_state(uid)["current_chat"]
        doc = upload.document_message(path, peer_id=peer_id, title=title)
        att = f"doc{doc['owner_id']}_{doc['id']}"
        vk.messages.send(**send_kwargs, random_id=0, message=caption, attachment=att)
    except Exception as e:
        tg.send_message(uid, f"❌ Ошибка: {e}")
        logger.error(f"_send_doc_to_vk: {e}")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@tg.message_handler(content_types=["text"])
def on_text(message):
    if not is_authorized(message.chat.id) or not message.text or message.text[0] in ("/", "!"):
        return
    send_kwargs = _vk_send_guard(message.chat.id)
    if not send_kwargs:
        return
    try:
        vk.messages.send(**send_kwargs, random_id=0, message=message.text)
    except Exception as e:
        tg.send_message(message.chat.id, f"❌ Ошибка: {e}")
        logger.error(f"on_text: {e}")


@tg.message_handler(content_types=["photo"])
def on_photo(message):
    uid = message.chat.id
    if not is_authorized(uid):
        return
    send_kwargs = _vk_send_guard(uid)
    if not send_kwargs:
        return
    path = None
    try:
        path = download_tg_file(message.photo[-1].file_id)
        photos = upload.photo_messages(path)
        att = f"photo{photos[0]['owner_id']}_{photos[0]['id']}"
        vk.messages.send(**send_kwargs, random_id=0, message=message.caption or "", attachment=att)
    except Exception as e:
        tg.send_message(uid, f"❌ Ошибка отправки фото: {e}")
        logger.error(f"on_photo: {e}")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@tg.message_handler(content_types=["voice"])
def on_voice(message):
    uid = message.chat.id
    if not is_authorized(uid):
        return
    send_kwargs = _vk_send_guard(uid)
    if not send_kwargs:
        return
    ogg = None
    try:
        path = download_tg_file(message.voice.file_id)
        ogg = path + ".ogg"
        os.rename(path, ogg)
        peer_id = get_user_state(uid)["current_chat"]
        doc = upload.document_message(ogg, peer_id=peer_id, title="voice.ogg")
        att = f"doc{doc['owner_id']}_{doc['id']}"
        vk.messages.send(**send_kwargs, random_id=0, attachment=att)
    except Exception as e:
        tg.send_message(uid, f"❌ Ошибка отправки голосового: {e}")
        logger.error(f"on_voice: {e}")
    finally:
        if ogg and os.path.exists(ogg):
            os.unlink(ogg)


@tg.message_handler(content_types=["document"])
def on_document(message):
    _send_doc_to_vk(message, message.document.file_id,
                    title=message.document.file_name or "file",
                    caption=message.caption or "")


@tg.message_handler(content_types=["video", "video_note"])
def on_video(message):
    file_id = message.video.file_id if message.video else message.video_note.file_id
    _send_doc_to_vk(message, file_id, title="video.mp4",
                    caption=getattr(message, "caption", "") or "")


@tg.message_handler(content_types=["audio"])
def on_audio(message):
    _send_doc_to_vk(message, message.audio.file_id,
                    title=message.audio.file_name or "audio.mp3")

# ─── VK → TG ─────────────────────────────────────────────────────────────────

def format_fwd_messages(fwd_messages: list, depth: int = 0) -> str:
    lines = []
    indent = "  " * depth
    for msg in fwd_messages:
        try:
            s = vk.users.get(user_ids=msg["from_id"])[0]
            name = f"{s['first_name']} {s['last_name']}"
        except Exception:
            name = f"ID{msg.get('from_id', '?')}"
        text = msg.get("text", "")
        lines.append(f"{indent}↪ _{name}: {text}_" if text else f"{indent}↪ _{name}_")
        if msg.get("fwd_messages"):
            lines.append(format_fwd_messages(msg["fwd_messages"], depth + 1))
    return "\n".join(lines)


def get_reply_text(message_data: dict) -> str:
    try:
        reply = message_data["reply_message"]
        s = vk.users.get(user_ids=reply["from_id"])[0]
        name = f"{s['first_name']} {s['last_name']}"
        return f"↩ _{name}_: {reply['text']}"
    except Exception as e:
        logger.error(f"get_reply_text: {e}")
        return "↩ _ошибка получения ответа_"


def handle_attachments(attachments: list, caption: str = ""):
    for att in attachments:
        att_type = att.get("type")
        try:
            if att_type == "photo":
                url = sorted(att["photo"]["sizes"], key=lambda x: x.get("width", 0))[-1]["url"]
                broadcast_media("send_photo", requests.get(url, timeout=30).content, caption=caption or None)
                caption = ""

            elif att_type == "audio_message":
                url = att["audio_message"].get("link_ogg") or att["audio_message"].get("link_mp3")
                if url:
                    broadcast_media("send_voice", requests.get(url, timeout=30).content)

            elif att_type == "doc":
                preview = att["doc"].get("preview", {})
                if "audio_msg" in preview:
                    audio = preview["audio_msg"]
                    url = audio.get("link_ogg") or audio.get("link_mp3")
                    if url:
                        broadcast_media("send_voice", requests.get(url, timeout=30).content)
                else:
                    url = att["doc"].get("url")
                    if url:
                        broadcast_media("send_document",
                                        requests.get(url, timeout=30).content,
                                        visible_file_name=att["doc"].get("title", "file"))

            elif att_type == "video":
                v = att["video"]
                broadcast(f"🎬 *{v.get('title', 'Видео')}*\nhttps://vk.com/video{v['owner_id']}_{v['id']}",
                          parse_mode="Markdown")

            elif att_type == "audio":
                a = att["audio"]
                broadcast(f"🎵 *{a.get('artist', '?')} — {a.get('title', '?')}*", parse_mode="Markdown")

            elif att_type == "sticker":
                imgs = att["sticker"].get("images_with_background") or att["sticker"].get("images", [])
                if imgs:
                    broadcast_media("send_photo", requests.get(imgs[-1]["url"], timeout=30).content)

            elif att_type == "link":
                lnk = att["link"]
                broadcast(f"🔗 {lnk.get('title', '')}\n{lnk.get('url', '')}")

        except Exception as e:
            logger.error(f"handle_attachments({att_type}): {e}")


def vk_work():
    logger.info("VK longpoll started")
    if not ALLOWED_PEER_IDS:
        logger.warning("Список разрешённых чатов пуст — входящие из VK пересылаться не будут")

    while True:
        try:
            longpoll = VkLongPoll(vk_session)
            for event in longpoll.listen():
                try:
                    if event.message_id is None:
                        continue

                    msg = vk.messages.getById(message_ids=event.message_id)["items"][0]
                    peer_id = msg.get("peer_id") or (
                        2_000_000_000 + event.chat_id if event.from_chat else event.user_id
                    )

                    if peer_id not in ALLOWED_PEER_IDS:
                        logger.debug(f"peer_id={peer_id} не в разрешённых")
                        continue

                    sender = vk.users.get(user_ids=event.user_id)[0]
                    sender_name = f"{sender['first_name']} {sender['last_name']}"
                    attachments = msg.get("attachments", [])
                    fwd = msg.get("fwd_messages", [])
                    parts = []

                    if event.from_chat and not event.from_me:
                        chat_title = vk_session.method("messages.getChat", {"chat_id": event.chat_id})["title"]
                        parts.append(f"*{chat_title}*")
                        if "reply_message" in msg:
                            parts.append(get_reply_text(msg))
                        if fwd:
                            parts.append(format_fwd_messages(fwd))
                        if event.message:
                            parts.append(f"*{sender_name}*: {event.message}")
                        broadcast("\n".join(p for p in parts if p), parse_mode="Markdown")
                        if attachments:
                            handle_attachments(attachments, caption=f"[{chat_title}] {sender_name}")
                        logger.info(f"Беседа '{chat_title}' → TG")

                    elif not event.from_me and event.from_user:
                        if "reply_message" in msg:
                            parts.append(get_reply_text(msg))
                        if fwd:
                            parts.append(format_fwd_messages(fwd))
                        if event.message:
                            parts.append(f"*{sender_name}*: {event.message}")
                        if parts:
                            broadcast("\n".join(parts), parse_mode="Markdown")
                        if attachments:
                            handle_attachments(attachments, caption=sender_name)
                        logger.info(f"Личка {sender_name} → TG")

                except Exception as e:
                    logger.error(f"Ошибка обработки события: {e}")

        except Exception as e:
            logger.error(f"Longpoll упал, перезапуск через 5с: {e}")
            time.sleep(5)


def run_bot():
    Thread(target=vk_work, daemon=True).start()
    while True:
        try:
            tg.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            logger.error(f"Polling упал, перезапуск через 5с: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()