import os
import re
import time
import json
import pyotp
import logging
import hashlib
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, jsonify, request

# Import Selenium web driver components
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Initialize Flask Instance Frame
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── ENVIRONMENTAL CONFIGURATION CREDENTIALS ─────────────────────────────────
BASE_URL = "https://api.shoonya.com/NorenWClientTP"
CLIENT_ID   = os.getenv("SHOONYA_CLIENT_ID")
USER_ID     = os.getenv("SHOONYA_USER_ID")
PASSWORD    = os.getenv("SHOONYA_PASSWORD")
TOTP_SECRET = os.getenv("SHOONYA_TOTP_SECRET")
SECRET_CODE = os.getenv("SHOONYA_API_SECRET")

LOGIN_URL = f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key={CLIENT_ID}&route_to={USER_ID}"

# In-Memory State Containers
ACCESS_TOKEN = None

INDEX_MAP = {
    "N": {"name": "NIFTY", "exch": "NSE", "token": "26000", "lot_size": 65, "strike_step": 50},
    "S": {"name": "SENSEX", "exch": "BSE", "token": "1", "lot_size": 20, "strike_step": 100}
}

# ─── CORE AUTHENTICATION UTILITIES ───────────────────────────────────────────
def scan_network_for_code(driver) -> str:
    try:
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
                if message.get("method") == "Network.requestWillBeSent":
                    url = message.get("params", {}).get("request", {}).get("url", "")
                    if "code=" in url and "shoonya" in url.lower():
                        return parse_qs(urlparse(url).query).get("code", [None])[0]
            except Exception: continue
    except Exception: pass
    return None

def run_background_login() -> bool:
    global ACCESS_TOKEN
    logger.info("Initializing headless Chromium workspace engine...")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    # Leverages native system package definitions from your dedicated VM paths automatically
    driver = webdriver.Chrome(service=Service(), options=options)
    wait = WebDriverWait(driver, 30)
    
    try:
        driver.get(LOGIN_URL)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
        
        all_inputs = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
        visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]
        
        visible_inputs[0].send_keys(USER_ID)
        visible_inputs[1].send_keys(PASSWORD)
        visible_inputs[2].send_keys(pyotp.TOTP(TOTP_SECRET).now())
        
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))).click()
        
        start_time = time.time()
        while time.time() - start_time < 45:
            code = scan_network_for_code(driver)
            if code:
                checksum = hashlib.sha256(f"{CLIENT_ID}{SECRET_CODE}{code}".encode('utf-8')).hexdigest()
                res = requests.post(f"{BASE_URL}/GenAcsTok", data=f'jData={{"code":"{code}","checksum":"{checksum}"}}').json()
                if res.get("stat") == "Ok" and "access_token" in res:
                    ACCESS_TOKEN = res["access_token"]
                    return True
            time.sleep(0.5)
    except Exception as e:
        logger.error(f"Headless driver runtime failure: {e}")
    finally:
        driver.quit()
    return False

# ─── ORDER EXECUTION ROUTINES ────────────────────────────────────────────────
def get_authenticated_headers():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"}

def get_spot_price(cfg):
    payload = f'jData={{"uid":"{USER_ID}","exch":"{cfg["exch"]}","token":"{cfg["token"]}"}}'
    res = requests.post(f"{BASE_URL}/GetQuotes", data=payload, headers=get_authenticated_headers()).json()
    return float(res[0]["lp"]) if isinstance(res, list) else float(res["lp"])

def search_option_symbol(cfg, strike, option_type):
    search_text = f"{cfg['name']} {int(strike)} {option_type}E"
    deriv_exch = "NFO" if cfg["name"] == "NIFTY" else "BFO"
    payload = f'jData={{"uid":"{USER_ID}","stext":"{search_text}","exch":"{deriv_exch}"}}'
    res = requests.post(f"{BASE_URL}/SearchScrip", data=payload, headers=get_authenticated_headers()).json()
    if res.get("stat") == "Ok" and "values" in res:
        for scrip in res["values"]:
            if scrip.get("weekly") == "W1" and scrip.get("optt") == f"{option_type}E":
                return scrip["tsym"]
        return res["values"][0]["tsym"]
    raise Exception("No active options scripts matched criteria parameters.")

