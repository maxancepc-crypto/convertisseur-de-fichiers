import html as htmllib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from html.parser import HTMLParser
from importlib.util import find_spec

from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps, features

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 Mo au total
Image.MAX_IMAGE_PIXELS = 50_000_000

MAX_FILES = 20
MAX_PIXELS = 40_000_000
MAX_PDF_PAGES = 30

# ---------- Fonctions optionnelles (selon les bibliothèques installées) ----------

HEIF = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:
    pass

FITZ = find_spec("fitz") is not None or find_spec("pymupdf") is not None
AVIF = features.check("avif")
JP2 = features.check("jpg_2000")


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


# ---------- Catégories et formats ----------

EXTENSIONS = {
    "image": {"jpg", "jpeg", "jfif", "jpe", "png", "webp", "gif", "bmp", "dib", "tif", "tiff", "ico", "icns",
              "tga", "ppm", "pgm", "pbm", "pcx", "sgi", "qoi", "dds", "psd", "xbm"},
    "pdf": {"pdf"},
    "table": {"csv", "tsv", "xlsx", "ods", "json", "jsonl"},
    "document": {"docx", "md", "markdown", "txt", "html", "htm"},
    "audio": {"mp3", "mp2", "wav", "ogg", "oga", "flac", "m4a", "aac", "opus", "wma", "aiff", "aif", "ac3", "amr", "mka"},
    "video": {"mp4", "mov", "m4v", "mkv", "webm", "avi", "wmv", "flv", "3gp", "mpg", "mpeg", "ogv"},
}
if AVIF:
    EXTENSIONS["image"].add("avif")
if JP2:
    EXTENSIONS["image"] |= {"jp2", "j2k"}
if HEIF:
    EXTENSIONS["image"] |= {"heic", "heif"}

# ext de sortie -> format Pillow
IMG_FORMAT = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF", "bmp": "BMP", "tiff": "TIFF",
              "ico": "ICO", "icns": "ICNS", "tga": "TGA", "ppm": "PPM", "pcx": "PCX", "sgi": "SGI",
              "qoi": "QOI", "eps": "EPS", "avif": "AVIF", "jp2": "JPEG2000", "pdf": "PDF"}

IMAGE_TARGETS = ["jpg", "png", "webp", "gif", "bmp", "tiff", "ico", "icns", "tga", "ppm", "pcx", "sgi", "qoi", "eps"]
if AVIF:
    IMAGE_TARGETS.append("avif")
if JP2:
    IMAGE_TARGETS.append("jp2")
IMAGE_TARGETS.append("pdf")

TEXT_TARGETS = ["txt", "html", "md", "docx"]
PDF_IMAGE_TARGETS = ["png", "jpg", "webp", "tiff", "bmp", "gif"]

TARGETS = {
    "image": IMAGE_TARGETS,
    "pdf": (PDF_IMAGE_TARGETS if FITZ else []) + TEXT_TARGETS,
    "table": ["csv", "tsv", "xlsx", "ods", "json", "jsonl", "html", "md", "xml"],
    "document": TEXT_TARGETS + ["pdf"],
    "audio": ["mp3", "wav", "ogg", "flac", "m4a", "opus", "aac", "aiff", "wma", "ac3", "mp2"],
    "video": ["mp4", "webm", "gif", "mp3", "wav", "ogg", "flac", "m4a", "opus", "aac"],
}
VIDEO_TARGETS = {"mp4", "webm", "gif"}

MIME = {
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    "tiff": "image/tiff", "ico": "image/x-icon", "icns": "image/icns", "tga": "image/x-tga", "avif": "image/avif",
    "jp2": "image/jp2", "pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8", "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8", "json": "application/json", "jsonl": "application/x-ndjson",
    "xml": "application/xml", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "mp3": "audio/mpeg", "mp2": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/ogg",
    "flac": "audio/flac", "m4a": "audio/mp4", "aac": "audio/aac", "aiff": "audio/aiff", "wma": "audio/x-ms-wma",
    "ac3": "audio/ac3", "mp4": "video/mp4", "webm": "video/webm",
}

