import os
import re
import time
import base64
import sqlite3
import logging
import threading
import requests
import telebot
import datetime
from bs4 import BeautifulSoup
from telebot import types
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pypdf import PdfReader
from flask import Flask

load_dotenv()

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
MODEL = os.getenv("MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
VISION_MODEL = "qwen/qwen3.8-27b"
GROUP_LINK = "https://t.me/rankverse_sectiongc" 
DB_FILE = "rankverse_v2.db"

OWNER_ID = 8893317448
OWNER_USERNAME = "wpxrh"

if not BOT_TOKEN or not GROQ_API_KEY:
    raise SystemExit("BOT_TOKEN ya GROQ_API_KEY .env mein missing hai.")

logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=32)

try: bot.set_my_commands([])
except Exception: pass

HTTP_SESSION = requests.Session()
retries = Retry(total=2, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
HTTP_SESSION.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=100))

# ---------- FLASK WEB SERVER ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "RankVerse AI is alive and running!"

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# ---------- PROMPTS ----------
SYSTEM = (
    "Act as RANKVERSE AI: a smart, helpful assistant. "
    "STRICT: Reply in HINGLISH or English. NEVER use Devanagari script. Use emojis sparingly. "
    "RULE 1: If user asks a study question, DO NOT greet. Answer directly. "
    "RULE 2: If user asks you to 'tag' or 'call' someone by their name, output their username with an '@' symbol (e.g., '@username') and a friendly message."
)

HELP_TEXT = (
    "✨ <b>RANKVERSE AI | COMMAND CENTER</b> ✨\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🤖 <b>AI Chat & Doubts:</b>\nBas apna sawal type karein.\n\n"
    "📸 <b>Photo Doubt:</b>\nPhoto bhejein, solve kar dunga.\n\n"
    "📄 <b>PDF Summarizer:</b>\nPDF bhejein, summary nikal dunga.\n\n"
    "🌐 <b>Search:</b>\n<code>/search [topic]</code> (Ab har cheez ka jawab dega)\n\n"
    "🌦️ <b>Weather:</b>\n<code>/weather [city]</code>\n\n"
    "⏰ <b>Reminder:</b>\n<code>/remind [time] [task]</code> (e.g., /remind 10m Study)\n\n"
    "🗣️ <b>Translator:</b>\n<code>/translate [lang] [text]</code>\n\n"
    "📢 <b>Tag All:</b>\n<code>/all [message]</code> (Note: Telegram bots mass-tag nahi kar sakte, par msg sabko chala jayega)\n"
    "━━━━━━━━━━━━━━━━━━"
)

