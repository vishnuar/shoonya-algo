import os
import re
import time
import json
import pyotp
import logging
import hashlib
import requests
import traceback
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Import Selenium components for background headless browser authorization
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service

# Import Telegram components for bot commands and polling loops
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ─── LOGGING CONFIGURATION ───────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global application reference to allow standard functions to dispatch Telegram alerts
tg_app = None

# ─── LOAD SYSTEM ENVIRONMENT VARIABLES ───────────────────────────────────────
#AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID"))
BASE_URL = "https://api.shoonya.com/NorenWClientTP"

CLIENT_ID   = os.getenv("SHOONYA_CLIENT_ID")    
USER_ID     = os.getenv("SHOONYA_USER_ID")      
PASSWORD    = os.getenv("SHOONYA_PASSWORD")     
TOTP_SECRET = os.getenv("SHOONYA_TOTP_SECRET")   
SECRET_CODE = os.getenv("SHOONYA_API_SECRET")    

# Endpoint template derived explicitly from Shoonya Support documentation
LOGIN_URL = f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key={CLIENT_ID}&route_to={USER_ID}"

# ─── IN-MEMORY STATE BOUNDARIES ──────────────────────────────────────────────
ACCESS_TOKEN = None
DAILY_TRADE_COUNT = 0
MAX_DAILY_TRADES = 6
LAST_TRADE_DATE = datetime.now().strftime("%Y-%m-%d")

# ─── INDEX SPECIFICATION CONSTANTS ───────────────────────────────────────────
# Standard contract size properties mapped for index execution routing
INDEX_MAP = {
    "N": {
        "name": "NIFTY",
        "exch": "NSE",
        "token": "26000",      # Nifty 50 Spot Token
        "lot_size": 65,        # Updated Nifty lot multiplier
        "strike_step": 50      # Strike price interval step
    },
    "S": {
        "name": "SENSEX",
        "exch": "BSE",
        "token": "1",          # Sensex Spot Token
        "lot_size": 20,        # Updated Sensex lot multiplier
        "strike_step": 100     # Strike price interval step
    }
}

# ─── TELEGRAM ERROR LOGGING DISPATCHER ────────────────────────────────────────
async def send_error_to_telegram(context_title: str, exception_obj: Exception):
    """Formats exceptions and traceback details and dispatches them to Telegram."""
    global tg_app
    tb_str = traceback.format_exc()
    error_msg = (
        f"🚨 **CRITICAL BOT ERROR LOGGED**\n\n"
        f"🔹 **Context:** {context_title}\n"
        f"🔹 **Error:** {str(exception_obj)}\n\n"
        f"📋 **Traceback Stack:**\n`{tb_str[-300:]}`"
    )
    logger.error(f"Sending error alert to Telegram: {context_title} - {str(exception_obj)}")
    if tg_app:
        try:
            await tg_app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=error_msg, parse_mode="Markdown")
        except Exception as telegram_err:
            logger.error(f"Failed transmitting alert via Telegram API channel: {telegram_err}")

# ─── BACKGROUND BROWSER AUTOMATION FUNCTIONS ──────────────────────────────────
def scan_network_for_code(driver) -> str:
    try:
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
                if message.get("method") == "Network.requestWillBeSent":
                    url = message.get("params", {}).get("request", {}).get("url", "")
                    if "code=" in url and "shoonya" in url.lower():
                        parsed = urlparse(url)
                        code = parse_qs(parsed.query).get("code", [None])[0]
                        if code:
                            return code
            except Exception:
                continue
    except Exception:
        pass
    return None

def fast_fill(driver, element, value: str):
    element.click()
    time.sleep(0.1)
    element.clear()
    element.send_keys(value)
    time.sleep(0.1)

def run_background_login() -> bool:
    global ACCESS_TOKEN
    logger.info("Initializing headless Chrome engine for silent session sign-in...")
    
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

