# main.py 主逻辑：包括字段拼接、模拟请求
import os
import sys
import json
import time
import random
import logging
import hashlib
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from push import push
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter

# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]
ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
READ_TIMEOUT = 30       # read 请求超时，避免整个任务被一个卡住的连接吊死
MAX_FAIL = 5            # 连续拿不到有效响应多少次就认输
MAX_RESCUE = 2          # 靠刷新 cookie 补救的次数上限
MAX_NOSYNC = 5          # 连续拿不到 synckey 的上限，防止空转刷接口
RETRY_SLEEP = 30        # 重试间隔
# 刷新后的 curl bash 落到这里，由 workflow 回写进 WXREAD_CURL_BASH，让登录态滚动续期
CURL_BASH_OUT = os.getenv('CURL_BASH_OUT') or 'new_curl_bash.txt'


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def format_duration(minutes):
    """把分钟数格式化为“x 小时 y 分钟”"""
    hours, mins = divmod(minutes, 60)
    mins_str = f"{mins:.0f}" if float(mins).is_integer() else f"{mins:.1f}"
    if hours >= 1:
        return f"{hours:.0f} 小时 {mins_str} 分钟"
    return f"{mins_str} 分钟"


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()


def dump_curl_bash():
    """把当前 headers/cookies 重新渲染成 curl bash，供回写 Secret 用"""
    parts = [f"curl '{READ_URL}'"]
    parts.extend(f"-H '{k}: {v}'" for k, v in headers.items())
    parts.append("-b '" + '; '.join(f"{k}={v}" for k, v in cookies.items()) + "'")
    try:
        # 不带结尾换行，回写 Secret 时无需再处理
        with open(CURL_BASH_OUT, 'w', encoding='utf-8') as fp:
            fp.write(' '.join(parts))
    except OSError as exc:
        logging.warning(f"写入 {CURL_BASH_OUT} 失败：{exc}")


