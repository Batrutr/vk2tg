import os
import time
import json
import tempfile
import requests
import sys
import random
import html
from functools import wraps
from threading import Thread, Lock
from collections import OrderedDict

import vk_api
import telebot
from telebot import types
from vk_api.longpoll import VkLongPoll, VkEventType
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

# ─── VK API safety helpers ───────────────────────────────────────────────────

_api_lock = Lock()
_last_api_call = 0.0
API_INTERVAL = float(os.getenv("VK_API_INTERVAL", "0.34"))
VK_PAUSE_6 = float(os.getenv("VK_PAUSE_6", "2"))
VK_PAUSE_983 = float(os.getenv("VK_PAUSE_983", "30"))
VK_PAUSE_984 = float(os.getenv("VK_PAUSE_984", "180"))

_vk_pause_until = 0.0

_user_name_cache: dict[int, str] = {}
_chat_title_cache: dict[int, str] = {}


def _extract_vk_error_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code

    err = getattr(exc, "error", None)
    if isinstance(err, dict):
        code = err.get("error_code")
        if isinstance(code, int):
            return code

    args = getattr(exc, "args", ())
    if args:
        first = args[0]
        if isinstance(first, int):
            return first
        if isinstance(first, str):
            for part in first.replace(":", " ").split():
                if part.isdigit():
                    return int(part)
    return None


def _register_vk_backoff(error_code: int, attempt: int, base_backoff: float) -> float:
    global _vk_pause_until

    if error_code == 984:
        pause = VK_PAUSE_984 + 30.0 * attempt
    elif error_code == 983:
        pause = VK_PAUSE_983 + 10.0 * attempt
    elif error_code == 6:
        pause = VK_PAUSE_6 + 1.5 * attempt
    else:
        pause = base_backoff * max(1, attempt)

    _vk_pause_until = max(_vk_pause_until, time.time() + pause)
    return pause


def vk_call(method, *args, retries: int = 0, backoff: float = 0.7, **kwargs):
    """Serialize VK API calls with interval and optional retries for transient errors."""
    global _last_api_call
    attempt = 0
    while True:
        try:
            with _api_lock:
                pause_wait = _vk_pause_until - time.time()
                if pause_wait > 0:
                    time.sleep(pause_wait)
                now = time.time()
                wait = API_INTERVAL - (now - _last_api_call)
                if wait > 0:
                    time.sleep(wait)
                result = method(*args, **kwargs)
                _last_api_call = time.time()
                return result
        except Exception as e:
            code = _extract_vk_error_code(e)
            if code in {6, 983, 984}:
                pause = _register_vk_backoff(code, attempt + 1, backoff)
                logger.warning(f"VK API code={code}: pause {pause:.1f}s before next calls")
            if attempt >= retries:
                raise
            attempt += 1
            if code not in {6, 983, 984}:
                time.sleep(backoff * attempt)


def next_random_id() -> int:
    return random.randint(1, 2_147_483_647)


def _detect_self_id() -> int:
    """Own VK user id of the token owner (used to ignore edits of our own messages)."""
    try:
        return int(vk_call(vk.users.get, retries=1)[0]["id"])
    except Exception as e:
        logger.warning(f"Не удалось определить собственный VK id: {e}")
        return 0


VK_SELF_ID = _detect_self_id()


def get_user_name(user_id: int) -> str:
    name = _user_name_cache.get(user_id)
    if name:
        return name
    try:
        s = vk_call(vk.users.get, user_ids=user_id, retries=1)[0]
        name = f"{s['first_name']} {s['last_name']}"
    except Exception:
        name = f"ID{user_id}"
    _user_name_cache[user_id] = name
    return name


def get_chat_title(chat_id: int) -> str:
    title = _chat_title_cache.get(chat_id)
    if title:
        return title
    try:
        title = vk_call(vk_session.method, "messages.getChat", {"chat_id": chat_id}, retries=1)["title"]
    except Exception:
        title = f"Беседа {chat_id}"
    _chat_title_cache[chat_id] = title
    return title


def should_fetch_full_message(event) -> bool:
    attachments = getattr(event, "attachments", None)
    if attachments:
        return True
    extra_values = getattr(event, "extra_values", {}) or {}
    return any(k in extra_values for k in ("fwd", "reply", "reply_message_id"))

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

def _without_parse_mode(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k != "parse_mode"}

def broadcast(text: str, **kwargs):
    for tid in get_tg_ids():
        try:
            tg.send_message(tid, text, **kwargs)
        except Exception as e:
            if kwargs.get("parse_mode"):
                # Most likely a Markdown parse error — resend as plain text
                # so the forwarded message is not lost.
                try:
                    tg.send_message(tid, text, **_without_parse_mode(kwargs))
                    continue
                except Exception as e2:
                    e = e2
            logger.error(f"broadcast → {tid}: {e}")

