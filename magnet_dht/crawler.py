#! usr/bin/python
# encoding=utf-8

import codecs
import hashlib
import hmac
import os
import socket
import time
from threading import Thread
from collections import deque
from multiprocessing import Process, cpu_count

import bencoder

from .utils import get_logger, get_nodes_info, get_rand_id, get_neighbor
from .database import RedisClient

# DHT bootstrap routers.  Tracker announce URLs are not DHT bootstrap nodes and
# cannot be passed to a UDP socket as an address.
BOOTSTRAP_NODES = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]

# 双端队列容量
MAX_NODE_QSIZE = 10000
# UDP 报文 buffsize
UDP_RECV_BUFFSIZE = 65535
# 服务 host
SERVER_HOST = "0.0.0.0"
# 服务端口
SERVER_PORT = 9090
# 磁力链接前缀
MAGNET_PER = "magnet:?xt=urn:btih:{}"
# while 循环休眠时间
SLEEP_TIME = 1e-5
# 节点 id 长度
PER_NID_LEN = 20
# 执行 bs 定时器间隔（秒）
PER_SEC_BS_TIMER = 8
# 是否使用全部进程
MAX_PROCESSES = cpu_count() // 2 or cpu_count()
# Optional Linux interface pinning, useful when the default wired route blocks
# UDP DHT but Wi-Fi works.
NETWORK_INTERFACE = os.getenv("MAGNET_DHT_INTERFACE")
TOKEN_ROTATE_SECONDS = 300


class HNode:
    def __init__(self, nid, ip=None, port=None):
        self.nid = nid
        self.ip = ip
        self.port = port


