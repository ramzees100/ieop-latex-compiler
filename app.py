import hmac
import io
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200_000

ALLOWED_ORIGIN = "https://ramzees100.github.io"
compile_lock = threading.Lock()


@app.after_request
def response_headers(response):
    if request.headers.get("Origin") == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization"
        )
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"

    response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def health():
    return jsonify(status="ok", service="IEOP LaTeX Compiler")


@app.route("/compile", methods=["POST", "OPTIONS"])
def compile_document():
    if request.method == "OPTIONS":
        return "", 204

    # Set this secret in hosting settings, never in GitHub source.
    token = os.environ.get("COMPILER_TOKEN", "")
    provided = request.headers.get("Authorization", "")

    if not token:
        return jsonify(error="Compiler access has not been configured."), 503

    if not hmac.compare_digest(
        provided.encode(), ("Bearer " + token).encode()
    ):
        return jsonify(error="Compiler access denied."), 401

    data = request.get_json(silent=True)
    source = data.get("source") if isinstance(data, dict) else None

    if not isinstance(source, str) or not source.strip():
        return jsonify(error="Provide LaTeX in the source field."), 400

    if len(source.encode("utf-8")) > 150_000:
        return jsonify(error="The LaTeX source is too large."), 413

    if not compile_lock.acquire(blocking=False):
        return jsonify(error="Compiler is busy. Please try again."), 429

    try:
        with tempfile.TemporaryDirectory(prefix="ieop-") as folder:
            work = Path(folder)
            (work / "main.tex").write_text(source, encoding="utf-8")

            # Do not pass hosting secrets to the LaTeX process.
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": folder,
                "TMPDIR": folder,
                "LANG": "C.UTF-8",
                "openin_any": "p",
                "openout_any": "p",
                "shell_escape": "f",
                "TEXMFOUTPUT": folder,
                "TEXMFVAR": folder,
                "TEXMFCONFIG": folder,
            }

            command = [
                "prlimit",
                "--cpu=15",
                "--as=402653184",
                "--fsize=10485760",
                "--nofile=64",
                "--",
                "pdflatex",
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "main.tex",
            ]

            # Two passes resolve ordinary internal references.
            for _ in range(2):
                result = subprocess.run(
                    command,
                    cwd=folder,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )

                if result.returncode != 0:
                    return jsonify(
                        error=(
                            "Compilation failed. Check the LaTeX source "
                            "and whether its packages are installed."
                        )
                    ), 422

            pdf = work / "main.pdf"

            if not pdf.is_file():
                return jsonify(error="No PDF was generated."), 422

            return send_file(
                io.BytesIO(pdf.read_bytes()),
                mimetype="application/pdf",
                as_attachment=True,
                download_name="document.pdf",
            )

    except subprocess.TimeoutExpired:
        return jsonify(error="Compilation exceeded the time limit."), 408
    except FileNotFoundError:
        return jsonify(error="The LaTeX compiler is not installed."), 503
    except Exception:
        return jsonify(error="The compiler could not complete the request."), 500
    finally:
        compile_lock.release()


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error="The request is too large."), 413