def broadcast_media(method: str, *args, **kwargs):
    fn = getattr(tg, method)
    for tid in get_tg_ids():
        try:
            fn(tid, *args, **kwargs)
        except Exception as e:
            if kwargs.get("parse_mode"):
                try:
                    fn(tid, *args, **_without_parse_mode(kwargs))
                    continue
                except Exception as e2:
                    e = e2
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
        doc = vk_call(upload.document_message, path, peer_id=peer_id, title=title, retries=1)
        att = f"doc{doc['owner_id']}_{doc['id']}"
        vk_call(vk.messages.send, **send_kwargs, random_id=next_random_id(), message=caption, attachment=att, retries=2)
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
        vk_call(vk.messages.send, **send_kwargs, random_id=next_random_id(), message=message.text, retries=2)
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
        photos = vk_call(upload.photo_messages, path, retries=1)
        att = f"photo{photos[0]['owner_id']}_{photos[0]['id']}"
        vk_call(vk.messages.send, **send_kwargs, random_id=next_random_id(), message=message.caption or "", attachment=att, retries=2)
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
        doc = vk_call(upload.document_message, ogg, peer_id=peer_id, title="voice.ogg", retries=1)
        att = f"doc{doc['owner_id']}_{doc['id']}"
        vk_call(vk.messages.send, **send_kwargs, random_id=next_random_id(), attachment=att, retries=2)
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

def esc(text: str) -> str:
    """Escape text for Telegram HTML parse mode (& < >)."""
    return html.escape(text or "", quote=False)


def esc_attr(url: str) -> str:
    """Escape a URL for use inside an HTML attribute (href)."""
    return html.escape(url or "", quote=True)


def fwd_quote_lines(fwd_messages: list, depth: int = 0) -> list[str]:
    """Forwarded VK messages as HTML lines meant to live inside a blockquote."""
    lines = []
    indent = "  " * depth
    for msg in fwd_messages:
        from_id = int(msg.get("from_id", 0) or 0)
        name = esc(get_user_name(from_id) if from_id else f"ID{msg.get('from_id', '?')}")
        text = esc(msg.get("text", ""))
        lines.append(f"{indent}↪ <b>{name}</b>: {text}" if text else f"{indent}↪ <b>{name}</b>")
        if msg.get("fwd_messages"):
            lines.extend(fwd_quote_lines(msg["fwd_messages"], depth + 1))
    return lines


def reply_quote_line(reply: dict) -> str:
    """Replied-to VK message as a single HTML line for a blockquote."""
    try:
        name = esc(get_user_name(int(reply.get("from_id", 0) or 0)))
        text = esc(reply.get("text", ""))
        return f"↩ <b>{name}</b>: {text}" if text else f"↩ <b>{name}</b>"
    except Exception as e:
        logger.error(f"reply_quote_line: {e}")
        return "↩ <i>ошибка получения ответа</i>"


# Bodies already forwarded to TG, keyed by VK message_id. Right after a link or
# video is posted, VK attaches a preview and fires MESSAGE_EDIT — an "edit" that
# changes nothing visible. We skip it when the rendered body matches what we
# already sent. Only touched from the single longpoll thread, so no lock needed.
_forwarded_bodies: OrderedDict[int, str] = OrderedDict()
_FORWARDED_CACHE_MAX = 2000


def remember_forwarded(message_id: int, body: str) -> None:
    _forwarded_bodies[message_id] = body
    _forwarded_bodies.move_to_end(message_id)
    while len(_forwarded_bodies) > _FORWARDED_CACHE_MAX:
        _forwarded_bodies.popitem(last=False)


def body_already_forwarded(message_id: int, body: str) -> bool:
    return _forwarded_bodies.get(message_id) == body


def build_message_parts(msg_text: str, sender_name: str, attachments: list,
                        fwd: list, reply: dict | None,
                        chat_title: str | None = None) -> tuple[list[str], list]:
    """Render a VK message into TG (HTML) blocks plus leftover media attachments.

    Blocks are joined with a blank line between them for readability;
    `media_atts` are photos/voices/docs/stickers uploaded separately.
    """
    blocks: list[str] = []
    if chat_title:
        blocks.append(f"<b>{esc(chat_title)}</b>")

    # Reply and forwarded messages share one Telegram blockquote.
    quote_lines: list[str] = []
    if reply:
        quote_lines.append(reply_quote_line(reply))
    if fwd:
        quote_lines.extend(fwd_quote_lines(fwd))
    if quote_lines:
        quoted = "\n".join(quote_lines)
        tag = "<blockquote expandable>" if len(quoted) > 300 else "<blockquote>"
        blocks.append(f"{tag}{quoted}</blockquote>")

    # Sender line + any link/video/audio note lines form the body block.
    body_lines: list[str] = [f"<b>{esc(sender_name)}</b>" + (f": {esc(msg_text)}" if msg_text else "")]

    media_atts = []
    for att in attachments:
        att_type = att.get("type")
        if att_type == "link":
            lnk = att.get("link", {})
            url = lnk.get("url", "")
            # The bare URL is already in msg_text; only add a separate line when
            # the link carries something extra (a different url or a title).
            if url and url not in msg_text:
                title = lnk.get("title", "")
                body_lines.append(
                    f'🔗 <a href="{esc_attr(url)}">{esc(title)}</a>' if title else f"🔗 {esc(url)}"
                )
        elif att_type == "video":
            v = att["video"]
            url = f"https://vk.com/video{v['owner_id']}_{v['id']}"
            title = v.get("title", "Видео")
            body_lines.append(f'🎬 <a href="{esc_attr(url)}">{esc(title)}</a>')
        elif att_type == "audio":
            a = att["audio"]
            body_lines.append(f"🎵 <b>{esc(a.get('artist', '?'))} — {esc(a.get('title', '?'))}</b>")
        else:
            media_atts.append(att)
    blocks.append("\n".join(body_lines))
    return blocks, media_atts


