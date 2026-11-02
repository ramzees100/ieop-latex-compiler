import hmac
import io
import os
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# Maximum incoming request: 15 MB.
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

ALLOWED_ORIGIN = "https://ramzees100.github.io"

MAX_FILES = 200
MAX_PROJECT_BYTES = 25 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024

# Keep the whole job below the existing Gunicorn 60-second timeout.
JOB_SECONDS = 45

ALLOWED_EXTENSIONS = {
    ".tex",
    ".bib",
    ".bst",
    ".sty",
    ".cls",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
}

compile_lock = threading.Lock()


class ProjectError(Exception):
    pass


class CompilationError(Exception):
    pass


@app.after_request
def response_headers(response):
    if request.headers.get("Origin") == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )

    response.vary.add("Origin")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def health():
    return jsonify(
        status="ok",
        service="IEOP LaTeX Compiler",
        supports=["single-source", "zip-project", "bibtex"],
        main_file="main.tex",
    )


def extract_project(upload, work):
    """Validate the complete archive before writing its files."""
    try:
        archive = zipfile.ZipFile(upload.stream)
    except zipfile.BadZipFile as exc:
        raise ProjectError("The uploaded file is not a valid ZIP.") from exc

    with archive:
        entries = archive.infolist()

        if len(entries) > MAX_FILES:
            raise ProjectError(
                f"The ZIP can contain at most {MAX_FILES} entries."
            )

        validated = []
        seen = set()
        total = 0

        for entry in entries:
            raw_name = entry.filename

            if (
                not raw_name
                or "\\" in raw_name
                or "\x00" in raw_name
                or ":" in raw_name
                or raw_name.startswith("/")
            ):
                raise ProjectError("The ZIP contains an invalid file path.")

            clean_name = raw_name.rstrip("/")
            parts = clean_name.split("/")

            if any(
                not part
                or part in {".", ".."}
                or part.startswith(".")
                for part in parts
            ):
                raise ProjectError(
                    "Hidden files and unsafe paths are not supported."
                )

            if len(raw_name) > 240:
                raise ProjectError("A project filename is too long.")

            if parts[0] == "__MACOSX":
                raise ProjectError(
                    "Remove the __MACOSX folder before uploading."
                )

            mode = entry.external_attr >> 16
            file_type = stat.S_IFMT(mode)

            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ProjectError(
                    "Links and special files are not allowed in projects."
                )

            if entry.flag_bits & 1:
                raise ProjectError("Password-protected ZIPs are not supported.")

            path = PurePosixPath(clean_name)

            if entry.is_dir():
                continue

            if file_type == stat.S_IFDIR:
                raise ProjectError("The ZIP contains an invalid file entry.")

            normalized = path.as_posix()

            if normalized.casefold() in seen:
                raise ProjectError("The ZIP contains duplicate file paths.")

            seen.add(normalized.casefold())

            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise ProjectError(
                    f"Unsupported file type: {path.suffix or '(none)'}"
                )

            # Keep generated output filenames separate from uploaded assets.
            if normalized.casefold() == "main.pdf":
                raise ProjectError(
                    "main.pdf is reserved for compiler output. "
                    "Place PDF figures inside an images folder."
                )

            if entry.file_size > MAX_SINGLE_FILE_BYTES:
                raise ProjectError("An individual file exceeds 10 MB.")

            total += entry.file_size

            if total > MAX_PROJECT_BYTES:
                raise ProjectError(
                    "The extracted project exceeds the 25 MB limit."
                )

            validated.append((entry, path))

        if not any(
            path.as_posix() == "main.tex"
            for _, path in validated
        ):
            raise ProjectError(
                "Put main.tex at the top level of the ZIP, "
                "not inside an enclosing project folder."
            )

        for entry, relative_path in validated:
            destination = work.joinpath(*relative_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)

            # Read with a hard limit rather than trusting archive metadata.
            with archive.open(entry) as source:
                content = source.read(MAX_SINGLE_FILE_BYTES + 1)

            if len(content) > MAX_SINGLE_FILE_BYTES:
                raise ProjectError("An individual file exceeds 10 MB.")

            destination.write_bytes(content)

        if not (work / "main.tex").read_text(
            encoding="utf-8", errors="replace"
        ).strip():
            raise ProjectError("main.tex is empty.")


def environment_for(folder):
    # Hosting secrets are not inherited by compiler subprocesses.
    return {
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
        "MKTEXPK": "0",
        "MKTEXTFM": "0",
    }


def run_tool(arguments, work, environment, deadline):
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise subprocess.TimeoutExpired(arguments, JOB_SECONDS)

    command = [
        "prlimit",
        "--cpu=20",
        "--as=402653184",
        "--fsize=10485760",
        "--nofile=64",
        "--",
        *arguments,
    ]

    result = subprocess.run(
        command,
        cwd=work,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=min(25, remaining),
        check=False,
    )

    return result.returncode


