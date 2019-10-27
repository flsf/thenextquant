# -*- coding:utf-8 -*-

"""
fcoin 交易模块
https://developer.fcoin.com/zh.html

Update: QiaoXiaofeng
Update Date: 2019/10/23
Author: QiaoXiaofeng
Date:   2019/10/23
"""

import json
import hmac
import copy
import gzip
import time
import base64
import urllib
import hashlib
import datetime
from urllib import parse
from urllib.parse import urljoin

from quant.error import Error
from quant.utils import tools
from quant.utils import logger
from quant.const import FCOIN
from quant.order import Order
from quant.tasks import SingleTask
from quant.utils.websocket import Websocket
from quant.asset import Asset, AssetSubscribe
from quant.utils.decorator import async_method_locker
from quant.utils.http_client import AsyncHttpRequests
from quant.order import ORDER_ACTION_BUY, ORDER_ACTION_SELL
from quant.order import ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET
from quant.order import ORDER_STATUS_SUBMITTED, ORDER_STATUS_PARTIAL_FILLED, ORDER_STATUS_FILLED, \
    ORDER_STATUS_CANCELED, ORDER_STATUS_FAILED
from quant.event import EventOrder


__all__ = ("FcoinRestAPI", "FcoinTrade", )


class FcoinRestAPI:
    """ Fcoin REST API 封装
    """

    def __init__(self, host, access_key, secret_key):
        """ 初始化
        @param host 请求host
        @param access_key API KEY
        @param secret_key SECRET KEY
        """
        self._host = host
        self._access_key = access_key
        self._secret_key = secret_key
        self._account_id = None

    async def get_server_time(self):
        """ 获取服务器时间
        @return data int 服务器时间戳(毫秒)
        """
        success, error = await self.request("GET", "/v2/public/server-time", auth=False)
        return success, error

    async def get_account_balance(self):
        """ 获取账户信息
        """
        uri = "/v2/accounts/balance"
        success, error = await self.request("GET", uri)
        return success, error

    async def create_order(self, symbol, price, quantity, order_type, side, exchange="main"):
        """ 创建订单
        @param symbol 交易对
        @param quantity 交易量
        @param price 交易价格
        @param order_type 订单类型 limit, market, fok, ioc
        @param side 交易方向 buy, sell
        @return order_no 订单id
        """
        payload = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'price': price,
            'amount': quantity,
            'exchange': exchange
        }
        if order_type == 'market':
            del payload['price']
        success, error = await self.request("POST", "/v2/orders", body=payload)
        return success, error

    async def revoke_order(self, order_no):
        """ 撤销委托单
        @param order_no 订单id
        @return True/False
        """
        uri = "/v2/orders/{order_no}/submit-cancel".format(order_no=order_no)
        success, error = await self.request("POST", uri)
        return success, error

    async def get_open_orders(self, symbol, states="submitted,partial_filled", limit=100, before="", after=""):
        """ 获取当前还未完全成交的订单信息
        @param symbol 交易对
        @param states submitted,partial_filled,partial_canceled,filled,canceled
        @before before timestamp
        @after after timestamp
        @limit default 20, max 100
        @account_type 如果是杠杆则填margin 
        * NOTE: 查询上限最多100个订单
        """
        payload = {
            'symbol': symbol,
            'states': states,
            'before': before,
            'after': after,
            'limit': limit
        }
        if before == '':
            del payload['before']
        if after == '':
            del payload['after']
        if limit == '':
            del payload['limit']
        success, error  = await self.request("GET", "/v2/orders", params=payload, body=payload)
        if error:
            return None, error
        else:
            return success['data'], None

    async def get_order_status(self, order_no):
        """ 获取订单的状态
        @param order_no 订单id
        """
        uri = "/v2/orders/{order_no}".format(order_no=order_no)
        success, error = await self.request("GET", uri)
        return success, error

    async def request(self, method, uri, params=None, body=None, auth=True):
        """ 发起请求
        @param method 请求方法 GET POST
        @param uri 请求uri
        @param params dict 请求query参数
        @param body dict 请求body数据
        """
        if params:
            query = "&".join(["{}={}".format(k, params[k]) for k in sorted(params.keys())])
            uri += "?" + query
        url = urljoin(self._host, uri)
        if auth:
            if not body:
                body = ""
            timestamp = str(int(round(time.time() * 1000)))
            headers = {
                "Accept": "application/json",
                "Content-type": "application/json"
            }
            signature = self.generate_signature(method, url, timestamp, body)
            headers['FC-ACCESS-KEY'] = self._access_key
            headers['FC-ACCESS-TIMESTAMP'] = timestamp
            headers['FC-ACCESS-SIGNATURE'] = signature
        _, success, error = await AsyncHttpRequests.fetch(method, url, params=params ,data=body, headers=headers,
                                                          timeout=10)
        if error:
            return success, error
        if success.get("status") != 0:
            return None, success
        return success.get("data"), None

    def generate_signature(self, method, url, timestamp, body):
        """ 创建签名
        """
        payload_result = ''
        if body != '':
            payload_result = self.sort_payload(body)
        data = method + url + timestamp + payload_result
        data_base64 = base64.b64encode(bytes(data, encoding='utf8'))
        data_base64_sha1 = hmac.new(bytes(self._secret_key, encoding='utf8'), data_base64, hashlib.sha1).digest()
        data_base64_sha1_base64 = base64.b64encode(data_base64_sha1)
        return str(data_base64_sha1_base64, encoding='utf-8')

    def sort_payload(self, payload):
        keys = sorted(payload.keys())
        result = ''
        for i in range(len(keys)):
            if i != 0:
                result += '&' + keys[i] + "=" + str(payload[keys[i]])
            else:
                result += keys[i] + "=" + str(payload[keys[i]])
        return result