# ─── INTERFACE WEB WEB ROUTE APIS ────────────────────────────────────────────
@app.route('/')
def home_dashboard():
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_api_status():
    return jsonify({"connected": ACCESS_TOKEN is not None})

@app.route('/api/trade', methods=['POST'])
def place_dashboard_trade():
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        return jsonify({"message": "Error: Core session token is offline. Please restart python application app context."}), 401
    
    data = request.json
    cfg = INDEX_MAP[data['index_type']]
    opt_type = data['option_type']
    offset_step = int(data['offset'])
    total_qty = int(data['lots']) * cfg["lot_size"]
    deriv_exch = "NFO" if cfg["name"] == "NIFTY" else "BFO"
    
    try:
        spot_val = get_spot_price(cfg)
        atm_strike = round(spot_val / cfg["strike_step"]) * cfg["strike_step"]
        
        # Calculate Option Chain matrix shifts relative to asset flavor logic bounds
        strike_target = atm_strike + (offset_step * cfg["strike_step"]) if opt_type == 'C' else atm_strike - (offset_step * cfg["strike_step"])
        tsym = search_option_symbol(cfg, strike_target, opt_type)
        
        # Dispatch Order Payload
        payload = (
            f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}","exch":"{deriv_exch}","tsym":"{tsym}",'
            f'"qty":"{total_qty}","prd":"M","trantype":"B","prctyp":"MKT","price":"0","ret":"DAY","remarks":"WEB UI TRADE"}}'
        )
        order_res = requests.post(f"{BASE_URL}/PlaceOrder", data=payload, headers=get_authenticated_headers()).json()
        
        if order_res.get("stat") == "Ok":
            return jsonify({"message": f"SUCCESS: Market Order executed cleanly! Filled {total_qty} units of {tsym}."})
        return jsonify({"message": f"OMS REJECTION: {order_res.get('emsg')}"}), 400
    except Exception as e:
        return jsonify({"message": f"CRITICAL RUNTIME ERROR: {str(e)}"}), 500

@app.route('/api/wipe', methods=['POST'])
def position_emergency_wipe():
    if not ACCESS_TOKEN: return jsonify({"message": "Session offline."}), 401
    try:
        pos_payload = f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}"}}'
        pos_res = requests.post(f"{BASE_URL}/PositionBook", data=pos_payload, headers=get_authenticated_headers()).json()
        closed_count = 0
        if isinstance(pos_res, list):
            for pos in pos_res:
                net_qty = int(pos.get("netqty", 0))
                if net_qty != 0:
                    exit_side = 'S' if net_qty > 0 else 'B'
                    order_payload = (
                        f'jData={{"uid":"{USER_ID}","actid":"{USER_ID}","exch":"{pos['exch']}","tsym":"{pos['tsym']}",'
                        f'"qty":"{abs(net_qty)}","prd":"M","trantype":"{exit_side}","prctyp":"MKT","price":"0","ret":"DAY","remarks":"WEB EMERGENCY EXIT"}}'
                    )
                    requests.post(f"{BASE_URL}/PlaceOrder", data=order_payload, headers=get_authenticated_headers())
                    closed_count += 1
        return jsonify({"message": f"LIQUIDATION COMPLETED: Flattened {closed_count} open index exposures to 0."})
    except Exception as e:
        return jsonify({"message": f"Wipe operation error: {str(e)}"}), 500

if __name__ == '__main__':
    # Log in automatically upon running app script execution entry points
    run_background_login()
    # Runs locally on port 5000
    app.run(host='0.0.0.0', port=5000, debug=False)