def latex_error(work):
    log_file = work / "main.log"

    if not log_file.is_file():
        return (
            "Compilation failed before a LaTeX log was generated. "
            "The document may have exceeded a resource limit."
        )

    # Return a short diagnostic only to the authenticated caller.
    with log_file.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 16_000))
        tail = stream.read().decode("utf-8", errors="replace")

    lines = tail.splitlines()

    for index, line in enumerate(lines):
        if (
            line.startswith("!")
            or ": LaTeX Error:" in line
            or ": Package " in line and "Error:" in line
            or "Emergency stop" in line
        ):
            return "\n".join(lines[index:index + 5])[:1200]

    return (
        "LaTeX compilation failed. Check the source, filenames, "
        "installed packages, and resource limits."
    )


def compile_project(work, environment, deadline):
    latex = [
        "pdflatex",
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "main.tex",
    ]

    if run_tool(latex, work, environment, deadline) != 0:
        raise CompilationError(latex_error(work))

    # This version supports traditional BibTeX, not Biber.
    if (work / "main.bcf").exists():
        raise CompilationError(
            "This compiler currently supports BibTeX, not Biber. "
            "Use traditional bibliography commands or configure "
            "biblatex with backend=bibtex."
        )

    # Chapter .aux files may contain their own bibliography data.
    needs_bibtex = False

    for aux_file in work.rglob("*.aux"):
        aux_text = aux_file.read_text(
            encoding="utf-8", errors="replace"
        )

        if "\\bibdata" not in aux_text:
            continue

        needs_bibtex = True
        aux_job = aux_file.relative_to(work).with_suffix("").as_posix()

        if run_tool(
            ["bibtex", aux_job], work, environment, deadline
        ) != 0:
            raise CompilationError(
                "BibTeX failed. Check citation keys, bibliography "
                "filenames, and the selected bibliography style."
            )

    # Additional passes resolve citations, contents, and references.
    for _ in range(2):
        if run_tool(latex, work, environment, deadline) != 0:
            raise CompilationError(latex_error(work))

    pdf = work / "main.pdf"

    if not pdf.is_file():
        raise CompilationError("No PDF was generated.")

    if pdf.stat().st_size > MAX_PDF_BYTES:
        raise CompilationError("The generated PDF exceeds 10 MB.")

    return pdf.read_bytes()


@app.route("/compile", methods=["POST", "OPTIONS"])
def compile_document():
    if request.method == "OPTIONS":
        return "", 204

    token = os.environ.get("COMPILER_TOKEN", "")
    provided = request.headers.get("Authorization", "")

    if not token:
        return jsonify(
            error="Compiler access has not been configured."
        ), 503

    if not hmac.compare_digest(
        provided.encode(),
        ("Bearer " + token).encode(),
    ):
        return jsonify(error="Compiler access denied."), 401

    if not compile_lock.acquire(blocking=False):
        return jsonify(
            error="Compiler is busy. Please try again."
        ), 429

    try:
        with tempfile.TemporaryDirectory(prefix="ieop-") as folder:
            work = Path(folder)

            if request.is_json:
                # Backward compatibility with the current website.
                data = request.get_json(silent=True)
                source = (
                    data.get("source")
                    if isinstance(data, dict)
                    else None
                )

                if not isinstance(source, str) or not source.strip():
                    raise ProjectError(
                        "Provide LaTeX in the source field."
                    )

                if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                    return jsonify(
                        error="The LaTeX source exceeds 2 MB."
                    ), 413

                (work / "main.tex").write_text(
                    source, encoding="utf-8"
                )

            elif request.mimetype == "multipart/form-data":
                upload = request.files.get("project")

                if upload is None:
                    raise ProjectError(
                        "Upload a ZIP in the project field."
                    )

                extract_project(upload, work)

            else:
                return jsonify(
                    error="Send JSON source or a multipart ZIP project."
                ), 415

            environment = environment_for(folder)
            deadline = time.monotonic() + JOB_SECONDS

            pdf_bytes = compile_project(
                work, environment, deadline
            )

            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype="application/pdf",
                as_attachment=True,
                download_name="document.pdf",
            )

    except ProjectError as error:
        return jsonify(error=str(error)), 400

    except CompilationError as error:
        return jsonify(error=str(error)), 422

    except (zipfile.BadZipFile, EOFError):
        return jsonify(
            error="The ZIP is damaged or could not be read."
        ), 400

    except subprocess.TimeoutExpired:
        return jsonify(
            error=(
                "Compilation exceeded the time limit. "
                "Try compiling fewer chapters."
            )
        ), 408

    except FileNotFoundError:
        return jsonify(
            error="A required compiler program is not installed."
        ), 503

    except HTTPException:
        raise

    except Exception:
        app.logger.exception("Compiler request failed")
        return jsonify(
            error="The compiler could not complete the request."
        ), 500

    finally:
        compile_lock.release()


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(
        error="The uploaded request exceeds 15 MB."
    ), 413
