# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py", "msgpack", "pygments", "pillow"]
# ///
# markdown preview for a wezterm split, drawn the way markview drew it inside
# nvim. follows an nvim over its pipe so it updates as you type, and mermaid
# and images are real images through wezterm's iterm image protocol
import argparse, base64, hashlib, io, json, msvcrt, os, queue, re, shutil, subprocess, sys, threading, time
import unicodedata, urllib.request
from collections import namedtuple
from pathlib import Path

import msgpack
from markdown_it import MarkdownIt
from PIL import Image
from pygments import lex
from pygments.lexers import get_lexer_by_name
from pygments.token import Token
from pygments.util import ClassNotFound

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "md-view"
GUTTER = 3
events = queue.Queue()


def log(*a):
    if os.environ.get("MDV_DEBUG"):
        with open(CACHE / "debug.log", "a", encoding="utf-8") as f:
            f.write(" ".join(map(str, a)) + "\n")

# ---------------------------------------------------------------- nvim

# everything the preview colours itself from. the Markview ones only exist
# once markview is loaded, the lua below requires it for that
GROUPS = ["Normal", "Comment", "CursorLine", "DiagnosticError", "@markup.quote", "@markup.list.markdown",
          "MarkviewCode", "MarkviewInlineCode", "MarkviewHyperlink", "MarkviewImage", "MarkviewPalette0Fg",
          "MarkviewIcon3Fg", "MarkviewTableHeader", "MarkviewTableBorder", "MarkviewTableAlignLeft",
          "MarkviewTableAlignRight", "MarkviewTableAlignCenter", "MarkviewListItemMinus", "MarkviewListItemPlus",
          "MarkviewListItemStar"]
GROUPS += [f"@markup.heading.{i}.markdown" for i in range(1, 7)]
GROUPS += [f"MarkviewGradient{i}" for i in range(1, 10)]
GROUPS += ["MarkviewBlockQuote" + k for k in ("Default", "Note", "Ok", "Special", "Warn", "Error")]
GROUPS += ["MarkviewCheckbox" + k for k in ("Checked", "Unchecked", "Pending", "Cancelled", "Progress", "Striked")]
SYNTAX = [
    (Token.Comment, "@comment"), (Token.Keyword.Constant, "@boolean"), (Token.Keyword.Type, "@type.builtin"),
    (Token.Keyword, "@keyword"), (Token.Operator.Word, "@keyword"), (Token.Name.Builtin.Pseudo, "@variable.builtin"),
    (Token.Name.Builtin, "@function.builtin"), (Token.Name.Function, "@function"), (Token.Name.Class, "@type"),
    (Token.Name.Decorator, "@attribute"), (Token.Name.Tag, "@tag"), (Token.Name.Attribute, "@property"),
    (Token.Name.Namespace, "@module"), (Token.Name.Constant, "@constant"), (Token.Name.Label, "@label"),
    (Token.Name, "@variable"), (Token.Literal.String.Escape, "@string.escape"),
    (Token.Literal.String.Regex, "@string.regexp"), (Token.Literal.String, "@string"),
    (Token.Literal.Number, "@number"), (Token.Literal, "@constant"), (Token.Operator, "@operator"),
    (Token.Punctuation, "@punctuation.delimiter"),
]
GROUPS += sorted({g for _, g in SYNTAX})

# runs inside nvim. pushes the buffer, cursor and colours to this channel on
# every change, so nothing here ever has to ask
COLORS = r"""
local function colors()
  local t = {}
  for _, g in ipairs(groups) do
    local h = vim.api.nvim_get_hl(0, { name = g, link = false })
    t[g] = { fg = h.fg, bg = h.bg, bold = h.bold, italic = h.italic, underline = h.underline, strikethrough = h.strikethrough }
  end
  return t
end"""

ATTACH = r"""
local chan, pane, pin, groups = ...
vim.g.mdv_pane = pane
pcall(require, "markview")
local group = vim.api.nvim_create_augroup("mdv", { clear = true })
local function send(...)
  if not pcall(vim.rpcnotify, chan, ...) then
    pcall(vim.api.nvim_del_augroup_by_id, group)
    if vim.g.mdv_pane == pane then vim.g.mdv_pane = nil end
  end
end
COLORS
-- the same icon and name markview put on each code block label
local function icons(lines)
  local t, ok, fts = {}, pcall(require, "markview.filetypes")
  for _, l in ipairs(lines) do
    local lang = l:match("^%s*[`~][`~][`~]+%s*([%w_+#.-]+)")
    if lang and not t[lang] then
      local icon, hl, name = "", nil, lang
      if ok then
        local c = fts.get(lang)
        icon, hl, name = vim.trim(c.icon or ""), c.icon_hl, c.name or lang
      end
      local h = hl and vim.api.nvim_get_hl(0, { name = hl, link = false }) or {}
      t[lang] = { icon, h.fg or vim.NIL, name }
    end
  end
  return t
end
local shown = pin ~= vim.NIL and pin or nil
local function md(b)
  return vim.api.nvim_buf_is_valid(b) and vim.bo[b].filetype == "markdown" and vim.bo[b].buftype == ""
end
local function text(b)
  if pin ~= vim.NIL and b ~= pin then return end
  if not md(b) then return end
  shown = b
  local lines = vim.api.nvim_buf_get_lines(b, 0, -1, false)
  send("text", b, vim.api.nvim_buf_get_name(b), lines, icons(lines))
end
local function cursor()
  if vim.api.nvim_get_current_buf() ~= shown then return end
  send("cursor", vim.fn.line("."), vim.fn.winline())
end
vim.api.nvim_create_autocmd({ "BufEnter", "FileType", "TextChanged", "TextChangedI", "TextChangedP" }, {
  group = group, callback = function(ev) text(ev.buf); cursor() end,
})
vim.api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI", "WinScrolled" }, { group = group, callback = cursor })
-- after markview has redone its groups for the new scheme
vim.api.nvim_create_autocmd("ColorScheme", {
  group = group, callback = vim.schedule_wrap(function()
    send("colors", colors())
    if shown then text(shown) end
  end),
})
send("colors", colors())
text(shown or vim.api.nvim_get_current_buf())
cursor()
"""

