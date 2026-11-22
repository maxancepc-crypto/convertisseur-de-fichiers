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


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


# ---------- Catégories et formats ----------

EXTENSIONS = {
    "image": {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "ico", "tga", "ppm", "pgm", "pbm", "jp2"},
    "pdf": {"pdf"},
    "table": {"csv", "xlsx", "json"},
    "document": {"docx", "md", "markdown", "txt", "html", "htm"},
    "audio": {"mp3", "wav", "ogg", "oga", "flac", "m4a", "aac", "opus", "wma", "aiff", "aif"},
    "video": {"mp4", "mov", "m4v", "mkv", "webm", "avi"},
}
if AVIF:
    EXTENSIONS["image"].add("avif")
if HEIF:
    EXTENSIONS["image"] |= {"heic", "heif"}

TARGETS = {
    "image": ["jpg", "png", "webp", "gif", "bmp", "tiff", "ico", "tga"] + (["avif"] if AVIF else []) + ["pdf"],
    "pdf": (["png", "jpg"] if FITZ else []) + ["txt"],
    "table": ["csv", "xlsx", "json"],
    "document": ["txt", "html"],
    "audio": ["mp3", "wav", "ogg", "flac", "m4a", "opus"],
    "video": ["mp3", "wav", "ogg", "flac", "m4a"],
}

MIME = {
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
    "bmp": "image/bmp", "tiff": "image/tiff", "ico": "image/x-icon", "tga": "image/x-tga",
    "avif": "image/avif", "pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json", "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "flac": "audio/flac", "m4a": "audio/mp4", "opus": "audio/ogg",
}

IMG_FORMAT = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF", "bmp": "BMP",
              "tiff": "TIFF", "ico": "ICO", "tga": "TGA", "avif": "AVIF", "pdf": "PDF"}

# Démultiplexeur imposé pour chaque extension (évite que ffmpeg « devine » un format dangereux)
DEMUXER = {
    "mp3": "mp3", "wav": "wav", "ogg": "ogg", "oga": "ogg", "opus": "ogg", "flac": "flac",
    "m4a": "mov", "aac": "aac", "wma": "asf", "aiff": "aiff", "aif": "aiff",
    "mp4": "mov", "mov": "mov", "m4v": "mov", "mkv": "matroska", "webm": "matroska", "avi": "avi",
}
AUDIO_CODEC = {"mp3": "libmp3lame", "wav": "pcm_s16le", "ogg": "libvorbis",
               "flac": "flac", "m4a": "aac", "opus": "libopus"}


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


def prepare(img, target, max_width):
    img = ImageOps.exif_transpose(img)
    if target in ("jpg", "pdf", "bmp", "gif"):  # pas de transparence : fond blanc
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
    else:
        img.save(buf, fmt)
    return buf.getvalue()


# ---------- PDF ----------

def pdf_to_text(data, filename):
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
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError(f"« {filename} » ne contient pas de texte sélectionnable (PDF scanné ?).")
    return text.encode("utf-8")


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
        if ext == "csv":
            return pd.read_csv(io.BytesIO(data), sep=None, engine="python", encoding="utf-8-sig", encoding_errors="replace")
        if ext == "xlsx":
            return pd.read_excel(io.BytesIO(data), engine="openpyxl")
        obj = json.loads(data.decode("utf-8-sig"))
        if isinstance(obj, dict):
            try:
                return pd.DataFrame(obj)
            except ValueError:
                return pd.json_normalize(obj)
        return pd.json_normalize(obj)
    except Exception:
        raise ValueError(f"« {filename} » n'a pas pu être lu comme tableau.")


def write_table(df, target):
    if target == "csv":
        return df.to_csv(index=False).encode("utf-8-sig")
    if target == "json":
        return df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


# ---------- Documents ----------

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html):
    p = _Text()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


