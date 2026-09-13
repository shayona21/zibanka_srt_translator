import os
import threading
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory, url_for
from werkzeug.utils import secure_filename

from main import (
    BATCH_SIZE,
    create_gemini_client,
    load_supporting_context,
    process_excel_file,
)


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
WORKBOOK_EXTENSIONS = {".xlsx"}
SYNOPSIS_EXTENSIONS = {".docx", ".txt"}
MAX_FILES_PER_JOB = 10


def load_local_env(env_path):
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, value = stripped_line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env(BASE_DIR / ".env")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
jobs = {}
jobs_lock = threading.Lock()


def has_extension(filename, allowed_extensions):
    return Path(filename).suffix.lower() in allowed_extensions


def append_log(job, message):
    job["logs"].append(message)
    job["logs"] = job["logs"][-200:]


def create_job(job_id, files, supporting_files):
    job = {
        "id": job_id,
        "title": f"{len(files)} workbook{'s' if len(files) != 1 else ''} queued",
        "input_lang": "English",
        "target_lang": "French",
        "files": files,
        "supporting_files": supporting_files,
        "current_file_number": 0,
        "total_files": len(files),
        "current_file_name": "",
        "current_output_file_name": "",
        "batch_size": BATCH_SIZE,
        "status": "queued",
        "progress": 0,
        "current_batch": 0,
        "total_batches": 0,
        "message": "Waiting to start.",
        "logs": [],
        "error": None,
    }
    with jobs_lock:
        jobs[job_id] = job
    return job


def update_job_progress(job, payload):
    message = payload.get("message", "")
    job["progress"] = payload.get("percent", job["progress"])
    job["current_batch"] = payload.get("current_batch", job["current_batch"])
    job["total_batches"] = payload.get("total_batches", job["total_batches"])
    job["message"] = message or job["message"]
    if message:
        append_log(job, message)


def cleanup_uploads(job):
    paths = [
        Path(file_job["upload_path"])
        for file_job in job["files"]
    ]
    paths.extend(
        Path(path)
        for path in job["supporting_files"].values()
        if path
    )
    for path in paths:
        try:
            if path.is_file() and path.parent == UPLOAD_DIR:
                path.unlink()
        except OSError:
            pass


def run_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return

    try:
        with jobs_lock:
            job["status"] = "running"
            job["message"] = "Reading supporting documents."
            append_log(job, "Reading the character list and optional synopses once for this job.")

        supporting_context = load_supporting_context(
            character_list_path=job["supporting_files"]["character_list_path"],
            episode_synopsis_path=job["supporting_files"]["episode_synopsis_path"],
            series_synopsis_path=job["supporting_files"]["series_synopsis_path"],
        )
        client = create_gemini_client()
        total_files = len(job["files"])
        output_dir = OUTPUT_DIR / job_id

        for file_index, file_job in enumerate(job["files"], start=1):
            with jobs_lock:
                live_job = jobs.get(job_id)
                if live_job is None:
                    return
                live_job["current_file_number"] = file_index
                live_job["current_file_name"] = file_job["filename"]
                live_job["current_output_file_name"] = file_job["requested_output_file_name"]
                live_job["current_batch"] = 0
                live_job["total_batches"] = 0
                file_job["status"] = "running"
                start_message = (
                    f"Starting workbook {file_index} of {total_files}: "
                    f"{file_job['filename']}"
                )
                live_job["message"] = start_message
                append_log(live_job, start_message)

            def progress_callback(payload, current_index=file_index, current_file=file_job):
                file_percent = payload.get("percent", 0)
                overall_percent = int(
                    (((current_index - 1) * 100) + file_percent) / total_files
                )
                message = payload.get("message", "")
                if message:
                    message = (
                        f"Workbook {current_index}/{total_files} "
                        f"({current_file['filename']}): {message}"
                    )
                with jobs_lock:
                    live_job = jobs.get(job_id)
                    if live_job is not None:
                        update_job_progress(
                            live_job,
                            {
                                "current_batch": payload.get("current_batch", 0),
                                "total_batches": payload.get("total_batches", 0),
                                "percent": overall_percent,
                                "message": message,
                            },
                        )

            output_path = process_excel_file(
                file_path=file_job["upload_path"],
                supporting_context=supporting_context,
                client=client,
                batch_size=BATCH_SIZE,
                output_file_name=file_job["requested_output_file_name"],
                output_dir=output_dir,
                progress_callback=progress_callback,
            )

            with jobs_lock:
                live_job = jobs.get(job_id)
                if live_job is None:
                    return
                file_job["status"] = "completed"
                file_job["output_file_name"] = output_path.name
                file_job["output_path"] = str(output_path)
                live_job["current_output_file_name"] = output_path.name
                live_job["progress"] = int((file_index / total_files) * 100)
                completion_message = (
                    f"Completed workbook {file_index} of {total_files}: {output_path.name}"
                )
                live_job["message"] = completion_message
                append_log(live_job, completion_message)

        with jobs_lock:
            live_job = jobs.get(job_id)
            if live_job is not None:
                live_job["status"] = "completed"
                live_job["progress"] = 100
                live_job["message"] = "All French workbooks are ready."
                append_log(live_job, "Processing complete.")
    except Exception as exc:
        with jobs_lock:
            live_job = jobs.get(job_id)
            if live_job is not None:
                current_file_number = live_job.get("current_file_number", 0)
                if 1 <= current_file_number <= len(live_job["files"]):
                    live_job["files"][current_file_number - 1]["status"] = "failed"
                live_job["status"] = "failed"
                live_job["error"] = str(exc)
                live_job["message"] = "Processing failed."
                append_log(live_job, f"Error: {exc}")
    finally:
        cleanup_uploads(job)


