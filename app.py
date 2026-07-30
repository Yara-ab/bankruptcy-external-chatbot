import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import rag

app = Flask(__name__)
store_id = rag.VECTOR_STORE_ID


def clean_answer(text):
    return re.sub(r"【[^】]*】|\[[^\]]*†[^\]]*\]", "", text).replace("*", "").replace("#", "")


@app.get("/")
def page():
    return send_from_directory(Path(__file__).parent, "index.html")


@app.get("/assets/<path:filename>")
def asset(filename):
    return send_from_directory(Path(__file__).parent / "assets", filename)


@app.get("/api/status")
def status():
    return jsonify({"ready": bool(store_id)})


@app.post("/api/ingest")
def ingest():
    global store_id
    try:
        pdf = request.files.get("pdf")
        if not pdf or not pdf.filename:
            return jsonify({"error": "Choose a PDF first."}), 400
        path = rag.DOCUMENTS / Path(pdf.filename).name
        pdf.save(path)
        store_id = rag.ingest(path)
        return jsonify({"ready": True})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.post("/api/chat")
def chat():
    try:
        if not store_id:
            return jsonify({"error": "No indexed document is available yet."}), 400
        body = request.json
        return jsonify({"answer": clean_answer(rag.answer(
            store_id, body["question"], body.get("history", [])))})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


app.run(port=8000)
