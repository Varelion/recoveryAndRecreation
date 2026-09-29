"""Crawl a Google Site and mirror each page as Markdown under ./website."""
import hashlib
import re
import sys
import urllib.parse as up
from collections import deque
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

HOST = "https://sites.google.com"
PREFIX = "/view/real-tales-of-nymrathis"
OUT = Path(sys.argv[1])
IMAGES = OUT / "images"
BLOCK = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "tr"}

http = requests.Session()
images: dict[str, str] = {}


def slug(path: str) -> str:
    rel = path[len(PREFIX):].strip("/")
    return rel or "home"


def page_file(path: str) -> Path:
    return OUT / f"{slug(path)}.md"


def rel_link(src: Path, path: str, frag: str) -> str:
    target = up.quote(Path(up.unquote(str(page_file(path).relative_to(OUT)))).as_posix())
    depth = len(src.relative_to(OUT).parts) - 1
    return "../" * depth + target + (f"#{frag}" if frag else "")


def fetch_image(url: str, src: Path) -> str:
    # Download once; name by URL hash since Google image URLs are opaque.
    if url not in images:
        r = http.get(url, timeout=60)
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(r.headers.get("content-type", ""), ".jpg")
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
        IMAGES.mkdir(parents=True, exist_ok=True)
        (IMAGES / name).write_bytes(r.content)
        images[url] = name
    depth = len(src.relative_to(OUT).parts) - 1
    return "../" * depth + "images/" + images[url]


def unwrap_google(href: str) -> str:
    # google.com/url?q=<real> redirect wrapper
    p = up.urlparse(href)
    if p.netloc.endswith("google.com") and p.path == "/url":
        return up.parse_qs(p.query).get("q", [href])[0]
    return href


def internal(href: str) -> tuple[str, str] | None:
    p = up.urlparse(up.urljoin(HOST + PREFIX + "/", href))
    if p.netloc != "sites.google.com" or not p.path.startswith(PREFIX):
        return None
    return p.path.rstrip("/") or PREFIX, p.fragment


def inline(node, src: Path, links: set) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    if node.name in ("script", "style", "noscript"):
        return ""
    if node.name == "br":
        return "  \n"
    if node.name == "img":
        url = node.get("src", "")
        return f"![{node.get('alt', '')}]({fetch_image(url, src)})" if url.startswith("http") else ""
    if node.name == "iframe":
        url = node.get("src") or node.get("data-src") or ""
        return f"[embed]({url})" if url else ""

    text = "".join(inline(c, src, links) for c in node.children)
    if node.name == "a" and node.get("href"):
        href = unwrap_google(node["href"])

        # In-page TOC entry: point at the Markdown heading anchor, e.g. "#how-to-earn-money".
        if href.startswith("#"):
            anchor = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
            return f"[{text.strip()}](#{anchor})" if text.strip() else ""

        hit = internal(href)
        if hit:
            links.add(hit[0])
            href = rel_link(src, *hit)
        return f"[{text.strip()}]({href})" if text.strip() else ""

    style = node.get("style", "")
    bold = node.name in ("b", "strong") or re.search(r"font-weight:\s*(bold|[6-9]00)", style)
    italic = node.name in ("i", "em") or "font-style: italic" in style
    core = text.strip()
    if not core:
        return text
    if bold:
        core = f"**{core}**"
    if italic:
        core = f"*{core}*"
    lead = " " if text[:1].isspace() else ""
    trail = " " if text[-1:].isspace() else ""
    return lead + core + trail


def block(node, src: Path, links: set, depth: int = 0) -> list[str]:
    # Emit Markdown lines for block containers; delegate inline runs to inline().
    if isinstance(node, NavigableString) or not isinstance(node, Tag):
        t = inline(node, src, links).strip()
        return [t] if t else []

    name = node.name
    if re.fullmatch(r"h[1-6]", name):
        t = inline(node, src, links).strip().replace("**", "")
        return [f"{'#' * int(name[1])} {t}", ""] if t else []
    if name in ("ul", "ol"):
        out = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            marker = f"{i}." if name == "ol" else "-"
            sub = block(li, src, links, depth + 1)
            body = [s for s in sub if s]
            if not body:
                continue
            out.append("  " * depth + f"{marker} {body[0].lstrip()}")
            out += ["  " * (depth + 1) + s.lstrip() if not s.lstrip().startswith(("-", "1")) else s for s in body[1:]]
        return out + ([""] if depth == 0 else [])
    if name == "table":
        rows = [[inline(td, src, links).strip().replace("|", "\\|") for td in tr.find_all(["td", "th"])] for tr in node.find_all("tr")]
        rows = [r for r in rows if r]
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return out + [""]

    # Generic container: group consecutive inline children into one paragraph.
    if not any(isinstance(c, Tag) and (c.name in BLOCK or c.find(BLOCK)) for c in node.children):
        t = inline(node, src, links).strip()
        return [t, ""] if t and depth == 0 else [t] if t else []
    out, run = [], []
    for c in node.children:
        if isinstance(c, Tag) and (c.name in BLOCK or c.find(BLOCK)):
            if "".join(run).strip():
                out += ["".join(run).strip(), ""]
            run = []
            out += block(c, src, links, depth)
        else:
            run.append(inline(c, src, links))
    if "".join(run).strip():
        out += ["".join(run).strip(), ""]
    return out


def convert(path: str, html: str) -> tuple[str, set]:
    soup = BeautifulSoup(html, "html.parser")
    src = page_file(path)
    links: set = set()

    # Nav links discover the full page tree.
    for a in soup.find_all("a", href=True):
        hit = internal(a["href"])
        if hit:
            links.add(hit[0])

    title = soup.title.get_text(strip=True) if soup.title else slug(path)
    lines = [f"<!-- source: {HOST}{path} -->", ""]
    for sec in soup.find_all("section"):
        lines += block(sec, src, links)
        lines.append("")
    md = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
    if not md.lstrip().split("\n", 2)[-1].startswith("#"):
        md = md.replace("-->\n", f"-->\n\n# {title}\n", 1)
    return md, links


def main() -> None:
    seen, queue = {PREFIX}, deque([PREFIX])
    while queue:
        path = queue.popleft()
        r = http.get(HOST + path, timeout=60)
        if r.status_code != 200:
            print(f"skip {r.status_code} {path}")
            continue
        md, links = convert(path, r.text)
        dest = page_file(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(md, encoding="utf-8")
        print(f"ok {dest.relative_to(OUT)}")
        for link in sorted(links - seen):
            seen.add(link)
            queue.append(link)


if __name__ == "__main__":
    main()
