import logging

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(
    route="refunds/{booking_id}",
    methods=[func.HttpMethod.POST],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def refund(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("queued", status_code=202)


@app.route(route="status")
def status(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok")


@app.blob_trigger(arg_name="blob", path="manifests/{name}", connection="MANIFEST_STORAGE")
def import_manifest(blob: func.InputStream):
    logging.info("manifest %s", blob.name)


@app.queue_trigger(arg_name="msg", queue_name="ticket-emails", connection="QUEUE_STORAGE")
def send_ticket_email(msg: func.QueueMessage):
    logging.info("email %s", msg.id)


@app.service_bus_queue_trigger(arg_name="msg", queue_name="fare-changes", connection="SERVICE_BUS")
def apply_fare_change(msg: func.ServiceBusMessage):
    logging.info("fare change %s", msg.message_id)


@app.timer_trigger(schedule="0 0 2 * * *", arg_name="timer")
def nightly_cleanup(timer: func.TimerRequest):
    logging.info("cleanup")
