import argparse
import copy
import dataclasses
import json
import os
import time
from typing import List
import threading

import uuid
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse
import httpx
import numpy as np
import requests
import uvicorn
from vllm.entrypoints.openai.serving_engine import LoRAModulePath
from vllm.entrypoints.openai.protocol import ErrorResponse
from vllm.logger import init_logger
import vllm.envs as envs

logger = init_logger(__name__)

CONTROLLER_HEART_BEAT_EXPIRATION = envs.VLLM_CONTROLLER_HEART_BEAT_EXPIRATION
WORKER_HEART_BEAT_INTERVAL = envs.VLLM_WORKER_HEART_BEAT_INTERVAL
fetch_timeout = httpx.Timeout(3 * 3600, read=None)


@dataclasses.dataclass
class WorkerInfo:
    model_names: List[str]
    speed: int
    queue_length: int
    check_heart_beat: bool
    last_heart_beat: str


def heart_beat_controller(controller):
    while True:
        time.sleep(CONTROLLER_HEART_BEAT_EXPIRATION)
        controller.remove_stale_workers_by_expiration()


class Controller:

    def __init__(self):
        # Dict[str -> WorkerInfo]
        self.worker_info = {}

        self.heart_beat_thread = threading.Thread(target=heart_beat_controller,
                                                  args=(self, ))
        self.heart_beat_thread.start()

    def register_worker(
        self,
        worker_name: str,
        check_heart_beat: bool,
        worker_status: dict,
    ):
        if worker_name not in self.worker_info:
            logger.info("Register a new worker: %s", worker_name)
        else:
            logger.info("Register an existing worker: %s", worker_name)

        self.worker_info[worker_name] = WorkerInfo(
            worker_status["model_names"],
            worker_status["speed"],
            worker_status["queue_length"],
            check_heart_beat,
            time.time(),
        )

        logger.info("Register done: %s, %s", worker_name, worker_status)
        return True

    def remove_worker(self, worker_name: str):
        logger.info("Remove stale worker: %s", worker_name)
        del self.worker_info[worker_name]

    def list_models(self):
        model_names = []

        for w_name, w_info in self.worker_info.items():
            for model in w_info.model_names:
                model_names.append(model)

        return sorted(set(model_names))

    def list_workers(self):
        cur = time.time()
        workers = {}

        for w_name, w_info in self.worker_info.items():
            worker = {
                w_name: {
                    "model": w_info.model_names,
                    "queue_length": w_info.queue_length,
                    "check_heart_beat": w_info.check_heart_beat,
                    "last_heart_beat": cur - w_info.last_heart_beat,
                }
            }
            workers.update(worker)

        return workers

    def get_worker_address(self, model_name: str):
        worker_names = []
        worker_qlen = []
        for w_name, w_info in self.worker_info.items():
            if model_name in w_info.model_names:
                worker_names.append(w_name)
                worker_qlen.append(w_info.queue_length / w_info.speed)
        if len(worker_names) == 0:
            return ""
        min_index = np.argmin(worker_qlen)
        w_name = worker_names[min_index]
        self.worker_info[w_name].queue_length += 1
        logger.info("names: %s, queue_lens: %d, ret: %s", worker_names,
                    min_index, w_name)
        return w_name

    def receive_heart_beat(self, worker_name: str, queue_length: int):
        if worker_name not in self.worker_info:
            logger.info("Receive unknown heart beat. %s", worker_name)
            return False

        self.worker_info[worker_name].queue_length = queue_length
        self.worker_info[worker_name].last_heart_beat = time.time()
        logger.info("Receive heart beat. %s", worker_name)
        return True

    def remove_stale_workers_by_expiration(self):
        expire = time.time() - CONTROLLER_HEART_BEAT_EXPIRATION
        to_delete = []
        for worker_name, w_info in self.worker_info.items():
            if w_info.check_heart_beat and w_info.last_heart_beat < expire:
                to_delete.append(worker_name)

        for worker_name in to_delete:
            self.remove_worker(worker_name)


def heart_beat_worker(obj):
    while True:
        time.sleep(WORKER_HEART_BEAT_INTERVAL)
        obj.send_heart_beat()