def get_welcome_data(user):
    text = (
        "✨ <b>RANKVERSE AI</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Heyy mittar! Main aapki smart AI dost hoon. 💖\n"
        "Kuch bhi pucho, ya bas timepass baat karo! 😉\n"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📘 Help & Features", callback_data="help_menu"))
    kb.add(types.InlineKeyboardButton("💬 Join Community", url=GROUP_LINK))
    kb.add(types.InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{get_me().username}?startgroup=true"))
    
    if is_owner(user):
        kb.add(types.InlineKeyboardButton("📢 Imp Announcement (Owner Only)", callback_data="bc_prompt"))
    return text, kb

# ---------- DB ----------
db_lock = threading.Lock()
db_conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
db_conn.execute("PRAGMA journal_mode=WAL;")
db_conn.execute("PRAGMA synchronous=OFF;")

def init_db():
    with db_lock:
        db_conn.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at INTEGER)")
        db_conn.execute("CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT, chat_type TEXT, added_at INTEGER)")
        db_conn.commit()

def save_user(u):
    if not u: return
    try:
        with db_lock:
            db_conn.execute("INSERT INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name", (u.id, u.username or "", u.first_name or "", int(time.time())))
            db_conn.commit()
    except Exception: pass

def save_chat(chat):
    if not chat: return
    try:
        with db_lock:
            db_conn.execute("INSERT INTO chats(chat_id, title, chat_type, added_at) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type", (chat.id, getattr(chat, "title", "") or "", chat.type, int(time.time())))
            db_conn.commit()
    except Exception: pass

# ---------- HELPERS ----------
markdown_bold = re.compile(r'\*\*(.*?)\*\*')
markdown_italic = re.compile(r'\*(.*?)\*')
html_tags = re.compile(r'<[^>]+>')

def clean_markdown(text):
    text = markdown_bold.sub(r'<b>\1</b>', text)
    return markdown_italic.sub(r'<i>\1</i>', text)

def strip_html(text):
    return html_tags.sub('', text)

sessions = {}   
waiting = {} 
bot_me = None   

def get_me():
    global bot_me
    if bot_me is None: bot_me = bot.get_me()
    return bot_me

def is_owner(user):
    if not user: return False
    return user.id == OWNER_ID or (user.username or "").lower() == OWNER_USERNAME

# ---------- AI ----------
def groq_ai(prompt, history=None, image_b64=None, image_mime="image/jpeg", system_prompt=SYSTEM):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    current_model = VISION_MODEL if image_b64 else MODEL
    messages = [{"role": "system", "content": system_prompt}]
    if history: messages.extend(history)
    
    if image_b64:
        messages.append({"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}}]})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {"model": current_model, "messages": messages, "temperature": 0.5, "max_tokens": 1500}
    try:
        r = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=25)
        if r.status_code == 200: return clean_markdown(r.json()["choices"][0]["message"]["content"].strip())
        if r.status_code == 429: return "Arre yaar, thoda rush hai! 1 min baad try karna. ✨"
        return f"⚠️ API Error ({r.status_code})."
    except Exception: return "⚠️ Network issue. Internet check karo."

def send_answer(chat_id, message_id, prompt, uid=None, image_b64=None, system_prompt=SYSTEM):
    history = sessions.get(uid, []) if uid else []
    answer = groq_ai(prompt, history, image_b64, system_prompt=system_prompt)
    if not answer.startswith(("⚠️", "Arre yaar")):
        try: bot.send_message(chat_id, answer[:4000], parse_mode="HTML", reply_to_message_id=message_id)
        except Exception:
            try: bot.send_message(chat_id, strip_html(answer)[:4000], parse_mode=None, reply_to_message_id=message_id)
            except Exception: pass
        if uid:
            history = history[-2:] 
            history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}])
            sessions[uid] = history[-2:]
            if len(sessions) > 250: sessions.pop(next(iter(sessions)))
    else: bot.send_message(chat_id, answer, reply_to_message_id=message_id)

# ---------- /ALL COMMAND ----------
@bot.message_handler(commands=['all'])
def all_cmd(m):
    text = m.text.partition(' ')[2].strip()
    if not text:
        return bot.reply_to(m, "❌ Format: <code>/all [message]</code>", parse_mode="HTML")
    
    msg = f"📢 <b>ATTENTION EVERYONE!</b>\n\n{text}\n\n<i>(Note: Telegram restricts bots from auto-tagging everyone. Please tag manually if needed.)</i>"
    bot.reply_to(m, msg, parse_mode="HTML")