def wrap_html(title, body):
    return ("<!DOCTYPE html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"UTF-8\">\n"
            f"<title>{htmllib.escape(title)}</title>\n"
            "<style>body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;line-height:1.6}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>\n"
            f"</head>\n<body>\n{body}\n</body>\n</html>\n")


def decode_text(data):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def docx_content(data, filename, as_html):
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
            text = p.text
            if as_html:
                m = re.match(r"Heading (\d)", style)
                if style == "Title":
                    out.append(f"<h1>{htmllib.escape(text)}</h1>")
                elif m:
                    lvl = min(6, int(m.group(1)))
                    out.append(f"<h{lvl}>{htmllib.escape(text)}</h{lvl}>")
                elif text.strip():
                    out.append(f"<p>{htmllib.escape(text)}</p>")
            else:
                out.append(text)
        elif child.tag.endswith("}tbl"):
            t = Table(child, doc)
            if as_html:
                rows = "".join("<tr>" + "".join(f"<td>{htmllib.escape(c.text)}</td>" for c in r.cells) + "</tr>" for r in t.rows)
                out.append(f"<table>{rows}</table>")
            else:
                out.extend("\t".join(c.text for c in r.cells) for r in t.rows)
    return out


def convert_document(data, ext, target, filename):
    if ext == "docx":
        if target == "html":
            body = "\n".join(docx_content(data, filename, True))
            return wrap_html(filename, body).encode("utf-8")
        return "\n".join(docx_content(data, filename, False)).encode("utf-8")

    text = decode_text(data)
    if ext in ("md", "markdown"):
        import markdown
        html = markdown.markdown(text, extensions=["tables", "fenced_code"])
        return (wrap_html(filename, html) if target == "html" else html_to_text(html)).encode("utf-8")
    if ext in ("html", "htm"):
        return (text if target == "html" else html_to_text(text)).encode("utf-8")
    # txt
    if target == "txt":
        return text.encode("utf-8")
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    body = "\n".join("<p>" + htmllib.escape(p).replace("\n", "<br>\n") + "</p>" for p in paras)
    return wrap_html(filename, body).encode("utf-8")


# ---------- Audio / vidéo (ffmpeg) ----------

def convert_media(data, src_ext, target, quality, filename):
    exe = ffmpeg_exe()
    if not exe:
        raise ValueError("La conversion audio n'est pas disponible sur ce serveur.")
    kbps = 64 + round((quality - 10) / 90 * (256 - 64))
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "entree." + src_ext)
        dst = os.path.join(tmp, "sortie." + target)
        with open(src, "wb") as f:
            f.write(data)
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-protocol_whitelist", "file", "-f", DEMUXER[src_ext], "-i", src,
               "-vn", "-map_metadata", "-1", "-c:a", AUDIO_CODEC[target]]
        if target in ("mp3", "ogg", "m4a", "opus"):
            cmd += ["-b:a", f"{kbps}k"]
        cmd += ["-y", dst]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=100)
        except subprocess.TimeoutExpired:
            raise ValueError(f"« {filename} » est trop long à convertir sur ce serveur.")
        if res.returncode != 0 or not os.path.exists(dst):
            raise ValueError(f"« {filename} » n'a pas pu être converti (fichier invalide ou sans piste audio).")
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
                elif cat == "pdf" and target == "txt":
                    outputs.append((unique(used, base, "txt"), pdf_to_text(data, f.filename)))
                elif cat == "pdf":
                    for page, content in pdf_to_images(data, f.filename, target, quality, max_width):
                        outputs.append((unique(used, f"{base}_page{page}", target), content))
                elif cat == "table":
                    df = read_table(data, ext, f.filename)
                    outputs.append((unique(used, base, target), write_table(df, target)))
                elif cat == "document":
                    outputs.append((unique(used, base, target), convert_document(data, ext, target, f.filename)))
                else:  # audio / vidéo
                    outputs.append((unique(used, base, target), convert_media(data, ext, target, quality, f.filename)))
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
