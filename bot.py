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

logging.basicConfig(level=logging.ERROR)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=32)

try: bot.set_my_commands([])
except Exception: pass

HTTP_SESSION = requests.Session()
retries = Retry(total=2, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
HTTP_SESSION.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=100))

SYSTEM = (
    "Act as RANKVERSE AI: a smart, helpful assistant. "
    "STRICT: Reply in HINGLISH or English. NEVER use Devanagari script. Use emojis sparingly. "
    "RULE: If user asks a study question, DO NOT greet. Answer directly."
)

# ---------- HELP & WELCOME TEXT ----------
HELP_TEXT = (
    "✨ <b>RANKVERSE AI | COMMAND CENTER</b> ✨\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Heyy mittar! Main in saare kaamon mein aapki help kar sakta hoon:\n\n"
    "🤖 <b>AI Chat & Doubts:</b>\n"
    "Bas apna sawal type karein. Main instantly jawab dunga.\n\n"
    "📸 <b>Photo Doubt:</b>\n"
    "Kisi bhi question ki photo bhejein, main solve kar dunga.\n\n"
    "📄 <b>PDF Summarizer:</b>\n"
    "Koi bhi PDF file bhejein, main uski short summary nikal kar dunga.\n\n"
    "🌐 <b>Live Web Search:</b>\n"
    "<code>/search [topic]</code>\n"
    "<i>(Example: /search NEET 2026 news)</i>\n\n"
    "🌦️ <b>Weather Update:</b>\n"
    "<code>/weather [city]</code>\n"
    "<i>(Example: /weather Delhi)</i>\n\n"
    "⏰ <b>Reminder System:</b>\n"
    "<code>/remind [minutes] [task]</code>\n"
    "<i>(Example: /remind 10 Study Biology)</i>\n\n"
    "🗣️ <b>Translator:</b>\n"
    "<code>/translate [language] [text]</code>\n"
    "<i>(Example: /translate Spanish I love you)</i>\n\n"
    "📌 <b>Group Chat:</b>\n"
    "Mujhe group me add karein aur <b>@RankVerseAIBot</b> likh kar apna message bhejein.\n"
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

# ---------- THREAD-SAFE GLOBAL DB ----------
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
            db_conn.execute("""INSERT INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?) 
                           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
                        (u.id, u.username or "", u.first_name or "", int(time.time())))
            db_conn.commit()
    except Exception: pass

def save_chat(chat):
    if not chat: return
    try:
        with db_lock:
            db_conn.execute("""INSERT INTO chats(chat_id, title, chat_type, added_at) VALUES(?,?,?,?) 
                           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type""",
                        (chat.id, getattr(chat, "title", "") or "", chat.type, int(time.time())))
            db_conn.commit()
    except Exception: pass

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
    if bot_me is None:
        bot_me = bot.get_me()
    return bot_me

def is_owner(user):
    if not user: return False
    return user.id == OWNER_ID or (user.username or "").lower() == OWNER_USERNAME

# ---------- AI GENERATOR ----------
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
        if r.status_code == 200:
            return clean_markdown(r.json()["choices"][0]["message"]["content"].strip())
        if r.status_code == 429: return "Arre yaar, thoda rush hai! 1 min baad try karna. ✨"
        return f"⚠️ API Error ({r.status_code})."
    except Exception:
        return "⚠️ Network issue lag raha hai. Internet check karo."

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
    else:
        bot.send_message(chat_id, answer, reply_to_message_id=message_id)

# ---------- PURE PYTHON SEARCH ----------
def pure_python_search(query):
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = HTTP_SESSION.post(url, data={"q": query}, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for result in soup.find_all('div', class_='result__body')[:3]:
            title_tag = result.find('a', class_='result__a')
            snippet_tag = result.find('a', class_='result__snippet')
            if title_tag:
                title = title_tag.get_text(strip=True)
                link = title_tag['href']
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else "No description."
                results.append({"title": title, "body": snippet, "href": link})
        return results
    except Exception as e:
        logging.error(f"Search error: {e}")
        return []

@bot.message_handler(commands=['search'])
def search_cmd(m):
    query = m.text.partition(' ')[2].strip()
    if not query:
        return bot.reply_to(m, "❌ Kya search karna hai? Example: <code>/search Latest NEET news</code>", parse_mode="HTML")
    
    bot.send_chat_action(m.chat.id, 'typing')
    results = pure_python_search(query)
    
    if not results:
        return bot.reply_to(m, "❌ Koi result nahi mila ya network issue hai. Thodi der baad try karein.")
    
    msg = f"🔍 <b>Search Results for:</b> {query}\n━━━━━━━━━━━━━━━━━━\n"
    for i, res in enumerate(results, 1):
        msg += f"<b>{i}. {res['title']}</b>\n{res['body'][:150]}...\n<a href='{res['href']}'>Read More</a>\n\n"
    
    bot.reply_to(m, msg, parse_mode="HTML", disable_web_page_preview=True)

# ---------- TRANSLATOR ----------
@bot.message_handler(commands=['translate'])
def translate_cmd(m):
    args = m.text.partition(' ')[2].strip()
    if not args:
        return bot.reply_to(m, "❌ Format: <code>/translate [language] [text]</code>\nExample: <code>/translate Spanish Hello how are you</code>", parse_mode="HTML")

    parts = args.split(' ', 1)
    if len(parts) < 2:
        return bot.reply_to(m, "❌ Kripya text bhi likhein. Example: <code>/translate French I love you</code>", parse_mode="HTML")
        
    target_lang = parts[0]
    text_to_translate = parts[1]

    bot.send_chat_action(m.chat.id, 'typing')
    prompt = f"Translate the following text into {target_lang}. Only provide the translated text, nothing else:\n\n{text_to_translate}"
    translation = groq_ai(prompt, system_prompt="You are an expert translator. Translate accurately.")
    
    bot.reply_to(m, f"🌐 <b>Translation ({target_lang}):</b>\n\n{translation}", parse_mode="HTML")

# ---------- WEATHER API ----------
@bot.message_handler(commands=['weather'])
def weather_cmd(m):
    city = m.text.partition(' ')[2].strip()
    if not city:
        return bot.reply_to(m, "❌ City ka naam likhein. Example: <code>/weather Delhi</code>", parse_mode="HTML")
    
    bot.send_chat_action(m.chat.id, 'typing')
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        geo_res = requests.get(geo_url, timeout=10).json()
        if not geo_res.get('results'):
            return bot.reply_to(m, f"❌ '{city}' naam ka koi sheher nahi mila.")
        
        lat = geo_res['results'][0]['latitude']
        lon = geo_res['results'][0]['longitude']
        name = geo_res['results'][0]['name']
        country = geo_res['results'][0].get('country', '')

        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        w_res = requests.get(weather_url, timeout=10).json()
        
        temp = w_res['current']['temperature_2m']
        hum = w_res['current']['relative_humidity_2m']
        wind = w_res['current']['wind_speed_10m']
        
        msg = (
            f"🌦️ <b>Weather Update: {name}, {country}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌡️ <b>Temperature:</b> {temp}°C\n"
            f"💧 <b>Humidity:</b> {hum}%\n"
            f"💨 <b>Wind Speed:</b> {wind} km/h\n"
        )
        bot.reply_to(m, msg, parse_mode="HTML")
    except Exception:
        bot.reply_to(m, "⚠️ Weather fetch karne mein error aaya.")

# ---------- REMINDER SYSTEM ----------
def send_reminder(chat_id, text):
    try:
        bot.send_message(chat_id, f"⏰ <b>REMINDER:</b>\n\n{text}", parse_mode="HTML")
    except Exception: pass

@bot.message_handler(commands=['remind'])
def remind_cmd(m):
    args = m.text.partition(' ')[2].strip().split(' ', 1)
    if len(args) < 2 or not args[0].isdigit():
        return bot.reply_to(m, "❌ Format: <code>/remind 5 Drink water</code>", parse_mode="HTML")
    
    minutes = int(args[0])
    task = args[1]
    
    t = threading.Timer(minutes * 60, send_reminder, args=[m.chat.id, task])
    t.daemon = True
    t.start()
    
    bot.reply_to(m, f"✅ Theek hai! Main aapko <b>{minutes} minute</b> baad yaad dila dunga: <i>{task}</i>", parse_mode="HTML")

# ---------- PDF SUMMARIZER ----------
@bot.message_handler(content_types=['document'])
def pdf_handler(m):
    save_user(m.from_user)
    if m.document.mime_type != 'application/pdf':
        return bot.reply_to(m, "❌ Sirf PDF files supported hain.")
    
    bot.send_chat_action(m.chat.id, 'typing')
    try:
        file_info = bot.get_file(m.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        temp_pdf_path = f"temp_{m.document.file_id}.pdf"
        with open(temp_pdf_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        reader = PdfReader(temp_pdf_path)
        text = ""
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        
        os.remove(temp_pdf_path)
        
        if len(text.strip()) < 50:
            return bot.reply_to(m, "⚠️ Is PDF mein readable text nahi mila.")
        
        text = text[:15000]
        prompt = f"Summarize the following text from a PDF in clear Hinglish/English bullet points:\n\n{text}"
        bot.reply_to(m, "⏳ PDF padh liya hai, ab summarize kar raha hoon...")
        send_answer(m.chat.id, m.message_id, prompt, m.from_user.id, system_prompt="You are an expert summarizer. Extract key points and present them in Hinglish.")
        
    except Exception:
        bot.reply_to(m, "⚠️ PDF read karne mein error aaya. Kya file password protected hai?")

# ---------- START, HELP & CALLBACKS ----------
@bot.message_handler(commands=["start", "reset"])
def start(m):
    waiting.pop(m.from_user.id, None)
    save_user(m.from_user)
    
    if m.chat.type != "private": return
    
    text, kb = get_welcome_data(m.from_user)
    bot.send_message(m.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["help"])
def help_command(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu"))
    bot.send_message(m.chat.id, HELP_TEXT, reply_markup=kb)

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
        
    uid = call.from_user.id
    
    if call.data == "help_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu"))
        try:
            bot.edit_message_text(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass # Ignore if message is same
            
    elif call.data == "back_to_menu":
        text, kb = get_welcome_data(call.from_user)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass # Ignore if message is same
            
    elif call.data == "bc_prompt":
        if not is_owner(call.from_user): return
        waiting[uid] = "broadcast"
        msg = "📢 <b>Broadcast Mode ON</b>\n\nAb jo bhi message aap bhejenge, wo saare users aur groups ko chala jayega.\n\nCancel: /reset"
        try: bot.send_message(call.message.chat.id, msg)
        except Exception: pass

def do_broadcast(m):
    msg_text = m.text or m.caption or "📢 Update"
    targets = []
    try:
        with db_lock:
            users = [r[0] for r in db_conn.execute("SELECT user_id FROM users").fetchall()]
            chats = [r[0] for r in db_conn.execute("SELECT chat_id FROM chats").fetchall()]
            targets = list(set(users + chats))
    except Exception: pass
    
    success = 0
    status_msg = bot.reply_to(m, f"⏳ Broadcasting to {len(targets)} targets...")
    for target in targets:
        try:
            if m.content_type == "photo":
                bot.send_photo(target, m.photo[-1].file_id, caption=m.caption, parse_mode="HTML")
            else:
                bot.send_message(target, msg_text, parse_mode="HTML")
            success += 1
            time.sleep(0.04)
        except Exception: pass
    bot.edit_message_text(f"✅ Broadcast Done: {success}/{len(targets)}", m.chat.id, status_msg.message_id)

@bot.message_handler(content_types=["photo"])
def photo_handler(m):
    save_user(m.from_user)
    if waiting.get(m.from_user.id) == "broadcast":
        if is_owner(m.from_user):
            waiting.pop(m.from_user.id, None)
            do_broadcast(m)
        return
        
    if m.chat.type != "private":
        me = get_me()
        caption = (m.caption or "").lower()
        if not (f"@{me.username.lower()}" in caption or (m.reply_to_message and m.reply_to_message.from_user.id == me.id)): return
    
    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        raw = bot.download_file(file_info.file_path)
        image_b64 = base64.b64encode(raw).decode('utf-8')
        prompt = (m.caption or "").strip() or "Explain this image."
        send_answer(m.chat.id, m.message_id, prompt, m.from_user.id, image_b64=image_b64)
    except Exception: bot.reply_to(m, "Photo padh nahi payi yaar.")

@bot.message_handler(content_types=["text"])
def text_handler(m):
    if not m.from_user or not m.text or m.text.startswith("/"): return
    text = m.text.strip()
    
    if waiting.get(m.from_user.id) == "broadcast":
        if is_owner(m.from_user):
            waiting.pop(m.from_user.id, None)
            do_broadcast(m)
        return

    if m.chat.type in ("group", "supergroup"):
        save_chat(m.chat)
        me = get_me()
        is_mentioned = f"@{me.username.lower()}" in text.lower()
        is_replied = bool(m.reply_to_message and m.reply_to_message.from_user.id == me.id)
        if not (is_mentioned or is_replied): return
        text = re.sub(rf"@{re.escape(me.username)}", "", text, flags=re.IGNORECASE).strip()
        send_answer(m.chat.id, m.message_id, text or "Kya bol rahe ho?", None)
        return

    save_user(m.from_user)
    send_answer(m.chat.id, m.message_id, text, m.from_user.id)

def main():
    init_db()
    bot.remove_webhook()
    time.sleep(0.5)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)

if __name__ == "__main__":
    main()

# ---------- FLASK WEB SERVER FOR RENDER ----------
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "RankVerse AI is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.start()

# Override main function to start Flask
def main():
    init_db()
    bot.remove_webhook()
    keep_alive() # Start web server for Render
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)

if __name__ == "__main__":
    main()