# Démultiplexeur imposé pour chaque extension (évite que ffmpeg « devine » un format dangereux)
DEMUXER = {
    "mp3": "mp3", "mp2": "mp3", "wav": "wav", "ogg": "ogg", "oga": "ogg", "ogv": "ogg", "opus": "ogg",
    "flac": "flac", "m4a": "mov", "aac": "aac", "wma": "asf", "wmv": "asf", "aiff": "aiff", "aif": "aiff",
    "ac3": "ac3", "amr": "amr", "mka": "matroska", "mp4": "mov", "mov": "mov", "m4v": "mov", "3gp": "mov",
    "mkv": "matroska", "webm": "matroska", "avi": "avi", "flv": "flv", "mpg": "mpeg", "mpeg": "mpeg",
}
AUDIO_CODEC = {"mp3": "libmp3lame", "wav": "pcm_s16le", "ogg": "libvorbis", "flac": "flac", "m4a": "aac",
               "opus": "libopus", "aac": "aac", "aiff": "pcm_s16be", "wma": "wmav2", "ac3": "ac3", "mp2": "mp2"}
LOSSY = {"mp3", "ogg", "m4a", "opus", "aac", "wma", "ac3", "mp2"}


def category_of(ext):
    for cat, exts in EXTENSIONS.items():
        if ext in exts:
            return cat
    return None


def ext_of(filename):
    return os.path.splitext(filename)[1].lower().lstrip(".")


def ascii_name(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9._ -]", "_", text).strip(" ._")


def to_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(text):
    return CTRL.sub("", text)


# ---------- Images ----------

def has_alpha(img):
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def open_image(data, filename):
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise ValueError(f"« {filename} » est trop grande (plus de 40 millions de pixels).")
        img.load()
        return img
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"« {filename} » n'est pas une image valide.")


FLATTEN = {"jpg", "pdf", "bmp", "gif", "pcx", "ppm", "eps"}  # formats sans transparence : fond blanc


def prepare(img, target, max_width):
    img = ImageOps.exif_transpose(img)
    if target in FLATTEN:
        if has_alpha(img):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if has_alpha(img) else "RGB")
    if max_width and img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))), Image.Resampling.LANCZOS)
    if target == "ico":
        img.thumbnail((256, 256))
    elif target == "icns":  # icône carrée de 512 px
        side = max(img.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img.convert("RGBA"), ((side - img.width) // 2, (side - img.height) // 2))
        img = canvas.resize((512, 512), Image.Resampling.LANCZOS)
    return img


def encode_image(img, target, quality):
    buf = io.BytesIO()
    fmt = IMG_FORMAT[target]
    if fmt in ("JPEG", "WEBP", "AVIF"):
        img.save(buf, fmt, quality=quality)
    elif fmt == "PNG":
        img.save(buf, fmt, optimize=True)
    elif fmt == "PDF":
        img.save(buf, fmt, resolution=100.0, quality=quality)
    elif fmt == "TIFF":
        try:
            img.save(buf, fmt, compression="tiff_adobe_deflate")
        except Exception:
            buf = io.BytesIO()
            img.save(buf, fmt)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


# ---------- PDF ----------

def pdf_text(data, filename):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError(f"« {filename} » est protégé par un mot de passe.")
        pages = [(p.extract_text() or "") for p in reader.pages[:500]]
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"« {filename} » n'est pas un PDF valide.")
    text = clean("\n\n".join(pages)).strip()
    if not text:
        raise ValueError(f"« {filename} » ne contient pas de texte sélectionnable (PDF scanné ?).")
    return text


def pdf_to_images(data, filename, target, quality, max_width):
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise ValueError(f"« {filename} » est protégé par un mot de passe.")
        if doc.page_count > MAX_PDF_PAGES:
            raise ValueError(f"« {filename} » a plus de {MAX_PDF_PAGES} pages.")
        out = []
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=130)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            out.append((i, encode_image(prepare(img, target, max_width), target, quality)))
        return out
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"« {filename} » n'est pas un PDF valide.")


# ---------- Tableaux ----------

