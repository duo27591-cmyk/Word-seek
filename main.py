import os
import logging
import asyncio
import random
import requests
from collections import defaultdict
from datetime import datetime, timedelta
import psycopg2 
from psycopg2 import sql
# Gemini import has been removed
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Gemini API Key configuration removed
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# NOTE: Replace '123456789' with your actual Telegram User ID for AADII_USER_ID 
AADII_USER_ID = int(os.getenv("AADII_USER_ID", "123456789")) 
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Data Storage ---
# Chat history for Ajwa removed
# Game state is stored by chat_id for group play
user_games = {} 

# --- DATABASE INTERFACE (POSTGRESQL - NEON TECH) ---

def db_connect():
    """Establishes a connection to the PostgreSQL database."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set. Database functions will fail.")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Error connecting to the database: {e}")
        return None

def db_init():
    """Initializes DB connection and creates necessary tables (scores, chats)."""
    conn = db_connect()
    if not conn:
        logger.warning("DB connection failed. Leaderboard/Broadcast will fail without DB.")
        return 
        
    try:
        with conn:
            with conn.cursor() as cur:
                # 1. Scores Table (for Leaderboard)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scores (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        user_name TEXT NOT NULL,
                        points INTEGER NOT NULL,
                        chat_id BIGINT NOT NULL,
                        recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 2. Chats Table (for Broadcast)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id BIGINT PRIMARY KEY,
                        chat_title TEXT,
                        added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
    finally:
        if conn: conn.close()

def db_add_score(user_id, name, points, chat_id):
    """Adds a score entry to the database."""
    conn = db_connect()
    if not conn: return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scores (user_id, user_name, points, chat_id) 
                    VALUES (%s, %s, %s, %s);
                """, (user_id, name, points, chat_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Error adding score: {e}")
    finally:
        if conn: conn.close()

def db_add_chat_id(chat_id, chat_title):
    """Adds or updates chat ID for broadcasting."""
    conn = db_connect()
    if not conn: return
    try:
        with conn:
            with conn.cursor() as cur:
                # Use UPSERT to insert if new, or do nothing if exists
                cur.execute("""
                    INSERT INTO chats (chat_id, chat_title) VALUES (%s, %s)
                    ON CONFLICT (chat_id) DO NOTHING;
                """, (chat_id, chat_title))
            conn.commit()
    except Exception as e:
        logger.error(f"Error adding chat ID: {e}")
    finally:
        if conn: conn.close()

def db_get_leaderboard(time_filter, scope, chat_id):
    """Fetches and aggregates leaderboard data from the database."""
    conn = db_connect()
    if not conn: return [], {} # Return empty on DB error
    
    time_condition = ""
    if time_filter == 'today':
        time_condition = "AND recorded_at >= CURRENT_DATE"
    elif time_filter == 'week':
        time_condition = "AND recorded_at >= CURRENT_DATE - INTERVAL '7 days'"
        
    scope_condition = ""
    if scope == 'local':
        scope_condition = f"AND chat_id = {chat_id}"

    query = sql.SQL("""
        SELECT 
            user_id, 
            user_name, 
            SUM(points) as total_points
        FROM 
            scores
        WHERE
            1=1 {scope_condition} {time_condition}
        GROUP BY 
            user_id, user_name
        ORDER BY 
            total_points DESC
        LIMIT 10;
    """).format(scope_condition=sql.SQL(scope_condition), time_condition=sql.SQL(time_condition))
    
    sorted_scores = []
    user_names = {}
    
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query)
                results = cur.fetchall()
                for user_id, user_name, total_points in results:
                    sorted_scores.append((user_id, total_points))
                    user_names[user_id] = user_name
    except Exception as e:
        logger.error(f"Error fetching leaderboard: {e}")
    finally:
        if conn: conn.close()
        
    return sorted_scores, user_names
    
