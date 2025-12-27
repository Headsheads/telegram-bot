import os
import asyncio
from aiohttp import web

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

import random
import time
import re
import urllib.parse
import aiosqlite
import aiohttp
import html
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
async def _run_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()

    async def ping(request):
        return web.Response(text="ok")

    app.router.add_get("/", ping)
    app.router.add_get("/health", ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] listening on 0.0.0.0:{port}", flush=True)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "words.db")


bot = Bot(TOKEN)
dp = Dispatcher()
class ImportState(StatesGroup):
    waiting_lines = State()
    choosing_fix = State()   # ✅ выбираем правильный вариант слова
class QuizState(StatesGroup):
    waiting_text_answer = State()
    choosing_mc = State()


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS words(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL,
    translation TEXT NOT NULL,
    transcription TEXT
);

CREATE TABLE IF NOT EXISTS stats(
    user_id INTEGER NOT NULL,
    word_id INTEGER NOT NULL,

    fwd_correct INTEGER NOT NULL DEFAULT 0,
    fwd_wrong   INTEGER NOT NULL DEFAULT 0,

    rev_correct INTEGER NOT NULL DEFAULT 0,
    rev_wrong   INTEGER NOT NULL DEFAULT 0,

    last_ts INTEGER,
    PRIMARY KEY(user_id, word_id)
);

CREATE TABLE IF NOT EXISTS progress(
    user_id INTEGER NOT NULL,
    word_id INTEGER NOT NULL,
    f_streak INTEGER NOT NULL DEFAULT 0,
    r_streak INTEGER NOT NULL DEFAULT 0,
    learned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, word_id)
);

CREATE TABLE IF NOT EXISTS active_pool(
    user_id INTEGER NOT NULL,
    word_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, word_id)
);
CREATE TABLE IF NOT EXISTS tts_msgs(
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    PRIMARY KEY(chat_id, msg_id)
);

"""
POOL_SIZE = 20


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(CREATE_SQL)

        # --- миграция: добавить transcription, если колонка отсутствует
        cur = await db.execute("PRAGMA table_info(words)")
        cols = [r[1] for r in await cur.fetchall()]  # r[1] = name
        await cur.close()

        if "transcription" not in cols:
            await db.execute("ALTER TABLE words ADD COLUMN transcription TEXT")
        # --- конец миграции
        # 1) удаляем дубликаты по lower(word), оставляем запись с минимальным id
        await db.execute("""
        DELETE FROM words
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM words
            GROUP BY lower(word)
        )
        """)

        # 2) теперь можно безопасно создать уникальный индекс
        await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_words_word_lower
        ON words (lower(word));
        """)

        await db.commit()


async def add_word(word: str, translation: str, transcription: str | None = None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO words(word, transcription, translation) VALUES (?, ?, ?)",
            (word, transcription, translation),
        )
        await db.commit()



async def fetchone(db, sql, params=()):
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchall(db, sql, params=()):
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

async def cleanup_tts(chat_id: int):
    """Удаляет все ранее отправленные ботом TTS-сообщения в этом чате (если они записаны)."""
    async with aiosqlite.connect(DB) as db:
        rows = await fetchall(db, "SELECT msg_id FROM tts_msgs WHERE chat_id=?", (chat_id,))
        for (mid,) in rows:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

        await db.execute("DELETE FROM tts_msgs WHERE chat_id=?", (chat_id,))
        await db.commit()


