# Python3 DHT 网络磁力种子采集器

## 😎 严肃的理论

### 磁力链接

现在我们使用迅雷等工具下载资源的时候，基本上都只需要一个叫做磁力链接的东西就可以了，非常方便。

![我仿佛听到了车鸣声](https://user-images.githubusercontent.com/19553554/42369819-a7cd8480-813d-11e8-9110-b371c2cb2b3d.jpg)

**磁力定义**

> 磁力链接是对等网络中进行信息检索和下载文档的电脑程序。和基于“位置”连接的统一资源定位符不同，磁力链接是基于元数据文件内容，属于统一资源名称。也就是说，磁力链接不基于文档的 IP 地址或定位符，而是在分布式数据库中，通过散列函数值来识别、搜索来下载文档。因为不依赖一个处于启动状态的主机来下载文档，所以特别适用没有中心服务器的对等网络。

磁力链接格式类似于 `magnet:?xt=urn:btih:E7FC73D9E20697C6C440203F5884EF52F9E4BD28`

分解一下这个链接

* magnet：协议名。
* xt：exact topic 的缩写，表示资源定位点。BTIH（BitTorrent Info Hash）表示哈希方法名，这里还可以使用 SHA1 和 MD5。这个值是文件的标识符，是不可缺少的。

一般来讲，一个磁力链接只需要上面两个参数即可找到唯一对应的资源。也有其他的可选参数提供更加详细的信息。

* dn：display name 的缩写，表示向用户显示的文件名。
* tr：tracker 的缩写，表示 tracker 服务器的地址。
* kt: 关键字，更笼统的搜索，指定搜索关键字而不是特定文件。
* mt：文件列表，链接到一个包含磁力链接的元文件 (MAGMA - MAGnet MAnifest）。

这里可以阅读阮一峰的 [BT 下载的未来](http://www.ruanyifeng.com/blog/2009/11/future_of_bittorrent.html)，我很喜欢他文章的最后一句话。
> 当互联网上每一台机器都在自动交换信息的时候，谎言和封锁又能持续多久呢？


### 种子/DHT

通过磁力就可以获取种子文件从而进行下载，这跟直接使用种子下载时一个道理的，只是少了从磁力到种子文件的一个过程而已。

![老司机 带带我](https://user-images.githubusercontent.com/19553554/42369745-74eb10a0-813d-11e8-9f65-10f141bc4dfc.jpg)

**种子定义**

> BitTorrent 协议的种子文件可以保存一组文件的元数据。这种格式的文件被 BitTorrent 协议所定义。扩展名一般为“.torrent”。

**种子结构**

> .torrent 种子文件本质上是文本文件，包含 Tracker 信息和文件信息两部分。Tracker 信息主要是 BT 下载中需要用到的 Tracker 服务器的地址和针对 Tracker 服务器的设置，文件信息是根据对目标文件的计算生成的，计算结果根据 BitTorrent 协议内的 *Bencode* 规则进行编码。它的主要原理是需要把提供下载的文件虚拟分成大小相等的块，块大小必须为2k的整数次方（由于是虚拟分块，硬盘上并不产生各个块文件），并把每个块的索引信息和 Hash 验证码写入种子文件中；所以，种子文件就是被下载文件的“索引”。

**种子-磁力联系**

磁力链接的唯一标识符就是 40 个 16 进制字符码，也就是 `magnet:?xt=urn:btih:E7FC73D9E20697C6C440203F5884EF52F9E4BD28` 中的 `E7FC73D9E20697C6C440203F5884EF52F9E4BD28`。这个同时也是种子文件的 info_hash，是每个种子的唯一标识码。根据它就能将磁力链接于种子联系起来，得到资源的详细信息，进而下载资源。

**DHT**

> BitTorrent 使用”分布式哈希表”(DHT)来为无 tracker 的种子(torrents)存储 peer 之间的联系信息。这样每个 peer 都成了 tracker。这个协议基于 Kademila 网络并且在 UDP 上实现。DHT 由节点组成，它存储了 peer 的位置。BitTorrent 客户端包含一个 DHT 节点，这个节点用来联系 DHT 中其他节点，从而得到 peer 的位置，进而通过 BitTorrent 协议下载。

* peer: 一个 TCP 端口上监听的客户端/服务器，它实现了 BitTorrent 协议。
* 节点: 一个 UDP 端口上监听的客户端/服务器，它实现了 DHT(分布式哈希表) 协议。

如果对 DHT 协议感兴趣的话一定要看下 [DHT 协议](http://www.bittorrent.org/beps/bep_0005.html) 的具体内容，这里有 [中文翻译版本](http://justjavac.com/other/2015/02/01/bittorrent-dht-protocol.html)。（想要彻底读懂项目的话一定要先了解该协议，代码都是基于该协议实现的，我也是反复的阅读了好几遍。）


## 😉 务实的实践

### 项目来源

一般来讲到 Python 爬取，大家的第一印象可能就是 requests/aiohttp，或者是 scrapy/pyspider 等爬虫框架。基本上都是从指定的 HTML 页面爬取信息。我有一个项目 [torrent-cli](https://github.com/chenjiandongx/torrent-cli) 就是一个从资源网站上爬取磁力信息的工具。

![赶紧上车](https://user-images.githubusercontent.com/19553554/42370007-35974b20-813e-11e8-9dc0-6f82f9e391d0.jpg)

然而我

![渴望进步](https://user-images.githubusercontent.com/19553554/42373600-020b19a8-8148-11e8-9503-29ae3c9d9aff.jpg)

想自给自足获取磁力种子，Google 了一番，发现大家基本上的代码都是从 [simDHT](https://github.com/fanpei91/simDHT) 这个项目来的，首先这个项目很棒，但是些问题如代码不规范、实现细节基本没有一行注释、不兼容 Python3。然而很多网上同类的代码基本上也是对这个完全照搬....

![眉头一皱](https://user-images.githubusercontent.com/19553554/42409921-ae3e7d98-8213-11e8-8c89-a15aaf7be224.png)

所以我知道我要开始干活了

![努力干活](https://user-images.githubusercontent.com/19553554/42371117-17e39fd6-8141-11e8-8ece-189774e28f41.gif)

经过一波 happy coding 之后。

![开开心心写代码](https://user-images.githubusercontent.com/19553554/42370122-83062ca0-813e-11e8-98cf-3c1177836042.gif)

当然最后还是给码出来了啦

![ojbk](https://user-images.githubusercontent.com/19553554/42406104-c3d48a8e-81d3-11e8-8494-30193459b293.png)


### 项目结构

#### 核心代码

* [crawler.py](https://github.com/chenjiandongx/magnet-dht/blob/master/magnet_dht/crawler.py)

从 DHT 网络中获取磁力链接。程序通过公共 DHT router 获取初始节点，
响应 `get_peers` 并签发与请求者 IP 绑定的短期 token。只有携带有效 token 的
`announce_peer` 才进入热度统计，避免把普通查询 hash 当成活跃资源。

![大哥喝可乐](https://user-images.githubusercontent.com/19553554/42406024-5b84186a-81d2-11e8-9ab9-b69762ea4186.jpg)

磁力数据存放在了 redis，利用 redis 的集合特性来去重。使用了多线程/多进程，用于提高爬取效率。在我的本地机器（i7-7700HQ/16G 内存/8M 网速）跑了一下，效果还不错。

**运行效果**

![运行效果](https://user-images.githubusercontent.com/19553554/42882296-56d7512c-8acb-11e8-8640-4dc79ac12c3a.gif)


**查看 redis 磁链数量**
```bash
$ redis-cli
127.0.0.1:6379> scard magnets
(integer) 1151256
```
然后代码推送到我那台 ~~性能强悍 1 核/2G 内存/1M 网速~~ 阿里云服务器跑一下，哎....

![穷](https://user-images.githubusercontent.com/19553554/42406158-ba6f066c-81d4-11e8-8400-bd00f87e8051.png)

* [magnet_to_torrent_aria2c.py](https://github.com/chenjiandongx/magnet-dht/blob/master/magnet_dht/magnet_to_torrent_aria2c.py)

利用 aria2 将磁力链接转换为种子文件。尝试了一些其他的方式将磁力转换为种子，但效果好像都不怎么理想。使用过 [libtorrent](https://github.com/arvidn/libtorrent) 的 Python 版本，不知道是我打开方式不对还是它本来效率就不高，反正愣是一个种子都没有转换成功。

![好气喔](https://user-images.githubusercontent.com/19553554/42406126-467a2da4-81d4-11e8-82c8-527233485439.png)

最后兜兜转转用到了 [aria2](https://github.com/aria2/aria2) 发现效率还可以。但是要先把 [aria2](https://github.com/aria2/aria2) 安装到你的 PATH 中，具体参考官网介绍。使用其 RPC 特性，节省线程开销。

![真香](https://user-images.githubusercontent.com/19553554/42406150-9676dec4-81d4-11e8-8c3d-6083a4f5a49b.png)

* [parse_torrent.py](https://github.com/chenjiandongx/magnet-dht/blob/master/magnet_dht/parse_torrent.py)

解析种子文件内容，同样也是利用了 Bencode 进行解码。有了种子我们当然要看看到底是些什么资源了啦。你说世界就是这么小，在我解析出来的几百个种子文件中，居然有几个都是一个社区的，那个以 1024 为标志的社区。

![还有这种操作](https://user-images.githubusercontent.com/19553554/42406124-435a0630-81d4-11e8-98df-23cc5c542cd6.png)

有图有真相

![知乎社区](https://user-images.githubusercontent.com/19553554/42373886-cac632ce-8148-11e8-889c-def385f54a0c.png)

不过我还是希望大家铭记下面这 24 字箴言

![社会主义核心价值观](https://user-images.githubusercontent.com/19553554/42373033-59add9cc-8146-11e8-9507-0f1dd570c2e2.jpg)

#### 辅助代码

* [database.py](https://github.com/chenjiandongx/magnet-dht/blob/master/magnet_dht/database.py)：封装了关于 redis 的数据操作，主要是利用其集合数据结构。
* [utils.py](https://github.com/chenjiandongx/magnet-dht/blob/master/magnet_dht/utils.py)：一些工具函数


### 如何使用

**获取源码及安装依赖环境**

确保已经安装好 redis，redis 的具体配置可以在 database.py 里面修改
```bash
$ git clone https://github.com/chenjiandongx/magnet-dht.git
$ cd magnet-dht
$ pip install -r requirements.txt
```

如果使用项目内已创建的虚拟环境，先执行 `source .venv/bin/activate`；也可把下文的
`python` 直接替换为 `.venv/bin/python`。

**运行项目**

进程数量可以在 crawler.py 进行调整
```bash
$ python manage.py -h
usage: manage.py [-h] [-s] [-m] [-p]

start manage.py with flag.

optional arguments:
  -h, --help  show this help message and exit
  -s          run start_server func.
  -m          run magnet2torrent func
  -p          run parse_torrent func
```

**Note:** 在运行 `python manage.py -m` 的时候，要先开个终端窗口启动 aria2c 服务。
```bash
$ export ARIA2RPC_SECRET='请换成长随机字符串'
$ env -u all_proxy -u ALL_PROXY -u http_proxy -u HTTP_PROXY \
    -u https_proxy -u HTTPS_PROXY \
    aria2c \
      --enable-rpc=true \
      --rpc-secret="$ARIA2RPC_SECRET" \
      --interface=wlp12s0 \
      --enable-dht=true \
      --bt-metadata-only=true \
      --bt-save-metadata=true \
      --max-concurrent-downloads=8 \
      --listen-port=6881 \
      --dht-listen-port=6881 \
      --dir="$PWD/torrents"
```

`wlp12s0` 是本机已测试可通 DHT 的 Wi-Fi 网卡，换机器后请用 `ip -br address`
确认实际网卡名。本机有线网卡 `enp13s0f1` 的默认路由无法收到 DHT UDP
回包，因此不要省略 `--interface` 。DHT 不走 HTTP/SOCKS 代理，所以上面显式清除了
代理环境变量。

然后在第二个终端运行一批候选检查：

```bash
$ export ARIA2RPC_SECRET='与-aria2c-一致的字符串'
$ export MAGNET_DHT_INTERFACE=wlp12s0
$ python manage.py -m
```

### announce 热度与候选选择

`python manage.py -m` 不查询公共 Tracker，也不从 `magnets` 集合随机取 hash，而是：

1. 只统计本机 crawler 真实收到且 token 验证通过的 `announce_peer`。
   同时按 BEP 5 保存公告者 IP 和有效端口：`implied_port=1` 时使用
   UDP 来源端口，否则使用公告中的 `port`。peer 最后公告时间作为
   Redis sorted-set 分数，默认 30 分钟过期，每个 info hash 最多保留 64 个；
2. 每个合法 announce 到达时立刻增加当前小时桶中的分数；“小时桶”只是 Redis 的
   滑动窗口存储方式，不需要等当前小时结束。默认统计最近 6 小时，热度每 2 小时
   衰减一半；
3. 常驻 worker 默认每 5 秒查看当前桶和历史桶，领取热度最高的 8 个候选，直接交给
   aria2 通过 DHT 取元数据；
4. metadata 成功或内容非法的 hash 永久过滤；网络失败的 hash 进入指数退避冷却，
   冷却结束后可由新的 announce 再次触发，而不是永久丢弃；
5. 默认 90 秒取不到元数据就停止；
6. 下载到 `.torrent` 后重算 `info` 字典 SHA-1，必须和 magnet hash 一致，
   同时拒绝绝对路径、`..` 穿越和 NUL 路径；
7. 对新取得的视频 metadata，立即接管 aria2 自动生成且处于暂停状态的 payload
   子任务，只取有限 piece 并抽一帧。这样复用刚刚活跃的 torrent 时机，不再等以后
   按 hash 扫描可能已经失活的旧种子。

可用以下变量调整策略：

```bash
export MAGNET_DHT_STOP_TIMEOUT=90          # 单个元数据任务最长时间
export MAGNET_DHT_MAX_CONCURRENT=8          # 每批最多处理数
export MAGNET_DHT_FAILURE_COOLDOWN=900      # 首次网络失败后冷却 15 分钟，再指数退避
export MAGNET_DHT_ANNOUNCE_WINDOW_HOURS=6   # announce 统计窗口
export MAGNET_DHT_ANNOUNCE_HALF_LIFE_HOURS=2
export MAGNET_DHT_PEER_TTL=1800             # 已验证 announce peer 保留 30 分钟
export MAGNET_DHT_PEERS_PER_HASH=64         # 每个 info hash 最多保留的 peer
export MAGNET_DHT_ONCE_ONLY=false           # 失败冷却后可由新 announce 触发重试
export MAGNET_DHT_SAMPLE_ON_METADATA=true   # metadata 成功后立即抽帧
export MAGNET_DHT_SAMPLE_WORKERS=2           # 同时运行的抽帧任务数
export MAGNET_DHT_METADATA_PROGRESS_INTERVAL=10
```

推荐让 worker 常驻，这样 crawler 写入当前桶后无需人工反复执行 `-m`：

```bash
python manage.py -m --watch --watch-interval 5 \
  --sample-mib 64 \
  --sample-idle-timeout 120 \
  --sample-workers 2
```

`--watch` 模式空闲时不会退出，并会输出“继续轮询，不需要等待整点”。同一套 Redis
建议只运行一个 `-m --watch` 实例；crawler 可以继续按原配置多进程运行。抽帧任务在
后台线程执行，不阻塞下一批 metadata；待处理抽帧数达到 worker 数的两倍时会触发
背压，暂缓领取新候选，避免队列无限增长。

程序只负责技术校验，不对内容做自动分类。`python manage.py -p` 会打印已验证
种子的文件名列表，供后续人工 review。

导出适合 Excel/LibreOffice 的 UTF-8 CSV（每个唯一 info hash 一行）：

```bash
$ python manage.py --csv
$ python manage.py --csv /path/to/result.csv
```

## 自托管影视目录（评分与影评）

项目现在包含一个不依赖前端框架的轻量 Web MVP，将已爬取并通过校验的 `.torrent` 元数据整理成类似 NeoDB 的目录：可以搜索条目、查看文件清单和统计信息、写影评、打 1–5 星评分，并通过 JSON API 给后续客户端使用。NeoDB 的核心启发是“共享目录 + 条目评分/评论”，本项目只复用这个产品思路，不复制其代码。

先导入本地元数据，再启动站点：

```bash
export MAGNET_DHT_TORRENT_DIR="$PWD/torrents"
python manage.py --import-catalog
python manage.py --web --host 127.0.0.1 --port 8080
```

浏览 `http://127.0.0.1:8080/`，在“导入元数据”页面也可以重新扫描目录。SQLite 默认写入 `catalog.sqlite3`，可用 `MAGNET_DHT_CATALOG_DB` 改路径。

可用接口：

* `GET /api/media?q=关键词&sort=rating`：搜索/排序条目；
* `GET /api/media/<info_hash>`：读取条目、评分聚合与影评；
* `POST /api/media/<info_hash>/reviews`：以表单或 JSON 提交 `author`、`rating`、`body`、`spoiler`；
* `POST /api/import`（表单字段 `directory`）：扫描本地 torrent 元数据；
* `GET /healthz`：健康检查。

这个目录站只保存描述性元数据、评分和影评，不提供 torrent 文件下载、磁力操作、在线播放或对等网络分发入口；如需接入合法的本地媒体播放器，建议在自己的部署边界内另行设计鉴权和合规策略。

### 分类和本地视频封面

条目会根据文件扩展名分为“视频、音频、图片、文档、压缩包、其他”。首页默认显示视频，顶部标签可以切换到其他分类，也可以通过 `GET /api/media?category=audio` 查询指定分类。

封面只从你已经放在本机的媒体目录中抽帧，不会通过磁力链接解析或下载视频。先安装 `ffmpeg`/`ffprobe`，再配置媒体目录：

```bash
export MAGNET_DHT_MEDIA_ROOT=/path/to/already-downloaded/media
export MAGNET_DHT_COVER_DIR="$PWD/catalog-covers"
python manage.py --generate-covers
```

也可以打开网页的“生成封面”页执行。生成的随机帧会按 info hash 保存为 JPG；重新导入或重新生成时会跳过已有封面。

### torrent 分段抽帧与健康验证

如果视频尚未在本机，不需要把完整视频下载下来。默认的 `python manage.py -m`
会在取得一份新 metadata 后立即处理它。项目从每个已校验的
`.torrent` 中选择体积最大的一个视频，要求 aria2 优先取得一小段经过 piece hash
校验的数据，并反复尝试用 ffmpeg 解码第一张可用画面。抽帧成功后会立刻停止任务，
临时视频片段也会删除；本机只长期保留 JPG 和 JSONL 健康报告。

先启动前文配置好的 aria2 RPC 服务并安装 `ffmpeg`。即使 aria2 服务全局使用了
`--bt-metadata-only=true`，这个抽样命令也会只对自己的 RPC 任务覆盖为 `false`。
建议先做不联网的清单检查：

```bash
export ARIA2RPC_SECRET='与-aria2c-一致的字符串'
python manage.py --sample-video-torrents --sample-dry-run --sample-limit 10
```

推荐直接让新 metadata 与抽帧衔接运行：

```bash
python manage.py -m --watch --watch-interval 5 \
  --sample-mib 64 \
  --sample-idle-timeout 120 \
  --sample-workers 2
```

如果某一批只想保存 metadata，可加 `--no-sample-on-metadata`。独立扫描已有种子仍然
可用，但它按文件名顺序处理的可能是很早以前取得、当前已经没有 peer 的资源：

```bash
python manage.py --sample-video-torrents \
  --sample-limit 10 \
  --sample-mib 64 \
  --sample-dir "$PWD/video-samples" \
  --sample-report "$PWD/video-health.jsonl"
```

BT 协议不能从 peer 处直接请求一个已经解码的 JPEG；最小传输单位是 piece，容器还
可能需要文件头、尾部索引和关键帧。因此 `--sample-mib 64` 表示每个 torrent 最多
尝试约 64 MiB 的分段数据，而不是只传输一张图片的字节数。程序还会将预算限制为
不超过视频体积的一半、把单任务速度限制为默认 2 MiB/s，并在每次 RPC 轮询后检查预算，
避免完成整个视频。可用 `--sample-rate-mib`、`--sample-timeout` 和
`--sample-idle-timeout` 调整边界；空速超时现在默认 120 秒。过小、无法安全分段的
视频会直接跳过。

报告中的 `healthy` 表示已经从 hash 校验通过的 piece 中成功解码一帧；
`unavailable` 表示本次检查没有及时取得数据或 aria2 报错；`inconclusive` 表示取得了
部分数据但预算内无法解码，不能据此断言种子已死。图片保存为
`video-samples/<info_hash>.jpg`，每次检查结果追加到 `video-health.jsonl`。请只下载你有
权获取的内容。

控制台每 10 秒打印一次 `[metadata-progress]` 或 `[sample-progress]`，包含当前状态、
已下载字节、连接数、做种数和速度。最终结果与 JSONL 的 `outcome` 会进一步区分：

* `no_peers`：连接数、做种数和下载量从始至终都是 0；
* `peers_no_data`：发现过 peer 或连接，但没有收到可校验数据；
* `partial_timeout`：已经下载了一部分，但停止前不足以解码；
* `sample_budget_exhausted`：达到分段预算仍没有可解码帧；
* `frame_decoded`：成功生成 JPG，抽样验证通过。

默认输出 `torrent_metadata.csv`，包含 info hash、magnet URI、资源名、总大小、
文件数、扩展名集合、创建者和创建时间。完整内部文件路径聚合在
`file_paths_json` 单元格的 JSON 数组中。重复 info hash 只保留一行；无效种子会以
`invalid` 状态记录错误原因。

`torrent_saved_time_utc` 从 `.torrent` 文件修改时间生成，因此旧数据也有值。
新 announce 热度流程成功时会追加写入 `torrents/.acquisitions.jsonl`，CSV 的
`acquisition_method=announce_heat`、`acquisition_time_utc` 和 `announce_heat` 会精确标识这些新资源。

查看 Redis 中的分类状态：

```bash
$ redis-cli SCARD magnets:metadata_valid
$ redis-cli SCARD magnets:invalid
$ redis-cli SCARD magnets:processed
$ redis-cli SCARD magnets:cold
$ redis-cli ZCARD magnets:retry_after
$ redis-cli HGET magnets:last_error 'magnet:?xt=urn:btih:...'
$ redis-cli HGET magnets:announce_count 'magnet:?xt=urn:btih:...'
$ redis-cli --scan --pattern 'magnets:announce_hour:*'
$ redis-cli ZREVRANGE 'magnets:announce_peers:<40位infohash>' 0 -1 WITHSCORES
```

`python manage.py -p` 会再次验证 `torrents/*.torrent` 的 info hash，并打印文件列表。
抓取新 hash 时仍用 `python manage.py -s`；如果默认路由不通 DHT，
先设置 `MAGNET_DHT_INTERFACE=wlp12s0`。普通 `get_peers` 只保存为原始观测，不会进入
元数据下载队列；只有通过 token 验证的 `announce_peer` 会进入热度队列。
这些 announce 中的 peer 端点会保存到 `magnets:announce_peers:<infohash>`，
为后续直连 peer 或完整 DHT `values` 响应提供数据。当前 aria2 RPC 还是
使用自己的 DHT 查找，因此“保存 endpoint”本身不会立即改变 aria2 的连接率。

## 😃 深刻的感悟

自我接触编程以来，我一直都是属于兴趣驱动的，对某种技术感兴趣的话就会花时间去研究去尝试。想成为一个有趣的人，去做一些有趣的事。

![有趣的灵魂](https://user-images.githubusercontent.com/19553554/42406008-0014fcd8-81d2-11e8-8af6-544510e59712.jpg)

真心觉得能把脑海里的想法转变为代码实现是件很棒的事，即使可能这件事在别人看来并没有什么了不起。技术发展变化总是那么快，不紧跟着可能不小心就掉队了。所以希望每个真心热爱编程的人都能不忘初心，永远保持对新技术的热情，永远能从编码中找到乐趣。

![stay real, stay wild](https://user-images.githubusercontent.com/19553554/42375144-2c90600c-814d-11e8-9824-2d5433c8e19c.png)


### License

MIT [©chenjiandongx](https://github.com/chenjiandongx)