# ─── FORCE NATIVE CHROMIUM PATHS FOR LINUX DAEMONS ──────────────────────
    options.binary_location = "/usr/bin/chromium"
    chrome_service = Service(executable_path="/usr/bin/chromedriver")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    driver = webdriver.Chrome(service=chrome_service, options=options)
    wait = WebDriverWait(driver, 30)
    auth_code = None
    
    try:
        driver.get(LOGIN_URL)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
        time.sleep(1)
        
        all_inputs = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
        visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]
        
        fast_fill(driver, visible_inputs[0], USER_ID)
        fast_fill(driver, visible_inputs[1], PASSWORD)
        
        otp_value = pyotp.TOTP(TOTP_SECRET).now()
        fast_fill(driver, visible_inputs[2], otp_value)
        
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))).click()
        logger.info("MFA Form signed off. Scanning network buffers for target tokens...")
        
        start_time = time.time()
        while True:
            auth_code = scan_network_for_code(driver)
            if auth_code:
                break
            if time.time() - start_time > 45:
                raise TimeoutError("OAuth loop sequence exceeded authorization window timing limits.")
            time.sleep(0.5)
            
        if auth_code:
            logger.info(f"Intercepted Auth Code: {auth_code}. Executing cryptographic signature step...")
            raw_string = f"{CLIENT_ID}{SECRET_CODE}{auth_code}"
            checksum = hashlib.sha256(raw_string.encode('utf-8')).hexdigest()
            
            token_url = "https://api.shoonya.com/NorenWClientTP/GenAcsTok"
            payload = f'jData={{"code":"{auth_code}","checksum":"{checksum}"}}'
            
            response = requests.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
            res_json = response.json()
            
            if res_json.get("stat") == "Ok" and "access_token" in res_json:
                ACCESS_TOKEN = res_json["access_token"]
                logger.info("New daily access token stored successfully into active process memory context.")
                return True
            else:
                raise ValueError(f"Token generation rejected by broker architecture: {res_json.get('emsg', res_json)}")
    except Exception as e:
        logger.error(f"Automation sequence failed: {e}")
        import asyncio
        asyncio.create_task(send_error_to_telegram("Headless Authentication Layer", e))
    finally:
        driver.quit()
    return False

# ─── RISK AND MARKET ANALYSIS UTILITIES ──────────────────────────────────────
def check_and_update_limit() -> int:
    global DAILY_TRADE_COUNT, LAST_TRADE_DATE
    today_str = datetime.now().strftime("%Y-%m-%d")
    if today_str != LAST_TRADE_DATE:
        DAILY_TRADE_COUNT = 0
        LAST_TRADE_DATE = today_str
    return DAILY_TRADE_COUNT

def get_authenticated_headers() -> dict:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

def get_spot_price(index_config: dict) -> float:
    """Queries the specific real-time spot price based on the selected index parameters."""
    url = f"{BASE_URL}/GetQuotes"
    payload = f'jData={{"uid":"{USER_ID}","exch":"{index_config["exch"]}","token":"{index_config["token"]}"}}'
    response = requests.post(url, data=payload, headers=get_authenticated_headers()).json()
    if isinstance(response, list): 
        response = response[0]
    return float(response["lp"])

def search_option_symbol(index_config: dict, strike: int, option_type: str) -> str:
    """Queries active contracts and filters for closest current front-week (W1) guidelines."""
    url = f"{BASE_URL}/SearchScrip"
    search_text = f"{index_config['name']} {int(strike)} {option_type}E"
    deriv_exch = "NFO" if index_config["name"] == "NIFTY" else "BFO"
    
    payload = f'jData={{"uid":"{USER_ID}","stext":"{search_text}","exch":"{deriv_exch}"}}'
    response = requests.post(url, data=payload, headers=get_authenticated_headers()).json()
    
    if response.get("stat") == "Ok" and "values" in response:
        scrip_list = response["values"]
        for scrip in scrip_list:
            if scrip.get("weekly") == "W1" and scrip.get("optt") == f"{option_type}E":
                return scrip["tsym"]
        return scrip_list[0]["tsym"]
    raise Exception(f"Failed to identify valid matching contract variants: {response.get('emsg', response)}")

