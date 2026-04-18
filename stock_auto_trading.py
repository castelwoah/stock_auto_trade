import json
import os
import sys
import traceback
from datetime import datetime
import time
import yaml
import requests
import shutil
import pandas as pd

# yaml 파일 로딩
with open('config.yaml', encoding='UTF-8') as f:
    _cfg = yaml.load(f, Loader=yaml.FullLoader)
APP_KEY = _cfg['APP_KEY']
APP_SECRET = _cfg['APP_SECRET']
ACCESS_TOKEN = ""
CANO = _cfg['CANO']
ACNT_PRDT_CD = _cfg['ACNT_PRDT_CD']
DISCORD_WEBHOOK_URL = _cfg['DISCORD_WEBHOOK_URL']
URL_BASE = _cfg['URL_BASE']

# 디스코드에 메시지 전송 함수
def send_message(msg):
    """
    디스코드 메세지를 전송합니다.
    msg: 전송할 메시지(문자열)
    """
    now = datetime.now()
    message = {"content": f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {str(msg)}"}
    try:
        requests.post(DISCORD_WEBHOOK_URL, data=message)
    except Exception as e:
        print(f"디스코드 전송 에러: {e}")
    print(message)

# 토큰 파일 경로 설정
config_root = os.path.join(os.path.expanduser("~"), "workspace", "mini_project")
token_tmp = os.path.join(config_root, f"KIS{datetime.today().strftime('%Y%m%d')}")

# 접근토큰 관리하는 파일 존재여부 체크, 없으면 생성
if not os.path.exists(token_tmp):
    with open(token_tmp, "w+"):
        pass

# 토큰 저장
def save_token(my_token, my_expired):   
    """
    발급받은 토큰과 만료일시를 파일로 저장합니다.
    my_token: 토큰 문자열
    my_expired: 만료일시 (문자열, "%Y-%m-%d %H:%M:%S" 형식)
    """
    valid_date = datetime.strptime(my_expired, "%Y-%m-%d %H:%M:%S")
    with open(token_tmp, "w", encoding="utf-8") as f:
        f.write(f"token: {my_token}\n")
        f.write(f"valid-date: {valid_date}\n")
    send_message(f"token file name [{token_tmp}] stored")

# 예전 토큰 파일 expired 폴더로 이동
def move_old_tokens_to_expired():
    """
    오늘 이전 날짜의 모든 KIS 토큰 파일을 expired 폴더로 이동시킵니다.
    """
    try:
        expired_dir = os.path.join(config_root, "expired")
        if not os.path.exists(expired_dir):
            os.makedirs(expired_dir)
        today = datetime.today().date()
        # config_root 하위의 모든 토큰 파일을 순회
        for fname in os.listdir(config_root):
            if fname.startswith("KIS") and fname[3:11].isdigit():
                token_date_str = fname[3:11]
                try:
                    token_date = datetime.strptime(token_date_str, "%Y%m%d").date()
                except Exception:
                    continue  # 파일명이 날짜 형식에 맞지 않으면 스킵

                if token_date < today:
                    token_file_path = os.path.join(config_root, fname)
                    # 파일명에 타임스탬프 추가하여 이동 (중복 방지)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    expired_file_path = os.path.join(expired_dir, f"{timestamp}_{fname}")
                    try:
                        shutil.move(token_file_path, expired_file_path)
                        send_message(f"오늘 이전({token_date})에 발급된 토큰 파일을 {expired_file_path}로 이동했습니다.")
                    except Exception as e:
                        send_message(f"{fname} 파일 이동 중 오류: {str(e)}")
    except Exception as e:
        send_message(f"만료된 토큰 파일 이동 작업 전체에서 오류 발생: {str(e)}")

# 토큰 불러오기 (만료체크)
def read_token():
    """
    저장된 토큰이 있으면 파일에서 읽어서 유효하면 반환,
    만료되었으면 None 반환 및 만료된 토큰 파일 expired로 이동
    """
    try:
        # yaml로 파싱 불안정하므로 직접 파싱
        token = None
        valid_date = None
        with open(token_tmp, encoding="UTF-8") as f:
            for line in f:
                if "token:" in line:
                    token = line.split(":",1)[1].strip()
                elif "valid-date:" in line:
                    valid_date_str = line.split(":",1)[1].strip()
                    valid_date = datetime.strptime(valid_date_str, "%Y-%m-%d %H:%M:%S")
        if token and valid_date:
            now = datetime.now()
            if valid_date > now:
                return token
            else:
                send_message("token expired")
                move_old_tokens_to_expired()
                return None
        return None
    except Exception:
        return None

# 토큰 발급 (저장된 토큰 있으면 재사용)
def get_access_token():
    """
    저장된 토큰 파일에서 유효한 토큰을 반환, 없거나 만료된 경우 새로 발급 후 저장 및 반환
    """
    global ACCESS_TOKEN
    saved_token = read_token()
    if saved_token is not None:
        send_message(f"saved token loading")
        ACCESS_TOKEN = saved_token
        move_old_tokens_to_expired()
        return saved_token
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    PATH = "oauth2/tokenP"
    URL = f"{URL_BASE}/{PATH}"
    res = requests.post(URL, headers=headers, data=json.dumps(body))
    if res.status_code == 200:
        ACCESS_TOKEN = res.json()["access_token"]
        my_expired = res.json()["access_token_token_expired"]
        save_token(ACCESS_TOKEN, my_expired)
        return ACCESS_TOKEN
    else:
        send_message(f"")
        print("토큰 발급 실패!")
        return None

# 주식 잔고 조회
def get_stock_balance():
    """
    주식 계좌의 보유 주식 종목과 수량을 조회하여 반환합니다.
    반환값: {종목코드(str): 보유수량(int)}
    """
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()
        if not ACCESS_TOKEN:
            raise RuntimeError("ACCESS_TOKEN 발급에 실패했습니다.")

    PATH = "uapi/domestic-stock/v1/trading/inquire-balance"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC8434R",
        "custtype": "P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    res = requests.get(URL, headers=headers, params=params)
    data = res.json()

    if res.status_code != 200 or "output1" not in data:
        msg = data.get("msg1", res.text)
        send_message(f"[주식 잔고조회 실패] {msg}")
        raise ValueError(f"주식 잔고 조회 실패: {msg}")

    stock_list = data["output1"]
    evaluation = data.get("output2", [])
    stock_dict = {} # dict[str, int]
    # send_message("====주식 보유잔고====")
    
    for stock in stock_list:
        try:
            qty = int(stock.get("hldg_qty", "0"))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            pdno = stock.get("pdno", "")
            stock_dict[pdno] = qty
            # send_message(f"{stock.get('prdt_name', '미상')}({pdno}): {qty}주")
            # time.sleep(0.1)

    if evaluation:
        eval0 = evaluation[0]
        # send_message(f"주식 평가 금액: {eval0.get('scts_evlu_amt', '0')}원")
        # time.sleep(0.1)
        # send_message(f"평가 손익 합계: {eval0.get('evlu_pfls_smtl_amt', '0')}원")
        # time.sleep(0.1)
        # send_message(f"총 평가 금액: {eval0.get('tot_evlu_amt', '0')}원")
        # time.sleep(0.1)
    # send_message("=====================")

    return stock_dict, eval0


def print_stock_balance(stock_dict, eval0):
    
    send_message("==== 주식 보유잔고 ====")
    for pdno in stock_dict:    
        send_message(f"{symbol_dict[pdno]}({pdno}): {stock_dict[pdno]}주")
        time.sleep(0.1)
    send_message(f"주식 평가 금액: {eval0.get('scts_evlu_amt', '0')}원")
    time.sleep(0.1)
    send_message(f"평가 손익 합계: {eval0.get('evlu_pfls_smtl_amt', '0')}원")
    time.sleep(0.1)
    send_message(f"총 평가 금액: {eval0.get('tot_evlu_amt', '0')}원")
    time.sleep(0.1)
    send_message("=====================")
    return


# 현금 잔고 조회
def get_balance(price_code="005930", order_price="65500"):
    """
    계좌의 주문 가능 현금 잔고를 integer로 반환합니다.
    price_code: 조회할 종목코드(기본 005930), order_price: 가격(기본 65500)
    """
    
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()
        if not ACCESS_TOKEN:
            raise RuntimeError("ACCESS_TOKEN 발급에 실패했습니다.")

    PATH = "uapi/domestic-stock/v1/trading/inquire-psbl-order"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "TTTC8908R",
        "custtype": "P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": price_code,
        "ORD_UNPR": order_price,
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "Y",
        "OVRS_ICLD_YN": "Y",
    }

    res = requests.get(URL, headers=headers, params=params)
    data = res.json()

    if res.status_code != 200 or "output" not in data:
        msg = data.get("msg1", res.text)
        send_message(f"[현금 잔고조회 실패] {msg}")
        raise ValueError(f"현금 잔고 조회 실패: {msg}")

    cash = int(data["output"]["ord_psbl_cash"])
    send_message(f"주문 가능 현금 잔고: {cash:,}원")
    return cash