class Worker:

    def __init__(self, controller_addr, worker_addr, worker_port, model_names,
                 lora_modules: List[LoRAModulePath], engine):
        self.controller_addr = controller_addr
        self.worker_addr = worker_addr
        self.worker_port = worker_port
        self.model_names = copy.deepcopy(model_names)
        self.worker_id = str(uuid.uuid4())[:8]
        self.call_ct = 0
        self.engine = engine

        if lora_modules is not None:
            for lora in lora_modules:
                self.model_names.append(lora.name)

        self.register_to_controller()
        self.heart_beat_thread = threading.Thread(
            target=heart_beat_worker,
            args=(self, ),
            daemon=True,
        )
        self.heart_beat_thread.start()

    def register_to_controller(self):
        url = "http://" + self.controller_addr + "/register_worker"
        logger.info("Register to controller %s", url)

        worker_name = "{}:{}".format(self.worker_addr, self.worker_port)
        data = {
            "worker_name": worker_name,
            "check_heart_beat": True,
            "worker_status": self.get_status(),
        }

        r = requests.post(url, json=data)
        assert r.status_code == 200

    def get_status(self):
        return {
            "model_names": self.model_names,
            "speed": 1,
            "queue_length": self.get_queue_length(),
        }

    def get_queue_length(self):
        return self.engine.engine.get_num_unfinished_requests()

    def send_heart_beat(self):
        logger.info(
            "Send heart beat. Models: %s. call_ct: %d. "
            "worker_id: %s. ", self.model_names, self.call_ct, self.worker_id)

        url = "http://" + self.controller_addr + "/receive_heart_beat"

        worker_name = "{}:{}".format(self.worker_addr, self.worker_port)
        while True:
            try:
                ret = requests.post(
                    url,
                    json={
                        "worker_name": worker_name,
                        "queue_length": self.get_queue_length(),
                    },
                    timeout=5,
                )
                exist = ret.json()["exist"]
                break
            except (requests.exceptions.RequestException, KeyError) as e:
                logger.error("heart beat error: ", exc_info=e)
            time.sleep(5)

        if not exist:
            self.register_to_controller()


app = FastAPI()


def create_controller():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--ssl",
        action="store_true",
        required=False,
        default=False,
        help="Enable SSL. Requires OS Environment variables "
        "'SSL_KEYFILE' and 'SSL_CERTFILE'.",
    )
    args = parser.parse_args()
    logger.info("Start up controller: %s", str(args))

    controller = Controller()
    return args, controller


@app.post("/register_worker")
async def register_worker(request: Request):
    data = await request.json()
    controller.register_worker(
        data["worker_name"],
        data["check_heart_beat"],
        data.get("worker_status", None),
    )


@app.post("/receive_heart_beat")
async def receive_heart_beat(request: Request):
    data = await request.json()
    exist = controller.receive_heart_beat(data["worker_name"],
                                          data["queue_length"])
    return {"exist": exist}


@app.get("/list_models")
async def show_available_models():
    models = controller.list_models()
    return {"models": models}


@app.get("/list_workers")
async def show_available_workers():
    return controller.list_workers()


async def generate_completions_stream(worker_addr, data, api):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST",
                                 f"http://{worker_addr}/{api}",
                                 json=data) as response:
            async for chunk in response.aiter_raw():
                yield chunk


async def generate_completions(worker_addr, data, api):
    async with httpx.AsyncClient(timeout=fetch_timeout) as client:
        response = await client.post(f"http://{worker_addr}/{api}", json=data)
        return JSONResponse(content=response.json(),
                            status_code=response.status_code)


async def gen_response(request, api):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return ErrorResponse(message="Json decode error",
                             type="BadRequestError",
                             code=400)
    if "model" not in data:
        return ErrorResponse(message="No model field found in the request",
                             type="BadRequestError",
                             code=400)
    model = data["model"]
    worker_addr = controller.get_worker_address(model)
    if worker_addr == "":
        return ErrorResponse(message=f"model [{model}] not register yet",
                             type="BadRequestError",
                             code=400)
    if "stream" in data and data["stream"]:
        return StreamingResponse(generate_completions_stream(
            worker_addr, data, api),
                                 media_type="text/event-stream")
    else:
        return await generate_completions(worker_addr, data, api)


@app.get("/health")
async def health() -> Response:
    """Health check."""
    return Response(status_code=200)


@app.post("/v1/chat/completions")
async def create_chat_completion(request: Request):

    return await gen_response(request, "v1/chat/completions")


@app.post("/v1/completions")
async def create_completion(request: Request):
    return await gen_response(request, "v1/completions")


if __name__ == "__main__":
    args, controller = create_controller()
    if args.ssl:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            ssl_keyfile=os.environ["SSL_KEYFILE"],
            ssl_certfile=os.environ["SSL_CERTFILE"],
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
