import json
import logging
import os
import random
import time

import requests

from config import (
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
)

logger = logging.getLogger(__name__)


class PushNotification:
    def __init__(self):
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                response.raise_for_status()
                logger.info("PushPlus 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("PushPlus 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}
        # 只有真配了代理才走代理；否则 proxies 全是 None，
        # “代理发送 + 直连兜底”实际上是同一个请求发两遍
        proxies = {k: v for k, v in self.proxies.items() if v}
        # 第二次尝试只在连接阶段失败时才走得到：配了代理就回退直连，否则原样再试一次
        attempts = [proxies, None] if proxies else [None, None]

        for index, proxy in enumerate(attempts):
            try:
                response = requests.post(url, json=payload, proxies=proxy, timeout=30)
                response.raise_for_status()
            except requests.exceptions.ConnectionError as exc:
                # 连不上 / DNS 解析失败，消息肯定没发出去，重试是安全的
                logger.error("Telegram 连接失败（第 %d/%d 次）: %s", index + 1, len(attempts), exc)
                if index + 1 < len(attempts):
                    time.sleep(5)
                continue
            except requests.exceptions.RequestException as exc:
                # 读超时或 HTTP 错误：请求已经发出去，Telegram 很可能已经投递，
                # 原样重发会让同一条通知发两遍，这里直接认输
                logger.error("Telegram 发送未得到确认，不重发（消息可能已送达）: %s", exc)
                return False

            try:
                logger.info("Telegram 响应: %s", response.text)
            except requests.exceptions.RequestException as exc:
                # 状态码已确认成功，读正文失败不影响结果，更不该触发重发
                logger.warning("Telegram 响应正文读取失败: %s", exc)
            return True

        return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, content)

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info("WxPusher 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("WxPusher 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                logger.info("ServerChan 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower()

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan", method)
    return False
