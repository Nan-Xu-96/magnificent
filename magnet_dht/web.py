#!/usr/bin/env python3
# coding=utf-8

"""A tiny self-hosted review catalogue built with the Python standard library.

It follows the useful part of a NeoDB-like flow (catalogue item -> rating ->
review) while keeping the existing torrent collector as an import source.  It
intentionally has no file download, media streaming, or peer-distribution
route.
"""

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from .catalog import CATEGORY_LABELS, CatalogStore


INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
CATEGORY_TABS = {"video": "视频", "audio": "音频", "image": "图片", "document": "文档", "archive": "压缩包", "other": "其他", "all": "全部"}


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _stars(value):
    if value is None:
        return '<span class="muted">暂无评分</span>'
    score = float(value)
    filled = max(0, min(5, round(score)))
    return '<span class="stars" aria-label="{} 分">{}<small> {:.1f}/5</small></span>'.format(
        _e(score), "★" * filled + "☆" * (5 - filled), score
    )


def _size(value):
    value = int(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return "{:.1f} {}".format(amount, unit) if unit != "B" else "{} B".format(value)
        amount /= 1024


def _date(value):
    if not value:
        return "未知时间"
    return str(value).replace("T", " ")[:19]


STYLE = r"""
:root { --ink:#18332f; --muted:#6b7870; --paper:#f8f6ef; --card:#fffdf8; --line:#dfe5db; --accent:#e35d3f; --accent2:#e4a93a; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:var(--paper); font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Noto Sans SC",sans-serif; line-height:1.6; }
a { color:inherit; text-decoration:none; }
.shell { width:min(1120px,calc(100% - 36px)); margin:auto; }
header { border-bottom:1px solid var(--line); background:rgba(248,246,239,.92); position:sticky; top:0; z-index:2; backdrop-filter:blur(10px); }
.nav { min-height:72px; display:flex; align-items:center; justify-content:space-between; gap:20px; }
.brand { font-weight:800; letter-spacing:-.04em; font-size:1.35rem; }
.brand span { color:var(--accent); }
.navlinks { display:flex; gap:18px; color:var(--muted); font-size:.92rem; }
main { padding:46px 0 70px; }
.hero { max-width:700px; margin:0 0 32px; }
.eyebrow { color:var(--accent); text-transform:uppercase; letter-spacing:.16em; font-size:.76rem; font-weight:800; }
h1,h2,h3,p { margin-top:0; } h1 { font-size:clamp(2.2rem,5vw,4.3rem); line-height:1.04; letter-spacing:-.06em; margin:8px 0 16px; } h2 { line-height:1.15; letter-spacing:-.035em; }
.lede { color:var(--muted); font-size:1.08rem; max-width:620px; }
.toolbar { display:flex; flex-wrap:wrap; gap:10px; margin:24px 0 32px; }
input,textarea,select { font:inherit; border:1px solid var(--line); border-radius:10px; padding:11px 13px; color:var(--ink); background:#fff; }
input:focus,textarea:focus,select:focus { outline:3px solid rgba(227,93,63,.18); border-color:var(--accent); }
.search { flex:1 1 300px; }
button,.button { border:0; cursor:pointer; border-radius:10px; padding:11px 16px; color:#fff; background:var(--ink); font:inherit; font-weight:700; display:inline-block; }
.button.alt,button.alt { background:var(--accent); } .button.ghost { color:var(--ink); background:transparent; border:1px solid var(--line); }
.meta { color:var(--muted); font-size:.88rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:20px; box-shadow:0 8px 30px rgba(30,50,42,.04); }
.card:hover { transform:translateY(-2px); transition:.18s ease; box-shadow:0 12px 30px rgba(30,50,42,.09); }
.card h3 { font-size:1.1rem; margin:10px 0 8px; overflow-wrap:anywhere; }
.index { color:var(--accent2); font-weight:800; font-size:.76rem; letter-spacing:.08em; }
.stars { color:#cc8c16; letter-spacing:.08em; white-space:nowrap; } .stars small { color:var(--muted); letter-spacing:0; font-weight:600; }
.tags { display:flex; flex-wrap:wrap; gap:7px; margin-top:14px; } .tag { background:#edf2e9; color:#4e685d; border-radius:999px; padding:3px 9px; font-size:.78rem; }
.category-tabs { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 24px; }
.category-tabs a { padding:8px 13px; border-radius:999px; border:1px solid var(--line); color:var(--muted); background:rgba(255,255,255,.55); }
.category-tabs a.active { color:#fff; border-color:var(--ink); background:var(--ink); }
.card-cover { height:150px; margin:-20px -20px 16px; overflow:hidden; border-radius:15px 15px 0 0; background:linear-gradient(135deg,#d8e6d9,#f6d3b8); }
.card-cover img { width:100%; height:100%; display:block; object-fit:cover; }
.detail-cover { width:100%; max-height:420px; object-fit:cover; border-radius:14px; margin:8px 0 18px; border:1px solid var(--line); }
.empty { border:1px dashed #bfcdbd; border-radius:16px; padding:42px; text-align:center; color:var(--muted); }
.pagination { display:flex; justify-content:center; gap:8px; margin-top:32px; }
.detail { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(260px,.8fr); gap:32px; align-items:start; }
.detail-title { font-size:clamp(2rem,5vw,4rem); margin-bottom:10px; overflow-wrap:anywhere; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:22px; }
.facts { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:24px 0; } .facts dt { color:var(--muted); font-size:.8rem; } .facts dd { margin:2px 0 0; font-weight:700; overflow-wrap:anywhere; }
.review { padding:20px 0; border-bottom:1px solid var(--line); } .review:last-child { border-bottom:0; }
.review-head { display:flex; justify-content:space-between; gap:14px; align-items:center; } .review-author { font-weight:800; }
.review-body { white-space:pre-wrap; overflow-wrap:anywhere; }
.spoiler { color:var(--muted); font-size:.8rem; }
.form-row { display:grid; grid-template-columns:1fr 1fr; gap:12px; } label { display:block; color:var(--muted); font-size:.86rem; margin:10px 0 5px; } textarea { width:100%; min-height:150px; resize:vertical; }
.flash { background:#fff2df; border:1px solid #f0d5a8; padding:12px 15px; border-radius:10px; margin-bottom:20px; }
footer { color:var(--muted); border-top:1px solid var(--line); padding:24px 0 42px; font-size:.86rem; }
.code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem; word-break:break-all; }
@media (max-width:760px) { .detail { grid-template-columns:1fr; } .navlinks { gap:10px; } .facts,.form-row { grid-template-columns:1fr; } main { padding-top:30px; } }
"""


class CatalogApplication:
    def __init__(self, store=None, torrent_dir=None):
        self.store = store or CatalogStore()
        self.torrent_dir = str(torrent_dir or os.getenv("MAGNET_DHT_TORRENT_DIR", "torrents"))
        self.media_root = str(os.getenv("MAGNET_DHT_MEDIA_ROOT", ""))
        self.cover_dir = Path(os.getenv("MAGNET_DHT_COVER_DIR", "catalog-covers")).expanduser()

    def _page(self, title, body):
        return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{} · 碟集</title><style>{}</style></head><body>
<header><div class="shell nav"><a class="brand" href="/">碟<span>集</span></a><nav class="navlinks"><a href="/">发现</a><a href="/admin/import">导入元数据</a><a href="/admin/covers">生成封面</a></nav></div></header>
<main class="shell">{}</main><footer><div class="shell">自托管影视目录 · 只展示元数据、评分与影评，不提供下载或在线播放。</div></footer>
</body></html>""".format(_e(title), STYLE, body)

    @staticmethod
    def _json(start_response, payload, status="200 OK"):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                                 ("Content-Length", str(len(data)))])
        return [data]

    @staticmethod
    def _html(start_response, body, status="200 OK"):
        data = body.encode("utf-8")
        start_response(status, [("Content-Type", "text/html; charset=utf-8"),
                                 ("Content-Length", str(len(data)))])
        return [data]

    @staticmethod
    def _redirect(start_response, location):
        start_response("303 See Other", [("Location", location), ("Content-Length", "0")])
        return [b""]

    @staticmethod
    def _body(environ):
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        raw = environ.get("wsgi.input").read(min(size, 256 * 1024)) if size else b""
        if "application/json" in (environ.get("CONTENT_TYPE") or "").lower():
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (TypeError, ValueError):
                return {}
            return {key: [value] for key, value in payload.items()} if isinstance(payload, dict) else {}
        return parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)

    @staticmethod
    def _value(params, key, default=""):
        value = params.get(key, [default])
        return value[0] if value else default

    def _home(self, environ, start_response):
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        q = self._value(query, "q").strip()
        sort = self._value(query, "sort", "recent")
        category = self._value(query, "category", "video")
        if category not in CATEGORY_TABS:
            category = "video"
        try:
            page = int(self._value(query, "page", "1"))
        except ValueError:
            page = 1
        result = self.store.list_media(q, sort, page, category=category)
        cards = []
        for item in result["items"]:
            cover = ""
            if item.get("cover_path"):
                cover = '<div class="card-cover"><img loading="lazy" src="/covers/{}.jpg" alt=""></div>'.format(_e(item["info_hash"]))
            cards.append("""<a class="card" href="/media/{hash}">
              {cover}
              <div class="index">{kind} · {count} 条影评</div><h3>{title}{year}</h3>
              <div>{stars}</div><p class="meta">{size} · {files} 个文件</p>
              <div class="tags">{tags}</div></a>""".format(
                hash=_e(item["info_hash"]), count=item["review_count"],
                cover=cover, kind=CATEGORY_LABELS.get(item.get("category"), "其他"),
                title=_e(item["title"]), year=" <span class=\"meta\">({})</span>".format(item["year"]) if item.get("year") else "", stars=_stars(item["rating_average"]),
                size=_e(_size(item["total_size"])), files=item["file_count"],
                tags="".join('<span class="tag">{}</span>'.format(_e(tag)) for tag in item.get("tags", [])),
            ))
        if cards:
            listing = '<div class="grid">{}</div>'.format("".join(cards))
        else:
            listing = '<div class="empty">还没有匹配的条目。请先到“导入元数据”扫描本地 torrent 目录。</div>'
        pages = []
        for number in range(1, result["pages"] + 1):
            if number == result["page"]:
                pages.append('<span class="button">{}</span>'.format(number))
            else:
                pages.append('<a class="button ghost" href="{}">{}</a>'.format(
                    self._query_url(q, sort, number, category), number))
        tabs = '<nav class="category-tabs" aria-label="媒体分类">{}</nav>'.format("".join(
            '<a class="{}" href="{}">{}</a>'.format(
                "active" if value == category else "", self._query_url(q, sort, 1, value), _e(label)
            ) for value, label in CATEGORY_TABS.items()
        ))
        body = """<section class="hero"><div class="eyebrow">Personal media commons</div>
<h1>把散落的元数据，<br>整理成值得回看的电影。</h1>
<p class="lede">从本地 torrent 元数据建立自己的影视目录。给条目打分、写下观后感，保留一份不被平台算法打扰的私人电影记忆。</p></section>
{tabs}<form class="toolbar" method="get"><input type="hidden" name="category" value="{category}"><input class="search" name="q" value="{q}" placeholder="搜索标题、文件名或 info hash…" autocomplete="off"><select name="sort"><option value="recent" {recent}>最近导入</option><option value="rating" {rating}>评分最高</option><option value="title" {title}>标题 A-Z</option><option value="size" {size}>体积最大</option></select><button class="alt">搜索</button></form>
<p class="meta">共 {total} 个条目 · 第 {page}/{pages} 页</p>{listing}<div class="pagination">{pagination}</div>""".format(
            q=_e(q), recent="selected" if sort == "recent" else "", rating="selected" if sort == "rating" else "",
            title="selected" if sort == "title" else "", size="selected" if sort == "size" else "",
            category=_e(category), tabs=tabs, total=result["total"], page=result["page"], pages=result["pages"], listing=listing,
            pagination="".join(pages),
        )
        return self._html(start_response, self._page("发现", body))

    @staticmethod
    def _query_url(q, sort, page, category="video"):
        return "/?q={}&sort={}&page={}&category={}".format(quote(q), quote(sort), page, quote(category))

    def _detail(self, info_hash, start_response, error=""):
        item = self.store.get_media(info_hash)
        if item is None:
            return self._html(start_response, self._page("未找到", '<div class="empty">没有找到这个条目。</div>'), "404 Not Found")
        reviews = []
        for review in item["reviews"]:
            reviews.append("""<article class="review"><div class="review-head"><span class="review-author">{author}</span><span>{stars}</span></div>
<p class="meta">{date}{spoiler}</p><p class="review-body">{body}</p></article>""".format(
                author=_e(review["author"]), stars=_stars(review["rating"]), date=_e(_date(review["created_at"])),
                spoiler=" · 含剧透" if review["spoiler"] else "", body=_e(review["body"]),
            ))
        review_section = "".join(reviews) if reviews else '<p class="meta">还没有影评，写下第一句吧。</p>'
        facts = """<dl class="facts"><div><dt>分类</dt><dd>{category}</dd></div><div><dt>条目评分</dt><dd>{stars}</dd></div><div><dt>文件数量</dt><dd>{files}</dd></div>
<div><dt>元数据体积</dt><dd>{size}</dd></div><div><dt>导入时间</dt><dd>{date}</dd></div><div><dt>创建者</dt><dd>{created}</dd></div><div><dt>格式</dt><dd>{extensions}</dd></div></dl>""".format(
            category=_e(CATEGORY_LABELS.get(item.get("category"), "其他")),
            stars=_stars(item["rating_average"]), files=item["file_count"], size=_e(_size(item["total_size"])),
            date=_e(_date(item["imported_at"])), created=_e(item["created_by"] or "未知"),
            extensions=_e(", ".join(item["file_extensions"]) or "未知"),
        )
        error_box = '<div class="flash">{}</div>'.format(_e(error)) if error else ""
        paths = "<details><summary>查看文件清单（{} 项）</summary><ul>{}</ul></details>".format(
            item["file_count"], "".join("<li class=\"code\">{}</li>".format(_e(path)) for path in item["file_paths"][:500])
        )
        cover = '<img class="detail-cover" src="/covers/{}.jpg" alt="{}">'.format(_e(item["info_hash"]), _e(item["title"])) if item.get("cover_path") else ""
        body = """<p class="meta"><a href="/">← 返回发现</a></p><div class="detail"><section>
<div class="eyebrow">{category}</div>{cover}<h1 class="detail-title">{title}{year}</h1><p class="code meta">info hash · {hash}</p>{facts}{paths}
<section id="reviews" style="margin-top:38px"><h2>影评 · {count}</h2><div class="panel">{reviews}</div></section></section>
<aside class="panel"><h2>写下你的看法</h2>{error}<form method="post" action="/media/{hash}/reviews"><label for="author">署名</label><input id="author" name="author" maxlength="80" placeholder="匿名"><div class="form-row"><div><label for="rating">评分</label><select id="rating" name="rating"><option value="5">★★★★★ 5</option><option value="4">★★★★☆ 4</option><option value="3">★★★☆☆ 3</option><option value="2">★★☆☆☆ 2</option><option value="1">★☆☆☆☆ 1</option></select></div><div><label for="spoiler">内容</label><select id="spoiler" name="spoiler"><option value="0">不含剧透</option><option value="1">含剧透</option></select></div></div><label for="body">影评</label><textarea id="body" name="body" required minlength="3" maxlength="5000" placeholder="这部作品让你想到什么？"></textarea><button class="alt" style="margin-top:12px">发布影评</button></form></aside></div>""".format(
            category=_e(CATEGORY_LABELS.get(item.get("category"), "其他")), cover=cover, title=_e(item["title"]), year=" <span class=\"meta\">({})</span>".format(item["year"]) if item.get("year") else "", hash=_e(item["info_hash"]), facts=facts, paths=paths,
            count=item["review_count"], reviews=review_section, error=error_box,
        )
        return self._html(start_response, self._page(item["title"], body))

    def _import_page(self, start_response, result=None):
        report = ""
        if result is not None:
            report = '<div class="flash">扫描 {}：{} 个文件，导入 {} 个，跳过 {} 个无效文件。</div>'.format(
                _e(result["directory"]), result["scanned"], result["imported"], result["invalid"])
            if result.get("errors"):
                report += "<details><summary>查看无效文件</summary><ul>{}</ul></details>".format(
                    "".join("<li>{}</li>".format(_e(error)) for error in result["errors"])
                )
        body = """<section class="hero"><div class="eyebrow">Local admin</div><h1>导入你的元数据。</h1>
<p class="lede">扫描一个本地目录中的 <code>*.torrent</code> 文件。站点只读取并保存标题、文件清单、体积和 info hash 等描述信息。</p></section>{report}<div class="panel" style="max-width:680px"><form method="post"><label for="directory">torrent 目录</label><input id="directory" name="directory" value="{directory}" style="width:100%"><button class="alt" style="margin-top:14px">开始扫描</button></form></div>""".format(report=report, directory=_e(self.torrent_dir))
        return self._html(start_response, self._page("导入元数据", body))

    def _cover_page(self, start_response, result=None):
        report = ""
        if result is not None:
            report = '<div class="flash">已生成 {} 张封面，跳过 {} 个条目。</div>'.format(result["generated"], result["skipped"])
            if result.get("errors"):
                report += "<details><summary>查看跳过原因</summary><ul>{}</ul></details>".format(
                    "".join("<li>{}</li>".format(_e(error)) for error in result["errors"])
                )
        body = """<section class="hero"><div class="eyebrow">Local artwork</div><h1>给视频加一张封面。</h1>
<p class="lede">从本机已有的视频文件中随机抽取一帧，保存为目录封面。这里不会解析、下载或连接磁力链接。</p></section>{report}<div class="panel" style="max-width:680px"><form method="post"><label for="media_root">本地媒体目录</label><input id="media_root" name="media_root" value="{media_root}" placeholder="例如 /data/movies" style="width:100%"><p class="meta">需要本机安装 ffmpeg/ffprobe，并且文件路径能和 torrent 清单对应。</p><button class="alt" style="margin-top:8px">生成随机帧封面</button></form></div>""".format(
            report=report, media_root=_e(self.media_root)
        )
        return self._html(start_response, self._page("生成封面", body))

    def _cover_response(self, info_hash, start_response):
        if not INFO_HASH_RE.match(info_hash):
            return self._html(start_response, self._page("未找到", '<div class="empty">封面标识无效。</div>'), "404 Not Found")
        path = (self.cover_dir / (info_hash.lower() + ".jpg")).resolve()
        try:
            path.relative_to(self.cover_dir.resolve())
        except ValueError:
            return self._html(start_response, self._page("未找到", '<div class="empty">封面不存在。</div>'), "404 Not Found")
        if not path.is_file():
            return self._html(start_response, self._page("未找到", '<div class="empty">封面不存在。</div>'), "404 Not Found")
        data = path.read_bytes()
        start_response("200 OK", [("Content-Type", "image/jpeg"), ("Cache-Control", "public, max-age=3600"), ("Content-Length", str(len(data)))])
        return [data]

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = unquote(environ.get("PATH_INFO", "/"))
        try:
            if path == "/static/site.css":
                data = STYLE.encode("utf-8")
                start_response("200 OK", [("Content-Type", "text/css; charset=utf-8"), ("Content-Length", str(len(data)))])
                return [data]
            if path == "/healthz":
                return self._json(start_response, {"ok": True})
            if path.startswith("/covers/") and method == "GET":
                return self._cover_response(path.rsplit("/", 1)[-1].removesuffix(".jpg"), start_response)
            if path == "/api/media" and method == "GET":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                return self._json(start_response, self.store.list_media(self._value(query, "q"), self._value(query, "sort", "recent"), self._value(query, "page", "1"), category=self._value(query, "category", "video")))
            if path.startswith("/api/media/") and method == "POST" and path.endswith("/reviews"):
                parts = path.strip("/").split("/")
                info_hash = parts[2] if len(parts) == 4 else ""
                if not INFO_HASH_RE.match(info_hash):
                    return self._json(start_response, {"error": "invalid info hash"}, "400 Bad Request")
                params = self._body(environ)
                try:
                    self.store.add_review(
                        info_hash, self._value(params, "author"),
                        self._value(params, "rating", "0"), self._value(params, "body"),
                        self._value(params, "spoiler") in ("1", 1, True),
                    )
                except (ValueError, KeyError) as exc:
                    return self._json(start_response, {"error": str(exc)}, "400 Bad Request")
                return self._json(start_response, self.store.get_media(info_hash), "201 Created")
            if path.startswith("/api/media/") and method == "GET":
                info_hash = path.rsplit("/", 1)[-1]
                if not INFO_HASH_RE.match(info_hash):
                    return self._json(start_response, {"error": "invalid info hash"}, "400 Bad Request")
                item = self.store.get_media(info_hash)
                return self._json(start_response, item or {"error": "not found"}, "200 OK" if item else "404 Not Found")
            if path == "/api/import" and method == "POST":
                params = self._body(environ)
                directory = self._value(params, "directory", self.torrent_dir)
                return self._json(start_response, self.store.import_torrents(directory))
            if path == "/" and method == "GET":
                return self._home(environ, start_response)
            if path == "/admin/import":
                if method == "GET":
                    return self._import_page(start_response)
                if method == "POST":
                    params = self._body(environ)
                    return self._import_page(start_response, self.store.import_torrents(self._value(params, "directory", self.torrent_dir)))
            if path == "/admin/covers":
                if method == "GET":
                    return self._cover_page(start_response)
                if method == "POST":
                    params = self._body(environ)
                    media_root = self._value(params, "media_root", self.media_root)
                    return self._cover_page(start_response, self.store.generate_covers(media_root=media_root, cover_dir=self.cover_dir))
            if path.startswith("/media/"):
                segments = path.strip("/").split("/")
                info_hash = segments[1] if len(segments) > 1 else ""
                if not INFO_HASH_RE.match(info_hash):
                    return self._html(start_response, self._page("未找到", '<div class="empty">条目标识无效。</div>'), "404 Not Found")
                if method == "GET":
                    return self._detail(info_hash, start_response)
                if method == "POST" and len(segments) == 3 and segments[2] == "reviews":
                    params = self._body(environ)
                    try:
                        self.store.add_review(info_hash, self._value(params, "author"), self._value(params, "rating", "0"), self._value(params, "body"), self._value(params, "spoiler") == "1")
                    except (ValueError, KeyError) as exc:
                        return self._detail(info_hash, start_response, str(exc))
                    return self._redirect(start_response, "/media/{}#reviews".format(info_hash))
            return self._html(start_response, self._page("未找到", '<div class="empty">页面不存在。</div>'), "404 Not Found")
        except Exception:
            return self._html(start_response, self._page("服务器错误", '<div class="empty">发生了一个内部错误，请查看服务端日志。</div>'), "500 Internal Server Error")


def create_app(store=None, torrent_dir=None):
    """Return a WSGI application (usable with gunicorn, waitress or wsgiref)."""
    return CatalogApplication(store=store, torrent_dir=torrent_dir)


def run_server(host="127.0.0.1", port=8080, store=None, torrent_dir=None):
    from wsgiref.simple_server import make_server

    app = create_app(store=store, torrent_dir=torrent_dir)
    with make_server(host, int(port), app) as server:
        print("catalog site listening on http://{}:{}".format(host, port))
        server.serve_forever()