ATTACH = ATTACH.replace("COLORS", COLORS)


class Nvim:
    def __init__(self, addr):
        # opened as a file, a named pipe reads and writes like one
        self.f = open(addr, "r+b", buffering=0)
        self.unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        self.id = 0

    def request(self, method, *args):
        # only before listen(), a windows pipe cant read on one thread while
        # another writes
        self.id += 1
        self.f.write(msgpack.packb([0, self.id, method, list(args)]))
        while True:
            for msg in self.unpacker:
                if msg[0] == 1 and msg[1] == self.id:
                    if msg[2]:
                        raise RuntimeError(msg[2])
                    return msg[3]
                if msg[0] == 2:
                    events.put(("rpc", msg[1], msg[2]))
            data = self.f.read(65536)
            if not data:
                raise EOFError
            self.unpacker.feed(data)

    def listen(self):
        def run():
            try:
                while True:
                    for msg in self.unpacker:
                        if msg[0] == 2:
                            events.put(("rpc", msg[1], msg[2]))
                    data = self.f.read(65536)
                    if not data:
                        break
                    self.unpacker.feed(data)
            except OSError:
                pass
            events.put(("eof",))
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self.f.close()


def nvim_pipes():
    return [r"\\.\pipe" + "\\" + p for p in os.listdir(r"\\.\pipe" + "\\") if p.startswith("nvim")]


# ---------------------------------------------------------------- styles

Style = namedtuple("Style", "fg bg bold italic ul strike", defaults=(None,) * 6)


def merge(a, b):
    return Style(*(y if y is not None else x for x, y in zip(a, b)))


def sgr(s):
    p = ["0"]
    if s.bold: p.append("1")
    if s.italic: p.append("3")
    if s.ul: p.append("4")
    if s.strike: p.append("9")
    if s.fg: p.append("38;2;%d;%d;%d" % s.fg)
    if s.bg: p.append("48;2;%d;%d;%d" % s.bg)
    return "\x1b[" + ";".join(p) + "m"


def rgb(n):
    return None if n is None else (n >> 16 & 255, n >> 8 & 255, n & 255)


def hexc(c):
    return "#%02x%02x%02x" % c if c else None


class Theme:
    def __init__(self, hl=None):
        self.hl = hl or {}

    def get(self, g):
        # an empty lua table comes over as a list
        h = self.hl.get(g)
        return h if isinstance(h, dict) else {}

    def fg(self, g):
        return rgb(self.get(g).get("fg"))

    def bg(self, g):
        return rgb(self.get(g).get("bg"))

    def st(self, g, bg=False):
        h = self.get(g)
        return Style(rgb(h.get("fg")), rgb(h.get("bg")) if bg else None, h.get("bold") or None,
                     h.get("italic") or None, h.get("underline") or None, h.get("strikethrough") or None)

    def syntax(self, tt):
        for t, g in SYNTAX:
            if tt in t:
                return self.st(g)
        return Style()


# ---------------------------------------------------------------- text helpers


def cw(ch):
    if ord(ch) < 32 or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def sw(s):
    return sum(cw(c) for c in s)


def spans_w(spans):
    return sum(sw(t) for t, _ in spans)


def cut(spans, width):
    out, n = [], 0
    for t, s in spans:
        if n + sw(t) <= width:
            out.append((t, s)); n += sw(t)
            continue
        part = ""
        for c in t:
            if n + cw(c) > width:
                break
            part += c; n += cw(c)
        if part:
            out.append((part, s))
        break
    return out


def wrap(spans, width, indent=0):
    rows, col = [[]], 0
    for text, st in spans:
        for w in re.findall(r"\s+|\S+", text):
            n = sw(w)
            if col + n > width and col > indent:
                if w.isspace():
                    continue
                rows.append([(" " * indent, Style())] if indent else []); col = indent
            while col + n > width and n > 1:
                head = cut([(w, st)], width - col)
                if not head:
                    break
                rows[-1].append(head[0]); w = w[len(head[0][0]):]; n = sw(w)
                rows.append([(" " * indent, Style())] if indent else []); col = indent
            rows[-1].append((w, st)); col += n
    return rows


class Row:
    __slots__ = ("spans", "src", "indent", "img", "sign")

    def __init__(self, spans=None, src=None, indent=0, img=None, sign=None):
        self.spans, self.src, self.indent, self.img, self.sign = spans or [], src, indent, img, sign


# ---------------------------------------------------------------- images