# 현재가 조회
def get_current_price(stock_no="005930"):
    """
    입력한 종목의 현재가를 조회 후 문자열(가격)로 반환합니다.
    stock_no: 종목코드(문자열)
    """
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()
        if not ACCESS_TOKEN:
            raise RuntimeError("ACCESS_TOKEN 발급에 실패했습니다.")
    
    PATH = "uapi/domestic-stock/v1/quotations/inquire-price"
    URL = f"{URL_BASE}/{PATH}"

    # 헤더 설정
    headers = {
        "Content-Type": "application/json", 
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "FHKST01010100"
    }

    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_no
    }

    # 호출
    res = requests.get(URL, headers=headers, params=params)
    try:
        if res.status_code == 200 and res.json()["rt_cd"] == "0":
            return res.json()['output']['stck_prpr']
    except Exception:
        pass
    print("Error Code : " + str(res.status_code) + " | " + res.text)
    send_message(f"Error Code : {str(res.status_code)} | {res.text}")
    return None



####
####
# 전일 최고가, 최저가, 시가 조회
def get_prev_day_high_low(code="005930"):
    """
    주어진 종목의 전일(이전거래일) 날짜, 고가, 저가, 오늘시가 딕셔너리로 반환.
    code: 종목코드(문자열)
    반환값: dict 또는 None
    """
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()
        if not ACCESS_TOKEN:
            raise RuntimeError("ACCESS_TOKEN 발급 실패")

    PATH = "uapi/domestic-stock/v1/quotations/inquire-daily-price"
    URL = f"{URL_BASE}/{PATH}"

    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": "FHKST01010400",
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": code,
        "fid_org_adj_prc": "1",
        "fid_period_div_code": "D",
    }

    res = requests.get(URL, headers=headers, params=params, timeout=10)
    if res.status_code != 200:
        print("API 호출 실패")
        print("res.status_code: ", res.status_code)
        return None

    data = res.json()
    rows = data.get("output", []) or []
    if not rows:
        return None

    today_str = datetime.today().strftime("%Y%m%d")
    # 전일: 오늘보다 과거 날짜 중 가장 최근
    prev = None
    for row in rows:
        d = row.get("stck_bsop_date")
        if d and d < today_str:
            if prev is None or d > prev.get("stck_bsop_date"):
                prev = row
    if not prev:
        # 혹시 API가 오늘자 미포함으로 내려오는 경우, 관례적으로 rows[0]이 최근일, rows[1]이 전일
        if len(rows) > 1:
            prev = rows[1]
        else:
            return None

    return {
        "stck_bsop_date": prev.get("stck_bsop_date"),
        "prev_high": prev.get("stck_hgpr"),
        "prev_low": prev.get("stck_lwpr"),
        "stck_oprc": data['output'][0]['stck_oprc']
    }
    
    