async def get_pool(user_id: int):
    """
    Возвращает слова, которые ещё НЕ выучены (progress.learned=0).
    Серии берём из progress (f_streak/r_streak).
    """
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """
            SELECT
                w.id,
                w.word,
                w.transcription,
                w.translation,
                COALESCE(p.f_streak, 0) AS f_streak,
                COALESCE(p.r_streak, 0) AS r_streak
            FROM words w
            LEFT JOIN progress p
                ON p.user_id = ? AND p.word_id = w.id
            WHERE COALESCE(p.learned, 0) = 0
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
    return rows  # (wid, word, trc, tr, f_streak, r_streak)



async def count_words():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM words")
        row = await cur.fetchone()
        await cur.close()
    return row[0] if row else 0

async def get_question_reverse(user_id: int):
    rows = await get_pool(user_id)  # wid, word, trc, tr, f_streak, r_streak
    if len(rows) < 4:
        return None

    wid, word, trc, tr, f_streak, r_streak = random.choice(rows)

    true_answer = word
    other_words = [w for (i, w, trc2, t, _, _) in rows if i != wid]
    opts = random.sample(other_words, 3) + [true_answer]
    random.shuffle(opts)

    correct_idx = opts.index(true_answer)
    prompt = tr  # RU

    show_word = fmt_en(word, trc)   # EN ['...']
    show_tr = tr

    return wid, prompt, true_answer, opts, correct_idx, show_word, show_tr


async def get_question(user_id: int):
    rows = await get_pool(user_id)  # wid, word, trc, tr, f_streak, r_streak
    if len(rows) < 4:
        return None

    weighted = []
    for wid, w, trc, t, f_streak, r_streak in rows:
        need = (3 - f_streak) + (3 - r_streak)
        score = max(1, need)
        weighted.extend([(wid, w, trc, t)] * min(score, 10))

    wid, word, trc, true_tr = random.choice(weighted)

    translations = [x[3] for x in rows if x[0] != wid]  # x[3]=translation
    options = random.sample(translations, 3) + [true_tr]
    random.shuffle(options)

    correct_idx = options.index(true_tr)
    return wid, word, trc, true_tr, options, correct_idx



def parse_pairs(text: str):
    """Импорт строк (по одной на строке).

    Поддерживаем варианты:
    - apple [ˈæpəl] яблоко
    - apple 'ˈæpəl' яблоко
    - apple "ˈæpəl" яблоко
    - apple яблоко        (без транскрипции)

    Возвращает список (word, transcription_or_None, translation)
    """
    out = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 1:
            continue

        word = parts[0]
        transcription = None
        translation = None

        if len(parts) >= 3:
            t2 = parts[1].strip()
            if (t2.startswith("[") and t2.endswith("]")) or \
                    (t2.startswith("'") and t2.endswith("'")) or \
                    (t2.startswith('"') and t2.endswith('"')):
                s = t2.strip().strip("'").strip('"').strip()
                transcription = f"[{s}]" if s else None
                translation = " ".join(parts[2:]).strip()
            else:
                translation = " ".join(parts[1:]).strip()
        elif len(parts) == 2:
            translation = parts[1].strip()
        else:
            # ✅ только английское слово — перевод и транскрипцию доберём по API
            translation = None

        if word:
            out.append((word, transcription, translation))

    return out




import re  # убедись, что есть один раз сверху файла

def fmt_en(word, trc):
    if not trc:
        return word

    s = str(trc).strip()

    # убираем внешние кавычки
    s = s.strip("'").strip('"')

    # если вдруг пришло [..] — убираем скобки
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()

    # на всякий: ещё раз уберём кавычки после снятия []
    s = s.strip("'").strip('"')

    return f"{word} ['{s}']" if s else word
# --- AUTO LOOKUP (translation RU + IPA) ---
async def is_valid_english_word(word: str, *, allow_on_error: bool = True) -> bool:
    """
    Проверяем слово через dictionaryapi.dev:
    200 -> слово найдено
    если ошибка сети:
      - allow_on_error=True  -> считаем валидным (чтобы не ломать импорт)
      - allow_on_error=False -> считаем НЕвалидным (для фильтра подсказок)
    """
    w = (word or "").strip().lower()
    if not w:
        return False

    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(w)}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                return r.status == 200
    except Exception:
        return allow_on_error



async def suggest_words(word: str, limit: int = 6) -> list[str]:
    """
    Подсказки правильных слов:
    1) берём кандидатов из Datamuse
    2) проверяем каждый через dictionaryapi.dev
    3) оставляем ТОЛЬКО реальные слова
    """
    w = (word or "").strip().lower()
    if not w:
        return []

    url = "https://api.datamuse.com/sug"
    params = {"s": w, "max": "10"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return []
                data = await r.json()

        candidates = []
        for item in (data or []):
            cand = (item.get("word") or "").strip().lower()
            if cand and cand.isalpha():
                candidates.append(cand)

        # удаляем исходное слово (armm → не показываем armm)
        candidates = [c for c in candidates if c != w]

        # проверяем кандидатов через словарь
        valid: list[str] = []
        for cand in candidates:
            if await is_valid_english_word(cand, allow_on_error=False):
                valid.append(cand)
            if len(valid) >= limit:
                break

        return valid
    except Exception:
        return []



def kb_import_fixes(cands: list[str]):
    kb = InlineKeyboardBuilder()
    for w in cands:
        kb.button(text=w, callback_data=f"impfix:pick:{w}")
    kb.button(text="⏭ Пропустить", callback_data="impfix:skip")
    kb.adjust(2)
    return kb.as_markup()


async def lookup_ipa_dictionaryapi(word: str) -> str | None:
    """
    Берём IPA из dictionaryapi.dev (обычно отдаёт фонетику/IPA).
    """
    word = (word or "").strip().lower()
    if not word:
        return None

    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        # data: list[entry], entry["phonetics"] -> [{"text": "/.../"}]
        if isinstance(data, list) and data:
            phonetics = data[0].get("phonetics") or []
            for ph in phonetics:
                t = (ph.get("text") or "").strip()
                if t:
                    # приводим к твоему формату [ipa]
                    t = t.strip()
                    t = t.strip("[]").strip("/")
                    return f"[{t}]"

    except Exception:
        return None

    return None


async def translate_ru_mymemory_variants(text: str, limit: int = 5) -> list[str]:
    """
    Варианты перевода через MyMemory (без ключа).
    Берём responseData + matches и собираем уникальные варианты.
    """
    text = (text or "").strip()
    if not text:
        return []

    url = "https://api.mymemory.translated.net/get"
    params = {"q": text, "langpair": "en|ru"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()

        variants: list[str] = []

        # основной перевод
        main_tr = (((data or {}).get("responseData") or {}).get("translatedText") or "").strip()
        if main_tr:
            variants.append(main_tr)

        # доп. варианты
        matches = (data or {}).get("matches") or []
        for m in matches:
            tr = (m.get("translation") or "").strip()
            if tr:
                variants.append(tr)

        # чистим: уникальные, без пустого
        uniq = []
        seen = set()
        for v in variants:
            v2 = v.strip()
            if not v2:
                continue
            if v2.lower() in seen:
                continue
            seen.add(v2.lower())
            uniq.append(v2)
            if len(uniq) >= limit:
                break
            # фильтруем мусор/капслок/слишком длинные технические фразы
            clean = []
            for v in uniq:
                v2 = v.strip()

                # убираем капслок-заголовки
                if len(v2) >= 6 and v2 == v2.upper():
                    continue

                # режем слишком длинные/технические формулировки
                if len(v2) > 40:
                    continue

                # убираем странные ключевые слова (можешь дополнять)
                bad = ["постановка на охрану", "конечность", "верхняя"]
                if any(b in v2.lower() for b in bad):
                    continue

                clean.append(v2)

            return clean[:limit]

        return uniq
    except Exception:
        return []



async def auto_en_info(word: str) -> tuple[list[str], str | None]:
    """
    Возвращает (translations_ru_variants, transcription_ipa)
    """
    word = (word or "").strip()
    ovr = override_translations(word)
    if ovr:
        ipa = await lookup_ipa_dictionaryapi(word)
        return (ovr, ipa)

    if not word:
        return ([], None)

    ipa_task = asyncio.create_task(lookup_ipa_dictionaryapi(word))
    tr_task = asyncio.create_task(translate_ru_mymemory_variants(word, limit=5))

    ipa = await ipa_task
    trs = await tr_task
    return (trs, ipa)

COMMON_OVERRIDES = {
    "arm": ["рука", "плечо", "вооружать"],
    "arms": ["руки", "оружие"],
    "army": ["армия"],
}

def override_translations(word: str) -> list[str] | None:
    w = (word or "").strip().lower()
    return COMMON_OVERRIDES.get(w)


async def send_pronunciation_google(chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return

    # 🔥 удаляем ВСЕ предыдущие TTS этого чата
    await cleanup_tts(chat_id)

    url = (
        "https://translate.google.com/translate_tts"
        f"?ie=UTF-8&client=tw-ob&tl=en&q={urllib.parse.quote(text)}"
    )

    msg = await bot.send_audio(chat_id, url, title=text)

    # сохраняем id нового TTS
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO tts_msgs(chat_id, msg_id) VALUES (?,?)",
            (chat_id, msg.message_id)
        )
        await db.commit()



async def _tts_should_send(state: FSMContext) -> bool:
    """
    Защита от двойной озвучки.
    Возвращает True только один раз на вопрос.
    """
    data = await state.get_data()
    if data.get("tts_sent"):
        return False
    await state.update_data(tts_sent=True)
    return True


def b(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"

def kb_choose_mc():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выбрать из вариантов", callback_data="mode:mc")
    return kb.as_markup()
def kb_clear_confirm():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data="clear:yes")
    kb.button(text="❌ Отмена", callback_data="clear:no")
    kb.adjust(1)
    return kb.as_markup()

def kb_dup_delete(word_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить слово", callback_data=f"dupdel:{word_id}")
    kb.button(text="✅ Оставить", callback_data="dupkeep")
    kb.adjust(1)
    return kb.as_markup()


def kb_answers_mc(options):
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(text=opt, callback_data=f"mcans:{i}")  # 👈 было ans:{i}
    kb.adjust(1)
    return kb.as_markup()


def kb_stats_commands():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧠 Начать тренировку", callback_data="stats:quiz")
    kb.button(text="➕ Добавить слово", callback_data="stats:add")
    kb.button(text="📥 Импорт слов", callback_data="stats:import")
    kb.button(text="♻️ Сбросить статистику", callback_data="stats:resetstats")
    kb.button(text="🗑 Очистить всё", callback_data="stats:clear")
    kb.adjust(1)
    return kb.as_markup()


async def ensure_pool(user_id: int):
    async with aiosqlite.connect(DB) as db:
        row = await fetchone(db, """
            SELECT COUNT(*)
            FROM active_pool ap
            JOIN progress p ON p.user_id=? AND p.word_id=ap.word_id
            WHERE ap.user_id=? AND p.learned=0
        """, (user_id, user_id))
        active_cnt = row[0] if row else 0

        need = max(0, POOL_SIZE - active_cnt)
        if need == 0:
            return

        rows = await fetchall(db, f"""
            SELECT w.id
            FROM words w
            LEFT JOIN progress p ON p.user_id=? AND p.word_id=w.id
            LEFT JOIN active_pool ap ON ap.user_id=? AND ap.word_id=w.id
            WHERE COALESCE(p.learned, 0)=0
              AND ap.word_id IS NULL
            ORDER BY w.id
            LIMIT {need}
        """, (user_id, user_id))

        for (wid,) in rows:
            await db.execute("""
                INSERT INTO progress(user_id, word_id, f_streak, r_streak, learned)
                VALUES(?,?,?,?,0)
                ON CONFLICT(user_id, word_id) DO NOTHING
            """, (user_id, wid, 0, 0))

            await db.execute(
                "INSERT OR IGNORE INTO active_pool(user_id, word_id) VALUES(?,?)",
                (user_id, wid)
            )

        await db.commit()


async def pick_question(user_id: int):
    """Берём случайное слово из активной ротации (POOL_SIZE), выбираем направление и варианты.

    Важно:
    - true_answer для RU→EN = ТОЛЬКО английское слово (без транскрипции)
    - en_plain всегда хранится отдельно и используется для озвучки
    """
    await ensure_pool(user_id)

    async with aiosqlite.connect(DB) as db:
        rows = await fetchall(db, """
            SELECT w.id, w.word, w.transcription, w.translation, p.f_streak, p.r_streak
            FROM active_pool ap
            JOIN words w ON w.id=ap.word_id
            JOIN progress p ON p.user_id=? AND p.word_id=w.id
            WHERE ap.user_id=? AND p.learned=0
        """, (user_id, user_id))

        if not rows:
            return None

        wid, word, trc, tr, f_streak, r_streak = random.choice(rows)

        if f_streak >= 3 and r_streak < 3:
            direction = "reverse"     # RU→EN
        elif r_streak >= 3 and f_streak < 3:
            direction = "forward"     # EN→RU
        else:
            direction = random.choice(["forward", "reverse"])

        en_show = fmt_en(word, trc)

        if direction == "forward":  # EN→RU
            prompt = en_show
            true_answer = tr
            wrong_pool = [x[3] for x in rows if x[0] != wid]  # русские переводы
        else:  # reverse, RU→EN
            prompt = tr
            true_answer = word  # ✅ только английское слово (без транскрипции)
            wrong_pool = [x[1] for x in rows if x[0] != wid]  # английские слова

        options = random.sample(wrong_pool, 3) + [true_answer]
        random.shuffle(options)

        return {
            "direction": direction,
            "word_id": wid,
            "prompt": prompt,
            "true_answer": true_answer,
            "options": options,
            "correct_idx": options.index(true_answer),
            "show_word": en_show,   # ✅ английское + транскрипция
            "show_tr": tr,
            "en_plain": word,       # ✅ чистое английское для озвучки
        }



async def apply_answer(user_id: int, word_id: int, direction: str, ok: bool):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT INTO progress(user_id, word_id, f_streak, r_streak, learned)
            VALUES(?,?,?,?,0)
            ON CONFLICT(user_id, word_id) DO NOTHING
        """, (user_id, word_id, 0, 0))

        if direction == "forward":  # EN→RU
            if ok:
                await db.execute("""
                    UPDATE progress
                    SET f_streak = MIN(f_streak + 1, 3)
                    WHERE user_id=? AND word_id=?
                """, (user_id, word_id))
            else:
                # ❗ошибка в прямом — сбрасываем только прямой
                await db.execute("""
                    UPDATE progress
                    SET f_streak = 0
                    WHERE user_id=? AND word_id=?
                """, (user_id, word_id))

        elif direction == "reverse":  # RU→EN
            if ok:
                await db.execute("""
                    UPDATE progress
                    SET r_streak = MIN(r_streak + 1, 3)
                    WHERE user_id=? AND word_id=?
                """, (user_id, word_id))
            else:
                # ❗ошибка в обратном — сбрасываем только обратный
                await db.execute("""
                    UPDATE progress
                    SET r_streak = 0
                    WHERE user_id=? AND word_id=?
                """, (user_id, word_id))
        row = await fetchone(
            db,
            "SELECT f_streak, r_streak FROM progress WHERE user_id=? AND word_id=?",
            (user_id, word_id)
        )
        f, r = row if row else (0, 0)

        if f >= 3 and r >= 3:
            await db.execute("UPDATE progress SET learned=1 WHERE user_id=? AND word_id=?", (user_id, word_id))
            await db.execute("DELETE FROM active_pool WHERE user_id=? AND word_id=?", (user_id, word_id))

        await db.commit()

