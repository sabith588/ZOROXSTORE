# -*- coding: utf-8 -*-
import os
import io
import time
import asyncio
import logging
import sqlite3
import qrcode
from PIL import Image
from dotenv import load_dotenv
from imap_tools import MailBox, AND
from datetime import datetime, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# This imports the Flask server to trick Render's Free Tier
from keep_alive import keep_alive

# --- 1. CONFIG & LOGGING ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0")) 
API_HASH = os.getenv("API_HASH") 
ADMIN_ID = 8861377143

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. ADVANCED DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_banned INTEGER DEFAULT 0, ref_by INTEGER, ref_bonus_claimed INTEGER DEFAULT 0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT, title TEXT, session_string TEXT,
            price REAL DEFAULT 50.0, is_sold INTEGER DEFAULT 0, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS claimed_emails (email_uid TEXT PRIMARY KEY, user_id INTEGER, amount REAL, user_input TEXT)")
    
    cursor.execute("CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, amount REAL, uses INTEGER DEFAULT 0, max_uses INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS claimed_promos (user_id INTEGER, code TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT, title TEXT, session_string TEXT, price REAL, bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

    try: cursor.execute("ALTER TABLE purchases ADD COLUMN category TEXT")
    except: pass

    defaults = {
        'upi_id': 'merchant@fam', 'upi_name': 'ZoroXStore', 'min_deposit': '10',
        'gmail_user': 'none@gmail.com', 'gmail_pass': 'none', 'maintenance_mode': '0',
        'support_username': '@Sabitheeyyy', 'store_name': 'ZoroX Store',
        'force_sub_channel': 'none', 'force_sub_link': 'none', 'receipt_channel': 'none', 'referral_bonus': '5.0',
        'flash_sale_active': '0', 'flash_sale_discount': '0', 'flash_sale_end': '0'
    }
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    
    cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))
    conn.commit()
    conn.close()

init_db()

# --- 3. DYNAMIC HELPERS ---
def get_set(key):
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def update_set(key, value):
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def is_admin(user_id):
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def get_vip_status(user_id):
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(price) FROM purchases WHERE user_id = ?", (user_id,))
    spent = cursor.fetchone()[0] or 0.0
    conn.close()
    
    if spent >= 1500: return 0.07, "🏆 Gold VIP", spent
    if spent >= 500: return 0.03, "🥈 Silver VIP", spent
    return 0.0, "Bronze", spent

def get_flash_sale():
    if get_set("flash_sale_active") == "1":
        end_time = float(get_set("flash_sale_end"))
        if time.time() < end_time:
            return float(get_set("flash_sale_discount")) / 100, end_time
        else:
            update_set("flash_sale_active", "0")
    return 0.0, 0

def calculate_final_price(base_price, user_id):
    vip_disc, _, _ = get_vip_status(user_id)
    flash_disc, _ = get_flash_sale()
    total_disc = vip_disc + flash_disc
    return max(0, base_price * (1 - total_disc))

async def send_receipt(context, text):
    channel = get_set('receipt_channel').strip()
    if channel and channel.lower() != 'none':
        if not channel.startswith('@') and not channel.startswith('-100'):
            channel = f"@{channel}"
        try: 
            await context.bot.send_message(
                chat_id=channel, 
                text=text, 
                parse_mode="HTML", 
                disable_notification=True 
            )
        except: pass

async def check_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    channel = get_set("force_sub_channel").strip()
    if channel.lower() == "none" or not channel: return True
    
    if "t.me/" in channel:
        channel = channel.split("t.me/")[1].split("/")[0].split("?")[0]
        
    if not channel.startswith('@') and not channel.startswith('-100'):
        channel = f"@{channel}"
        
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        if member.status in ['left', 'kicked']: return False
        return True
    except Exception as e:
        print(f"⚠️ Force Sub Check Failed! Target: {channel} | Error: {e}")
        return False

def generate_upi_qr(amount: float, note: str) -> io.BytesIO:
    upi_id = get_set("upi_id")
    upi_name = get_set("upi_name")
    upi_str = f"upi://pay?pa={upi_id}&pn={upi_name}&am={amount:.2f}&cu=INR&tn={note}"
    qr = qrcode.QRCode(box_size=10, border=1)
    qr.add_data(upi_str)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    
    try: poster = Image.open("Luffy QR Payment Poster.png").convert("RGBA")
    except:
        try: poster = Image.open("6231147405424398789.jpg").convert("RGBA")
        except:
            bio = io.BytesIO()
            qr_img.save(bio, format="PNG")
            bio.seek(0)
            return bio

    TARGET_SIZE = (434, 410)
    PASTE_POSITION = (83, 653)
    qr_resized = qr_img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)
    poster.paste(qr_resized, PASTE_POSITION, qr_resized)
    
    bio = io.BytesIO()
    poster.save(bio, format="PNG")
    bio.seek(0)
    return bio