def db_get_all_chat_ids():
    """Returns a list of all chat IDs for broadcasting."""
    conn = db_connect()
    if not conn: return []
    chat_ids = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id FROM chats;")
                chat_ids = [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching all chat IDs: {e}")
    finally:
        if conn: conn.close()
    return chat_ids

# --- Game Logic ---

def get_random_5_letter_word():
    """Fetches a random word."""
    try:
        response = requests.get("https://random-word-api.herokuapp.com/word?length=5")
        if response.status_code == 200:
            return response.json()[0].upper()
    except:
        pass
    return random.choice(["APPLE", "BRAIN", "CHAIR", "DREAM", "EAGLE", "GHOST", "LIGHT", "MUSIC"]).upper()

def format_guess_result(target, guess):
    """
    Generates the colored emoji string (🟩, 🟨, 🟥).
    """
    target_list = list(target)
    guess_list = list(guess)
    result_emoji = [""] * 5
    
    # 1. Green Check (Correct Position)
    for i in range(5):
        if guess_list[i] == target_list[i]:
            result_emoji[i] = "🟩"
            target_list[i] = None 
            guess_list[i] = None
            
    # 2. Yellow/Red Check
    for i in range(5):
        if result_emoji[i] == "":
            if guess_list[i] is not None and guess_list[i] in target_list:
                result_emoji[i] = "🟨"
                target_list[target_list.index(guess_list[i])] = None
            else:
                result_emoji[i] = "🟥" 
    
    return "".join(result_emoji)

# --- Leaderboard Logic (English & Designer) ---

def get_leaderboard_text(time_frame, scope, chat_id):
    # Fetch data using DB structure
    sorted_scores, user_names = db_get_leaderboard(time_frame, scope, chat_id)

    scope_txt = "🌍 Global Rankings" if scope == 'global' else "🏠 Local Chat Rankings"
    time_labels = {'today': "📅 Today's Elite", 'week': "📆 Weekly Warriors", 'all': "⏳ All-Time Legends"}
    time_txt = time_labels.get(time_frame, "")

    text = (
        "👑 **ULTIMATE WORD SEEK LEADERBOARD** 👑\n"
        f"*{scope_txt}* • *{time_txt}*\n"
        "=============================\n"
    )

    if not sorted_scores:
        text += "No scores recorded yet. Start your challenge with **/game**! 🎮\n"
        text += "=============================\n"
        return text
    
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    for idx, (uid, score) in enumerate(sorted_scores, 1):
        icon = medals.get(idx, f"▪️ {idx}.")
        name = user_names.get(uid, "Unknown Player")
        
        # Applying quotes to the name and professional styling
        text += f"{icon} **`{name}`** - **{score}** Points\n"
        
    text += "=============================\n"

    return text

def get_leaderboard_markup(current_time, current_scope):
    new_scope = 'local' if current_scope == 'global' else 'global'
    scope_switch_txt = "🏠 Local Chat" if current_scope == 'global' else "🌍 Global"

    keyboard = [
        [
            InlineKeyboardButton("📅 Today", callback_data=f"lb_today_{current_scope}"),
            InlineKeyboardButton("📆 Week", callback_data=f"lb_week_{current_scope}"),
            InlineKeyboardButton("⏳ All Time", callback_data=f"lb_all_{current_scope}"),
        ],
        [
            InlineKeyboardButton(f"Switch to {scope_switch_txt}", callback_data=f"lb_{current_time}_{new_scope}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Command Handlers (All English) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Add chat ID to DB for broadcast
    if update.effective_chat.type != Chat.PRIVATE:
        db_add_chat_id(update.effective_chat.id, update.effective_chat.title or "Group Chat")
        
    await update.message.reply_text(
        (
            "✨ **Welcome to Word Seek — The Ultimate Word Challenge!** ✨\n\n"
            "🧠 **The Objective**\n"
            "Guess the secret **5-letter English word** and climb the ranks!\n\n"
            "🎮 **Quick Start Guide**\n"
            "• Initiate a new game by typing: `/game`\n"
            "• Submit your guess by simply sending a **5-letter word**.\n\n"
            "📊 **Point System**\n"
            "• 🟢 **Correct Word:** `+5 Points` (Victory)\n"
            "• 🔴 **Incorrect Guess:** `No penalty.` (😎 No Minus!)\n"
            "• ❌ **Invalid Word Length:** `Error / No Penalty`\n\n"
            "👑 **View the Elite:** `/leaderboard`"
        ),
        parse_mode='Markdown'
    )


async def game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    if chat_id in user_games and user_games[chat_id]["active"]:
        await update.message.reply_text("⚠️ **Error:** An active Word Seek game is already running in this chat! Just send your 5-letter guess to join.")
        return

    word = get_random_5_letter_word()
    
    user_games[chat_id] = {
        "word": word,
        "attempts": 0,
        "active": True,
        "history": [], 
        "guessed_words": set() 
    }
    
    await update.message.reply_text(
        "--- **WORD SEEK CHALLENGE INITIATED** ---\n"
        "🎯 **Target:** A 5-letter English word.\n"
        "⏱️ **Attempts:** Unlimited.\n\n"
        "**[ G O G O G ]**\n"
        "**[ L U C K ! ]**\n\n"
        "Enter your first 5-letter guess below to start the hunt! 🕵️‍♂️",
        parse_mode='Markdown'
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in user_games and user_games[chat_id]["active"]:
        word = user_games[chat_id]["word"]
        del user_games[chat_id]
        await update.message.reply_text(f"🛑 **Game Stopped.** The target word was: **{word}**")
    else:
        await update.message.reply_text("There is no active Word Seek game in this chat.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = get_leaderboard_text('today', 'global', update.effective_chat.id)
    markup = get_leaderboard_markup('today', 'global')
    await update.message.reply_text(text, reply_markup=markup, parse_mode='Markdown')

async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split('_')
    if len(data_parts) != 3: return
    
    _, time_frame, scope = data_parts
    
    text = get_leaderboard_text(time_frame, scope, update.effective_chat.id)
    markup = get_leaderboard_markup(time_frame, scope)
    
    try:
        await query.edit_message_text(text=text, reply_markup=markup, parse_mode='Markdown')
    except:
        pass
        
async def get_file_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    target_message = message.reply_to_message

    if not target_message:
        await message.reply_text(
            "❌ **Error:** Please reply to the media (Photo, Video, Document, etc.) "
            "you want the File ID for, and then use the `/getfileid` command.",
            parse_mode='Markdown'
        )
        return

    file_id = None
    file_type = "File"

    if target_message.photo:
        file_id = target_message.photo[-1].file_id
        file_type = "Photo"
    elif target_message.document:
        file_id = target_message.document.file_id
        file_type = "Document"
    elif target_message.video:
        file_id = target_message.video.file_id
        file_type = "Video"
    elif target_message.audio:
        file_id = target_message.audio.file_id
        file_type = "Audio"
    elif target_message.sticker:
        file_id = target_message.sticker.file_id
        file_type = "Sticker"
    elif target_message.voice:
        file_id = target_message.voice.file_id
        file_type = "Voice"

    if file_id:
        await message.reply_text(
            f"✅ **{file_type} File ID:**\n\n`{file_id}`\n\n"
            "You can use this ID in your code for sending media.",
            parse_mode='Markdown'
        )
    else:
        await message.reply_text(
            f"❌ **Error:** No recognized media found in the replied message. (Found: {target_message.content_type})",
            parse_mode='Markdown'
        )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message to all recorded chats (Owner only) via reply."""
    if update.effective_user.id != AADII_USER_ID:
        await update.message.reply_text("⛔️ **Access Denied:** Only the bot owner can use this command.")
        return

    message_to_broadcast = update.effective_message.reply_to_message
    
    if not message_to_broadcast:
        await update.message.reply_text(
            "Usage: Reply to the message (text/photo/video/etc.) you want to broadcast and use the `/broadcast` command."
        )
        return
        
    chat_ids = db_get_all_chat_ids()
    
    success_count = 0
    total_chats = len(chat_ids)
    
    # Identify content type with robust check
    try:
        content_type = message_to_broadcast.content_type
    except AttributeError:
        logger.error(f"Failed to get content_type for replied message ID {message_to_broadcast.message_id}. Object is likely malformed.")
        await update.message.reply_text(
            "❌ **Error:** Cannot broadcast the type of message you replied to. Please try a text, photo, or video.", 
            parse_mode='Markdown'
        )
        return
    
    # The prefix for all broadcast messages
    caption_prefix = f"📣 **BROADCAST MESSAGE:**\n"
    
    for chat_id in chat_ids:
        # Check if it's the current chat (to avoid sending the broadcast back)
        if chat_id == update.effective_chat.id:
            continue

        try:
            # Broadcast the message based on its content type
            if content_type == 'text':
                text = f"{caption_prefix}{message_to_broadcast.text}"
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
            elif content_type == 'photo':
                caption_text = f"{caption_prefix}{message_to_broadcast.caption or ''}"
                await context.bot.send_photo(
                    chat_id=chat_id, 
                    photo=message_to_broadcast.photo[-1].file_id, 
                    caption=caption_text,
                    parse_mode='Markdown'
                )
            elif content_type == 'video':
                caption_text = f"{caption_prefix}{message_to_broadcast.caption or ''}"
                await context.bot.send_video(
                    chat_id=chat_id, 
                    video=message_to_broadcast.video.file_id, 
                    caption=caption_text,
                    parse_mode='Markdown'
                )
            elif content_type == 'document':
                caption_text = f"{caption_prefix}{message_to_broadcast.caption or ''}"
                await context.bot.send_document(
                    chat_id=chat_id, 
                    document=message_to_broadcast.document.file_id, 
                    caption=caption_text,
                    parse_mode='Markdown'
                )
            else:
                # Fallback for unhandled media types (sticker, audio, voice, service messages)
                await context.bot.forward_message(
                    chat_id=chat_id, 
                    from_chat_id=update.effective_chat.id, 
                    message_id=message_to_broadcast.message_id
                )

            success_count += 1
            await asyncio.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Broadcast failed for chat {chat_id} ({content_type}): {e}")
            
    await update.message.reply_text(
        f"✅ **Broadcast Complete!**\nSent `{content_type.upper()}` content to {success_count}/{total_chats} chats."
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and notify the bot owner (Aadii)."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if update and update.effective_chat and update.effective_user and update.effective_user.id == AADII_USER_ID:
        error_message = f"🚨 **System Error Detected (Owner Alert)** 🚨\n\nDetails:\n`{context.error}`"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=error_message,
            parse_mode='Markdown'
        )
    elif update and update.effective_chat:
        # Generic message for group/other users
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ An unexpected error occurred. The system owner has been notified."
        )


# --- MAIN MESSAGE PROCESSOR (The Brain) ---

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # chat_type variable is no longer needed since Ajwa logic is removed
    text = update.effective_message.text.strip().upper()

    # --- 1. GAME LOGIC (Runs in ALL chats) ---
    is_game_active = chat_id in user_games and user_games[chat_id]["active"]
    
    if is_game_active and len(text) == 5 and text.isalpha():
        
        game = user_games[chat_id]
        
        # --- CHECK: Word Already Guessed ---
        if text in game["guessed_words"]:
            await update.message.reply_text(
                f"❌ **Error:** The word `**{text}**` has already been guessed by someone else in this game. Try a new word! 💡",
                parse_mode='Markdown'
            )
            return

        # Add word to guessed set before processing
        game["guessed_words"].add(text) 
        
        # Continue with game logic
        game["attempts"] += 1
        
        result_emoji = format_guess_result(game["word"], text)
        game["history"].append((text, result_emoji)) 
        
        # --- SCORING LOGIC UPDATE: No negative points ---
        score_change = 5 if text == game["word"] else 0 
        
        # Fetch current total score from DB (for display)
        user_scores_data, _ = db_get_leaderboard('all', 'global', chat_id) 
        current_total_score = next((score for uid, score in user_scores_data if uid == user_id), 0)
        
        # --- Generate PREMIUM Display ---
        # Compact header
        display_message = "🧩 **WORD SEEK CHALLENGE**\n━━━━━━━━━━━━━━━━━━━\n" 
        
        # Side-by-Side Guess History (EMOJI LEFT, WORD RIGHT)
        for guessed_word, emoji_res in game["history"]:
            blocks_display = " ".join(list(emoji_res))
            display_message += f"{blocks_display} **{guessed_word}**\n"
        
        display_message += f"\nAttempts: **{game['attempts']}** | Score: **{current_total_score + score_change} pts**"
        
        # WIN
        if text == game["word"]:
            # Record score (only +5)
            db_add_score(user_id, update.effective_user.first_name, 5, chat_id)
            
            del user_games[chat_id] # Delete game using chat_id
            
            # Recalculate final score for win message
            final_score_data, _ = db_get_leaderboard('all', 'global', chat_id)
            final_total_score = next((score for uid, score in final_score_data if uid == user_id), 5)
            
            # --- SHANDAR WIN MESSAGE (English) ---
            win_message = (
                f"🏆 **SPECTACULAR VICTORY! CHALLENGE CONQUERED!** 👑\n\n"
                f"✅ **{update.effective_user.first_name} solved it in {game['attempts']} attempts!**\n"
                f"💰 **Reward:** +5 Points awarded!\n"
                f"New Total Score: **{final_total_score} pts**\n\n"
                f"Final Board:\n━━━━━━━━━━━━━━━━━━━\n"
            )
            for guessed_word, emoji_res in game["history"]:
                blocks_display = " ".join(list(emoji_res))
                win_message += f"{blocks_display} **{guessed_word}**\n"
            
            win_message += "\nReady for the next round? Start another game instantly with **/game**! 🎮"
            
            await update.message.reply_text(win_message, parse_mode='Markdown')
                
        # INCORRECT
        else:
            # score_change is 0, so no db_add_score call here.
            await update.message.reply_text(display_message, parse_mode='Markdown')
        return 
        
    elif is_game_active and (len(text) != 5 or not text.isalpha()):
        # Simple error message for wrong format
        await update.message.reply_text(
            f"❌ **Error:** Please enter exactly **5 letters** (A-Z) to make a guess. 🤔",
            parse_mode='Markdown'
        )
        return

    # Ajwa/Gemini logic removed

# --- Main ---

def main() -> None:
    # Initialize DB (This will create tables if they don't exist)
    db_init() 
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("game", game_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command)) 
    application.add_handler(CommandHandler("getfileid", get_file_id_command)) 
    application.add_handler(CommandHandler("broadcast", broadcast_command)) 

    # Buttons
    application.add_handler(CallbackQueryHandler(leaderboard_callback, pattern="^lb_"))

    # Message Handler (Only for Game Guess)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))

    # Global Error Handler
    application.add_error_handler(error_handler)

    if WEBHOOK_URL:
        PORT = int(os.getenv("PORT", "8000"))
        application.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
        )
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