async def get_progress(user_id: int, word_id: int):
    async with aiosqlite.connect(DB) as db:
        row = await fetchone(
            db,
            "SELECT f_streak, r_streak FROM progress WHERE user_id=? AND word_id=?",
            (user_id, word_id),
        )
    if not row:
        return (0, 0)
    return row[0], row[1]


@dp.callback_query(F.data == "mode:mc")
async def cb_choose_mc(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        await c.answer("Вопрос устарел — даю новый")
        await state.clear()
        await start_quiz(c.message.chat.id, c.from_user.id, state)
        return

    await state.set_state(QuizState.choosing_mc)

    f_streak, r_streak = await get_progress(c.from_user.id, data["word_id"])
    stats_line = f"\n\n📈 Серии: EN→RU {f_streak}/3 • RU→EN {r_streak}/3" if (f_streak + r_streak) > 0 else ""

    await c.message.edit_text(
        f"✅ Выбери правильный ответ:\n\n{b(data['prompt'])}{stats_line}",
        parse_mode="HTML",
        reply_markup=kb_answers_mc(data["options"]),
    )

    await c.answer()


@dp.callback_query(F.data.startswith("mcans:"))
async def cb_answer_choice(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        await c.answer("Вопрос устарел — даю новый")
        await state.clear()
        await start_quiz(c.message.chat.id, c.from_user.id, state)
        return
    # защита от двойного нажатия / повторного callback
    if data.get("answered"):
        await c.answer("Уже отвечено")
        return
    await state.update_data(answered=True)

    idx = int(c.data.split(":")[1])
    ok = idx == data["correct_idx"]
    await apply_answer(c.from_user.id, data["word_id"], data["direction"], ok)

    f_streak, r_streak = await get_progress(c.from_user.id, data["word_id"])
    stats_line = (
        f"\n📈 Серии: EN→RU {f_streak}/3 • RU→EN {r_streak}/3"
        if (f_streak + r_streak) > 0 else ""
    )




    await update_stat(
        c.from_user.id,
        data["word_id"],
        ok,
        data["direction"],
    )


    word = data["show_word"]
    tr = data["show_tr"]
    chosen = data["options"][idx]

    # английское слово без транскрипции (первый токен)
    en_plain = str(data.get("show_word", "")).split()[0]

    # отключим кнопки, чтобы не нажимали дважды и не было двойной озвучки
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if ok:
        await c.message.answer(
            f"✅ Верно!\n<b>{word}</b> = {tr}{stats_line}",
            parse_mode="HTML",
        )
    else:
        await c.message.answer(
            f"❌ Неверно.\n<b>{word}</b> = {tr}\nТвой ответ: {chosen}{stats_line}",
            parse_mode="HTML",
        )

    # озвучка ВСЕГДА правильного английского (и при верном, и при неверном)
    en_plain = str(data.get("en_plain", "")).strip().lower()
    if en_plain and await _tts_should_send(state):
        await send_pronunciation_google(c.message.chat.id, en_plain)

    # ❌ Не произносим при неверном ответе (и чтобы не было двойного воспроизведения)

    await c.answer()
    await state.clear()
    await start_quiz(c.message.chat.id, c.from_user.id, state)

@dp.message(Command("clear"))
async def cmd_clear(m: Message):
    await m.answer(
        "⚠️ Ты уверен, что хочешь удалить ВСЕ слова и статистику?\n\n"
        "Это действие нельзя отменить.",
        reply_markup=kb_clear_confirm()
    )
@dp.callback_query(F.data.startswith("clear:"))
async def cb_clear_confirm(c: CallbackQuery):
    action = c.data.split(":")[1]

    if action == "no":
        await c.message.edit_text("❌ Очистка отменена.")
        await c.answer()
        return

    if action == "yes":
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM words")
            await db.execute("DELETE FROM stats")
            await db.execute("DELETE FROM progress")
            await db.execute("DELETE FROM active_pool")
            await db.commit()

        await c.message.edit_text("🗑️ Словарь и статистика полностью очищены.")
        await c.answer()

@dp.callback_query(ImportState.choosing_fix, F.data.startswith("impfix:"))
async def cb_import_fix(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("import_fix_queue") or []
    if not q:
        await state.set_state(ImportState.waiting_lines)
        await c.answer("Очередь пустая")
        return

    cmd = c.data.split(":", 2)[1]

    if cmd == "skip":
        q = q[1:]
        await state.update_data(import_fix_queue=q)
        await c.answer("Пропущено")

    elif cmd == "pick":
        picked = c.data.split(":", 2)[2].strip().lower()
        await c.answer("Добавляю...")

        # берём варианты перевода + IPA (у тебя это делается через auto_en_info)
        translation = ", ".join(variants) if variants else ""


        if not translation:
            await c.message.answer(
                f"⏭ Не удалось найти перевод для <b>{html.escape(picked)}</b>, пропускаю.",
                parse_mode="HTML",
            )
        else:
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "INSERT INTO words(word, transcription, translation) VALUES (?, ?, ?)",
                    (picked, ipa, translation),
                )
                await db.commit()

            var_txt = "; ".join(variants[:5]) if variants else translation
            ipa_txt = ipa if ipa else "—"
            await c.message.answer(
                f"✅ Внесено в базу:\n• <b>{html.escape(picked)}</b> {html.escape(ipa_txt)} → {html.escape(var_txt)}",
                parse_mode="HTML",
            )

        q = q[1:]
        await state.update_data(import_fix_queue=q)

    # показываем следующий выбор или возвращаемся в импорт
    if q:
        nxt = q[0]
        await c.message.answer(
            f"⚠️ Похоже, слово введено с ошибкой: <b>{html.escape(nxt['orig'])}</b>\n"
            f"Выбери правильный вариант:",
            parse_mode="HTML",
            reply_markup=kb_import_fixes(nxt["cands"]),
        )
    else:
        await state.set_state(ImportState.waiting_lines)
        await c.message.answer("✅ Исправления закончены. Продолжай импорт или напиши /done")

@dp.callback_query(F.data == "dupkeep")
async def cb_dup_keep(c: CallbackQuery):
    await c.answer("Оставил как есть ✅")


@dp.callback_query(F.data.startswith("dupdel:"))
async def cb_dup_delete(c: CallbackQuery):
    word_id = int(c.data.split(":")[1])

    async with aiosqlite.connect(DB) as db:
        # получим инфу, чтобы красиво ответить
        row = await fetchone(db, "SELECT word, transcription, translation FROM words WHERE id=?", (word_id,))
        if not row:
            await c.answer("Уже удалено или не найдено")
            return

        w, trc, tr = row

        # удаляем слово и хвосты (если они были)
        await db.execute("DELETE FROM words WHERE id=?", (word_id,))
        await db.execute("DELETE FROM stats WHERE word_id=?", (word_id,))
        await db.execute("DELETE FROM progress WHERE word_id=?", (word_id,))
        await db.execute("DELETE FROM active_pool WHERE word_id=?", (word_id,))
        await db.commit()

    await c.message.edit_text(
        f"🗑 Удалено из базы:\n<b>{html.escape(str(w))}</b> "
        f"{html.escape(str(trc) if trc else '—')} — {html.escape(str(tr))}",
        parse_mode="HTML",
    )
    await c.answer("Удалено ✅")


def normalize_token(s: str) -> str:
    return (s or "").lower().replace("ё", "е").strip()

def split_variants(s: str) -> list[str]:
    return [normalize_token(p) for p in (s or "").split(",") if normalize_token(p)]


@dp.message(QuizState.waiting_text_answer, F.text, ~F.text.startswith("/"))
async def text_answer(m: Message, state: FSMContext):
    data = await state.get_data()
    if not m.text:
        return
    if not data:
        await m.answer("Вопрос устарел — даю новый")
        await state.clear()
        await start_quiz(m.chat.id, m.from_user.id, state)
        return

    user_answer = (m.text or "").strip()

    def norm(s: str):
        return s.lower().replace("ё", "е").strip()

    # правильный ответ (может быть: "бытие, существо")
    expected = data["true_answer"]
    if data.get("direction") == "reverse":
        expected = str(expected).split()[0]  # для RU→EN

    # разбиваем правильный ответ на варианты
    variants = [norm(v) for v in str(expected).split(",") if norm(v)]

    user_norm = norm(user_answer)

    # ✅ достаточно любого одного слова
    ok = user_norm in variants

    await update_stat(
        m.from_user.id,
        data["word_id"],
        ok,
        data["direction"],
    )

    await apply_answer(m.from_user.id, data["word_id"], data["direction"], ok)
    f_streak, r_streak = await get_progress(m.from_user.id, data["word_id"])
    stats_line = (
        f"\n📈 Серии: EN→RU {f_streak}/3 • RU→EN {r_streak}/3"
        if (f_streak + r_streak) > 0 else ""
    )

    word = data["show_word"]
    tr = data["show_tr"]

    if ok:
        await m.answer(f"✅ Верно!\n{word} = {tr}{stats_line}", parse_mode="HTML")

        en_plain = str(data.get("show_word", "")).split()[0]  # безопасно
        if en_plain and await _tts_should_send(state):
            await send_pronunciation_google(m.chat.id, en_plain)


    else:

        await m.answer(

            f"❌ Неверно.\n{word} = {tr}\nТвой ответ: {user_answer}{stats_line}",

            parse_mode="HTML",

        )

        # 🔊 озвучиваем правильный вариант (английское слово)

        en_plain = str(data.get("show_word", "")).split()[0]  # например "bothered"

        if en_plain and await _tts_should_send(state):
            await send_pronunciation_google(m.chat.id, en_plain)

    await state.clear()
    await start_quiz(m.chat.id, m.from_user.id, state)



@dp.message(Command("import"))
async def import_start(m: Message, state: FSMContext):
    await state.set_state(ImportState.waiting_lines)
    await state.update_data(added=0, skipped=0)

    # если в этом же сообщении есть строки после /import — тоже обработаем
    tail = "\n".join(m.text.splitlines()[1:]) if m.text else ""
    pairs = parse_pairs(tail)

    if pairs:
        inserted_lines: list[str] = []
        inserted_count = 0

        async with aiosqlite.connect(DB) as db:
            for w, trc, t in pairs:
                w_clean = (w or "").strip()
                trc_clean = (trc or "").strip() if trc else None

                variants: list[str] = []
                if not t:
                    variants, auto_ipa = await auto_en_info(w_clean)
                    if not trc_clean:
                        trc_clean = auto_ipa
                    t = ", ".join(variants) if variants else ""


                if not t:
                    data = await state.get_data()
                    await state.update_data(skipped=data.get("skipped", 0) + 1)
                    continue

                await db.execute(
                    "INSERT INTO words(word, transcription, translation) VALUES (?, ?, ?)",
                    (w_clean, trc_clean, t)
                )
                inserted_count += 1

                if not variants:
                    variants = [t]
                var_txt = "; ".join(variants[:5])
                ipa_txt = trc_clean if trc_clean else "—"
                inserted_lines.append(f"• {w_clean} {ipa_txt} → {var_txt}")

            await db.commit()

        data = await state.get_data()
        await state.update_data(added=data.get("added", 0) + inserted_count)

        if inserted_lines:
            preview = "\n".join(inserted_lines[:25])
            more = "" if len(inserted_lines) <= 25 else f"\n…и ещё {len(inserted_lines) - 25} строк"
            await m.answer(f"📌 Внесено в базу:\n{preview}{more}")

        data = await state.get_data()
        if not m.text:
            return
        await state.update_data(added=data["added"] + len(pairs))

    await m.answer(
        "🟢 Режим импорта включён.\n"
            "Отправляй строки в формате:\n"
            "apple [ˈæpəl] яблоко\n"
            "или\n"
            "apple 'ˈæpəl' яблоко\n"
            "или просто введи английское слово\n"
            "Когда закончишь — напиши /done\n"
            "Отмена — /cancel"
    )


@dp.message(Command("cancel"))
async def import_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Импорт отменён.")


@dp.message(Command("done"))
async def import_done(m: Message, state: FSMContext):
    data = await state.get_data()
    if not m.text:
        return
    added = data.get("added", 0)
    skipped = data.get("skipped", 0)
    await state.clear()
    await m.answer(
        f"✅ Импорт завершён.\n"
        f"Добавлено: {added}\n"
        f"Пропущено: {skipped}\n\n",
        reply_markup=kb_stats_commands()

    )


@dp.message(ImportState.waiting_lines, ~F.text.startswith("/"))
async def import_lines(m: Message, state: FSMContext):
    text = m.text or ""
    pairs = parse_pairs(text)

    data = await state.get_data()
    added = data.get("added", 0)
    skipped = data.get("skipped", 0)

    if not pairs:
        skipped += max(1, len(text.splitlines()))
        await state.update_data(skipped=skipped)
        await m.answer("⏭ Не нашёл строк для импорта. Шли дальше или /done")
        return

    fix_queue = []          # слова с опечатками
    inserted_lines = []     # что реально добавили
    inserted_count = 0
    dup_rows = []  # ✅ найденные дубликаты (id, word, trc, tr)

    async with aiosqlite.connect(DB) as db:
        for w, trc, t in pairs:
            w_clean = (w or "").strip().lower()
            if not w_clean:
                skipped += 1
                continue

            trc_clean = (trc or "").strip() if trc else None

            used_variants = []

            # 🔎 если перевода нет — проверяем, не опечатка ли
            if not t:
                ok_word = await is_valid_english_word(w_clean)
                if not ok_word:
                    cands = await suggest_words(w_clean, limit=6)

                    # если подсказок нет — не подсовываем исходную ошибку кнопкой
                    if not cands:
                        cands = ["arm", "arms", "army"] if w_clean.startswith("arm") else []
                    # (можно оставить пустым — тогда будет только кнопка "Пропустить")

                    fix_queue.append({"orig": w_clean, "cands": cands})
                    skipped += 1
                    continue

                    fix_queue.append({"orig": w_clean, "cands": cands})
                    skipped += 1
                    continue

                # автопоиск перевода + IPA
                variants, auto_ipa = await auto_en_info(w_clean)
                if not trc_clean:
                    trc_clean = auto_ipa
                t = variants[0] if variants else ""
                used_variants = variants[:] if variants else [t]

            if not t:
                skipped += 1
                continue
            # ✅ проверка дубликата по word (без регистра)
            dups = await fetchall(
                db,
                "SELECT id, word, transcription, translation FROM words WHERE lower(word)=lower(?)",
                (w_clean,),
            )
            if dups:
                dup_rows.extend(dups)  # добавим все совпадения
                skipped += 1
                continue

            t_store = ", ".join(used_variants) if used_variants else t
            await db.execute(
                "INSERT INTO words(word, transcription, translation) VALUES (?, ?, ?)",
                (w_clean, trc_clean, t_store)
            )

            inserted_count += 1
            if not used_variants:
                used_variants = [t]

            var_txt = "; ".join(used_variants[:5])
            ipa_txt = trc_clean if trc_clean else "—"
            inserted_lines.append(f"• {w_clean} {ipa_txt} → {var_txt}")

        await db.commit()
        # 📌 если нашли дубликаты — выводим карточки с кнопкой удаления
        for (did, dw, dtrc, dtr) in dup_rows[:20]:  # ограничим, чтобы не заспамить
            await m.answer(
                "⚠️ Найдено слово в базе:\n"
                f"• <b>{html.escape(str(dw))}</b> "
                f"{html.escape(str(dtrc) if dtrc else '—')} — {html.escape(str(dtr))}",
                parse_mode="HTML",
                reply_markup=kb_dup_delete(did),
            )
        if len(dup_rows) > 20:
            await m.answer(f"…и ещё {len(dup_rows) - 20} дубликатов (не показал, чтобы не заспамить)")

    # если есть слова с опечатками — запускаем режим выбора
    if fix_queue:
        await state.update_data(
            import_fix_queue=fix_queue,
            added=added + inserted_count,
            skipped=skipped
        )
        await state.set_state(ImportState.choosing_fix)

        first = fix_queue[0]
        await m.answer(
            f"⚠️ Похоже, слово введено с ошибкой: <b>{html.escape(first['orig'])}</b>\n"
            f"Выбери правильный вариант:",
            parse_mode="HTML",
            reply_markup=kb_import_fixes(first["cands"]),
        )
        return

    added += inserted_count
    await state.update_data(added=added, skipped=skipped)

    if inserted_lines:
        preview = "\n".join(inserted_lines[:25])
        more = "" if len(inserted_lines) <= 25 else f"\n…и ещё {len(inserted_lines) - 25} строк"
        await m.answer(
            f"➕ Добавил: {inserted_count}\n⏭ Пропустил: {skipped}\n\n"
            f"📌 Внесено в базу:\n{preview}{more}\n\n"
            f"/done чтобы закончить"
        )
    else:
        await m.answer(
            f"⏭ Ничего не добавил.\n"
            f"Пропущено: {skipped}\n\n/done чтобы закончить"
        )




async def start_quiz(chat_id: int, user_id: int, state: FSMContext):
    q = await pick_question(user_id)  # или get_question/get_question_reverse
    if not q:
        await bot.send_message(chat_id, "Недостаточно слов ...")
        return

    direction = q["direction"]  # или direction = random.choice(...)

    # ✅ защита: в RU→EN true_answer храним без транскрипции
    await state.set_state(QuizState.waiting_text_answer)

    await state.update_data(tts_sent=False)  # 👈 добавь эту строку
    await state.update_data(answered=False)
    await state.update_data(**q)


    title = "✍️ Переведи (EN→RU):" if direction == "forward" else "✍️ Переведи (RU→EN):"

    await bot.send_message(
        chat_id,
        f"{title}\n\n{b(q['prompt'])}",
        parse_mode="HTML",
        reply_markup=kb_choose_mc(),
    )

@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats_buttons(c: CallbackQuery, state: FSMContext):
    action = c.data.split(":", 1)[1]

    if action == "quiz":
        await c.answer()
        await start_quiz(c.message.chat.id, c.from_user.id, state)
        return

    if action == "add":
        await c.answer()
        await c.message.answer("Формат добавления:\n/add apple=яблоко")
        return

    if action == "import":
        await c.answer()
        # запускаем тот же сценарий, что и /import
        await state.set_state(ImportState.waiting_lines)
        await state.update_data(added=0, skipped=0)
        await c.message.answer(
            "🟢 Режим импорта включён.\n"
            "Отправляй строки в формате:\n"
            "apple [ˈæpəl] яблоко\n"
            "или\n"
            "apple 'ˈæpəl' яблоко\n"
            "или просто введи английское слово\n"
            "Когда закончишь — напиши /done\n"
            "Отмена — /cancel"
        )
        return

    if action == "resetstats":
        await c.answer()
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM stats")
            await db.execute("DELETE FROM progress")
            await db.execute("DELETE FROM active_pool")
            await db.commit()

        await state.clear()
        await c.message.answer("✅ Статистика и прогресс обучения сброшены.\n\nНапиши /quiz чтобы начать заново.")
        return

    if action == "clear":
        await c.answer()
        await c.message.answer(
            "⚠️ Ты уверен, что хочешь удалить ВСЕ слова и статистику?\n\n"
            "Это действие нельзя отменить.",
            reply_markup=kb_clear_confirm()
        )
        return


@dp.message(Command("start"))
async def start(m: Message):
    total = await count_words()
    await m.answer(
        "👋 Привет!\n"
        "Я бот для тренировки слов.\n\n"
        f"📚 Слов в словаре: {total}\n\n"
        "Команды:\n"
        "/add слово=перевод\n"
        "/import — добавить списком\n"
        "/quiz — тренировка\n"
        "/clear — удаления словаря\n"
        "/stats — статистика"
    )


@dp.message(Command("resetstats"))
async def cmd_reset_stats(m: Message, state: FSMContext):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM stats")
        await db.execute("DELETE FROM progress")
        await db.execute("DELETE FROM active_pool")
        await db.commit()

    await state.clear()
    await m.answer("✅ Статистика и прогресс обучения сброшены.\n\nНапиши /quiz чтобы начать заново.")



@dp.message(Command("add"))
async def cmd_add(m: Message):
    text = m.text.replace("/add", "").strip()
    if "=" not in text:
        await m.answer("Формат: /add apple=яблоко")
        return

    word, tr = map(str.strip, text.split("=", 1))
    if not word or not tr:
        await m.answer("Пустое слово или перевод")
        return

    await add_word(word, tr)
    await m.answer(f"✅ Добавлено: {word} — {tr}")


@dp.message(Command("quiz"))
async def quiz(m: Message, state: FSMContext):
    await start_quiz(m.chat.id, m.from_user.id, state)

@dp.message(Command("dbcheck"))
async def cmd_dbcheck(m: Message):
    async with aiosqlite.connect(DB) as db:
        # таблицы
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in await cur.fetchall()]
        await cur.close()

        # сколько строк в stats
        stats_count = None
        if "stats" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM stats")
            stats_count = (await cur.fetchone())[0]
            await cur.close()

        # структура stats
        cols = []
        if "stats" in tables:
            cur = await db.execute("PRAGMA table_info(stats)")
            cols = [r[1] for r in await cur.fetchall()]  # r[1] = column name
            await cur.close()

    await m.answer(
        "🧪 DB CHECK\n"
        f"DB: {DB}\n"
        f"Tables: {', '.join(tables)}\n"
        f"stats rows: {stats_count}\n"
        f"stats cols: {', '.join(cols)}"
    )



@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    async with aiosqlite.connect(DB) as db:
        row = await fetchone(
            db,
            """
            SELECT
                COALESCE(SUM(fwd_correct + rev_correct), 0),
                COALESCE(SUM(fwd_wrong + rev_wrong), 0)
            FROM stats
            WHERE user_id=?
            """,
            (m.from_user.id,),
        )

        learned = await fetchone(
            db,
            """
            SELECT COUNT(*)
            FROM progress
            WHERE user_id=? AND learned=1
            """,
            (m.from_user.id,),
        )

    c, w = row if row else (0, 0)
    learned_cnt = learned[0] if learned else 0
    total = c + w
    acc = (c / total * 100) if total else 0.0
    total_words = await count_words()

    await m.answer(
        f"📊 Статистика:\n\n"
        f"📚 Слов в словаре: {total_words}\n"
        f"📈 Ответов всего: {total}\n"
        f"✅ Верных: {c}\n"
        f"❌ Ошибок: {w}\n"
        f"🎓 Выучено слов: {learned_cnt}\n"
        f"🎯 Точность: {acc:.1f}%\n",
        reply_markup=kb_stats_commands()
    )


async def update_stat(user_id: int, word_id: int, is_correct: bool, direction: str):
    async with aiosqlite.connect(DB) as db:
        if direction == "forward":
            sql = """
            INSERT INTO stats(user_id, word_id, fwd_correct, fwd_wrong, last_ts)
            VALUES(?,?,?,?, strftime('%s','now'))
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                fwd_correct = fwd_correct + ?,
                fwd_wrong   = fwd_wrong   + ?,
                last_ts = strftime('%s','now')
            """
        else:
            sql = """
            INSERT INTO stats(user_id, word_id, rev_correct, rev_wrong, last_ts)
            VALUES(?,?,?,?, strftime('%s','now'))
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                rev_correct = rev_correct + ?,
                rev_wrong   = rev_wrong   + ?,
                last_ts = strftime('%s','now')
            """

        await db.execute(
            sql,
            (
                user_id,
                word_id,
                1 if is_correct else 0,
                0 if is_correct else 1,
                1 if is_correct else 0,
                0 if is_correct else 1,
            ),
        )
        await db.commit()



async def main():
    # 🔥 ОБЯЗАТЕЛЬНО запускаем health-server для Pella
    asyncio.create_task(_run_health_server())
    print("[boot] health server started", flush=True)

    await init_db()
    print("[boot] db initialized", flush=True)

    await init_db()

    await bot.delete_webhook(drop_pending_updates=True)
    print("WEBHOOK DELETED, START POLLING", flush=True)

    await dp.start_polling(bot)




if __name__ == "__main__":
    asyncio.run(main())