# ---------- SEARCH (Now always answers) ----------
def pure_python_search(query):
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = HTTP_SESSION.get(url, params={"q": query}, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for result in soup.find_all('div', class_='result__body')[:3]:
            title_tag = result.find('a', class_='result__a')
            snippet_tag = result.find('a', class_='result__snippet')
            if title_tag: 
                results.append({
                    "title": title_tag.get_text(strip=True), 
                    "body": snippet_tag.get_text(strip=True) if snippet_tag else "", 
                    "href": title_tag['href']
                })
        return results
    except Exception: 
        return []

@bot.message_handler(commands=['search'])
def search_cmd(m):
    query = m.text.partition(' ')[2].strip()
    if not query: return bot.reply_to(m, "❌ Format: <code>/search topic</code>", parse_mode="HTML")
    
    bot.send_chat_action(m.chat.id, 'typing')
    results = pure_python_search(query)
    
    if results:
        # If web results are found, feed them to AI to create a clean answer
        context = "\n".join([f"{r['title']}: {r['body']}" for r in results])
        prompt = f"User asked: {query}\n\nWeb Search Context:\n{context}\n\nBased on this, give a direct, helpful answer in Hinglish/English."
        send_answer(m.chat.id, m.message_id, prompt, m.from_user.id)
    else:
        # If web search fails, use AI's internal knowledge
        prompt = f"Search is unavailable. Based on your knowledge, answer this query directly: {query}"
        send_answer(m.chat.id, m.message_id, prompt, m.from_user.id)

# ---------- TRANSLATOR ----------
@bot.message_handler(commands=['translate'])
def translate_cmd(m):
    args = m.text.partition(' ')[2].strip().split(' ', 1)
    if len(args) < 2: return bot.reply_to(m, "❌ Format: <code>/translate language text</code>", parse_mode="HTML")
    bot.send_chat_action(m.chat.id, 'typing')
    translation = groq_ai(f"Translate to {args[0]}: {args[1]}", system_prompt="Expert translator.")
    bot.reply_to(m, f"🌐 <b>Translation:</b>\n{translation}", parse_mode="HTML")

# ---------- WEATHER ----------
@bot.message_handler(commands=['weather'])
def weather_cmd(m):
    city = m.text.partition(' ')[2].strip()
    if not city: return bot.reply_to(m, "❌ Format: <code>/weather city</code>", parse_mode="HTML")
    try:
        geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1", timeout=10).json()
        if not geo.get('results'): return bot.reply_to(m, "❌ City not found.")
        lat, lon, name = geo['results'][0]['latitude'], geo['results'][0]['longitude'], geo['results'][0]['name']
        w = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m", timeout=10).json()
        bot.reply_to(m, f"🌦️ <b>{name}</b>\n🌡️ {w['current']['temperature_2m']}°C\n💧 {w['current']['relative_humidity_2m']}%\n💨 {w['current']['wind_speed_10m']} km/h", parse_mode="HTML")
    except Exception: bot.reply_to(m, "⚠️ Weather error.")

# ---------- REMINDER (Now tags user) ----------
def send_reminder(chat_id, text, user_tag):
    try: 
        bot.send_message(chat_id, f"⏰ <b>REMINDER FOR {user_tag}:</b>\n\n{text}", parse_mode="HTML")
    except Exception: pass

@bot.message_handler(commands=['remind'])
def remind_cmd(m):
    args = m.text.partition(' ')[2].strip()
    match = re.match(r'(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\s+(.*)', args, re.IGNORECASE)
    
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        task = match.group(3)
        minutes = amount * 60 if unit.startswith('h') else amount
    else:
        parts = args.split(' ', 1)
        if len(parts) < 2 or not parts[0].isdigit():
            return bot.reply_to(m, "❌ Format: <code>/remind 10m Task</code> ya <code>/remind 2h Study</code>", parse_mode="HTML")
        minutes = int(parts[0])
        task = parts[1]
    
    # Create user tag
    user_tag = f"@{m.from_user.username}" if m.from_user.username else m.from_user.first_name
    
    threading.Timer(minutes * 60, send_reminder, args=[m.chat.id, task, user_tag]).start()
    
    time_str = f"{minutes} minutes" if minutes < 60 else f"{minutes//60} hours"
    bot.reply_to(m, f"✅ Reminder set for <b>{time_str}</b>.\nI will tag you ({user_tag}) when time is up!", parse_mode="HTML")

# ---------- PDF SUMMARIZER ----------
@bot.message_handler(content_types=['document'])
def pdf_handler(m):
    save_user(m.from_user)
    if m.document.mime_type != 'application/pdf': return bot.reply_to(m, "❌ Sirf PDF allowed.")
    bot.send_chat_action(m.chat.id, 'typing')
    try:
        file_info = bot.get_file(m.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        temp_path = f"temp_{m.document.file_id}.pdf"
        with open(temp_path, 'wb') as f: f.write(downloaded)
        reader = PdfReader(temp_path)
        text = "".join([p.extract_text() for p in reader.pages if p.extract_text()])[:15000]
        os.remove(temp_path)
        if len(text) < 50: return bot.reply_to(m, "⚠️ No readable text.")
        bot.reply_to(m, "⏳ Summarizing...")
        send_answer(m.chat.id, m.message_id, f"Summarize:\n{text}", m.from_user.id, system_prompt="Expert summarizer.")
    except Exception: bot.reply_to(m, "⚠️ PDF error.")

# ---------- START & HELP ----------
@bot.message_handler(commands=["start", "reset"])
def start(m):
    waiting.pop(m.from_user.id, None)
    save_user(m.from_user)
    if m.chat.type == "private":
        text, kb = get_welcome_data(m.from_user)
        bot.send_message(m.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["help"])
def help_command(m):
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_menu"))
    bot.send_message(m.chat.id, HELP_TEXT, reply_markup=kb)

@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    if call.data == "help_menu":
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_menu"))
        try: bot.edit_message_text(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception: pass
    elif call.data == "back_to_menu":
        text, kb = get_welcome_data(call.from_user)
        try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception: pass
    elif call.data == "bc_prompt":
        if not is_owner(call.from_user): return
        waiting[call.from_user.id] = "broadcast"
        bot.send_message(call.message.chat.id, "📢 Broadcast Mode ON. Send message or /reset.")

def do_broadcast(m):
    text = m.text or m.caption or "📢 Update"
    try:
        with db_lock: targets = list(set([r[0] for r in db_conn.execute("SELECT user_id FROM users")] + [r[0] for r in db_conn.execute("SELECT chat_id FROM chats")]))
    except Exception: targets = []
    ok = 0
    msg = bot.reply_to(m, f"⏳ Broadcasting...")
    for t in targets:
        try:
            if m.content_type == "photo": bot.send_photo(t, m.photo[-1].file_id, caption=m.caption, parse_mode="HTML")
            else: bot.send_message(t, text, parse_mode="HTML")
            ok += 1; time.sleep(0.04)
        except Exception: pass
    bot.edit_message_text(f"✅ Sent: {ok}/{len(targets)}", m.chat.id, msg.message_id)

@bot.message_handler(content_types=["photo"])
def photo_handler(m):
    save_user(m.from_user)
    if waiting.get(m.from_user.id) == "broadcast" and is_owner(m.from_user):
        waiting.pop(m.from_user.id); do_broadcast(m); return
    if m.chat.type != "private":
        if not (f"@{get_me().username.lower()}" in (m.caption or "").lower() or (m.reply_to_message and m.reply_to_message.from_user.id == get_me().id)): return
    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        image_b64 = base64.b64encode(bot.download_file(file_info.file_path)).decode('utf-8')
        send_answer(m.chat.id, m.message_id, m.caption or "Explain image.", m.from_user.id, image_b64=image_b64)
    except Exception: bot.reply_to(m, "Photo error.")

@bot.message_handler(content_types=["text"])
def text_handler(m):
    if not m.from_user or not m.text or m.text.startswith("/"): return
    if waiting.get(m.from_user.id) == "broadcast" and is_owner(m.from_user):
        waiting.pop(m.from_user.id); do_broadcast(m); return
    if m.chat.type in ("group", "supergroup"):
        save_chat(m.chat)
        if not (f"@{get_me().username.lower()}" in m.text.lower() or (m.reply_to_message and m.reply_to_message.from_user.id == get_me().id)): return
        text = re.sub(rf"@{re.escape(get_me().username)}", "", m.text, flags=re.IGNORECASE).strip()
        send_answer(m.chat.id, m.message_id, text or "Hello", None); return
    save_user(m.from_user)
    send_answer(m.chat.id, m.message_id, m.text, m.from_user.id)

def main():
    init_db()
    bot.remove_webhook()
    keep_alive()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)

if __name__ == "__main__":
    main()