def read_table(data, ext, filename):
    import pandas as pd
    try:
        if ext in ("csv", "tsv"):
            sep = "\t" if ext == "tsv" else None
            return pd.read_csv(io.BytesIO(data), sep=sep, engine="python", encoding="utf-8-sig", encoding_errors="replace")
        if ext == "xlsx":
            return pd.read_excel(io.BytesIO(data), engine="openpyxl")
        if ext == "ods":
            return pd.read_excel(io.BytesIO(data), engine="odf")
        text = data.decode("utf-8-sig")
        if ext == "jsonl":
            return pd.json_normalize([json.loads(line) for line in text.splitlines() if line.strip()])
        obj = json.loads(text)
        if isinstance(obj, dict):
            try:
                return pd.DataFrame(obj)
            except ValueError:
                return pd.json_normalize(obj)
        return pd.json_normalize(obj)
    except Exception:
        raise ValueError(f"« {filename} » n'a pas pu être lu comme tableau.")


def table_to_md(df):
    def cell(v):
        return "" if v != v or v is None else str(v).replace("|", "\\|").replace("\n", " ")
    cols = [cell(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(" --- " for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def write_table(df, target, title="tableau"):
    if target == "csv":
        return df.to_csv(index=False).encode("utf-8-sig")
    if target == "tsv":
        return df.to_csv(index=False, sep="\t").encode("utf-8-sig")
    if target == "json":
        return df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")
    if target == "jsonl":
        return df.to_json(orient="records", lines=True, force_ascii=False).encode("utf-8")
    if target == "html":
        return wrap_html(title, df.to_html(index=False, na_rep="")).encode("utf-8")
    if target == "md":
        return table_to_md(df).encode("utf-8")
    if target == "xml":
        safe = df.copy()
        names = []
        for i, c in enumerate(safe.columns):
            n = re.sub(r"\W", "_", str(c)) or f"col{i}"
            names.append(("_" + n) if n[0].isdigit() else n)
        safe.columns = names
        return safe.to_xml(index=False, parser="etree").encode("utf-8")
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="odf" if target == "ods" else "openpyxl")
    return buf.getvalue()


# ---------- Documents : blocs intermédiaires ----------
# Un document est une liste de blocs : ("h1".."h6", texte), ("p", texte), ("li", texte),
# ("pre", texte) ou ("table", lignes).

class _Blocks(HTMLParser):
    HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}
    FLOW = {"p", "div", "blockquote", "section", "article"}

    def __init__(self):
        super().__init__()
        self.blocks, self.buf, self.kind = [], [], "p"
        self.skip, self.in_pre = 0, False
        self.table = self.row = self.cell = None

    def flush(self):
        text = "".join(self.buf)
        self.buf = []
        text = text.strip("\n") if self.in_pre else re.sub(r"\s+", " ", text).strip()
        if text.strip():
            self.blocks.append((self.kind, text))

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.table is not None:
            if tag == "tr":
                self.row = []
            elif tag in ("td", "th"):
                self.cell = []
            return
        if tag == "table":
            self.flush()
            self.table = []
        elif tag in self.HEAD:
            self.flush()
            self.kind = tag
        elif tag in self.FLOW:
            self.flush()
            self.kind = "p"
        elif tag == "li":
            self.flush()
            self.kind = "li"
        elif tag == "pre":
            self.flush()
            self.kind, self.in_pre = "pre", True
        elif tag == "br":
            self.buf.append("\n" if self.in_pre else " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.table is not None:
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.table.append(self.row)
                self.row = None
            elif tag == "table":
                if self.table:
                    self.blocks.append(("table", self.table))
                self.table = None
            return
        if tag in self.HEAD or tag in self.FLOW or tag in ("li", "pre"):
            self.flush()
            self.kind, self.in_pre = "p", False

    def handle_data(self, data):
        if self.skip:
            return
        if self.table is not None:
            if self.cell is not None:
                self.cell.append(data)
            return
        self.buf.append(data)


def blocks_from_html(html):
    p = _Blocks()
    p.feed(html)
    p.close()
    p.flush()
    return [(k, clean(v) if isinstance(v, str) else v) for k, v in p.blocks]


def blocks_from_text(text, join_lines=False):
    out = []
    for para in re.split(r"\n\s*\n", clean(text)):
        para = para.strip()
        if para:
            out.append(("p", re.sub(r"\s*\n\s*", " ", para) if join_lines else para))
    return out


def blocks_from_docx(data, filename):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise ValueError(f"« {filename} » n'est pas un document Word valide.")
    out = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            p = Paragraph(child, doc)
            style = (p.style.name if p.style is not None else "") or ""
            text = clean(p.text)
            if not text.strip():
                continue
            m = re.match(r"Heading (\d)", style)
            if style == "Title":
                out.append(("h1", text))
            elif m:
                out.append((f"h{min(6, max(1, int(m.group(1))))}", text))
            elif style.startswith("List"):
                out.append(("li", text))
            else:
                out.append(("p", text))
        elif child.tag.endswith("}tbl"):
            t = Table(child, doc)
            out.append(("table", [[clean(c.text) for c in r.cells] for r in t.rows]))
    return out


def decode_text(data):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def wrap_html(title, body):
    return ("<!DOCTYPE html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"UTF-8\">\n"
            f"<title>{htmllib.escape(title)}</title>\n"
            "<style>body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;line-height:1.6}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>\n"
            f"</head>\n<body>\n{body}\n</body>\n</html>\n")


def blocks_to_txt(blocks, title=""):
    out, prev = [], None
    for kind, val in blocks:
        if kind == "table":
            s = "\n".join("\t".join(r) for r in val)
        elif kind == "li":
            s = "- " + val
        else:
            s = val
        out.append(("\n" if (kind == "li" and prev == "li") else "\n\n") * bool(out) + s)
        prev = kind
    return ("".join(out) + "\n").encode("utf-8")


def blocks_to_md(blocks, title=""):
    out, prev = [], None
    for kind, val in blocks:
        if kind == "table":
            rows = [[c.replace("|", "\\|") for c in r] for r in val]
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            s = "| " + " | ".join(rows[0]) + " |\n|" + "|".join(" --- " for _ in rows[0]) + "|"
            for r in rows[1:]:
                s += "\n| " + " | ".join(r) + " |"
        elif kind == "li":
            s = "- " + val
        elif kind == "pre":
            s = "```\n" + val + "\n```"
        elif kind[0] == "h":
            s = "#" * int(kind[1]) + " " + val
        else:
            s = val.replace("\n", "  \n")
        out.append(("\n" if (kind == "li" and prev == "li") else "\n\n") * bool(out) + s)
        prev = kind
    return ("".join(out) + "\n").encode("utf-8")


def blocks_to_html(blocks, title=""):
    parts, in_list = [], False
    for kind, val in blocks:
        if kind == "li" and not in_list:
            parts.append("<ul>")
            in_list = True
        elif kind != "li" and in_list:
            parts.append("</ul>")
            in_list = False
        if kind == "li":
            parts.append(f"<li>{htmllib.escape(val)}</li>")
        elif kind == "table":
            rows = "".join("<tr>" + "".join(f"<td>{htmllib.escape(c)}</td>" for c in r) + "</tr>" for r in val)
            parts.append(f"<table>{rows}</table>")
        elif kind == "pre":
            parts.append(f"<pre>{htmllib.escape(val)}</pre>")
        elif kind[0] == "h":
            parts.append(f"<{kind}>{htmllib.escape(val)}</{kind}>")
        else:
            parts.append("<p>" + htmllib.escape(val).replace("\n", "<br>\n") + "</p>")
    if in_list:
        parts.append("</ul>")
    return wrap_html(title, "\n".join(parts)).encode("utf-8")


def blocks_to_docx(blocks, title=""):
    from docx import Document
    d = Document()
    for kind, val in blocks:
        if kind == "table":
            width = max(len(r) for r in val)
            t = d.add_table(rows=len(val), cols=width)
            t.style = "Table Grid"
            for i, r in enumerate(val):
                for j, c in enumerate(r):
                    t.cell(i, j).text = c
        elif kind == "li":
            d.add_paragraph(val, style="List Bullet")
        elif kind == "pre":
            d.add_paragraph().add_run(val).font.name = "Courier New"
        elif kind[0] == "h":
            d.add_heading(val, level=int(kind[1]))
        else:
            d.add_paragraph(val)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def blocks_to_pdf(blocks, title=""):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

    def latin(s):  # la police standard ne gère que l'alphabet latin
        return s.encode("cp1252", "replace").decode("cp1252")

    def markup(s):
        return htmllib.escape(latin(s)).replace("\n", "<br/>")

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    li_style = ParagraphStyle("li", parent=body, leftIndent=16, bulletIndent=4)
    avail = A4[0] - 108
    story = []
    for kind, val in blocks:
        if kind == "table":
            width = max(len(r) for r in val)
            rows = [[Paragraph(markup(c), body) for c in r + [""] * (width - len(r))] for r in val]
            t = Table(rows, colWidths=[avail / width] * width)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t, Spacer(1, 8)]
        elif kind == "li":
            story.append(Paragraph(markup(val), li_style, bulletText="•"))
        elif kind == "pre":
            story += [Preformatted(latin(val), styles["Code"]), Spacer(1, 6)]
        elif kind[0] == "h":
            story.append(Paragraph(markup(val), styles[f"Heading{kind[1]}"]))
        else:
            story.append(Paragraph(markup(val), body))
    if not story:
        story.append(Spacer(1, 1))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=54, rightMargin=54, topMargin=54, bottomMargin=54,
                      title=latin(title)).build(story)
    return buf.getvalue()