def render_error(message, status_code=400):
    return render_template("index.html", active_job=None, form_error=message), status_code


def save_upload(uploaded_file):
    safe_name = secure_filename(uploaded_file.filename)
    unique_name = f"{uuid4().hex}_{safe_name}"
    destination = UPLOAD_DIR / unique_name
    uploaded_file.save(destination)
    return safe_name, destination


def unique_output_names(files):
    used = set()
    names = []
    for uploaded_file in files:
        safe_stem = secure_filename(Path(uploaded_file.filename).stem) or "translated_subtitles"
        candidate = f"{safe_stem}_French.xlsx"
        counter = 2
        while candidate.lower() in used:
            candidate = f"{safe_stem}_French_{counter}.xlsx"
            counter += 1
        used.add(candidate.lower())
        names.append(candidate)
    return names


@app.route("/", methods=["GET"])
def index():
    job_id = request.args.get("job")
    with jobs_lock:
        active_job = deepcopy(jobs.get(job_id)) if job_id else None
    return render_template("index.html", active_job=active_job)


@app.route("/assets/<path:filename>", methods=["GET"])
def asset(filename):
    local_asset_path = BASE_DIR / filename
    if local_asset_path.exists():
        return send_from_directory(BASE_DIR, filename)
    return send_from_directory(PROJECT_DIR, filename)


@app.errorhandler(413)
def upload_too_large(_error):
    return render_error("The upload is too large. Keep the complete job under 50 MB.", 413)


