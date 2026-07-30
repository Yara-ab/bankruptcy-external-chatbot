import json
import os
import time
from pathlib import Path

import httpx
import oci
from oci_openai import OciOpenAI, OciUserPrincipalAuth
from openai import OpenAI

CONFIG_FILE = Path.home() / ".oci" / "config"
PROFILE = "DEFAULT"
CONFIG = oci.config.from_file(str(CONFIG_FILE), PROFILE)


def setting(env_name, config_name):
    return (os.getenv(env_name) or CONFIG.get(config_name, "")).strip().strip('"\'')


REGION = "me-riyadh-1"
OCR_REGION = "eu-frankfurt-1"
PROJECT_ID = setting("OCI_GENAI_PROJECT_ID", "genai_project_id")
COMPARTMENT_ID = setting("OCI_COMPARTMENT_ID", "compartment_id")
OCR_BUCKET = setting("OCI_OCR_BUCKET", "ocr_bucket")
VECTOR_STORE_ID = setting("OCI_VECTOR_STORE_ID", "vector_store_id")
DOCUMENTS = Path(__file__).parent / "documents"
STORE_FILE = Path(__file__).parent / "vector_store_id.txt"


def settings():
    if not all((PROJECT_ID, COMPARTMENT_ID, OCR_BUCKET)):
        raise RuntimeError("Add genai_project_id, compartment_id, and ocr_bucket to %USERPROFILE%\\.oci\\config.")


def genai():
    settings()
    return OpenAI(
        base_url=f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1",
        api_key="not-used",
        project=PROJECT_ID,
        http_client=httpx.Client(auth=OciUserPrincipalAuth(
            config_file=str(CONFIG_FILE), profile_name=PROFILE)),
    )


def vector_clients():
    settings()
    auth = OciUserPrincipalAuth(config_file=str(CONFIG_FILE), profile_name=PROFILE)
    return (
        OciOpenAI(
            service_endpoint=f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/20231130",
            auth=auth, compartment_id=COMPARTMENT_ID,
            default_headers={"opc-compartment-id": COMPARTMENT_ID, "OpenAI-Project": PROJECT_ID}),
        OciOpenAI(
            service_endpoint=f"https://generativeai.{REGION}.oci.oraclecloud.com/20231130",
            auth=auth, compartment_id=COMPARTMENT_ID,
            default_headers={"opc-compartment-id": COMPARTMENT_ID, "OpenAI-Project": PROJECT_ID}),
    )


def ocr(pdf):
    settings()
    storage = oci.object_storage.ObjectStorageClient(CONFIG)
    document = oci.ai_document.AIServiceDocumentClient(CONFIG)
    storage.base_client.set_region(OCR_REGION)
    document.base_client.set_region(OCR_REGION)
    namespace = storage.get_namespace().data
    try:
        storage.get_bucket(namespace, OCR_BUCKET)
    except oci.exceptions.ServiceError as error:
        if error.status != 404:
            raise
        storage.create_bucket(namespace, oci.object_storage.models.CreateBucketDetails(
            compartment_id=COMPARTMENT_ID, name=OCR_BUCKET))
    source = f"input/{Path(pdf).name}"
    prefix = f"ocr/{Path(pdf).stem}"
    with Path(pdf).open("rb") as file:
        storage.put_object(namespace, OCR_BUCKET, source, file)
    job = document.create_processor_job(oci.ai_document.models.CreateProcessorJobDetails(
        compartment_id=COMPARTMENT_ID,
        input_location=oci.ai_document.models.ObjectStorageLocations(
            object_locations=[oci.ai_document.models.ObjectLocation(
                namespace_name=namespace, bucket_name=OCR_BUCKET, object_name=source)]),
        output_location=oci.ai_document.models.OutputLocation(
            namespace_name=namespace, bucket_name=OCR_BUCKET, prefix=prefix),
        processor_config=oci.ai_document.models.GeneralProcessorConfig(
            language="ar", features=[oci.ai_document.models.DocumentTextExtractionFeature()]),
    )).data
    while document.get_processor_job(job.id).data.lifecycle_state not in ("SUCCEEDED", "FAILED"):
        time.sleep(5)
    output = next(item.name for item in storage.list_objects(
        namespace, OCR_BUCKET, prefix=prefix).data.objects if item.name.endswith(".json"))
    result = json.loads(storage.get_object(namespace, OCR_BUCKET, output).data.content)
    markdown = "\n\n".join(
        "\n".join(line["text"] for line in page.get("lines", []))
        for page in result.get("pages", [])
    )
    output = DOCUMENTS / f"{Path(pdf).stem}.md"
    output.write_text(markdown, encoding="utf-8")
    return output


def ingest(pdf):
    data_plane, _ = vector_clients()
    store_id = VECTOR_STORE_ID or (STORE_FILE.read_text().strip() if STORE_FILE.exists() else None)
    if not store_id:
        raise RuntimeError("Add vector_store_id to %USERPROFILE%\\.oci\\config. No new vector store is created.")
    with ocr(pdf).open("rb") as file:
        uploaded = data_plane.files.create(file=file, purpose="user_data")
    attached = data_plane.vector_stores.files.create(vector_store_id=store_id, file_id=uploaded.id)
    while data_plane.vector_stores.files.retrieve(attached.id, vector_store_id=store_id).status == "in_progress":
        time.sleep(2)
    print(store_id)
    return store_id


def answer(store_id, question, history=None):
    turns = (history or [])[-8:]
    context = "\n".join(
        f"{'User' if turn.get('role') == 'user' else 'Assistant'}: {turn.get('content', '')}"
        for turn in turns
    )
    return genai().responses.create(
        model="openai.gpt-oss-120b",
        instructions="أجب بالعربية فقط وبالاعتماد على الملفات المسترجعة. قل لا أعلم عند غياب الدليل.",
        input=f"Conversation so far:\n{context}\n\nCurrent user question: {question}",
        tools=[{"type": "file_search", "vector_store_ids": [store_id]}],
    ).output_text