class FcoinTrade:
    """ fcoin Trade模块
    """

    def __init__(self, **kwargs):
        """ 初始化
        """
        e = None
        if not kwargs.get("account"):
            e = Error("param account miss")
        if not kwargs.get("strategy"):
            e = Error("param strategy miss")
        if not kwargs.get("symbol"):
            e = Error("param symbol miss")
        if not kwargs.get("host"):
            kwargs["host"] = "https://api.fcoin.com"
        if not kwargs.get("access_key"):
            e = Error("param access_key miss")
        if not kwargs.get("secret_key"):
            e = Error("param secret_key miss")
        if e:
            logger.error(e, caller=self)
            if kwargs.get("init_success_callback"):
                SingleTask.run(kwargs["init_success_callback"], False, e)
            return

        self._account = kwargs["account"]
        self._strategy = kwargs["strategy"]
        self._platform = FCOIN
        self._symbol = kwargs["symbol"]
        self._host = kwargs["host"]
        self._access_key = kwargs["access_key"]
        self._secret_key = kwargs["secret_key"]
        self._asset_update_callback = kwargs.get("asset_update_callback")
        self._init_success_callback = kwargs.get("init_success_callback")

        self._raw_symbol = self._symbol.replace("/", "").lower()  # 转换成交易所对应的交易对格式

        self._assets = {}  # 资产 {"BTC": {"free": "1.1", "locked": "2.2", "total": "3.3"}, ... }

        # 初始化 REST API 对象
        self._rest_api = FcoinRestAPI(self._host, self._access_key, self._secret_key)

        # 初始化资产订阅
        if self._asset_update_callback:
            AssetSubscribe(self._platform, self._account, self.on_event_asset_update)

    @property
    def assets(self):
        return copy.copy(self._assets)

    @property
    def rest_api(self):
        return self._rest_api

    async def create_order(self, action, price, quantity, order_type=ORDER_TYPE_LIMIT):
        """ 创建订单
        @param action 交易方向 BUY / SELL
        @param price 委托价格
        @param quantity 委托数量
        @param order_type 委托类型 LIMIT / MARKET
        """
        if action == ORDER_ACTION_BUY:
            if order_type == ORDER_TYPE_LIMIT:
                t = "limit"
                side = "buy"
            elif order_type == ORDER_TYPE_MARKET:
                t = "market"
                side = "buy"
            else:
                logger.error("order_type error! order_type:", order_type, caller=self)
                return None, "order type error"
        elif action == ORDER_ACTION_SELL:
            if order_type == ORDER_TYPE_LIMIT:
                t = "limit"
                side = "sell"
            elif order_type == ORDER_TYPE_MARKET:
                t = "market"
                side = "sell"
            else:
                logger.error("order_type error! order_type:", order_type, caller=self)
                return None, "order type error"
        else:
            logger.error("action error! action:", action, caller=self)
            return None, "action error"
        price = str(price)
        quantity = str(quantity)
        result, error = await self._rest_api.create_order(self._raw_symbol, price, quantity, t, side)
        return result, error

    async def revoke_order(self, *order_nos):
        """ 撤销订单
        @param order_nos 订单号列表，可传入任意多个，如果不传入，那么就撤销所有订单
        """
        # 如果传入order_nos为空，即撤销全部委托单 FixMe
        if len(order_nos) == 0:
            order_nos, error = await self.get_open_order_nos()
            if error:
                return [], error
            if not order_nos:
                return [], None
            success = []
            error = []
            for order_no in order_nos:
                s, e = await self._rest_api.revoke_order(order_no)
                if e:
                    error.append(e)
                else:
                    if s["status"] == 0:
                        success.append(order_no)
                    else:
                        error.append(order_no)
            return success, error

        # 如果传入order_nos为一个委托单号，那么只撤销一个委托单
        if len(order_nos) == 1:
            success, error = await self._rest_api.revoke_order(order_nos[0])
            if error:
                return order_nos[0], error
            else:
                return order_nos[0], None

        # 如果传入order_nos数量大于1，那么就批量撤销传入的委托单
        if len(order_nos) > 1:
            success = []
            error = []
            for order_no in order_nos:
                s, e = await self._rest_api.revoke_order(order_no)
                if e:
                    error.append(e)
                else:
                    if s["status"] == 0:
                        success.append(order_no)
                    else:
                        error.append(order_no)
            return success, error

    async def get_open_order_nos(self):
        """ 获取未完全成交订单号列表
        """
        success, error = await self._rest_api.get_open_orders(self._raw_symbol)
        if error:
            return None, error
        else:
            order_nos = []
            for order_info in success:
                order_nos.append(order_info["id"])
            return order_nos, None

    async def on_event_asset_update(self, asset: Asset):
        """ 资产数据更新回调
        """
        self._assets = asset
        SingleTask.run(self._asset_update_callback, asset)