# ─── TELEGRAM ASYNCHRONOUS INTERFACE ROUTER ──────────────────────────────────
async def handle_trading_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACCESS_TOKEN, DAILY_TRADE_COUNT
    
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    text = update.message.text.strip().upper()

    # Manual override login command
    if text == "LOGIN":
        await update.message.reply_text("🔄 Spinning up secure headless environment to sync authorization status...")
        if run_background_login():
            await update.message.reply_text("🚀 **Login Successful!** Daily API token loaded into memory context.")
        else:
            await update.message.reply_text("❌ **Login Failed.** Check error stack alert details.")
        return

    # Emergency Close Command
    if text == "CLOSE":
        try:
            await update.message.reply_text("⚠️ **WIPE INITIATED.** Clearing all active positions...")
            pos_url = f"{BASE_URL}/PositionBook"
            pos_payload = f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}"}}'
            pos_res = requests.post(pos_url, data=pos_payload, headers=get_authenticated_headers()).json()
            
            closed_count = 0
            if isinstance(pos_res, list):
                for pos in pos_res:
                    net_qty = int(pos.get("netqty", 0))
                    if net_qty != 0:
                        exit_side = 'S' if net_qty > 0 else 'B'
                        place_url = f"{BASE_URL}/PlaceOrder"
                        order_payload = (
                            f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}","exch":"{pos["exch"]}","tsym":"{pos["tsym"]}",'
                            f'"qty":"{abs(net_qty)}","prd":"M","trantype":"{exit_side}","prctyp":"MKT","price":"0","ret":"DAY","remarks":"EMERGENCY EXIT"}}'
                        )
                        requests.post(place_url, data=order_payload, headers=get_authenticated_headers())
                        closed_count += 1
            await update.message.reply_text(f"🛑 **Position Book Flattened!**\nPositions Closed: {closed_count}\nStatus: All open risks successfully minimized.")
        except Exception as wipe_err:
            await send_error_to_telegram("Emergency Liquidation Protocol Exception", wipe_err)
        return

    # Enforce strict day/time operational limits (Monday - Friday, 9:00 AM - 3:35 PM IST)
    now = datetime.now()
    current_weekday = now.weekday()
    current_time_str = now.strftime("%H:%M")

    if current_weekday > 4:
        await update.message.reply_text("❌ **Trade Blocked:** Strategy execution is closed on weekends.")
        return

    if not ("09:00" <= current_time_str <= "15:35"):
        await update.message.reply_text(f"❌ **Trade Blocked:** Restricted to 9:00 AM - 3:35 PM IST. Current time: {current_time_str}")
        return

    if not ACCESS_TOKEN:
        await update.message.reply_text("🔒 **Session Offline.** Type `LOGIN` to establish tokens.")
        return

    # Syntax Pattern Parser (Accepts N or S as first dynamic capture character token)
    pattern = r"^(N|S)(C|P)([+-]\d+)\s+(\d+)(?:\s+(\d+))?$"
    match = re.match(pattern, text)
    
    if not match:
        await update.message.reply_text("❌ Input syntax error. Pattern format criteria unmet.")
        return

    if check_and_update_limit() >= MAX_DAILY_TRADES:
        await update.message.reply_text("🚫 Risk Protection: Daily order execution ceiling reached.")
        return

    index_type, opt_type, offset_str, lots, sl_points = match.groups()
    cfg = INDEX_MAP[index_type]
    
    total_qty = int(lots) * cfg["lot_size"]
    deriv_exch = "NFO" if cfg["name"] == "NIFTY" else "BFO"
    
    try:
        spot_val = get_spot_price(cfg)
        atm_strike = round(spot_val / cfg["strike_step"]) * cfg["strike_step"]
        
        strike_target = atm_strike + (int(offset_str) * cfg["strike_step"]) if opt_type == 'C' else atm_strike - (int(offset_str) * cfg["strike_step"])
        tsym = search_option_symbol(cfg, strike_target, opt_type)
        
        place_url = f"{BASE_URL}/PlaceOrder"
        entry_payload = (
            f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}","exch":"{deriv_exch}","tsym":"{tsym}",'
            f'"qty":"{total_qty}","prd":"M","trantype":"B","prctyp":"MKT","price":"0","ret":"DAY","remarks":"BOT ENTRY"}}'
        )
        order_res = requests.post(place_url, data=entry_payload, headers=get_authenticated_headers()).json()
        
        if order_res.get("stat") == "Ok":
            DAILY_TRADE_COUNT += 1
            execution_msg = f"🎯 **Trade Executed Successfully!**\nAsset: {cfg['name']}\nSymbol: {tsym}\nAction: BUY MARKET\nQuantity: {total_qty} ({lots} Lots)\nDaily Limit: {DAILY_TRADE_COUNT}/{MAX_DAILY_TRADES}"
            
            if sl_points:
                time.sleep(0.4)
                hist_url = f"{BASE_URL}/SingleOrdHist"
                hist_payload = f'jData={{"uid":"{USER_ID}","norenordno":"{order_res["norenordno"]}"}}'
                hist_res = requests.post(hist_url, data=hist_payload, headers=get_authenticated_headers()).json()
                
                avg_price = 0.0
                if isinstance(hist_res, list) and len(hist_res) > 0:
                    avg_price = float(hist_res[0].get("avgprc", 0.0))
                    
                if avg_price > 0:
                    sl_trigger = round((avg_price - int(sl_points)), 2)
                    sl_limit = round((sl_trigger - 0.50), 2)
                    
                    sl_payload = (
                        f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}","exch":"{deriv_exch}","tsym":"{tsym}","qty":"{total_qty}",'
                        f'"prd":"M","trantype":"S","prctyp":"SL-LMT","price":"{sl_limit}","trgprc":"{sl_trigger}","ret":"DAY","remarks":"BOT SL"}}'
                    )
                    sl_res = requests.post(place_url, data=sl_payload, headers=get_authenticated_headers()).json()
                    
                    if sl_res.get("stat") == "Ok":
                        execution_msg += f"\n\n📉 **Stop Loss Order Active**\nTrigger Target: {sl_trigger}\nLimit Execution: {sl_limit}"
                    else:
                        raise ValueError(f"Stop loss placement rejected by broker: {sl_res.get('emsg', sl_res)}")
                else:
                    raise ValueError("Could not extract entry execution average price from history books.")
                    
            await update.message.reply_text(execution_msg)
        else:
            raise ValueError(f"Order rejected by broker OMS pipeline: {order_res.get('emsg', order_res)}")
            
    except Exception as strategy_err:
        await send_error_to_telegram(f"{cfg['name']} Strategy Processing Exception", strategy_err)

# ─── ENTRY POINT AUTOMATED PRE-FLIGHT INTERFACE ──────────────────────────────
async def post_init_startup_routine(application: Application):
    """Executes background OAuth token extraction and transmits a direct status ping back to Telegram."""
    global tg_app
    tg_app = application
    
    logger.info("Starting automated pre-market boot synchronization protocol...")
    login_success = run_background_login()
    
    if login_success:
        success_text = "🚀 **Shoonya Login Successful!**\n\nThe 2026 OAuth token authentication layer is initialized and securely verified. Standing by for strategy parameters."
        await application.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=success_text)
        logger.info("Automatic login message pushed to Telegram channel.")
    else:
        logger.warning("Automatic pre-market authentication layer initialization failed.")

def main():
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    
    # Inject the startup pipeline hook
    app.post_init = post_init_startup_routine
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_trading_commands))
    
    logger.info("Automation loops established. Launching environment polling...")
    app.run_polling()

if __name__ == '__main__':
    main()