@app.route("/process", methods=["POST"])
def process():
    subtitle_files = [
        file
        for file in request.files.getlist("subtitle_files")
        if file and file.filename
    ]
    character_list = request.files.get("character_list")
    episode_synopsis = request.files.get("episode_synopsis")
    series_synopsis = request.files.get("series_synopsis")

    if not subtitle_files:
        return render_error("Upload at least one English subtitle workbook.")
    if len(subtitle_files) > MAX_FILES_PER_JOB:
        return render_error(f"Upload no more than {MAX_FILES_PER_JOB} workbooks per job.")
    for uploaded_file in subtitle_files:
        if not has_extension(uploaded_file.filename, WORKBOOK_EXTENSIONS):
            return render_error(
                f"{uploaded_file.filename} must be an .xlsx workbook."
            )

    if not character_list or not character_list.filename:
        return render_error("Upload the required character list workbook.")
    if not has_extension(character_list.filename, WORKBOOK_EXTENSIONS):
        return render_error("The character list must be an .xlsx workbook.")

    for label, uploaded_file in (
        ("Episode synopsis", episode_synopsis),
        ("Series synopsis", series_synopsis),
    ):
        if (
            uploaded_file
            and uploaded_file.filename
            and not has_extension(uploaded_file.filename, SYNOPSIS_EXTENSIONS)
        ):
            return render_error(f"{label} must be a .docx or .txt file.")

    output_names = unique_output_names(subtitle_files)
    saved_paths = []
    try:
        file_entries = []
        for uploaded_file, output_name in zip(subtitle_files, output_names):
            safe_name, upload_path = save_upload(uploaded_file)
            saved_paths.append(upload_path)
            file_entries.append(
                {
                    "filename": safe_name,
                    "requested_output_file_name": output_name,
                    "upload_path": str(upload_path),
                    "status": "queued",
                    "output_file_name": "",
                    "output_path": None,
                }
            )

        character_name, character_path = save_upload(character_list)
        saved_paths.append(character_path)

        supporting_files = {
            "character_list_name": character_name,
            "character_list_path": str(character_path),
            "episode_synopsis_name": "",
            "episode_synopsis_path": None,
            "series_synopsis_name": "",
            "series_synopsis_path": None,
        }
        for prefix, uploaded_file in (
            ("episode_synopsis", episode_synopsis),
            ("series_synopsis", series_synopsis),
        ):
            if uploaded_file and uploaded_file.filename:
                safe_name, upload_path = save_upload(uploaded_file)
                saved_paths.append(upload_path)
                supporting_files[f"{prefix}_name"] = safe_name
                supporting_files[f"{prefix}_path"] = str(upload_path)
    except Exception:
        for path in saved_paths:
            try:
                if path.is_file() and path.parent == UPLOAD_DIR:
                    path.unlink()
            except OSError:
                pass
        raise

    job_id = uuid4().hex
    job = create_job(
        job_id=job_id,
        files=file_entries,
        supporting_files=supporting_files,
    )
    worker = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    worker.start()
    return render_template("index.html", active_job=job, started=True)


def serialize_job(job):
    return {
        "id": job["id"],
        "title": job["title"],
        "status": job["status"],
        "progress": job["progress"],
        "current_batch": job["current_batch"],
        "total_batches": job["total_batches"],
        "message": job["message"],
        "logs": job["logs"],
        "error": job["error"],
        "input_lang": job["input_lang"],
        "target_lang": job["target_lang"],
        "supporting_files": {
            key: value
            for key, value in job["supporting_files"].items()
            if key.endswith("_name")
        },
        "files": [
            {
                "filename": file_job["filename"],
                "requested_output_file_name": file_job["requested_output_file_name"],
                "output_file_name": file_job["output_file_name"],
                "status": file_job["status"],
                "download_url": (
                    url_for(
                        "download_file",
                        job_id=job["id"],
                        filename=file_job["output_file_name"],
                    )
                    if file_job["output_path"]
                    else None
                ),
            }
            for file_job in job["files"]
        ],
        "current_file_number": job["current_file_number"],
        "total_files": job["total_files"],
        "current_file_name": job["current_file_name"],
        "current_output_file_name": job["current_output_file_name"],
        "batch_size": job["batch_size"],
    }


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    with jobs_lock:
        job = deepcopy(jobs.get(job_id))
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(serialize_job(job))


@app.route("/download/<job_id>/<path:filename>", methods=["GET"])
def download_file(job_id, filename):
    with jobs_lock:
        job = deepcopy(jobs.get(job_id))
    if job is None:
        return "File not found.", 404

    matching_file = next(
        (
            file_job
            for file_job in job["files"]
            if file_job["output_file_name"] == filename and file_job["output_path"]
        ),
        None,
    )
    if matching_file is None:
        return "File not found.", 404

    file_path = Path(matching_file["output_path"])
    expected_parent = OUTPUT_DIR / job_id
    if file_path.parent != expected_parent or not file_path.is_file():
        return "File not found.", 404
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


#if __name__ == "__main__":
#    port = int(os.environ.get("PORT", "5041"))
#    app.run(debug=False, port=port)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5041)

