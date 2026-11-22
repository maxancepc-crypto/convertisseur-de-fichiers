import io
import os
import re
import unicodedata
import zipfile

from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 Mo au total
Image.MAX_IMAGE_PIXELS = 50_000_000

MAX_FILES = 20
FORMATS = {
    "png": ("PNG", "image/png", ".png"),
    "jpg": ("JPEG", "image/jpeg", ".jpg"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "pdf": ("PDF", "application/pdf", ".pdf"),
}


def ascii_name(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9._ -]", "_", text).strip(" ._")


def has_alpha(img):
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def prepare(img, target, max_width):
    """Corrige l'orientation, convertit le mode couleur et redimensionne."""
    img = ImageOps.exif_transpose(img)

    if target in ("jpg", "pdf"):  # pas de transparence : fond blanc
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
        height = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, height), Image.Resampling.LANCZOS)
    return img


def encode(img, target, quality):
    buf = io.BytesIO()
    fmt = FORMATS[target][0]
    if fmt == "JPEG":
        img.save(buf, fmt, quality=quality, optimize=True)
    elif fmt == "WEBP":
        img.save(buf, fmt, quality=quality)
    elif fmt == "PNG":
        img.save(buf, fmt, optimize=True)
    else:
        img.save(buf, fmt, resolution=100.0, quality=quality)
    return buf.getvalue()


def to_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Les fichiers sont trop lourds (25 Mo maximum au total)."), 413


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/convert", methods=["POST"])
def convert():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify(error="Ajoute au moins une image."), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"Maximum {MAX_FILES} fichiers à la fois."), 400

    target = request.form.get("format", "png").lower()
    if target not in FORMATS:
        return jsonify(error="Format de sortie inconnu."), 400
    quality = to_int(request.form.get("quality"), 85, 1, 100)
    max_width = to_int(request.form.get("max_width"), 0, 0, 10000)
    merge = target == "pdf" and request.form.get("merge") == "1"

    images = []
    for f in files:
        try:
            img = Image.open(f.stream)
            img.load()
            images.append((f.filename, prepare(img, target, max_width)))
        except Exception:
            return jsonify(error=f"« {f.filename} » n'est pas une image valide, ou elle est trop grande."), 400

    outputs = []  # (nom, octets)
    ext = FORMATS[target][2]
    if merge:
        first, rest = images[0][1], [i[1] for i in images[1:]]
        buf = io.BytesIO()
        first.save(buf, "PDF", save_all=True, append_images=rest, resolution=100.0, quality=quality)
        outputs.append(("document.pdf", buf.getvalue()))
    else:
        used = {}
        for filename, img in images:
            base = ascii_name(os.path.splitext(filename)[0]) or "image"
            n = used.get(base, 0)
            used[base] = n + 1
            name = f"{base}{ext}" if n == 0 else f"{base}_{n}{ext}"
            outputs.append((name, encode(img, target, quality)))

    if len(outputs) == 1:
        name, data = outputs[0]
        return send_file(io.BytesIO(data), mimetype=FORMATS[target][1], as_attachment=True, download_name=name)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in outputs:
            z.writestr(name, data)
    zbuf.seek(0)
    return send_file(zbuf, mimetype="application/zip", as_attachment=True, download_name="fichiers_convertis.zip")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