def handle_attachments(attachments: list, caption: str = "", parse_mode: str | None = None):
    if caption and len(caption) > 1024:
        broadcast(caption, parse_mode=parse_mode)
        caption = ""
        parse_mode = None
    header_used = not caption

    def flush_header():
        nonlocal header_used
        if not header_used:
            broadcast(caption, parse_mode=parse_mode)
            header_used = True

    for att in attachments:
        att_type = att.get("type")
        try:
            if att_type == "photo":
                url = sorted(att["photo"]["sizes"], key=lambda x: x.get("width", 0))[-1]["url"]
                kw = {}
                if not header_used:
                    kw["caption"] = caption
                    if parse_mode:
                        kw["parse_mode"] = parse_mode
                    header_used = True
                broadcast_media("send_photo", requests.get(url, timeout=30).content, **kw)

            elif att_type == "audio_message":
                flush_header()
                url = att["audio_message"].get("link_ogg") or att["audio_message"].get("link_mp3")
                if url:
                    broadcast_media("send_voice", requests.get(url, timeout=30).content)

            elif att_type == "doc":
                preview = att["doc"].get("preview", {})
                if "audio_msg" in preview:
                    flush_header()
                    audio = preview["audio_msg"]
                    url = audio.get("link_ogg") or audio.get("link_mp3")
                    if url:
                        broadcast_media("send_voice", requests.get(url, timeout=30).content)
                else:
                    url = att["doc"].get("url")
                    if url:
                        kw = {"visible_file_name": att["doc"].get("title", "file")}
                        if not header_used:
                            kw["caption"] = caption
                            if parse_mode:
                                kw["parse_mode"] = parse_mode
                            header_used = True
                        broadcast_media("send_document", requests.get(url, timeout=30).content, **kw)

            elif att_type == "sticker":
                flush_header()
                imgs = att["sticker"].get("images_with_background") or att["sticker"].get("images", [])
                if imgs:
                    broadcast_media("send_photo", requests.get(imgs[-1]["url"], timeout=30).content)

        except Exception as e:
            logger.error(f"handle_attachments({att_type}): {e}")

    flush_header()


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

                    is_edit = event.type == VkEventType.MESSAGE_EDIT
                    attachments = []
                    fwd = []
                    reply = None
                    msg = None

                    if is_edit or should_fetch_full_message(event):
                        try:
                            msg = vk_call(vk.messages.getById, message_ids=event.message_id)["items"][0]
                            attachments = msg.get("attachments", [])
                            fwd = msg.get("fwd_messages", [])
                            reply = msg.get("reply_message")
                        except Exception as e:
                            logger.error(f"getById failed: {e}")

                    peer_id = (msg or {}).get("peer_id") or (
                        CHAT_PEER_OFFSET + event.chat_id if event.from_chat else event.user_id
                    )

                    if peer_id not in ALLOWED_PEER_IDS:
                        logger.debug(f"peer_id={peer_id} не в разрешённых")
                        continue

                    # vk_api sets from_me only on MESSAGE_NEW, never on edits, so
                    # also match our own id to keep our own edited messages out.
                    is_from_me = event.from_me or (
                        msg is not None and int(msg.get("from_id", 0) or 0) == VK_SELF_ID
                    )
                    if is_from_me or not (event.from_chat or event.from_user):
                        continue

                    sender_id = (msg or {}).get("from_id") or event.user_id
                    sender_name = get_user_name(sender_id)
                    msg_text = (msg.get("text") if msg else None) or event.message or ""

                    chat_title = get_chat_title(event.chat_id) if event.from_chat else None
                    blocks, media_atts = build_message_parts(
                        msg_text, sender_name, attachments, fwd, reply, chat_title
                    )
                    body = "\n\n".join(b for b in blocks if b)

                    # VK attaches link/video previews via MESSAGE_EDIT; that edit
                    # leaves the rendered body unchanged, so drop the duplicate.
                    if is_edit and body_already_forwarded(event.message_id, body):
                        logger.debug(f"MESSAGE_EDIT без изменений (превью VK), id={event.message_id}")
                        continue
                    remember_forwarded(event.message_id, body)

                    text = body
                    if is_edit:
                        text += "\n\n<i>✏️ изменено</i>"
                    if media_atts:
                        handle_attachments(media_atts, caption=text, parse_mode="HTML")
                    else:
                        broadcast(text, parse_mode="HTML")
                    logger.info(
                        f"Беседа '{chat_title}' → TG" if chat_title else f"Личка {sender_name} → TG"
                    )

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