def get_wr_skey():
    """刷新cookie密钥，返回 renewal 响应里的全部 Set-Cookie"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(RENEW_URL,headers=headers,cookies=cookies,data=json.dumps(cookie_data, separators=(',', ':')),timeout=10)

            if 'wr_skey' in response.cookies:
                new_cookies = requests.utils.dict_from_cookiejar(response.cookies)
                # 只打字段名，别把值写进日志
                logging.info(f"renewal 返回 cookie 字段：{sorted(new_cookies)}")
                return new_cookies
            else:
                continue
        except requests.RequestException as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue


    return None


def fix_no_synckey():
    try:
        requests.post(FIX_SYNCKEY_URL, headers=headers, cookies=cookies,data=json.dumps({"bookIds":["3300060341"]}, separators=(',', ':')),timeout=READ_TIMEOUT)
    except requests.RequestException as exc:
        logging.warning(f"chapterInfos 请求失败：{exc}")

refresh_print = setup_logging()


def build_read_data(last_time):
    """拼接一次 read 请求的 data，返回 (data, 本次时间戳)"""
    data.pop('s', None)
    data['b'] = random.choice(book)
    data['c'] = random.choice(chapter)
    this_time = int(time.time())
    data['ct'] = this_time
    data['rt'] = this_time - last_time
    data['ts'] = int(this_time * 1000) + random.randint(0, 1000)
    data['rn'] = random.randint(0, 1000)
    data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
    data['s'] = cal_hash(encode_data(data))
    return data, this_time


def do_read(payload):
    """发一次 read 请求。网络异常或响应不是 JSON 时返回 None，由调用方重试"""
    try:
        response = requests.post(READ_URL, headers=headers, cookies=cookies,data=json.dumps(payload, separators=(',', ':')),timeout=READ_TIMEOUT)
    except requests.RequestException as exc:
        logging.warning(f"read 请求失败：{exc}")
        return None
    try:
        return response.json()
    except ValueError:
        # weread 偶尔会吐空 body 或 HTML 错误页，不该让整个任务崩掉
        preview = response.text[:120].replace('\n', ' ')
        logging.warning(f"read 响应不是 JSON，HTTP {response.status_code}，前 120 字符：{preview}")
        return None


def cookie_alive():
    """用 read 接口验活。renewal 鉴权失败时 cookie 往往仍可用，不该直接终止"""
    payload, _ = build_read_data(int(time.time()) - 30)
    resData = do_read(payload)
    return bool(resData) and 'succ' in resData


def refresh_cookie():
    """续期成功返回 True。失败只告警，由调用方决定是否终止"""
    logging.info("刷新 cookie")
    new_cookies = get_wr_skey()
    if not new_cookies:
        logging.warning("renewal 未返回新密钥。")
        return False

    new_cookies['wr_skey'] = new_cookies['wr_skey'][:8]
    # 全量合并：wr_rt 等会轮转的字段必须留下，否则登录态的 7 天计时永远不会重置
    cookies.update(new_cookies)
    logging.info(f"密钥刷新成功，新密钥：{cookies['wr_skey'][:2]}***")
    dump_curl_bash()
    return True


def abort():
    logging.error(ERROR_CODE)
    push(ERROR_CODE, PUSH_METHOD, is_success=False)
    raise Exception(ERROR_CODE)


# 先续期（顺带轮转 wr_rt）；续期失败但 cookie 还能用就继续，两者都不行才终止
if not refresh_cookie() and not cookie_alive():
    abort()
logging.info("重新本次阅读。")

index = 1
lastTime = int(time.time()) - 30
readTime = READ_NUM * 0.5
logging.info(f"一共需要阅读 {READ_NUM} 次, 阅读时长 {readTime:.1f} 分钟")

fails = 0        # 连续失败计数
nosync = 0       # 连续无 synckey 计数
rescues = 0      # 已用掉的 cookie 补救次数
aborted = False

while index <= READ_NUM:
    payload, thisTime = build_read_data(lastTime)

    refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
    logging.debug("data: %s", payload)
    resData = do_read(payload)
    logging.debug("response: %s", resData)

    if resData is None:
        fails += 1
        if fails < MAX_FAIL:
            logging.warning(f"第 {fails}/{MAX_FAIL} 次连续失败，{RETRY_SLEEP} 秒后重试")
            time.sleep(RETRY_SLEEP)
            continue
        # 连续失败也可能是会话被踢，刷一次 cookie 再搏一次
        if rescues < MAX_RESCUE and refresh_cookie():
            rescues += 1
            fails = 0
            logging.info(f"已刷新 cookie 后重试（第 {rescues}/{MAX_RESCUE} 次补救）")
            continue
        logging.error(f"连续 {MAX_FAIL} 次拿不到有效响应，提前结束。")
        aborted = True
        break

    fails = 0

    if 'succ' in resData:
        if 'synckey' in resData:
            nosync = 0
            lastTime = thisTime
            index += 1
            time.sleep(30)
            refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        else:
            nosync += 1
            if nosync > MAX_NOSYNC:
                logging.error(f"连续 {MAX_NOSYNC} 次修复后仍无 synckey，提前结束。")
                aborted = True
                break
            logging.warning(f"无 synckey，尝试修复（第 {nosync}/{MAX_NOSYNC} 次）...")
            fix_no_synckey()
            time.sleep(RETRY_SLEEP)
    else:
        logging.warning("cookie 已过期，尝试刷新...")
        if not refresh_cookie():
            abort()

done_minutes = (index - 1) * 0.5
now_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')

if aborted:
    logging.error(f"阅读中断，已完成 {format_duration(done_minutes)}。")
    push(f"微信读书自动阅读中断。\n已完成：{format_duration(done_minutes)}（{index - 1}/{READ_NUM} 次）。\n中断时间：{now_str}", PUSH_METHOD, is_success=False)
    sys.exit(1)

logging.info("阅读脚本已完成。")
logging.info("开始推送...")
push(f"微信读书自动阅读完成。\n阅读时长：{format_duration(done_minutes)}。\n完成时间：{now_str}", PUSH_METHOD, is_success=True)