def check_gmail_payment_live(amount, utr_or_txn):
    g_user = get_set("gmail_user")
    g_pass = get_set("gmail_pass")
    if g_user == 'none@gmail.com' or g_pass == 'none': return None

    try:
        with MailBox('imap.gmail.com').login(g_user, g_pass) as mailbox:
            for msg in mailbox.fetch('ALL', limit=10, reverse=True):
                sender = msg.from_.lower()
                body = (msg.text or "") + " " + (msg.html or "")
                if "fam" not in sender and "fampay" not in sender: continue
                if str(utr_or_txn) in body and str(amount) in body:
                    mailbox.flag(msg.uid, '\\Seen', True)
                    return str(msg.uid)
    except Exception as e:
        logger.error(f"IMAP Error: {e}")
    return None

async def get_acc_phone(session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        me = await client.get_me()
        await client.disconnect()
        return f"+{me.phone}" if me else "Unknown"
    except: return "Dead/Banned"

async def fetch_acc_otp(session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        otp_msg = "No recent OTP found."
        async for msg in client.iter_messages(777000, limit=3):
            if "code" in msg.text.lower() or "login" in msg.text.lower():
                otp_msg = msg.text
                break
        await client.disconnect()
        return otp_msg
    except Exception as e: return f"Error: {e}"

async def clear_acc_chats(session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        async for dialog in client.iter_dialogs():
            if dialog.id != 777000:
                try: await client.delete_dialog(dialog.id)
                except: pass
        await client.disconnect()
        return True
    except: return False

async def logout_acc_bot(session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        await client.log_out()
        return True
    except: return False

# --- 4. CORE BOT ROUTING ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if get_set("maintenance_mode") == "1" and not is_admin(user.id):
        await update.message.reply_text("🛠️ <b>System Upgrade</b>\n\nOur store is currently undergoing maintenance. Please check back shortly.", parse_mode="HTML")
        return

    ref_id = None
    if context.args and context.args[0].isdigit():
        ref_id = int(context.args[0])

    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance, is_banned, ref_by, ref_bonus_claimed FROM users WHERE user_id = ?", (user.id,))
    row = cursor.fetchone()
    
    if not row:
        if ref_id and ref_id != user.id: cursor.execute("INSERT INTO users (user_id, username, ref_by) VALUES (?, ?, ?)", (user.id, user.username, ref_id))
        else: cursor.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        conn.commit()
        balance, is_banned, db_ref_by, ref_bonus_claimed = 0.0, 0, ref_id, 0
    else:
        balance, is_banned, db_ref_by, ref_bonus_claimed = row[0], row[1], row[2], row[3]
        
    if is_banned == 1:
        await update.message.reply_text("🚫 <b>Access Revoked</b>\n\nYou are permanently banned from using this store.", parse_mode="HTML")
        conn.close()
        return

    if not is_admin(user.id):
        is_subbed = await check_force_sub(update, context, user.id)
        if not is_subbed:
            channel_id = get_set('force_sub_channel').strip()
            channel_link = get_set('force_sub_link').strip()
            
            if channel_link != 'none' and channel_link.startswith('http'): join_url = channel_link
            else:
                if not channel_id.startswith('@') and not channel_id.startswith('-100'): channel_id = f"@{channel_id}"
                join_url = f"https://t.me/{channel_id.replace('@', '')}"
                
            kb = [[InlineKeyboardButton("📢 Join Official Channel", url=join_url)],
                  [InlineKeyboardButton("✅ Verify Subscription", callback_data="check_sub")]]
            await update.message.reply_text("🛡️ <b>Security Check</b>\n\nYou must join our channel to access the store.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            conn.close()
            return

    if db_ref_by and ref_bonus_claimed == 0:
        bonus = float(get_set("referral_bonus"))
        if bonus > 0:
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, db_ref_by))
            cursor.execute("UPDATE users SET ref_bonus_claimed = 1 WHERE user_id = ?", (user.id,))
            conn.commit()
            try: await context.bot.send_message(chat_id=db_ref_by, text=f"🎉 <b>Referral Bonus Credited!</b>\n\nRs {bonus:.2f} has been added to your balance.", parse_mode="HTML")
            except: pass

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0] + 1240 
    conn.close()

    _, vip_badge, _ = get_vip_status(user.id)
    flash_disc, flash_end = get_flash_sale()
    
    store_name = get_set('store_name')
    welcome_text = (
        f"👋 <b>Welcome to {store_name}</b>\n\n"
        f"🛡️ <b>Trusted by:</b> {total_users:,}+ Users\n"
        f"👤 <b>Account:</b> {user.first_name} | {vip_badge}\n"
        f"💰 <b>Balance:</b> Rs {balance:.2f}\n"
    )
    
    if flash_disc > 0:
        hrs_left = int((flash_end - time.time()) / 3600)
        welcome_text += f"\n⚡ <b>FLASH SALE:</b> {int(flash_disc*100)}% OFF EVERYTHING! <i>({hrs_left}h remaining)</i>\n"

    welcome_text += f"\n<i>Select an option below:</i>"

    keyboard = [
        [InlineKeyboardButton("🛒 Browse Accounts", callback_data="buy_menu")],
        [InlineKeyboardButton("💳 Add Funds", callback_data="add_funds"), InlineKeyboardButton("👤 My Profile", callback_data="my_profile")],
        [InlineKeyboardButton("🎁 Refer & Earn", callback_data="referral"), InlineKeyboardButton("❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton("🎧 Contact Support", callback_data="support"), InlineKeyboardButton("📜 TOS", callback_data="tos")]
    ]
    if is_admin(user.id): keyboard.append([InlineKeyboardButton("👑 Executive Admin Panel", callback_data="admin_home")])

    if update.callback_query: await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else: await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "check_sub":
        await start(update, context)
        return

    # --- ADMIN PANEL ---
    if is_admin(user_id):
        if data.startswith("adm_reply_"):
            context.user_data['state'] = data
            target_id = data.split("_")[2]
            await query.message.reply_text(f"✍️ Type your reply to User ID {target_id} below:")
            return
            
        elif data == "admin_home":
            kb = [
                [InlineKeyboardButton("📊 Analytics", callback_data="adm_analytics"), InlineKeyboardButton("⚡ Flash Sale", callback_data="adm_flash")],
                [InlineKeyboardButton("📦 Add Stock", callback_data="adm_add_acc"), InlineKeyboardButton("🗑️ Del Stock", callback_data="adm_del_stock")],
                [InlineKeyboardButton("📢 Broadcast", callback_data="adm_cast"), InlineKeyboardButton("👥 Manage User", callback_data="adm_manage_user")],
                [InlineKeyboardButton("💰 Add Bal", callback_data="adm_add_bal"), InlineKeyboardButton("🎟️ Promos", callback_data="adm_promos")],
                [InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"), InlineKeyboardButton("📧 Payment Config", callback_data="adm_pay_config")],
                [InlineKeyboardButton("📢 Channels Config", callback_data="adm_chan_config"), InlineKeyboardButton("📥 Backup DB", callback_data="adm_backup")],
                [InlineKeyboardButton("🔙 Return to User View", callback_data="back_home")]
            ]
            await query.message.edit_text("👑 <b>Executive Admin Dashboard</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return
            
        elif data == "adm_analytics":
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            
            cursor.execute("SELECT SUM(CASE WHEN is_sold=1 THEN 1 ELSE 0 END), SUM(CASE WHEN is_sold=0 THEN 1 ELSE 0 END) FROM accounts")
            inv_stats = cursor.fetchone()
            sold, unsold = inv_stats[0] or 0, inv_stats[1] or 0
            
            cursor.execute("SELECT SUM(price) FROM purchases")
            all_time = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT SUM(price) FROM purchases WHERE date(bought_at) = date('now')")
            today_rev = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT SUM(price) FROM purchases WHERE date(bought_at) >= date('now', '-7 days')")
            week_rev = cursor.fetchone()[0] or 0.0
            
            cursor.execute("SELECT category, COUNT(*) FROM purchases GROUP BY category ORDER BY COUNT(*) DESC LIMIT 1")
            top_cat = cursor.fetchone()
            top_cat_str = f"{top_cat[0]} ({top_cat[1]} sales)" if top_cat else "N/A"
            
            conn.close()
            
            text = (
                f"📊 <b>Store Analytics</b>\n\n"
                f"💰 <b>Revenue:</b>\n"
                f"• Today: Rs {today_rev:.2f}\n"
                f"• This Week: Rs {week_rev:.2f}\n"
                f"• All-Time: Rs {all_time:.2f}\n\n"
                f"📦 <b>Inventory:</b>\n"
                f"• Sold: {sold}\n"
                f"• Active Stock: {unsold}\n\n"
                f"🏆 <b>Top Category:</b> {top_cat_str}"
            )
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]))
            return

        elif data == "adm_flash":
            context.user_data['state'] = "adm_flash_setup"
            text = "⚡ <b>Flash Sale Generator</b>\n\nReply with the discount percentage and duration in hours.\n<code>PERCENTAGE HOURS</code>\n\n<i>Example (15% off for 6 hours):</i>\n<code>15 6</code>\n\nTo cancel an active sale, send <code>0 0</code>."
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]))
            return

        elif data == "adm_backup":
            await query.message.reply_document(document=open('store.db', 'rb'), caption="📥 <b>Database Backup</b>\nHere is your latest store.db file.", parse_mode="HTML")
            return

        elif data == "adm_manage_user":
            context.user_data['state'] = "adm_manage_user"
            await query.message.edit_text("👥 <b>User Lookup</b>\n\nSend the User ID you want to manage:", parse_mode="HTML")
            return
            
        elif data.startswith("adm_ban_") or data.startswith("adm_unban_"):
            action, target_id = data.split("_")[1], int(data.split("_")[2])
            val = 1 if action == "ban" else 0
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (val, target_id))
            conn.commit()
            conn.close()
            await query.answer(f"User {action}ned successfully!", show_alert=True)
            return

        elif data == "adm_promos":
            context.user_data['state'] = "adm_create_promo"
            text = "🎟️ <b>Promo Code Manager</b>\n\nTo create a promo code, reply with the format:\n<code>CODE AMOUNT MAX_USES</code>\n\n<i>Example (Gives Rs 50 to 100 users):</i>\n<code>NEWYEAR 50 100</code>"
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]))
            return

        elif data == "adm_del_stock":
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("SELECT category, COUNT(*) FROM accounts WHERE is_sold = 0 GROUP BY category")
            categories = cursor.fetchall()
            conn.close()
            kb = []
            for cat, count in categories: kb.append([InlineKeyboardButton(f"📁 {cat} ({count})", callback_data=f"delcat_{cat}_0")])
            kb.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin_home")])
            await query.message.edit_text("🗑️ <b>Delete Stock</b>\n\nSelect a category to view items:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return

        elif data.startswith("delcat_"):
            parts = data.split("_")
            cat_name = parts[1]
            page = int(parts[2])
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM accounts WHERE category = ? AND is_sold = 0 LIMIT 10 OFFSET ?", (cat_name, page * 10))
            items = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) FROM accounts WHERE category = ? AND is_sold = 0", (cat_name,))
            total = cursor.fetchone()[0]
            conn.close()
            
            kb = [[InlineKeyboardButton(f"❌ Delete: {t}", callback_data=f"delitem_{i}")] for i, t in items]
            
            nav = []
            if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"delcat_{cat_name}_{page-1}"))
            if (page + 1) * 10 < total: nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"delcat_{cat_name}_{page+1}"))
            if nav: kb.append(nav)
                
            kb.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="adm_del_stock")])
            await query.message.edit_text(f"🗑️ <b>Delete items in {cat_name}</b> (Page {page+1}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return

        elif data.startswith("delitem_"):
            item_id = int(data.split("_")[1])
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM accounts WHERE id = ?", (item_id,))
            conn.commit()
            conn.close()
            await query.answer("Item permanently deleted!", show_alert=True)
            await query.message.edit_text("✅ Item Deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_del_stock")]]))
            return
            
        elif data == "adm_pay_config":
            kb = [
                [InlineKeyboardButton("Gmail ID", callback_data="set_g_id"), InlineKeyboardButton("App Password", callback_data="set_g_pass")], 
                [InlineKeyboardButton("UPI ID", callback_data="set_u_id"), InlineKeyboardButton("Min Deposit", callback_data="set_u_min")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin_home")]
            ]
            text = f"💳 <b>Payment Config</b>\n\nGmail: <code>{get_set('gmail_user')}</code>\nUPI: <code>{get_set('upi_id')}</code>\nMin Dep: Rs {get_set('min_deposit')}"
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return
            
        elif data == "adm_chan_config":
            kb = [
                [InlineKeyboardButton("Set Force Sub Channel (ID/@)", callback_data="set_force_sub_channel")],
                [InlineKeyboardButton("Set Force Sub Link (URL)", callback_data="set_force_sub_link")],
                [InlineKeyboardButton("Set Receipt Channel", callback_data="set_receipt_channel")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin_home")]
            ]
            text = f"📢 <b>Channels Config</b>\n\nForce Sub ID: <code>{get_set('force_sub_channel')}</code>\nForce Sub Link: <code>{get_set('force_sub_link')}</code>\nReceipts: <code>{get_set('receipt_channel')}</code>"
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return
            
        elif data == "adm_settings":
            kb = [
                [InlineKeyboardButton("Store Name", callback_data="set_store_name"), InlineKeyboardButton("Support ID", callback_data="set_support_username")],
                [InlineKeyboardButton("Referral Bonus", callback_data="set_referral_bonus")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="admin_home")]
            ]
            text = f"⚙️ <b>Settings</b>\n\nName: {get_set('store_name')}\nSupport: {get_set('support_username')}\nRef Bonus: Rs {get_set('referral_bonus')}"
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return
            
        elif data == "adm_add_acc":
            context.user_data['state'] = "adm_add_acc"
            await query.message.edit_text("✍️ Send account details:\n<code>Category | Title | Price | Session_String</code>", parse_mode="HTML")
            return
        elif data == "adm_cast":
            context.user_data['state'] = "adm_cast"
            await query.message.edit_text("📢 Send the broadcast message:", parse_mode="HTML")
            return
        elif data == "adm_add_bal":
            context.user_data['state'] = "adm_add_bal"
            await query.message.edit_text("💰 Send User ID and Amount:\nExample: <code>123456789 50</code>", parse_mode="HTML")
            return
        elif data.startswith("set_"):
            context.user_data['state'] = data
            await query.message.reply_text("✍️ Please send the new value:")
            return

    # --- USER MENU & STORE LOGIC ---
    if data == "buy_menu":
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT category, COUNT(*) FROM accounts WHERE is_sold = 0 GROUP BY category")
        categories = cursor.fetchall()
        conn.close()
        
        kb = []
        if categories:
            for cat, count in categories: kb.append([InlineKeyboardButton(f"📁 {cat} ({count} Available)", callback_data=f"showcat_{cat}_0")])
            text = "🛒 <b>Premium Inventory</b>\nSelect a category:"
        else: text = "🛒 <b>Out of Stock!</b>"
            
        kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data.startswith("showcat_"):
        parts = data.split("_")
        cat_name = parts[1]
        page = int(parts[2])
        
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, price FROM accounts WHERE category = ? AND is_sold = 0 LIMIT 10 OFFSET ?", (cat_name, page * 10))
        items = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM accounts WHERE category = ? AND is_sold = 0", (cat_name,))
        total = cursor.fetchone()[0]
        conn.close()
        
        kb = []
        for i, t, p in items:
            final_p = calculate_final_price(p, user_id)
            if final_p < p:
                btn_txt = f"✨ {t} - Rs {final_p:.2f} (Sale)"
            else:
                btn_txt = f"✨ {t} - Rs {p:.2f}"
            kb.append([InlineKeyboardButton(btn_txt, callback_data=f"buyacc_{i}")])
        
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"showcat_{cat_name}_{page-1}"))
        if (page + 1) * 10 < total: nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"showcat_{cat_name}_{page+1}"))
        if nav: kb.append(nav)
            
        kb.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="buy_menu")])
        await query.message.edit_text(f"📂 <b>{cat_name}</b> (Page {page+1})\nSelect package:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("buyacc_"):
        acc_id = int(data.split("_")[1])
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT title, price, category FROM accounts WHERE id = ? AND is_sold = 0", (acc_id,))
        acc = cursor.fetchone()
        conn.close()
        if not acc: return await query.answer("❌ Sold out!", show_alert=True)
        
        base_price = acc[1]
        final_price = calculate_final_price(base_price, user_id)
        
        text = f"🛍️ <b>Checkout</b>\n\n🧾 Item: {acc[0]}\n"
        if final_price < base_price:
            text += f"💳 Base Price: <s>Rs {base_price:.2f}</s>\n🔥 <b>Your Price: Rs {final_price:.2f}</b>"
        else:
            text += f"💳 Price: Rs {base_price:.2f}"
            
        kb = [[InlineKeyboardButton("✅ Confirm & Pay", callback_data=f"confirm_{acc_id}")], [InlineKeyboardButton("❌ Cancel", callback_data=f"showcat_{acc[2]}_0")] ]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("confirm_"):
        acc_id = int(data.split("_")[1])
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance = cursor.fetchone()[0]
        cursor.execute("SELECT title, price, session_string, category FROM accounts WHERE id = ? AND is_sold = 0", (acc_id,))
        acc = cursor.fetchone()
        
        if not acc: return await query.answer("❌ Error: Account sold out.", show_alert=True)
        title, base_price, session_str, category = acc[0], float(acc[1]), acc[2], acc[3]
        
        final_price = calculate_final_price(base_price, user_id)
        
        if balance < final_price: return await query.answer(f"❌ Insufficient Balance!", show_alert=True)
            
        cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (final_price, user_id))
        cursor.execute("UPDATE accounts SET is_sold = 1 WHERE id = ?", (acc_id,))
        cursor.execute("INSERT INTO purchases (user_id, category, title, session_string, price) VALUES (?, ?, ?, ?, ?)", (user_id, category, title, session_str, final_price))
        conn.commit()
        conn.close()
        
        await send_receipt(context, f"🛒 <b>Verified Purchase</b>\n\n<b>Item:</b> {title}\n<b>Value:</b> Rs {final_price:.2f}\n<b>Store:</b> {get_set('store_name')}")

        context.user_data['managing_session'] = session_str
        wait_msg = await query.message.edit_text("🔄 <b>Connecting to Account securely via Telethon... Please wait.</b>", parse_mode="HTML")
        
        phone = await get_acc_phone(session_str)
        kb = [
            [InlineKeyboardButton("📬 Get OTP", callback_data="mgr_otp"), InlineKeyboardButton("🔄 Refresh", callback_data="mgr_otp")],
            [InlineKeyboardButton("🧹 Clear All Chats", callback_data="mgr_clear")],
            [InlineKeyboardButton("🚪 Bot Logout", callback_data="mgr_logout")],
            [InlineKeyboardButton("🔙 Store Main Menu", callback_data="back_home")]
        ]
        text = f"📱 <b>Account Manager</b>\n\n<b>Item:</b> {title}\n<b>Phone Number:</b> <code>{phone}</code>\n\n<b>Session String (Backup):</b>\n<code>{session_str}</code>"
        await wait_msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    # --- PURCHASE HISTORY ---
    elif data == "my_purchases":
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, price, bought_at FROM purchases WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
        purchases = cursor.fetchall()
        conn.close()
        
        if not purchases:
            return await query.answer("You haven't purchased anything yet.", show_alert=True)
            
        kb = [[InlineKeyboardButton(f"🧾 {t} (Rs {p:.2f})", callback_data=f"view_pur_{i}")] for i, t, p, d in purchases]
        kb.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="my_profile")])
        await query.message.edit_text("📜 <b>My Purchase History</b>\n\nClick on an account below to view its session string and open the Account Manager:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("view_pur_"):
        pur_id = int(data.split("_")[2])
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT title, session_string FROM purchases WHERE id = ? AND user_id = ?", (pur_id, user_id))
        pur = cursor.fetchone()
        conn.close()
        
        if not pur: return await query.answer("Error locating purchase.", show_alert=True)
        title, session_str = pur[0], pur[1]
        
        context.user_data['managing_session'] = session_str
        wait_msg = await query.message.edit_text("🔄 <b>Re-Connecting to Account securely via Telethon...</b>", parse_mode="HTML")
        phone = await get_acc_phone(session_str)
        
        kb = [
            [InlineKeyboardButton("📬 Get OTP", callback_data="mgr_otp"), InlineKeyboardButton("🔄 Refresh", callback_data="mgr_otp")],
            [InlineKeyboardButton("🧹 Clear All Chats", callback_data="mgr_clear")],
            [InlineKeyboardButton("🚪 Bot Logout", callback_data="mgr_logout")],
            [InlineKeyboardButton("🔙 Back to Purchases", callback_data="my_purchases")]
        ]
        text = f"📱 <b>Account Manager (Recovered)</b>\n\n<b>Item:</b> {title}\n<b>Phone Number:</b> <code>{phone}</code>\n\n<b>Session String (Backup):</b>\n<code>{session_str}</code>"
        await wait_msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    # --- PROMO REDEMPTION ---
    elif data == "redeem_promo":
        context.user_data['state'] = "user_redeem_promo"
        await query.message.edit_text("🎟️ <b>Redeem Promo Code</b>\n\nPlease send your promo code below:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="my_profile")]]))

    # --- SUPPORT TICKET SYSTEM ---
    elif data == "support":
        context.user_data['state'] = "user_support_ticket"
        await query.message.edit_text("🎧 <b>Contact Support</b>\n\nPlease type your message or issue below. Our team will review it and reply directly through this bot.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_home")]]))

    # --- ACCOUNT MANAGER BUTTONS ---
    elif data == "mgr_otp":
        session = context.user_data.get('managing_session')
        if not session: return await query.answer("❌ Session expired.", show_alert=True)
        await query.answer("Fetching latest OTP...")
        otp_text = await fetch_acc_otp(session)
        kb = [
            [InlineKeyboardButton("📬 Get OTP", callback_data="mgr_otp"), InlineKeyboardButton("🔄 Refresh", callback_data="mgr_otp")],
            [InlineKeyboardButton("🧹 Clear All Chats", callback_data="mgr_clear")],
            [InlineKeyboardButton("🚪 Bot Logout", callback_data="mgr_logout")],
            [InlineKeyboardButton("🔙 Store Main Menu", callback_data="back_home")]
        ]
        msg = f"📱 <b>Account Manager</b>\n\n<b>Latest Telegram Message:</b>\n<code>{otp_text}</code>"
        await query.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "mgr_clear":
        session = context.user_data.get('managing_session')
        if not session: return await query.answer("❌ Session expired.", show_alert=True)
        await query.answer("🧹 Clearing chats... This may take a moment.")
        success = await clear_acc_chats(session)
        if success: await query.answer("✅ All chats cleared successfully!", show_alert=True)
        else: await query.answer("❌ Failed to clear chats.", show_alert=True)

    elif data == "mgr_logout":
        session = context.user_data.get('managing_session')
        if not session: return await query.answer("❌ Session expired.", show_alert=True)
        await logout_acc_bot(session)
        context.user_data['managing_session'] = None
        await query.message.edit_text("🚪 <b>Logged Out</b>\n\nThe bot has successfully disconnected from the session.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_home")]]))

    # --- STANDARD MENU ITEMS ---
    elif data == "my_profile":
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT balance, joined_at FROM users WHERE user_id = ?", (user_id,))
        u = cursor.fetchone()
        conn.close()
        
        _, vip_badge, total_spent = get_vip_status(user_id)
        
        kb = [
            [InlineKeyboardButton("📜 My Purchases", callback_data="my_purchases"), InlineKeyboardButton("🎟️ Redeem Promo", callback_data="redeem_promo")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]
        ]
        await query.message.edit_text(f"👤 <b>Client Dashboard</b>\n\n<b>ID:</b> <code>{user_id}</code>\n<b>Wallet:</b> Rs {u[0]:.2f}\n<b>Total Spent:</b> Rs {total_spent:.2f}\n<b>Status:</b> {vip_badge}\n<b>Joined:</b> {u[1].split()[0]}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "faq":
        text = "❓ <b>Frequently Asked Questions</b>\n\n<b>Q: How to buy?</b>\nA: Add funds via UPI, then Browse Accounts to purchase.\n\n<b>Q: Delivery time?</b>\nA: Instant."
        kb = [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "tos":
        text = "📜 <b>Terms of Service</b>\n\n1. All sales are final.\n2. Ensure you secure your account after purchase."
        kb = [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "referral":
        bot_user = context.bot.username
        ref_link = f"https://t.me/{bot_user}?start={user_id}"
        bonus = get_set('referral_bonus')
        text = f"🎁 <b>Affiliate Program</b>\n\nEarn <b>Rs {bonus}</b> for every new user you invite!\n\n<b>Your Link:</b>\n<code>{ref_link}</code>"
        kb = [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "add_funds":
        context.user_data['state'] = 'waiting_dep'
        await query.message.reply_text(f"💳 Enter deposit amount (Min: Rs {get_set('min_deposit')}):")
        
    elif data.startswith("paid_"):
        amount = data.split("_")[1]
        context.user_data['state'] = f'waiting_utr_{amount}'
        await query.message.reply_text("🧾 Enter your 12-digit UTR or Transaction ID:")
        
    elif data == "back_home":
        await start(update, context)

async def handle_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # --- DATABASE SYNC RESTORATION ENGINE ---
    if update.message.document:
        if is_admin(user_id) and update.message.document.file_name == 'store.db':
            wait_msg = await update.message.reply_text("🔄 Downloading and syncing database...")
            try:
                file = await context.bot.get_file(update.message.document.file_id)
                await file.download_to_drive("store.db")
                await wait_msg.edit_text("✅ <b>Database Sync Complete!</b>\n\nThe store data has been successfully overwritten and restored.", parse_mode="HTML")
            except Exception as e:
                await wait_msg.edit_text(f"❌ Failed to sync database: {e}")
        return

    text = update.message.text
    if not text: return
    text = text.strip()
    state = context.user_data.get('state')

    if state == "user_support_ticket":
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"📩 <b>New Support Ticket</b>\n\n<b>From ID:</b> <code>{user_id}</code>\n<b>User:</b> @{update.effective_user.username}\n\n<b>Message:</b>\n{text}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Reply to User", callback_data=f"adm_reply_{user_id}")]]))
        context.user_data['state'] = None
        return await update.message.reply_text("✅ <b>Ticket Sent</b>\n\nOur team has received your message and will reply shortly.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]))

    if state == "user_redeem_promo":
        code = text.upper()
        conn = sqlite3.connect("store.db")
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM claimed_promos WHERE user_id = ? AND code = ?", (user_id, code))
        if cursor.fetchone():
            conn.close()
            return await update.message.reply_text("❌ You have already redeemed this promo code.")
            
        cursor.execute("SELECT amount, uses, max_uses FROM promos WHERE code = ?", (code,))
        promo = cursor.fetchone()
        
        if not promo:
            conn.close()
            return await update.message.reply_text("❌ Invalid promo code.")
            
        amount, uses, max_uses = promo[0], promo[1], promo[2]
        
        if uses >= max_uses:
            conn.close()
            return await update.message.reply_text("❌ This promo code has reached its maximum usage limit.")
            
        cursor.execute("UPDATE promos SET uses = uses + 1 WHERE code = ?", (code,))
        cursor.execute("INSERT INTO claimed_promos (user_id, code) VALUES (?, ?)", (user_id, code))
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        conn.close()
        
        context.user_data['state'] = None
        await update.message.reply_text(f"🎉 <b>Promo Redeemed!</b>\n\nRs {amount:.2f} has been added to your wallet.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]))
        return

    if is_admin(user_id) and state:
        if state.startswith("adm_reply_"):
            target_id = int(state.split("_")[2])
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🎧 <b>Support Team Reply:</b>\n\n{text}", parse_mode="HTML")
                await update.message.reply_text("✅ Reply sent to user successfully.")
            except:
                await update.message.reply_text("❌ Failed to send reply. The user might have blocked the bot.")
            context.user_data['state'] = None
            return

        if state == "adm_flash_setup":
            if text == "0 0":
                update_set("flash_sale_active", "0")
                context.user_data['state'] = None
                return await update.message.reply_text("✅ Flash sale deactivated.")
            
            try:
                perc, hrs = map(int, text.split())
                end_time = time.time() + (hrs * 3600)
                update_set("flash_sale_active", "1")
                update_set("flash_sale_discount", str(perc))
                update_set("flash_sale_end", str(end_time))
                context.user_data['state'] = None
                await update.message.reply_text(f"⚡ <b>Flash Sale Active!</b>\n\nDiscount: {perc}%\nDuration: {hrs} hours.", parse_mode="HTML")
                
                conn = sqlite3.connect("store.db")
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM users")
                users = cursor.fetchall()
                conn.close()
                for u in users:
                    try: await context.bot.send_message(chat_id=u[0], text=f"⚡ <b>FLASH SALE!</b>\n\nGet <b>{perc}% OFF</b> all premium accounts for the next {hrs} hours! Head to the store now to claim your discount.", parse_mode="HTML")
                    except: pass
            except:
                await update.message.reply_text("❌ Invalid format. Use: PERCENTAGE HOURS")
            return

        key_map = {
            "set_g_id": "gmail_user", "set_g_pass": "gmail_pass", "set_u_id": "upi_id",
            "set_u_min": "min_deposit", "set_store_name": "store_name",
            "set_support_username": "support_username", "set_force_sub_channel": "force_sub_channel",
            "set_force_sub_link": "force_sub_link", "set_receipt_channel": "receipt_channel", "set_referral_bonus": "referral_bonus"
        }
        
        if state in key_map:
            actual_key = key_map[state]
            update_set(actual_key, text)
            context.user_data['state'] = None
            await update.message.reply_text(f"✅ Configuration for `{actual_key}` updated to: {text}")
            return
            
        if state == "adm_manage_user":
            try:
                target_id = int(text)
                conn = sqlite3.connect("store.db")
                cursor = conn.cursor()
                cursor.execute("SELECT username, balance, joined_at, is_banned FROM users WHERE user_id = ?", (target_id,))
                u = cursor.fetchone()
                
                if not u:
                    conn.close()
                    return await update.message.reply_text("❌ User not found in database.")
                    
                cursor.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (target_id,))
                refs = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM purchases WHERE user_id = ?", (target_id,))
                purs = cursor.fetchone()[0]
                conn.close()
                
                status = "🔴 Banned" if u[3] == 1 else "🟢 Active"
                _, vip, spent = get_vip_status(target_id)
                
                msg = f"👤 <b>User Info:</b> <code>{target_id}</code>\n\n<b>Username:</b> @{u[0]}\n<b>Balance:</b> Rs {u[1]:.2f}\n<b>Total Spent:</b> Rs {spent:.2f} ({vip})\n<b>Purchases:</b> {purs}\n<b>Referrals:</b> {refs}\n<b>Status:</b> {status}"
                
                kb = [
                    [InlineKeyboardButton("🚫 Ban User", callback_data=f"adm_ban_{target_id}"), InlineKeyboardButton("✅ Unban User", callback_data=f"adm_unban_{target_id}")],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")]
                ]
                context.user_data['state'] = None
                await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            except ValueError:
                await update.message.reply_text("❌ Please send a valid numeric User ID.")
            return

        if state == "adm_create_promo":
            try:
                code, amt, mx = text.split()
                code, amt, mx = code.upper(), float(amt), int(mx)
                conn = sqlite3.connect("store.db")
                cursor = conn.cursor()
                cursor.execute("INSERT INTO promos (code, amount, max_uses) VALUES (?, ?, ?)", (code, amt, mx))
                conn.commit()
                conn.close()
                context.user_data['state'] = None
                await update.message.reply_text(f"✅ Promo code <b>{code}</b> created!\nValue: Rs {amt} | Max Uses: {mx}", parse_mode="HTML")
            except:
                await update.message.reply_text("❌ Format error. Send: CODE AMOUNT MAX_USES (e.g. ZOROX 50 100)")
            return

        if state == "adm_add_bal":
            try:
                target_user, amt = text.split()
                target_user, amt = int(target_user), float(amt)
                conn = sqlite3.connect("store.db")
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amt, target_user))
                if cursor.rowcount == 0:
                    cursor.execute("INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)", (target_user, "AddedByAdmin", amt))
                conn.commit()
                conn.close()
                context.user_data['state'] = None
                await update.message.reply_text(f"✅ Added Rs {amt} to User ID {target_user}.")
            except: await update.message.reply_text("❌ Invalid format. Use: USER_ID AMOUNT")
            return
            
        if state == "adm_add_acc":
            try:
                category, title, price, session_str = [x.strip() for x in text.split("|")]
                price = float(price)
                conn = sqlite3.connect("store.db")
                cursor = conn.cursor()
                cursor.execute("INSERT INTO accounts (category, title, price, session_string) VALUES (?, ?, ?, ?)", (category, title, price, session_str))
                conn.commit()
                conn.close()
                context.user_data['state'] = None
                await update.message.reply_text(f"✅ Account added to {category}.")
            except: await update.message.reply_text("❌ Error. Format: Category | Title | Price | Session_String")
            return
            
        if state == "adm_cast":
            context.user_data['state'] = None
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = cursor.fetchall()
            conn.close()
            
            success, fail = 0, 0
            wait_msg = await update.message.reply_text(f"🔄 Broadcasting to {len(users)} users...")
            for u in users:
                try:
                    await context.bot.send_message(chat_id=u[0], text=f"📢 <b>Announcement</b>\n\n{text}", parse_mode="HTML")
                    success += 1
                except: fail += 1
            await wait_msg.edit_text(f"✅ Broadcast Complete.\nSent: {success} | Failed: {fail}")
            return

    if state == 'waiting_dep':
        try: amount = float(text)
        except: return await update.message.reply_text("❌ Invalid input.")
        if amount < float(get_set('min_deposit')): return await update.message.reply_text(f"❌ Below minimum deposit.")
        
        context.user_data['state'] = None
        
        wait_msg = await update.message.reply_text("🔄 Generating UPI Invoice...")
        qr = await asyncio.to_thread(generate_upi_qr, amount, f"User_{user_id}")
        
        kb = [[InlineKeyboardButton("✅ I have paid (Submit UTR / TXN ID)", callback_data=f"paid_{amount}")]]
        await wait_msg.delete()
        await update.message.reply_photo(photo=qr, caption=f"🧾 <b>Amount Due:</b> Rs {amount}\n\nScan & Pay, then submit your UTR or TXN ID.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return

    if state and state.startswith("waiting_utr_"):
        amount = float(state.split("_")[2])
        utr = text.replace(" ", "")
        
        wait_msg = await update.message.reply_text("⏳ Securely verifying your payment with the banking gateway. Please hold...")
        email_uid = await asyncio.to_thread(check_gmail_payment_live, amount, utr)
        
        if email_uid:
            conn = sqlite3.connect("store.db")
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO claimed_emails (email_uid, user_id, amount, user_input) VALUES (?, ?, ?, ?)", (email_uid, user_id, amount, utr))
            if cursor.rowcount > 0:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
                conn.commit()
                await wait_msg.edit_text(f"✅ <b>Payment Verified!</b>\n\nRs {amount:.2f} credited to your wallet.", parse_mode="HTML")
                await send_receipt(context, f"💰 <b>New Deposit Verified!</b>\n\n<b>Amount:</b> Rs {amount:.2f}\n<b>User ID:</b> <code>{user_id}</code>")
            else: await wait_msg.edit_text("❌ <b>Security Alert</b>: This transaction has already been claimed.", parse_mode="HTML")
            conn.close()
        else: await wait_msg.edit_text("❌ <b>Payment not found.</b> Verify your UTR/Transaction ID, or wait 60 seconds for banking delays and try again.", parse_mode="HTML")
        return

def main():
    # Keep-Alive call triggers the Flask server for Render Free Tier
    keep_alive() 
    
    app = Application.builder().token(BOT_TOKEN).read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30).build()
    
    app.add_handler(CallbackQueryHandler(callback_router, pattern="^adm_reply_"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_message_input))
    app.run_polling()

if __name__ == "__main__":
    main()