WRITERS = {"txt": blocks_to_txt, "md": blocks_to_md, "html": blocks_to_html,
           "docx": blocks_to_docx, "pdf": blocks_to_pdf}


def convert_document(data, ext, target, filename):
    title = os.path.splitext(filename)[0]
    # Chemins directs qui conservent toute la mise en forme
    if ext in ("md", "markdown"):
        text = decode_text(data)
        if target == "md":
            return text.encode("utf-8")
        if target == "html":
            import markdown
            return wrap_html(title, markdown.markdown(text, extensions=["tables", "fenced_code"])).encode("utf-8")
        import markdown
        blocks = blocks_from_html(markdown.markdown(text, extensions=["tables", "fenced_code"]))
    elif ext in ("html", "htm"):
        text = decode_text(data)
        if target == "html":
            return text.encode("utf-8")
        if target == "md":
            try:
                from markdownify import markdownify
                return markdownify(text, heading_style="ATX").strip().encode("utf-8") + b"\n"
            except ImportError:
                pass
        blocks = blocks_from_html(text)
    elif ext == "docx":
        blocks = blocks_from_docx(data, filename)
    else:  # txt
        text = decode_text(data)
        if target in ("txt", "md"):
            return text.encode("utf-8")
        blocks = blocks_from_text(text)
    if not blocks:
        raise ValueError(f"« {filename} » est vide.")
    return WRITERS[target](blocks, title)