class DHTServer:
    def __init__(self, bind_ip, bind_port, process_id):
        self.bind_ip = bind_ip
        self.bind_port = bind_port
        self.process_id = process_id
        self.nid = get_rand_id()
        self.token_secret = os.urandom(20)
        self.previous_token_secret = None
        self.token_secret_changed = time.monotonic()
        # nodes 节点是一个双端队列
        self.nodes = deque(maxlen=MAX_NODE_QSIZE)
        # KRPC 协议是由 bencode 编码组成的一个简单的 RPC 结构，使用 UDP 报文发送。
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if NETWORK_INTERFACE:
            if not hasattr(socket, "SO_BINDTODEVICE"):
                raise RuntimeError("MAGNET_DHT_INTERFACE 仅支持 Linux")
            try:
                self.udp.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    NETWORK_INTERFACE.encode() + b"\0",
                )
            except OSError as exc:
                raise RuntimeError(
                    "无法绑定网卡 {!r}: {}".format(NETWORK_INTERFACE, exc)
                ) from exc
        # UDP 地址绑定
        self.udp.bind((self.bind_ip, self.bind_port))
        # redis 客户端
        self.rc = RedisClient()
        self.logger = get_logger("logger_{}".format(bind_port))

    def bootstrap(self):
        """
        通过公共 DHT router 获取初始节点，加入 DHT 网络。
        """
        for address in BOOTSTRAP_NODES:
            self.send_find_node(address)

    def bs_timer(self):
        """
        定时执行 bootstrap()
        """
        t = 1
        while True:
            if t % PER_SEC_BS_TIMER == 0:
                t = 1
                self.bootstrap()
            t += 1
            time.sleep(1)

    def send_krpc(self, msg, address):
        """
        发送 krpc 协议

        :param msg: 发送 UDP 报文信息
        :param address: 发送地址，(ip, port) 元组
        """
        try:
            # msg 要经过 bencode 编码
            self.udp.sendto(bencoder.bencode(msg), address)
        except:
            pass

    def send_error(self, tid, address):
        """
        发送错误回复
        """
        msg = dict(t=tid, y="e", e=[202, "Server Error"])
        self.send_krpc(msg, address)

    def send_response(self, tid, address, **values):
        values.setdefault("id", self.nid)
        self.send_krpc(dict(t=tid, y="r", r=values), address)

    def _rotate_token_secret(self):
        if time.monotonic() - self.token_secret_changed < TOKEN_ROTATE_SECONDS:
            return
        self.previous_token_secret = self.token_secret
        self.token_secret = os.urandom(20)
        self.token_secret_changed = time.monotonic()

    @staticmethod
    def _token_for(secret, ip):
        return hashlib.sha1(secret + ip.encode("ascii")).digest()[:8]

    def make_token(self, ip):
        self._rotate_token_secret()
        return self._token_for(self.token_secret, ip)

    def validate_token(self, token, ip):
        self._rotate_token_secret()
        if not isinstance(token, bytes):
            return False
        valid_tokens = [self._token_for(self.token_secret, ip)]
        if self.previous_token_secret is not None:
            valid_tokens.append(self._token_for(self.previous_token_secret, ip))
        return any(hmac.compare_digest(token, value) for value in valid_tokens)

    def neighbor_id(self, target):
        """Return a stable per-process node ID close to the requested hash."""
        return target[:14] + self.nid[14:]

    def send_find_node(self, address, nid=None):
        """
        发送 find_node 请求。

        `find_node 请求`
        find_node 被用来查找给定 ID 的节点的联系信息。这时 KPRC 协议中的
        "q" == "find_node"。find_node 请求包含 2 个参数，第一个参数是 id，
        包含了请求节点的 ID。第二个参数是 target，包含了请求者正在查找的
        节点的 ID。当一个节点接收到了 find_node 的请求，他应该给出对应的
        回复，回复中包含 2 个关键字 id 和 nodes，nodes 是字符串类型，
        包含了被请求节点的路由表中最接近目标节点的 K(8) 个最接近的节点的联系信息。

        `示例`
        参数: {"id" : "<querying nodes id>", "target" : "<id of target node>"}
        回复: {"id" : "<queried nodes id>", "nodes" : "<compact node info>"}

        :param address: 地址元组（ip, port)
        :param nid: 节点 id
        """
        nid = get_neighbor(nid) if nid else self.nid
        tid = get_rand_id()
        msg = dict(
            t=tid,
            y="q",
            q="find_node",  # 指定请求为 find_node
            a=dict(id=nid, target=get_rand_id()),
        )
        self.send_krpc(msg, address)

    def send_find_node_forever(self):
        """
        循环发送 find_node 请求
        """
        self.logger.info("send find node forever...")
        while True:
            try:
                # 弹出一个节点
                node = self.nodes.popleft()
                self.send_find_node((node.ip, node.port), node.nid)
                time.sleep(SLEEP_TIME)
            except IndexError:
                # 一旦节点队列为空，则重新加入 DHT 网络
                self.bootstrap()

    def save_magnet(self, info_hash, source="get_peers", announced_peer=None):
        """
        将磁力链接保存到数据库

        :param info_hash:  磁力链接的 info_hash
        :param announced_peer: token 验证通过的 (ip, port)
        """
        if len(info_hash) != PER_NID_LEN:
            return
        # 使用 codecs 解码 info_hash
        hex_info_hash = codecs.getencoder("hex")(info_hash)[0].decode()
        magnet = MAGNET_PER.format(hex_info_hash)
        self.rc.add_magnet(magnet, source=source)
        if announced_peer is not None:
            peer_ip, peer_port = announced_peer
            self.rc.add_announced_peer(
                hex_info_hash,
                peer_ip,
                peer_port,
            )
        # self.logger.info("pid " + str(self.process_id) + " - " + magnet)
        self.logger.info("pid_{0} - {1}".format(self.process_id, magnet))

    @staticmethod
    def announced_peer_endpoint(arguments, address):
        """Resolve BEP 5 ``port``/``implied_port`` into a validated endpoint."""
        ip = address[0]
        try:
            socket.inet_pton(socket.AF_INET, ip)
        except OSError as exc:
            raise ValueError("invalid announced peer IP") from exc
        implied_port = arguments.get(b"implied_port", 0)
        port = address[1] if implied_port else arguments[b"port"]
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("invalid announced peer port")
        if not 1 <= port <= 65535:
            raise ValueError("invalid announced peer port")
        return ip, port

    def on_message(self, msg, address):
        """
        负责返回信息的处理

        :param msg: 报文信息
        :param address: 报文地址
        """
        try:
            # `回复`
            # 对应于 KPRC 消息字典中的 y 关键字的值是 r，包含了一个附加的关键字 r。
            # 关键字 r 是字典类型，包含了返回的值。发送回复消息是在正确解析了请求消息的
            # 基础上完成的。
            if msg[b"y"] == b"r":
                # nodes 是字符串类型，包含了被请求节点的路由表中最接近目标节点
                # 的 K个最接近的节点的联系信息。
                if msg[b"r"].get(b"nodes", None):
                    self.on_find_node_response(msg)
            # `请求`
            # 对应于 KPRC 消息字典中的 y 关键字的值是 q，它包含 2 个附加的关键字
            # q 和 a。关键字 q 是字符串类型，包含了请求的方法名字。关键字 a 一个字典
            # 类型包含了请求所附加的参数。
            # 而实际上我们只需要获取这两者中的 info hash，用于构造磁力链接进而获取种子。
            elif msg[b"y"] == b"q":
                if msg[b"q"] == b"ping":
                    self.send_response(msg[b"t"], address)
                elif msg[b"q"] == b"find_node":
                    self.send_response(msg[b"t"], address, nodes=b"")
                # get_peers 与 torrent 文件的 info_hash 有关。这时 KPRC 协议中的
                # "q" = "get_peers"。get_peers 请求包含 2 个参数。第一个参数是 id，
                # 包含了请求节点的 ID。第二个参数是 info_hash，它代表 torrent 文件的 info_hash
                elif msg[b"q"] == b"get_peers":
                    self.on_get_peers_request(msg, address)
                # announce_peer 表明请求的节点正在某个端口下载 torrent
                # 文件。announce_peer 包含 4 个参数。第一个参数是 id，包含了请求节点的 ID；
                # 第二个参数是 info_hash，包含了 torrent 文件的 info_hash；第三个参数是 port
                # 包含了整型的端口号，表明 peer 在哪个端口下载；第四个参数数是 token，
                # 这是在之前的 get_peers 请求中收到的回复中包含的。
                elif msg[b"q"] == b"announce_peer":
                    self.on_announce_peer_request(msg, address)
        except KeyError:
            pass

    def on_find_node_response(self, msg):
        """
        解码 nodes 节点信息，并存储在双端队列

        :param msg: 节点报文信息
        """
        nodes = get_nodes_info(msg[b"r"][b"nodes"])
        for node in nodes:
            nid, ip, port = node
            # 进行节点有效性判断
            if len(nid) != PER_NID_LEN or ip == self.bind_ip:
                continue
            # 将节点加入双端队列
            self.nodes.append(HNode(nid, ip, port))

    def on_get_peers_request(self, msg, address):
        """
        处理 get_peers 请求，获取 info hash

        :param msg: 节点报文信息
        :param address: 节点地址
        """
        tid = msg[b"t"]
        try:
            info_hash = msg[b"a"][b"info_hash"]
            if len(info_hash) != PER_NID_LEN:
                raise ValueError("invalid info hash length")
            self.save_magnet(info_hash, source="get_peers")
            self.send_response(
                tid,
                address,
                id=self.neighbor_id(info_hash),
                nodes=b"",
                token=self.make_token(address[0]),
            )
        except (KeyError, ValueError):
            # 没有对应的 info hash，发送错误回复
            self.send_error(tid, address)

    def on_announce_peer_request(self, msg, address):
        """
        处理 get_announce 请求，获取 info hash，address, port

        token 验证通过后，同时保存公告者的 IP 和有效端口。
        ``implied_port`` 为真时使用 UDP 来源端口，否则使用 ``port``。

        :param msg: 节点报文信息
        :param address: 节点地址
        """
        tid = msg[b"t"]
        try:
            arguments = msg[b"a"]
            info_hash = arguments[b"info_hash"]
            token = arguments[b"token"]
            if len(info_hash) != PER_NID_LEN or not self.validate_token(
                token, address[0]
            ):
                raise ValueError("invalid announce token or info hash")
            announced_peer = self.announced_peer_endpoint(arguments, address)
            self.save_magnet(
                info_hash,
                source="announce_peer",
                announced_peer=announced_peer,
            )
            self.send_response(
                tid,
                address,
                id=self.neighbor_id(info_hash),
            )
        except (KeyError, ValueError):
            # 没有对应的 info hash，发送错误回复
            self.send_error(tid, address)

    def receive_response_forever(self):
        """
        循环接受 udp 数据
        """
        self.logger.info(
            "receive response forever {}:{}".format(self.bind_ip, self.bind_port)
        )
        # 首先加入到 DHT 网络
        self.bootstrap()
        while True:
            try:
                # 接受返回报文
                data, address = self.udp.recvfrom(UDP_RECV_BUFFSIZE)
                # 使用 bdecode 解码返回数据
                msg = bencoder.bdecode(data)
                # 处理返回信息
                self.on_message(msg, address)
                time.sleep(SLEEP_TIME)
            except Exception as e:
                self.logger.warning(e)


def _start_thread(offset):
    """
    启动线程

    :param offset: 端口偏移值
    """
    dht = DHTServer(SERVER_HOST, SERVER_PORT + offset, offset)
    threads = [
        Thread(target=dht.send_find_node_forever),
        Thread(target=dht.receive_response_forever),
        Thread(target=dht.bs_timer),
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()


def start_server():
    """
    多线程启动服务
    """
    processes = []
    for i in range(MAX_PROCESSES):
        processes.append(Process(target=_start_thread, args=(i,)))

    for p in processes:
        p.start()

    for p in processes:
        p.join()