class Images:
    """images by key, loaded off the main thread. a key is a path or url"""

    def __init__(self):
        self.state = {}
        self.payloads = {}

    def get(self, key):
        s = self.state.get(key)
        if s is None:
            self.state[key] = ("loading",)
            threading.Thread(target=self.load, args=(key,), daemon=True).start()
            return ("loading",)
        return s

    def put(self, key, path):
        try:
            with Image.open(path) as im:
                self.state[key] = ("ready", str(path), im.width, im.height)
        except FileNotFoundError:
            self.state[key] = ("error", "not found: " + Path(key).name)
        except Exception as e:
            self.state[key] = ("error", str(e))

    def load(self, key):
        try:
            path = key
            if re.match(r"https?://", key):
                path = CACHE / ("img-" + hashlib.sha1(key.encode()).hexdigest()[:16] + Path(key.split("?")[0]).suffix)
                if not path.exists():
                    req = urllib.request.Request(key, headers={"User-Agent": "md-view"})
                    path.write_bytes(urllib.request.urlopen(req, timeout=15).read())
            if str(path).lower().endswith(".svg"):
                png = CACHE / ("svg-" + hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".png")
                if not png.exists():
                    subprocess.run(["magick", "-background", "none", "-density", "192", str(path), str(png)],
                                   check=True, capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                path = png
            self.put(key, path)
        except Exception as e:
            self.state[key] = ("error", str(e).splitlines()[0] if str(e) else type(e).__name__)
        events.put(("image",))

    def payload(self, path, cols, rows, r0, r1, cell):
        # scaled to the cells it covers and cropped to the rows on screen, so
        # nothing ever spills past the pane and makes it scroll
        k = (path, cols, rows, r0, r1, cell)
        if k not in self.payloads:
            with Image.open(path) as im:
                w = cols * cell[0]
                h = max(1, round(im.height * w / im.width))
                im = im.convert("RGBA").resize((w, h), Image.LANCZOS)
                im = im.crop((0, min(h, r0 * cell[1]), w, min(h, r1 * cell[1])))
                buf = io.BytesIO()
                im.save(buf, "PNG")
            data = buf.getvalue()
            if len(self.payloads) > 64:
                self.payloads.clear()
            self.payloads[k] = (len(data), base64.b64encode(data).decode())
        return self.payloads[k]


class Mermaid:
    """mermaid.mjs keeps a browser open, one render per block at a time with
    only the newest text for each block kept while typing"""

    def __init__(self):
        self.state = {}  # key -> ("ready", path) | ("error", msg) | ("loading",)
        self.want = {}  # slot -> (key, code, config)
        self.cv = threading.Condition()
        self.proc = None
        self.dead = None
        threading.Thread(target=self.run, daemon=True).start()

    def get(self, slot, code, config):
        key = hashlib.sha1((code + json.dumps(config, sort_keys=True)).encode()).hexdigest()[:20]
        if key in self.state:
            return key, self.state[key]
        out = CACHE / f"mmd-{key}.png"
        if out.exists():
            self.state[key] = ("ready", str(out))
            return key, self.state[key]
        if self.dead:
            return key, ("error", self.dead)
        with self.cv:
            self.want[slot] = (key, code, config)
            self.cv.notify()
        return key, ("loading",)

    def start(self):
        # global modules sit next to the mmdc shim on windows. npm root -g
        # works too but it goes through cmd, which renames the pane
        mmdc = shutil.which("mmdc")
        root = str(Path(mmdc).parent / "node_modules") if mmdc else ""
        if not (Path(root) / "@mermaid-js" / "mermaid-cli").exists():
            root = subprocess.run("npm root -g", shell=True, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace").stdout.strip()
        if not (Path(root) / "@mermaid-js" / "mermaid-cli").exists():
            raise RuntimeError("needs npm i -g @mermaid-js/mermaid-cli")
        browser = os.environ.get("MDV_BROWSER") or next(
            (p for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Google\Chrome\Application\chrome.exe"] if Path(p).exists()), None)
        if not browser:
            raise RuntimeError("no edge or chrome found, set MDV_BROWSER")
        self.proc = subprocess.Popen(["node", str(HERE / "mermaid.mjs"), root, browser], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        if '"ready"' not in self.proc.stdout.readline():
            raise RuntimeError("mermaid renderer didnt start")

    def run(self):
        while True:
            with self.cv:
                while not self.want:
                    self.cv.wait()
                jobs, self.want = list(self.want.values()), {}
            try:
                if not self.proc:
                    self.start()
                for key, code, config in jobs:
                    if key in self.state and self.state[key][0] != "loading":
                        continue
                    out = CACHE / f"mmd-{key}.png"
                    tmp = out.with_suffix(".tmp")
                    self.proc.stdin.write(json.dumps({"code": code, "out": str(tmp), "config": config}) + "\n")
                    self.proc.stdin.flush()
                    r = json.loads(self.proc.stdout.readline())
                    if r["ok"]:
                        tmp.replace(out)
                        self.state[key] = ("ready", str(out))
                    else:
                        self.state[key] = ("error", r["error"])
                    events.put(("image",))
            except Exception as e:
                self.dead = str(e) or "mermaid renderer died"
                for key, _, _ in jobs:
                    self.state[key] = ("error", self.dead)
                events.put(("image",))


def mermaid_config(t):
    n, code, ic = t.fg("Normal") or (211, 198, 170), t.bg("MarkviewCode") or (45, 45, 66), \
        t.bg("MarkviewInlineCode") or (52, 52, 75)
    line, accent, note = t.fg("Comment") or n, t.fg("@markup.heading.5.markdown") or n, \
        t.fg("@markup.heading.3.markdown") or n
    v = {k: hexc(c) for k, c in {
        "primaryColor": code, "primaryTextColor": n, "primaryBorderColor": accent, "secondaryColor": ic,
        "tertiaryColor": code, "lineColor": line, "textColor": n, "mainBkg": code, "nodeBorder": accent,
        "clusterBkg": code, "clusterBorder": line, "edgeLabelBackground": code, "titleColor": n,
        "noteBkgColor": ic, "noteTextColor": n, "noteBorderColor": note, "actorBkg": code, "actorBorder": accent,
        "actorTextColor": n, "actorLineColor": line, "signalColor": n, "signalTextColor": n,
        "labelBoxBkgColor": code, "labelBoxBorderColor": accent, "labelTextColor": n, "loopTextColor": n,
        "activationBkgColor": ic, "activationBorderColor": accent, "sequenceNumberColor": code,
    }.items()}
    v.update(darkMode=True, fontFamily="FiraCode Nerd Font, Fira Code, monospace", fontSize="14px")
    return {"theme": "base", "themeVariables": v}


# ---------------------------------------------------------------- markdown

CALLOUTS = {
    "ABSTRACT": ("\U000f126b", "Note"), "SUMMARY": ("\U000f126b", "Note"), "TLDR": ("\U000f126b", "Note"),
    "TODO": ("\ue69c", "Note"), "INFO": ("\ue66a", "Note"), "SUCCESS": ("\U000f05e0", "Ok"),
    "CHECK": ("\U000f05e0", "Ok"), "DONE": ("\U000f05e0", "Ok"), "QUESTION": ("\U000f02d7", "Warn"),
    "HELP": ("\U000f02d7", "Warn"), "FAQ": ("\U000f02d7", "Warn"), "FAILURE": ("\U000f0159", "Error"),
    "FAIL": ("\U000f0159", "Error"), "MISSING": ("\U000f0159", "Error"), "DANGER": ("\uf06d", "Error"),
    "ERROR": ("\uf06d", "Error"), "BUG": ("\uf46f", "Error"), "EXAMPLE": ("\U000f15ab", "Special"),
    "QUOTE": ("\uf10d", "Default"), "CITE": ("\uf10d", "Default"), "HINT": ("\uf400", "Ok"),
    "ATTENTION": ("\uea6c", "Warn"), "NOTE": ("\U000f02fd", "Note"), "TIP": ("\uf400", "Ok"),
    "IMPORTANT": ("\ueb42", "Special"), "WARNING": ("\uea6c", "Warn"), "CAUTION": ("\U000f0ce6", "Error"),
}
CHECKBOXES = {
    " ": ("\U000f0130", "Unchecked", "Unchecked"), "x": ("\U000f05e0", "Checked", "Checked"),
    "X": ("\U000f05e0", "Checked", "Checked"), "-": ("\U000f0376", "Cancelled", "Striked"),
    "/": ("\U000f1396", "Pending", None),
}
MARKERS = {"-": ("\u25cf", "MarkviewListItemMinus"), "+": ("\u25c8", "MarkviewListItemPlus"),
           "*": ("\u25c7", "MarkviewListItemStar")}
LINK, GITHUB, URI, IMG = "\U000f0337 ", "\uea84 ", "\ueb15 ", "\U000f0976 "


class Node:
    __slots__ = ("tok", "children")

    def __init__(self, tok):
        self.tok, self.children = tok, []

    @property
    def type(self):
        return self.tok.type.removesuffix("_open")


def tree(tokens):
    root = Node(None)
    stack = [root]
    for t in tokens:
        if t.nesting == 1:
            n = Node(t); stack[-1].children.append(n); stack.append(n)
        elif t.nesting == -1:
            stack.pop()
        else:
            stack[-1].children.append(Node(t))
    return root


class Doc:
    """markdown lines -> rows. blank lines are kept where they were, so the
    preview lines up with the source the way markview's split did"""

    md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

    def __init__(self, view, lines, path, icons):
        self.v, self.t, self.lines, self.icons = view, view.theme, lines, icons or {}
        self.dir = Path(path).parent if path else Path.cwd()
        self.mermaid_n = 0
        self.normal = Style(self.t.fg("Normal"))

    def render(self, width):
        root = tree(self.md.parse("\n".join(self.lines)))
        return self.blocks(root.children, width, 0, 0, len(self.lines))

    def blocks(self, nodes, width, col, start, end):
        rows, at, prev = [], start, None
        for n in nodes:
            m = n.tok.map
            if m:
                gap = m[0] - at
                # a table's borders sit on the blank lines around it
                if n.type == "table" and gap > 0: gap -= 1
                if prev == "table" and gap > 0: gap -= 1
                rows += [Row(src=at + i) for i in range(gap)]
                at = m[1]
            rows += self.block(n, width, col)
            prev = n.type
        if end is not None:
            gap = end - at - (1 if prev == "table" and end > at else 0)
            rows += [Row(src=at + i) for i in range(max(0, gap))]
        return rows

    def block(self, n, width, col):
        t = n.type
        if t == "heading":
            lvl = int(n.tok.tag[1])
            st = merge(self.normal, merge(self.t.st(f"@markup.heading.{lvl}.markdown"), Style(bold=True)))
            rows = self.para(n.children[0].tok, width, col, st, n.tok.map[0])
            if n.tok.markup in ("=", "-"):
                rows.append(Row([(("\u2582" if lvl == 1 else "\u2581") * width, st)], src=n.tok.map[0] + 1))
            return rows
        if t == "paragraph":
            return self.para(n.children[0].tok, width, col, self.normal, n.tok.map[0])
        if t in ("bullet_list", "ordered_list"):
            return self.listing(n, width, col)
        if t == "blockquote":
            return self.quote(n, width, col)
        if t in ("fence", "code_block"):
            return self.code(n, width)
        if t == "table":
            return self.table(n, width)
        if t == "hr":
            return [self.rule(width, n.tok.map[0])]
        if t == "html_block":
            st = self.t.st("Comment")
            return [Row([(l, st)], src=n.tok.map[0] + i) for i, l in enumerate(n.tok.content.rstrip("\n").split("\n"))]
        if n.children:
            return self.blocks(n.children, width, col, n.tok.map[0] if n.tok.map else 0, None)
        return []

    # ---- inline

    def inline(self, tok, base):
        """returns lines of spans and the images found, as (line, src, alt)"""
        lines, imgs, stack = [[]], [], [base]
        add = lambda text, st=None: lines[-1].append((text, st or stack[-1]))
        for c in tok.children or []:
            k = c.type
            if k == "text":
                add(c.content)
            elif k in ("softbreak", "hardbreak"):
                lines.append([])
            elif k == "strong_open":
                stack.append(merge(stack[-1], Style(bold=True)))
            elif k == "em_open":
                stack.append(merge(stack[-1], Style(italic=True)))
            elif k == "s_open":
                stack.append(merge(stack[-1], Style(strike=True)))
            elif k in ("strong_close", "em_close", "s_close", "link_close"):
                if len(stack) > 1: stack.pop()
            elif k == "code_inline":
                add(" " + c.content + " ", merge(stack[-1], self.t.st("MarkviewInlineCode", bg=True)))
            elif k == "link_open":
                href = c.attrGet("href") or ""
                if c.markup == "autolink":
                    icon, st = URI, self.t.st("MarkviewHyperlink")
                elif re.search(r"github\.com/[\w.-]+(/[\w.-]+)?/?$", href):
                    icon, st = GITHUB, self.t.st("MarkviewPalette0Fg")
                else:
                    icon, st = LINK, self.t.st("MarkviewHyperlink")
                st = merge(stack[-1], st)
                add(icon, st); stack.append(st)
            elif k == "image":
                src = c.attrGet("src") or ""
                add(IMG + c.content, merge(stack[-1], self.t.st("MarkviewImage")))
                imgs.append((len(lines) - 1, src, c.content))
            elif k == "html_inline":
                if re.match(r"<br\s*/?>", c.content, re.I):
                    lines.append([])
                else:
                    add(c.content, self.t.st("Comment"))
        return lines, imgs

    def para(self, inl, width, col, base, src, first=None):
        lines, imgs = self.inline(inl, base)
        if first:
            first(lines)
        rows = []
        for i, spans in enumerate(lines):
            only = [x for x in imgs if x[0] == i]
            alone = only and len(only) == 1 and not "".join(t for t, _ in spans).replace(IMG + only[0][2], "").strip()
            img_rows = [r for _, s, a in only for r in self.image(s, a, width, src + i)]
            if alone and img_rows:
                rows += img_rows
                continue
            for j, w in enumerate(wrap(spans, width) if spans else [[]]):
                rows.append(Row(w, src=src + i if j == 0 else None))
            rows += img_rows
        return rows

    # ---- images

    def image(self, src, alt, width, line):
        if not src:
            return []
        key = src if re.match(r"https?://", src) else str((self.dir / src).resolve())
        st = self.v.images.get(key)
        if st[0] != "ready":
            if st[0] == "error":
                return [Row([("  " + st[1], self.t.st("DiagnosticError"))])]
            return []
        return self.place(st[1], st[2], st[3], width, line)

    def place(self, path, w, h, width, line):
        cell = self.v.cell
        cols = max(1, min(width, -(-w // cell[0])))
        rows = max(1, round(cols * cell[0] * h / w / cell[1]))
        cap = max(4, self.v.size[1] - 2)
        if rows > cap:
            cols = max(1, int(cols * cap / rows)); rows = cap
        return [Row(src=line if i == 0 else None, img=(path, cols, rows, i)) for i in range(rows)]

    # ---- lists and quotes

    def listing(self, n, width, col):
        rows = []
        depth = 1 + col // 4
        items = [c for c in n.children if c.type == "list_item"]
        num = int(n.tok.attrGet("start") or 1)
        for idx, item in enumerate(items):
            first = item.children[0] if item.children else None
            box = None
            if first is not None and first.type == "paragraph" and first.children[0].tok.children:
                tx = first.children[0].tok.children[0]
                m = re.match(r"^\[(.)\]\s+", tx.content) if tx.type == "text" else None
                if m and m.group(1) in CHECKBOXES:
                    box = CHECKBOXES[m.group(1)]
                    tx.content = tx.content[m.end():]
            if box:
                mark, mst = box[0], self.t.st("MarkviewCheckbox" + box[1])
            elif n.type == "ordered_list":
                mark, mst = f"{item.tok.info or num + idx}{item.tok.markup}", self.t.st("@markup.list.markdown")
            else:
                mark, mst = MARKERS.get(item.tok.markup, MARKERS["-"])
                mst = self.t.st(mst)
            pad = max(0, 4 * depth - col)
            inner = pad + sw(mark) + 1
            sub = self.blocks(item.children, max(1, width - inner), col + inner, item.tok.map[0], item.tok.map[1]
                              if idx == len(items) - 1 else None)
            if box and box[2]:
                scope = self.t.st("MarkviewCheckbox" + box[2])
                if box[2] == "Striked":
                    scope = merge(scope, Style(strike=True))
                for r in sub:
                    r.spans = [(t, merge(s, Style(fg=scope.fg, strike=scope.strike))) for t, s in r.spans]
            for i, r in enumerate(sub):
                lead = [(" " * pad, Style()), (mark, mst), (" ", Style())] if i == 0 else [(" " * inner, Style())]
                r.spans = lead + r.spans
                r.indent += inner
            rows += sub
        # trailing blank lines belong to whatever comes next
        while rows and not rows[-1].img and not "".join(t for t, _ in rows[-1].spans).strip() and \
                rows[-1].src is not None and rows[-1].src >= n.tok.map[1]:
            rows.pop()
        return rows

    def quote(self, n, width, col):
        kind, title = "Default", None
        first = n.children[0] if n.children else None
        if first is not None and first.type == "paragraph" and first.children[0].tok.children:
            kids = first.children[0].tok.children
            m = re.match(r"^\[!(\w+)\][+-]?\s*(.*)", kids[0].content) if kids[0].type == "text" else None
            if m and m.group(1).upper() in CALLOUTS:
                icon, kind = CALLOUTS[m.group(1).upper()]
                title = f"{icon} {m.group(2) or m.group(1).capitalize()}"
                # the [!NOTE] line becomes the title, the text starts after it
                drop = 1
                while drop < len(kids) and kids[drop].type not in ("softbreak", "hardbreak"):
                    drop += 1
                first.children[0].tok.children = kids[drop + 1:]
                first.tok.map = [first.tok.map[0] + 1, first.tok.map[1]]
        bar = self.t.st("MarkviewBlockQuote" + kind)
        body = Style(self.t.fg("@markup.quote"))
        rows = []
        if title:
            rows.append(Row([(title, bar)], src=n.tok.map[0]))
            if first is not None and not first.children[0].tok.children:
                n.children = n.children[1:]
        sub = self.blocks(n.children, max(1, width - 2), col + 2, n.tok.map[0] + (1 if title else 0), n.tok.map[1])
        for r in sub:
            r.spans = [(t, merge(s, body) if s.fg is None or s.fg == self.normal.fg else s) for t, s in r.spans]
        rows += sub
        for r in rows:
            r.spans = [("\u258b ", bar)] + (r.spans if r is not rows[0] or not title else r.spans)
            r.indent += 2
        return rows

    # ---- code

    def code(self, n, width):
        tok = n.tok
        lang = (tok.info or "").split()[0] if tok.info else ""
        lines = tok.content.rstrip("\n").split("\n") if tok.content.strip("\n") else []
        span = tok.map[1] - tok.map[0]
        fenced = tok.type == "fence"
        closed = fenced and span >= len(lines) + 2
        bg = self.t.st("MarkviewCode", bg=True)
        bg = Style(bg=bg.bg)
        icon, icolor, ft = self.icons.get(lang, ["\U000f0219", None, lang.capitalize()])
        ist = Style(rgb(icolor) if isinstance(icolor, int) else self.t.fg("Comment"))
        label = f"{icon} {ft or lang}" if lang else ""
        err = None
        if lang == "mermaid":
            r, err = self.diagram(tok, lines, width, ist, label, icon, closed)
            if r:
                return r
        bw = min(width, max(60, max((sw(l) for l in lines), default=0) + 4))
        rows = []
        if fenced:
            fill = max(0, bw - sw(label) - 1)
            rows.append(Row([(" " * fill, bg), (label, merge(bg, ist)), (" ", bg)] if label else [(" " * bw, bg)],
                            src=tok.map[0], sign=(icon, ist) if label else None))
        if err:
            rows.append(Row(cut([("  " + err, merge(self.t.st("DiagnosticError"), bg))], bw), src=tok.map[0]))
        try:
            lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=False) if lang else None
        except ClassNotFound:
            lexer = None
        hl = [[]]
        if lexer:
            for tt, val in lex("\n".join(lines), lexer):
                for i, part in enumerate(val.split("\n")):
                    if i:
                        hl.append([])
                    if part:
                        hl[-1].append((part, merge(merge(self.normal, self.t.syntax(tt)), bg)))
        else:
            hl = [[(l, merge(self.normal, bg))] for l in lines]
        for i in range(len(lines)):
            spans = cut([("  ", bg)] + (hl[i] if i < len(hl) else []), bw)
            spans.append((" " * (bw - spans_w(spans)), bg))
            rows.append(Row(spans, src=tok.map[0] + (1 if fenced else 0) + i))
        if closed:
            rows.append(Row([(" " * bw, bg)], src=tok.map[1] - 1))
        return rows

    def diagram(self, tok, lines, width, ist, label, icon, closed):
        slot = self.mermaid_n
        self.mermaid_n += 1
        code = "\n".join(lines)
        if not code.strip():
            return None, None
        key, st = self.v.mermaid.get(slot, code, mermaid_config(self.t))
        if st[0] == "ready":
            self.v.last_diagram[slot] = st[1]
        shown = st[1] if st[0] == "ready" else self.v.last_diagram.get(slot)
        if not shown:
            return None, st[1] if st[0] == "error" else None
        with Image.open(shown) as im:
            w, h = im.size
        bw = width
        rows = [Row([(" " * max(0, bw - sw(label) - 1), Style()), (label, ist), (" ", Style())],
                    src=tok.map[0], sign=(icon, ist))]
        if st[0] == "error":
            rows.append(Row(cut([("  " + st[1], self.t.st("DiagnosticError"))], width), src=tok.map[0]))
        rows += self.place(shown, w, h, width - 2, tok.map[0] + 1)
        for r in rows[1:]:
            r.indent += 2
        if closed:
            rows.append(Row(src=tok.map[1] - 1))
        return rows, None

    # ---- tables and rules

    def table(self, n, width):
        rows, aligns = [], []
        for part in n.children:
            for tr in part.children:
                cells = []
                for c in tr.children:
                    style = c.tok.attrGet("style") or ""
                    m = re.search(r"text-align:(\w+)", style)
                    if part.type == "thead":
                        aligns.append(m.group(1) if m else None)
                    cells.append(c.children[0].tok if c.children else None)
                rows.append((part.type == "thead", tr.tok.map[0] if tr.tok.map else None, cells))
        ncol = max(len(r[2]) for r in rows)
        aligns += [None] * (ncol - len(aligns))
        head = merge(self.normal, self.t.st("MarkviewTableHeader"))
        border = self.t.st("MarkviewTableBorder")
        rendered = []
        for is_head, src, cells in rows:
            out = []
            for i in range(ncol):
                c = cells[i] if i < len(cells) else None
                spans = self.inline(c, head if is_head else self.normal)[0][0] if c is not None else []
                out.append(spans)
            rendered.append((is_head, src, out))
        widths = [max(spans_w(r[2][i]) for r in rendered) for i in range(ncol)]
        hb = head
        line = lambda l, m, r, st, fills=None: [(l + m.join(fills or ["\u2500" * (w + 2) for w in widths]) + r, st)]
        out = [Row(cut(line("\u256d", "\u252c", "\u256e", hb), width),
                   src=n.tok.map[0] - 1 if n.tok.map[0] > 0 else None)]
        for k, (is_head, src, cells) in enumerate(rendered):
            st = hb if is_head else border
            spans = [("\u2502", st)]
            for i, c in enumerate(cells):
                gap = widths[i] - spans_w(c)
                a = aligns[i]
                left = gap // 2 if a == "center" else gap if a == "right" else 0
                spans += [(" " + " " * left, Style())] + c + [(" " * (gap - left) + " ", Style()), ("\u2502", st)]
            out.append(Row(cut(spans, width), src=src))
            if is_head and (k + 1 == len(rendered) or not rendered[k + 1][0]):
                seg = []
                for w, a in zip(widths, aligns):
                    if a == "left":
                        seg.append(("\u257c" + "\u2500" * (w + 1), "MarkviewTableAlignLeft"))
                    elif a == "right":
                        seg.append(("\u2500" * (w + 1) + "\u257e", "MarkviewTableAlignRight"))
                    elif a == "center":
                        seg.append(("\u2574" + "\u2500" * w + "\u2576", "MarkviewTableAlignCenter"))
                    else:
                        seg.append(("\u2500" * (w + 2), None))
                spans = [("\u251c", hb)]
                for i, (txt, g) in enumerate(seg):
                    spans.append((txt, self.t.st(g) if g else hb))
                    spans.append(("\u253c" if i < len(seg) - 1 else "\u2524", hb))
                out.append(Row(cut(spans, width), src=n.tok.map[0] + 1))
        bottom = n.tok.map[1]
        out.append(Row(cut(line("\u2570", "\u2534", "\u256f", border), width),
                       src=bottom if bottom < len(self.lines) else None))
        return out

    def rule(self, width, src):
        grad = [f"MarkviewGradient{i}" for i in range(1, 10) for _ in (0, 1)]
        left = (width - 3) // 2
        right = width - 3 - left
        spans = [("\u2500", self.t.st(grad[min(i, len(grad) - 1)])) for i in range(left)]
        spans.append((" \ueaaa ", self.t.st("MarkviewIcon3Fg")))
        spans += [("\u2500", self.t.st(grad[min(right - 1 - i, len(grad) - 1)])) for i in range(right)]
        return Row(spans, src=src)


# ---------------------------------------------------------------- terminal


class Screen:
    def __init__(self):
        self.rows, self.imgs = [], {}
        self.out = sys.stdout

    def invalidate(self, lines):
        # conpty only resends lines it thinks changed, and it never knew an
        # image was there. change them to something else first so the real
        # redraw goes out
        if not lines:
            return
        self.out.write("".join(f"\x1b[{i + 1};1H\x1b[2K\x1b[48;2;1;1;1m \x1b[0m" for i in sorted(lines)))
        self.out.flush()
        time.sleep(0.03)
        for i in lines:
            if i < len(self.rows):
                self.rows[i] = None

    def draw(self, lines, imgs, size, cell, images):
        h = size[1]
        if len(self.rows) != h:
            self.rows = [None] * h
        old = {k: v for k, v in self.imgs.items()}
        gone = [k for k in old if imgs.get(k) != old[k]]
        dirty = {old[k][0] + j for k in gone for j in range(old[k][3]) if old[k][0] + j < h}
        self.invalidate(dirty)
        buf = []
        for i in range(h):
            s = lines[i] if i < len(lines) else ""
            if s != self.rows[i]:
                buf.append(f"\x1b[{i + 1};1H\x1b[0m\x1b[2K{s}\x1b[0m")
                self.rows[i] = s
        new = [k for k in imgs if old.get(k) != imgs[k]]
        if buf:
            self.out.write("".join(buf))
            self.out.flush()
        if new:
            if buf:
                time.sleep(0.03)
            for k in new:
                row, col, (path, cols, rows, r0), n = imgs[k]
                size_, data = images.payload(path, cols, rows, r0, r0 + n, cell)
                self.out.write(f"\x1b[{row + 1};{col + 1}H\x1b]1337;File=inline=1;size={size_};width={cols};"
                               f"height={n};preserveAspectRatio=1;doNotMoveCursor=1:{data}\x07")
        # the cursor cant be hidden, conpty drops the images while it is
        self.out.write(f"\x1b[{h};{size[0]}H")
        self.out.flush()
        self.imgs = dict(imgs)

    def reset(self):
        self.invalidate(set(range(len(self.rows))))
        self.rows = []
        self.imgs = {}


class View:
    def __init__(self):
        self.theme = Theme()
        self.images = Images()
        self.mermaid = Mermaid()
        self.last_diagram = {}
        self.lines, self.path, self.icons = None, None, {}
        self.cur, self.winline, self.offset = 1, 1, 0
        self.size = shutil.get_terminal_size()
        self.cell = self.cell_size()
        self.screen = Screen()
        self.status = None

    def cell_size(self):
        try:
            panes = json.loads(subprocess.run(["wezterm", "cli", "list", "--format", "json"], capture_output=True,
                                              text=True, encoding="utf-8", errors="replace", timeout=5).stdout)
            p = next(p for p in panes if str(p["pane_id"]) == os.environ.get("WEZTERM_PANE"))
            s = p["size"]
            return (max(1, s["pixel_width"] // s["cols"]), max(1, s["pixel_height"] // s["rows"]))
        except Exception:
            return (9, 18)

    def render(self):
        cols, h = self.size
        width = max(10, cols - GUTTER - 1)
        if self.lines is None:
            lines = ["", " " * GUTTER + sgr(Style(self.theme.fg("Comment"))) + (self.status or "no markdown buffer")]
            self.screen.draw(lines, {}, self.size, self.cell, self.images)
            return
        doc = Doc(self, self.lines, self.path, self.icons)
        rows = doc.render(width)
        cur = self.cur - 1
        # the last row at or before the cursor line, so a line inside a
        # diagram lands on the diagram
        first = max((i for i, r in enumerate(rows) if r.src is not None and r.src <= cur), default=0)
        top = max(0, min(first - (self.winline - 1) + self.offset, len(rows) - h))
        cur_src = rows[first].src if 0 <= first < len(rows) else None
        line_bg = self.theme.bg("CursorLine")
        lines, imgs = [], {}
        on_cur = False
        for i in range(h):
            r = top + i
            if r >= len(rows):
                lines.append(""); continue
            row = rows[r]
            if row.src is not None:
                on_cur = row.src == cur_src
            if row.img:
                path, icols, irows, k = row.img
                # never onto the last row, an image ending there scrolls the
                # pane and conpty doesnt know it happened
                n = min(irows - k, h - 1 - i)
                if (k == 0 or i == 0) and n > 0:
                    imgs[(path, r - k)] = (i, GUTTER + row.indent, (path, icols, irows, k), n)
                lines.append(""); continue
            out = []
            if row.sign:
                out.append(sgr(row.sign[1]) + cut([(row.sign[0], None)], GUTTER - 1)[0][0] + "\x1b[0m")
                out.append(" " * (GUTTER - 1 - sw(row.sign[0])) + " ")
            else:
                out.append(" " * GUTTER)
            n = 0
            # anything past the edge wraps onto the next row and drags the rest down
            for text, st in cut(row.spans, width):
                if on_cur and line_bg and st.bg is None:
                    st = merge(st, Style(bg=line_bg))
                out.append(sgr(st) + text); n += sw(text)
            if on_cur and line_bg:
                out.append(sgr(Style(bg=line_bg)) + " " * max(0, cols - GUTTER - n - 1))
            lines.append("".join(out))
        self.screen.draw(lines, imgs, self.size, self.cell, self.images)


def keys():
    while True:
        events.put(("key", msvcrt.getwch()))


def resizes():
    size = shutil.get_terminal_size()
    while True:
        time.sleep(0.15)
        s = shutil.get_terminal_size()
        if s != size:
            size = s
            events.put(("resize", s))


def watch(path):
    mtime = None
    while True:
        try:
            m = path.stat().st_mtime
            if m != mtime:
                mtime = m
                events.put(("rpc", "text", [None, str(path), path.read_text(encoding="utf-8").split("\n"), {}]))
        except OSError:
            pass
        time.sleep(0.2)


def connect(args):
    """-> (nvim, buffer to pin) for --nvim, or the nvim that already has the
    file open, or (None, None) to just watch the file"""
    if args.nvim:
        return Nvim(args.nvim), args.buf
    if args.file:
        target = str(Path(args.file).resolve()).lower()
        for p in nvim_pipes():
            try:
                n = Nvim(p)
                for b in n.request("nvim_list_bufs"):
                    if n.request("nvim_buf_get_name", b).lower() == target:
                        return n, b
                n.close()
            except Exception:
                pass
    return None, None


def main():
    ap = argparse.ArgumentParser(description="markdown preview for a wezterm split")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--nvim", help="nvim server to follow, v:servername")
    ap.add_argument("--buf", type=int, help="only show this buffer")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    view = View()
    nvim, pin = connect(args)
    if nvim:
        chan = nvim.request("nvim_get_api_info")[0]
        pane = int(os.environ["WEZTERM_PANE"]) if os.environ.get("WEZTERM_PANE") else None
        nvim.request("nvim_exec_lua", ATTACH, [chan, pane, pin, GROUPS])
        nvim.listen()
    elif args.file:
        view.theme = Theme(fetch_colors())
        threading.Thread(target=watch, args=(Path(args.file).resolve(),), daemon=True).start()
    else:
        ap.error("give a file or --nvim")

    threading.Thread(target=keys, daemon=True).start()
    threading.Thread(target=resizes, daemon=True).start()
    out = sys.stdout
    out.write("\x1b[?1049h\x1b]0;md-view\x07")
    out.flush()
    try:
        while True:
            batch = [events.get()]
            try:
                while True:
                    batch.append(events.get_nowait())
            except queue.Empty:
                pass
            quit = False
            for ev in batch:
                kind = ev[0]
                log("event", kind, str(ev[1:])[:200])
                if kind == "eof":
                    quit = True
                elif kind == "rpc":
                    name, a = ev[1], ev[2]
                    if name == "text":
                        view.lines, view.path, view.icons = a[2], a[1], a[3] or {}
                    elif name == "cursor":
                        view.cur, view.winline, view.offset = a[0], a[1], 0
                    elif name == "colors":
                        view.theme = Theme(a[0])
                elif kind == "key":
                    k = ev[1]
                    h = view.size[1]
                    if k in ("q", "\x03"):
                        quit = True
                    elif k == "j": view.offset += 1
                    elif k == "k": view.offset -= 1
                    elif k == "\x04": view.offset += h // 2
                    elif k == "\x15": view.offset -= h // 2
                    elif k == "g": view.offset = -10 ** 6
                    elif k == "G": view.offset = 10 ** 6
                elif kind == "resize":
                    view.size = ev[1]
                    view.cell = view.cell_size()
                    view.screen.reset()
            if quit:
                break
            view.render()
    finally:
        out.write("\x1b[0m\x1b[?1049l")
        out.flush()
        if nvim:
            # let nvim know the split is gone so the next md file opens one again
            try:
                n = Nvim(args.nvim) if args.nvim else None
                if n:
                    n.request("nvim_exec_lua", "if vim.g.mdv_pane == ... then vim.g.mdv_pane = nil end",
                              [int(os.environ.get("WEZTERM_PANE", -1))])
                    n.close()
            except Exception:
                pass
    # the key thread is stuck in getwch, so dont wait on it
    os._exit(0)


def fetch_colors():
    # no nvim to follow, a headless one with the same config gives the colours
    lua = ("local groups = vim.json.decode(vim.env.MDV_GROUPS) "
           "pcall(function() require('markview.highlights').setup() end) " + COLORS
           + " io.stdout:write(vim.json.encode(colors()))")
    try:
        r = subprocess.run(["nvim", "--headless", "-c", "lua " + lua.replace("\n", " "), "-c", "qa!"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                           env={**os.environ, "MDV_GROUPS": json.dumps(GROUPS)}, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        return {}


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        sys.stdout.write("[0m[?1049l")
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / "mdv.log").write_text(traceback.format_exc(), encoding="utf-8")
        traceback.print_exc()
        input("mdv crashed, enter to close")