def convert_pdf_text(data, filename, target):
    text = pdf_text(data, filename)
    if target == "txt":
        return text.encode("utf-8")
    return WRITERS[target](blocks_from_text(text, join_lines=True), os.path.splitext(filename)[0])


# ---------- Audio / vidéo (ffmpeg) ----------

def convert_media(data, src_ext, target, quality, max_width, filename):
    exe = ffmpeg_exe()
    if not exe:
        raise ValueError("La conversion audio et vidéo n'est pas disponible sur ce serveur.")
    kbps = 64 + round((quality - 10) / 90 * (256 - 64))
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "entree." + src_ext)
        dst = os.path.join(tmp, "sortie." + target)
        with open(src, "wb") as f:
            f.write(data)
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-protocol_whitelist", "file", "-f", DEMUXER[src_ext], "-i", src, "-map_metadata", "-1"]
        if target in VIDEO_TARGETS:
            scale = f"scale='min({max_width or 640},iw)':-2"
            if target == "gif":
                cmd += ["-map", "0:v:0", "-t", "10", "-an", "-loop", "0", "-vf",
                        f"fps=10,{scale}:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse"]
            elif target == "mp4":
                crf = round(35 - (quality - 10) / 90 * 17)
                cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-t", "60", "-vf", scale, "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
            else:  # webm
                crf = round(40 - (quality - 10) / 90 * 20)
                cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-t", "60", "-vf", scale, "-c:v", "libvpx",
                        "-deadline", "realtime", "-cpu-used", "8", "-crf", str(crf), "-b:v", "1M",
                        "-c:a", "libvorbis", "-b:a", "128k"]
        else:
            cmd += ["-vn", "-c:a", AUDIO_CODEC[target]]
            if target in LOSSY:
                if target in ("mp2", "ac3"):  # débits imposés par ces codecs
                    kbps = min((64, 80, 96, 112, 128, 160, 192, 224, 256), key=lambda v: abs(v - kbps))
                cmd += ["-b:a", f"{kbps}k"]
        cmd += ["-y", dst]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=100)
        except subprocess.TimeoutExpired:
            raise ValueError(f"« {filename} » est trop long à convertir sur ce serveur (essaie un fichier plus court).")
        if res.returncode != 0 or not os.path.exists(dst):
            raise ValueError(f"« {filename} » n'a pas pu être converti (fichier invalide, ou sans piste audio/vidéo).")
        with open(dst, "rb") as f:
            return f.read()