# 목표가 지정
def get_target_price(code="005930"):
    """
    변동성 돌파전략: 오늘 시가 + (전일고가-전일저가)*0.5 = 목표가 반환
    code: 종목코드(문자열)
    반환값: float 또는 None (목표가)
    """
    prev_day_info = get_prev_day_high_low(code)
    if prev_day_info is None:
        send_message(f"{code} 목표가 조회 실패 (전일 정보 없음)")
        return None
    stck_oprc = int(prev_day_info['stck_oprc'])
    stck_hgpr = int(prev_day_info['prev_high'])
    stck_lwpr = int(prev_day_info['prev_low'])
    target_price = stck_oprc + (stck_hgpr - stck_lwpr) * 0.5
    return target_price

# 매수
def buy(code="005930", qty="1"):
    """
    특정 종목을 지정한 수량만큼 시장가로 매수. 성공시 True, 실패시 False 반환
    code: 종목코드, qty: 수량 (문자열 또는 정수)
    """
    global ACCESS_TOKEN
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": "0",
    }
    headers = {
        "Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC0012U",    # (구)TTTC0802U → (신)TTTC0012U
        "custtype":"P",
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    try:
        if res.json()['rt_cd'] == '0':
            send_message(f"[매수 성공]{str(res.json())}")
            return True
        else:
            send_message(f"[매수 실패]{str(res.json())}")
            return False
    except Exception as ex:
        send_message(f"[매수 오류]{ex}")
        return False

# 매도
def sell(code="005930", qty="1"):
    """
    특정 종목을 지정한 수량만큼 시장가로 매도. 성공시 True, 실패시 False 반환
    code: 종목코드, qty: 수량 (문자열 또는 정수)
    """
    global ACCESS_TOKEN
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": qty,
        "ORD_UNPR": "0",
    }
    headers = {
        "Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC0011U",    # (구)TTTC0801U → (신)TTTC0011U
        "custtype":"P",
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    try:
        if res.json()['rt_cd'] == '0':
            send_message(f"[매도 성공]{str(res.json())}")
            return True
        else:
            send_message(f"[매도 실패]{str(res.json())}")
            return False
    except Exception as ex:
        send_message(f"[매도 오류]{ex}")
        return False
    
symbol_dict = {
    "005930": "삼성전자",
    # "092200": "디아이씨",
    "035720": "카카오",
    "000660": "sk하이닉스",
    "036630": "세종텔레콤",
    "166090": "하나머티리얼즈",
    "020150": "롯데에너지머티리얼즈",
    "001500": "현대차증권",
    "267250": "HD현대",
    "489460": "바이오비쥬",
    "036540": "SFA반도체",
    "131970": "두산테스나",
    "126730": "코칩"
}
symbol_list = list(symbol_dict.keys())
    
# 자동매매 실행
if __name__ == "__main__":
    
    """
    국내 주식 자동매매 메인 루프

    이 코드는 종목별 목표가 조회, 거래가능 현금 산출, 보유 주식 현황 조회를 시작으로, 
    평일 9:05~15:15 동안 자동으로 매수/매도 전략을 실행합니다.

    주요 로직:
      - 장 시작 전(9:00~9:05) : 보유 주식 전량 매도 후 초기화
      - 장중(9:05~15:15) : 
          1. 보유 주식이 매입가 대비 7% 이상 상승하면 자동 전량 매도
          2. 정해진 종목 중 목표가 돌파 종목을 자동 매수 (총 3종목 분산, 각 33% 비중)
          3. 30분마다 매수·매도 현황/잔고 메시지 발송
      - 장 마감(15:15~15:20) : 보유 주식 전량 매도 및 프로그램 종료
      - 주말엔 자동 종료
    예외 발생 시 메시지 전송 및 트레이스백 출력.
    """

    try:
        ACCESS_TOKEN = get_access_token()
                
        for i in symbol_list:
            send_message(f"{symbol_dict[i]} 목표가 = {int(get_target_price(i))} 원")

        # 매수 시각별 평단가 저장용 딕셔너리
        buy_prices = {}

        bought_list = []
        total_cash = get_balance()
        stock_dict, eval0 = get_stock_balance()
        for sym in stock_dict.keys():
            bought_list.append(sym)
        target_buy_count = 3
        buy_percent = 0.33
        buy_amount = total_cash * buy_percent
        soldout = False

        send_message("=== 국내 주식 자동매매 프로그램을 시작합니다 ===")
        last_notify_minute = -1  # 마지막 알림을 보낸 분(0, 30 등)
        print_stock_balance(stock_dict, eval0)
        while True:
            t_now = datetime.now()
            t_9 = t_now.replace(hour=9, minute=0, second=0, microsecond=0)
            t_start = t_now.replace(hour=9, minute=5, second=0, microsecond=0)
            t_sell = t_now.replace(hour=15, minute=15, second=0, microsecond=0)
            t_exit = t_now.replace(hour=15, minute=20, second=0,microsecond=0)
            today = datetime.today().weekday()
            if today == 5 or today == 6:
                send_message("주말이므로 프로그램을 종료합니다.")
                break
            if t_9 < t_now < t_start and soldout == False:
                for sym, qty in stock_dict.items():
                    sell(sym, qty)
                soldout = True
                bought_list = []
                buy_prices = {}
                print_stock_balance(stock_dict, eval0)

            # 메인 매수/매도 구간
            if t_start < t_now < t_sell:
                stock_dict, eval0 = get_stock_balance()  # 최신화
                # 1. 7% 수익시 매도 로직
                for sym, qty in stock_dict.items():
                    try:
                        if qty == 0:
                            continue
                        avg_price = None
                        # 잔고에서 매입가 추출
                        stock_info = stock_dict
                        if type(stock_info[sym]) == dict and 'pchs_avg_pric' in stock_info[sym]:
                            avg_price = float(stock_info[sym]['pchs_avg_pric'])
                        elif sym in buy_prices:
                            avg_price = buy_prices[sym]
                        else:
                            # 못찾으면 잔고 정보에 있는지 확인해보고 아니면 건너뜀
                            continue
                        cur_price = get_current_price(sym)
                        if avg_price and cur_price:
                            rate = (int(cur_price) - float(avg_price)) / float(avg_price)
                            if rate >= 0.07:
                                send_message(f"{symbol_dict.get(sym,sym)} 7% 수익 달성({avg_price} → {cur_price}). 전량 매도 진행.")
                                sell(sym, qty)
                                if sym in bought_list:
                                    bought_list.remove(sym)
                                if sym in buy_prices:
                                    del buy_prices[sym]
                    except Exception as _ex_:
                        send_message(f"매도 7% 체크중 오류: {sym} - {_ex_}")

                # 2. 매수 로직
                for sym in symbol_list:
                    if len(bought_list) < target_buy_count:
                        if sym in bought_list:
                            continue
                        target_price = get_target_price(sym)
                        current_price = get_current_price(sym)
                        if target_price is None or current_price is None:
                            continue
                        target_price = int(target_price)
                        current_price = int(current_price)
                        if target_price < current_price:
                            buy_qty = int(buy_amount // current_price)
                            if buy_qty > 0:
                                send_message(f"{symbol_dict[sym]} 목표가 달성({target_price} < {current_price}) 매수를 시도합니다.")
                                result = buy(sym, buy_qty)
                                if result:
                                    soldout = False
                                    bought_list.append(sym)
                                    # 평단가 저장 (실시간 잔고정보 등으로 업데이트 가능)
                                    buy_prices[sym] = current_price
                                    stock_dict, eval0 = get_stock_balance()
                                                                    
                        time.sleep(1)
                time.sleep(1)

                # 30분 마다(예: 9:00, 9:30, 10:00, ...) 잔고 등 알림
                if (t_now.minute % 30 == 0) and (last_notify_minute != t_now.minute):
                    send_message(f"[알림] {t_now.strftime('%Y-%m-%d %H:%M')} 잔고 현황 업데이트")
                    print_stock_balance(stock_dict, eval0)
                    last_notify_minute = t_now.minute

            if t_sell < t_now < t_exit:
                if soldout == False:
                    stock_dict, eval0 = get_stock_balance()
                    for sym, qty in stock_dict.items():
                        sell(sym, qty)
                    soldout = True
                    bought_list = []
                    buy_prices = {}
                    time.sleep(1)
            if t_exit < t_now:
                send_message("프로그램을 종료합니다.")
                break
    except Exception as e:
        send_message(f"[오류 발생]{e}")
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        time.sleep(1)
