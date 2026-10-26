"""
Asura Scans -> Telegram: уведомления о новых главах.

Скрипт рассчитан на запуск "один проход" (например, по расписанию в GitHub Actions):
  1. забирает накопившиеся команды из Telegram (/add, /remove, /list, /check ...)
  2. проверяет все отслеживаемые тайтлы на новые главы
  3. сохраняет состояние в state.json

Переменные окружения:
  TELEGRAM_TOKEN   — токен бота от @BotFather (обязательно)
  TELEGRAM_CHAT_ID — твой chat id; бот слушается только его.
                     Если не задан — бот на /start просто ответит, какой у тебя id.

Локально можно гонять в цикле:  python bot.py --loop
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://asurascans.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# После стольких неудачных проверок подряд бот один раз предупредит (при запуске раз в 15 мин ≈ 3 часа)
FAIL_ALERT_THRESHOLD = 12

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ---------------------------------------------------------------- HTTP / Telegram

def http_get(url, timeout=30, attempts=3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.geturl(), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if i == attempts - 1:
                raise
        time.sleep(3 * (i + 1))


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[tg] {method} failed: {e.code} {e.read()[:300]!r}")
        return {"ok": False}


def send(chat_id, text, buttons=None):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **params)


# ---------------------------------------------------------------- State

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("offset", 0)
    state.setdefault("series", {})   # base_slug -> {title, last_chapter, url, fails}
    state.setdefault("pending", {})  # short id -> base_slug (для кнопок из поиска)
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def short_id(slug):
    return hashlib.sha1(slug.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- Asura Scans

def base_slug(slug):
    """Asura добавляет к slug хэш (-3ebcc5a9), который периодически меняется.
    Храним slug без него — сайт сам редиректит на актуальный адрес."""
    return re.sub(r"-[0-9a-f]{8}$", "", slug)


def slug_from_text(text):
    m = re.search(r"/(?:comics|series)/([a-z0-9-]+)", text)
    return base_slug(m.group(1)) if m else None


def fmt_ch(num):
    return str(int(num)) if num == int(num) else str(num)


def fetch_series(slug):
    """Возвращает (title, url, [номера глав по возрастанию])."""
    final_url, page = http_get(f"{BASE_URL}/comics/{slug}")
    real_slug = urllib.parse.urlparse(final_url).path.rstrip("/").split("/")[-1]
    if base_slug(real_slug) != slug:
        raise RuntimeError(f"редирект на неожиданную страницу: {final_url}")

    m = re.search(r'property="og:title" content="([^"]+)"', page) or re.search(r"<title>([^<]+)</title>", page)
    title = html.unescape(m.group(1)).replace(" | Asura Scans", "").strip() if m else slug

    nums = {
        float(n)
        for n in re.findall(rf"/comics/{re.escape(real_slug)}/chapter/([0-9]+(?:\.[0-9]+)?)", page)
    }
    if not nums:
        raise RuntimeError("на странице не нашлось ни одной главы (сменилась вёрстка?)")
    return title, f"{BASE_URL}/comics/{real_slug}", sorted(nums)


def search_series(query):
    _, page = http_get(f"{BASE_URL}/browse?search={urllib.parse.quote(query)}")
    seen, out = set(), []
    for slug in re.findall(r'href="/comics/([a-z0-9-]+)"', page):
        b = base_slug(slug)
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def pretty(slug):
    return slug.replace("-", " ").title()


# ---------------------------------------------------------------- Commands

HELP = (
    "📚 <b>Бот уведомлений Asura Scans</b>\n\n"
    "/add &lt;ссылка или название&gt; — отслеживать тайтл\n"
    "/remove — перестать отслеживать\n"
    "/list — что отслеживается\n"
    "/check — проверить новые главы прямо сейчас\n"
    "/help — эта справка\n\n"
    "Проверка идёт автоматически по расписанию."
)


def add_series(state, chat, slug):
    if slug in state["series"]:
        s = state["series"][slug]
        send(chat, f"Уже отслеживается: <b>{html.escape(s['title'])}</b> (гл. {fmt_ch(s['last_chapter'])})")
        return
    try:
        title, url, chapters = fetch_series(slug)
    except Exception as e:
        send(chat, f"❌ Не смог открыть тайтл <code>{html.escape(slug)}</code>: {html.escape(str(e))}")
        return
    state["series"][slug] = {"title": title, "url": url, "last_chapter": chapters[-1], "fails": 0}
    send(
        chat,
        f"✅ Добавлено: <a href=\"{url}\">{html.escape(title)}</a>\n"
        f"Последняя глава сейчас: <b>{fmt_ch(chapters[-1])}</b>. Пришлю, когда выйдет новая.",
    )


def cmd_add(state, chat, arg):
    if not arg:
        send(chat, "Пришли ссылку или название: <code>/add absolute regression</code>")
        return
    slug = slug_from_text(arg)
    if slug:
        add_series(state, chat, slug)
        return
    try:
        results = search_series(arg)
    except Exception as e:
        send(chat, f"❌ Поиск не сработал: {html.escape(str(e))}")
        return
    if not results:
        send(chat, "Ничего не нашёл 🤷 Попробуй другое название или пришли ссылку.")
        return
    # точное совпадение или единственный результат — добавляем сразу
    wanted = re.sub(r"[^a-z0-9]+", "-", arg.lower()).strip("-")
    if wanted in results or len(results) == 1:
        add_series(state, chat, wanted if wanted in results else results[0])
        return
    buttons = []
    for slug in results[:10]:
        sid = short_id(slug)
        state["pending"][sid] = slug
        buttons.append([{"text": pretty(slug)[:60], "callback_data": f"add:{sid}"}])
    send(chat, "Нашёл несколько, выбери:", buttons)


def cmd_remove(state, chat, arg):
    if not state["series"]:
        send(chat, "Список пуст.")
        return
    slug = slug_from_text(arg) if arg else None
    if slug and slug in state["series"]:
        s = state["series"].pop(slug)
        send(chat, f"🗑 Удалено: {html.escape(s['title'])}")
        return
    buttons = [
        [{"text": s["title"][:60], "callback_data": f"rm:{short_id(slug)}"}]
        for slug, s in sorted(state["series"].items(), key=lambda kv: kv[1]["title"])
    ]
    send(chat, "Что убрать?", buttons)


def cmd_list(state, chat):
    if not state["series"]:
        send(chat, "Пока ничего не отслеживается. Добавь: /add &lt;название&gt;")
        return
    lines = [
        f"• <a href=\"{s['url']}\">{html.escape(s['title'])}</a> — гл. {fmt_ch(s['last_chapter'])}"
        for s in sorted(state["series"].values(), key=lambda s: s["title"])
    ]
    send(chat, "📖 <b>Отслеживаю:</b>\n" + "\n".join(lines))


def handle_callback(state, cq):
    chat = str(cq["message"]["chat"]["id"])
    data = cq.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if chat != CHAT_ID:
        return
    kind, _, sid = data.partition(":")
    if kind == "add":
        slug = state["pending"].pop(sid, None)
        if slug:
            add_series(state, chat, slug)
        else:
            send(chat, "Кнопка устарела, повтори поиск.")
    elif kind == "rm":
        for slug in list(state["series"]):
            if short_id(slug) == sid:
                s = state["series"].pop(slug)
                send(chat, f"🗑 Удалено: {html.escape(s['title'])}")
                return
        send(chat, "Уже удалено.")


def process_updates(state):
    """Возвращает True, если пользователь попросил /check."""
    want_check = False
    resp = tg("getUpdates", offset=state["offset"], timeout=0,
              allowed_updates=["message", "callback_query"])
    for upd in resp.get("result", []):
        state["offset"] = upd["update_id"] + 1
        try:
            if "callback_query" in upd:
                handle_callback(state, upd["callback_query"])
                continue
            msg = upd.get("message")
            if not msg or "text" not in msg:
                continue
            chat = str(msg["chat"]["id"])
            text = msg["text"].strip()
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()

            if chat != CHAT_ID:
                if cmd == "/start":
                    send(chat, f"Твой chat id: <code>{chat}</code>\n"
                               "Добавь его в секрет <b>TELEGRAM_CHAT_ID</b>, и бот начнёт тебя слушаться.")
                continue

            if cmd in ("/start", "/help"):
                send(chat, HELP)
            elif cmd == "/add":
                cmd_add(state, chat, arg)
            elif cmd in ("/remove", "/rm", "/del"):
                cmd_remove(state, chat, arg)
            elif cmd == "/list":
                cmd_list(state, chat)
            elif cmd == "/check":
                want_check = True
            elif slug_from_text(text):  # просто кинули ссылку — добавим
                add_series(state, chat, slug_from_text(text))
            else:
                send(chat, HELP)
        except Exception as e:
            print(f"[update {upd.get('update_id')}] error: {e}")
    return want_check


# ---------------------------------------------------------------- Checking

def check_all(state, verbose=False):
    found_any = False
    for slug, s in list(state["series"].items()):
        try:
            title, url, chapters = fetch_series(slug)
        except Exception as e:
            s["fails"] = s.get("fails", 0) + 1
            print(f"[check] {slug}: {e} (fails={s['fails']})")
            if s["fails"] == FAIL_ALERT_THRESHOLD and CHAT_ID:
                send(CHAT_ID, f"⚠️ Уже давно не получается проверить <b>{html.escape(s['title'])}</b>:\n"
                              f"<code>{html.escape(str(e))[:300]}</code>")
            continue
        s.update(title=title, url=url, fails=0)
        new = [c for c in chapters if c > s["last_chapter"]]
        if not new:
            continue
        found_any = True
        s["last_chapter"] = new[-1]
        shown = new[-10:]
        links = "\n".join(f"<a href=\"{url}/chapter/{fmt_ch(c)}\">Глава {fmt_ch(c)}</a>" for c in shown)
        extra = f"\n…и ещё {len(new) - len(shown)}" if len(new) > len(shown) else ""
        word = "Новая глава" if len(new) == 1 else f"Новые главы ({len(new)})"
        print(f"[check] {slug}: new {[fmt_ch(c) for c in new]}")
        send(CHAT_ID, f"🔥 <b>{html.escape(title)}</b>\n{word}:\n{links}{extra}")
    if verbose and not found_any:
        send(CHAT_ID, "Новых глав пока нет 😴")


# ---------------------------------------------------------------- Main

def run_once():
    state = load_state()
    want_check = process_updates(state)
    if CHAT_ID:
        check_all(state, verbose=want_check)
    else:
        print("TELEGRAM_CHAT_ID не задан: напиши боту /start, чтобы узнать свой id.")
    save_state(state)


def main():
    if not TOKEN:
        sys.exit("Нужна переменная окружения TELEGRAM_TOKEN")
    if "--loop" in sys.argv:
        interval = 15 * 60
        last_check = 0.0
        while True:
            state = load_state()
            want_check = process_updates(state)
            if CHAT_ID and (want_check or time.time() - last_check > interval):
                check_all(state, verbose=want_check)
                last_check = time.time()
            save_state(state)
            time.sleep(5)
    else:
        run_once()


if __name__ == "__main__":
    main()