# ---------- Routes ----------

@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Les fichiers sont trop lourds (50 Mo maximum au total)."), 413


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/capabilities")
def capabilities():
    return jsonify(
        extensions={c: sorted(e) for c, e in EXTENSIONS.items()},
        targets={c: t for c, t in TARGETS.items() if t},
    )


def unique(used, base, ext):
    n = used.get(base, 0)
    used[base] = n + 1
    return f"{base}.{ext}" if n == 0 else f"{base}_{n}.{ext}"


@app.route("/convert", methods=["POST"])
def convert():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify(error="Ajoute au moins un fichier."), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"Maximum {MAX_FILES} fichiers à la fois."), 400

    cats = set()
    for f in files:
        cat = category_of(ext_of(f.filename))
        if cat is None:
            return jsonify(error=f"Format non pris en charge : « {f.filename} »."), 400
        cats.add(cat)
    if len(cats) > 1:
        return jsonify(error="Convertis un seul type de fichiers à la fois (par exemple uniquement des images)."), 400
    cat = cats.pop()

    target = request.form.get("format", "").lower()
    if target not in TARGETS.get(cat, []):
        return jsonify(error="Ce format de sortie n'est pas disponible pour ces fichiers."), 400
    quality = to_int(request.form.get("quality"), 85, 10, 100)
    max_width = to_int(request.form.get("max_width"), 0, 0, 10000)
    merge = cat == "image" and target == "pdf" and request.form.get("merge") == "1"

    outputs, used = [], {}
    try:
        if merge:
            imgs = [prepare(open_image(f.read(), f.filename), "pdf", max_width) for f in files]
            buf = io.BytesIO()
            imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=100.0, quality=quality)
            outputs.append(("document.pdf", buf.getvalue()))
        else:
            for f in files:
                data = f.read()
                ext = ext_of(f.filename)
                base = ascii_name(os.path.splitext(f.filename)[0]) or "fichier"
                if cat == "image":
                    img = prepare(open_image(data, f.filename), target, max_width)
                    outputs.append((unique(used, base, target), encode_image(img, target, quality)))
                elif cat == "pdf" and target in PDF_IMAGE_TARGETS:
                    for page, content in pdf_to_images(data, f.filename, target, quality, max_width):
                        outputs.append((unique(used, f"{base}_page{page}", target), content))
                elif cat == "pdf":
                    outputs.append((unique(used, base, target), convert_pdf_text(data, f.filename, target)))
                elif cat == "table":
                    df = read_table(data, ext, f.filename)
                    outputs.append((unique(used, base, target), write_table(df, target, base)))
                elif cat == "document":
                    outputs.append((unique(used, base, target), convert_document(data, ext, target, f.filename)))
                else:  # audio / vidéo
                    outputs.append((unique(used, base, target),
                                    convert_media(data, ext, target, quality, max_width, f.filename)))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception:
        return jsonify(error="Une erreur est survenue pendant la conversion."), 400

    if len(outputs) == 1:
        name, data = outputs[0]
        return send_file(io.BytesIO(data), mimetype=MIME.get(target, "application/octet-stream"),
                         as_attachment=True, download_name=name)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in outputs:
            z.writestr(name, data)
    zbuf.seek(0)
    return send_file(zbuf, mimetype="application/zip", as_attachment=True, download_name="fichiers_convertis.zip")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
