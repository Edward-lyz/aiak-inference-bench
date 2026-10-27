"""Run native SGLang clients against isolated Docker servers; retain evidence locally."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from summarize import build_summary


def validate_config(config: dict) -> None:
    required = (
        "model_name", "model_path", "model_revision", "precision", "gpu_ids",
        "port", "output_dir", "client_image", "engines", "server_args",
        "phase_args", "input_length", "decode_output_length", "concurrencies",
        "num_prompts", "repetitions", "min_measurement_seconds", "max_cv",
        "startup_timeout_seconds", "point_timeout_seconds",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Missing config fields: {missing}")
    for key in (
        "input_length", "decode_output_length", "num_prompts", "repetitions",
        "startup_timeout_seconds", "point_timeout_seconds",
    ):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if config["decode_output_length"] <= 1:
        raise ValueError("Decode needs more than one output token")
    if not config["concurrencies"] or any(type(c) is not int or c <= 0 for c in config["concurrencies"]):
        raise ValueError("concurrencies must contain positive integers")
    if config["concurrencies"] != sorted(set(config["concurrencies"])):
        raise ValueError("concurrencies must be unique and increasing")
    if not config["gpu_ids"] or len(set(config["gpu_ids"])) != len(config["gpu_ids"]):
        raise ValueError("gpu_ids must be nonempty and unique")
    if any(type(gpu) is not int or gpu < 0 for gpu in config["gpu_ids"]):
        raise ValueError("gpu_ids must contain nonnegative integers")
    if not 1024 <= config["port"] <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    if not 0 < config["max_cv"] <= 0.05 or config["min_measurement_seconds"] < 120:
        raise ValueError("Capacity qualification needs min_measurement_seconds >= 120 and 0 < max_cv <= 0.05")
    if not Path(config["model_path"]).joinpath("config.json").is_file():
        raise ValueError("model_path must contain config.json")
    if len({engine["id"] for engine in config["engines"]}) != len(config["engines"]):
        raise ValueError("Engine IDs must be unique")
    for engine in config["engines"]:
        if engine["id"] not in ("community", "aiak"):
            raise ValueError("Engine ID must be community or aiak")
        if not isinstance(engine["image"], str) or not engine["image"]:
            raise ValueError("Each engine needs an image")
        if any("speculative" in arg or "ep-dispatch-algorithm" in arg for arg in engine.get("extra_args", [])):
            raise ValueError("Engine overrides cannot enable MTP or fake expert routing")
    for args in [config["server_args"], *config["phase_args"].values()]:
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("Server arguments must be arrays of strings")
    if "--disable-radix-cache" not in config["server_args"]:
        raise ValueError("This capacity profile requires --disable-radix-cache")
    if any("speculative" in arg or "ep-dispatch-algorithm" in arg for arg in config["server_args"]):
        raise ValueError("This profile supports MTP off and real expert routing only")


def save_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def image_identity(image: str) -> dict:
    details = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    source = subprocess.check_output([
        "docker", "run", "--rm", "--entrypoint", "git", details["Id"],
        "-C", "/sgl-workspace/sglang", "rev-parse", "HEAD",
    ], text=True).strip()
    return {"reference": image, "id": details["Id"], "repo_digests": details["RepoDigests"], "source_sha": source}


def check_gpu_ownership(server_name: str | None = None, terminate_foreign: bool = False) -> bool:
    active_pids = set(subprocess.check_output([
        "nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits",
    ], text=True).split())
    owned_pids = set()
    if server_name:
        owned_pids = set(subprocess.check_output(["docker", "top", server_name, "-eo", "pid"], text=True).split()[1:])
    foreign_pids = active_pids - owned_pids
    if foreign_pids and not terminate_foreign:
        raise RuntimeError(f"External GPU compute processes present: {sorted(foreign_pids)}")
    if not foreign_pids:
        return False
    if os.geteuid() != 0:
        raise PermissionError("Terminating foreign GPU processes requires an explicitly authorized root run")
    pidfds = {}
    try:
        for pid in sorted(foreign_pids):
            try:
                pidfds[pid] = os.pidfd_open(int(pid))
            except ProcessLookupError:
                continue
        refreshed = set(subprocess.check_output([
            "nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits",
        ], text=True).split())
        targets = {pid: fd for pid, fd in pidfds.items() if pid in refreshed}
        print(json.dumps({"event": "terminate_foreign_gpu", "pids": sorted(targets),
                          "timestamp": datetime.now(timezone.utc).isoformat()}), flush=True)
        for termination_signal in (signal.SIGTERM, signal.SIGKILL):
            for fd in targets.values():
                try:
                    signal.pidfd_send_signal(fd, termination_signal)
                except ProcessLookupError:
                    continue
            time.sleep(5 if termination_signal == signal.SIGTERM else 2)
    finally:
        for fd in pidfds.values():
            os.close(fd)
    return True


def run_point(config: dict, engine: dict, phase: str, concurrency: int, repetition: int,
              run_dir: Path, client_image: str, profile: str, manifest: dict, server_name: str) -> dict:
    point_dir = run_dir / f"{engine['id']}-{phase}-c{concurrency}-r{repetition}"
    point_dir.mkdir()
    client_name = f"aiak-bench-client-{run_dir.name}-{engine['id']}-{phase.lower()}-{concurrency}-{repetition}"
    prompts = config["num_prompts"] if profile == "smoke" else max(config["num_prompts"], 8 * concurrency)
    point = {
        "schema_version": 1, "run_id": run_dir.name, "timestamp": datetime.now(timezone.utc).isoformat(),
        "engine": engine["id"], "engine_sha": manifest["engines"][engine["id"]]["source_sha"],
        "image_id": manifest["engines"][engine["id"]]["id"], "client_image_id": client_image,
        "model": config["model_name"], "model_revision": config["model_revision"],
        "model_config_sha256": manifest["model_config_sha256"], "precision": config["precision"],
        "phase": phase, "measurement_mode": "client" if phase == "P" else "client-synthetic-kv",
        "fake_kv": phase == "D", "fake_ep": False, "mtp": False,
        "cache_mode": "off", "profile": profile, "input_length": config["input_length"],
        "output_length": 1 if phase == "P" else config["decode_output_length"],
        "concurrency": concurrency, "repetition": repetition, "num_prompts": prompts,
        "gpu_count": len(config["gpu_ids"]), "hardware": manifest["hardware"],
        "seed": 42, "status": "running", "error": None,
        "dataset": "random-ids", "tokenized_prompt": True, "random_range_ratio": 1.0,
        "measurement_basis": "native-full-run",
        "min_measurement_seconds": config["min_measurement_seconds"], "max_cv": config["max_cv"],
        "server_args": config["server_args"] + config["phase_args"][phase],
    }
    save_json(point_dir / "point.json", point)
    command = [
        "docker", "run", "--rm", "--name", client_name, "--network", "host",
        "--label", f"aiak-bench.run={run_dir.name}",
        "-e", "NVIDIA_VISIBLE_DEVICES=void", "-e", "CUDA_VISIBLE_DEVICES=",
        "-e", "OPENBLAS_NUM_THREADS=1",
        "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
        *(["--security-opt", config["docker_security_opt"]] if config.get("docker_security_opt") else []),
        "-v", f"{config['model_path']}:/model:ro", "-v", f"{point_dir}:/results",
        "--entrypoint", "python3", client_image,
        "-m", "sglang.benchmark.steady_state_serving",
        "--steady-state-concurrency-ratio", "0.8",
        "--steady-state-output-file", "/results/steady.jsonl",
        "--backend", "sglang", "--base-url", f"http://127.0.0.1:{config['port']}",
        "--model", "/model", "--tokenizer", "/model", "--dataset-name", "random-ids",
        "--tokenize-prompt",
        "--random-input-len", str(config["input_length"]),
        "--random-output-len", str(point["output_length"]), "--random-range-ratio", "1",
        "--num-prompts", str(prompts), "--max-concurrency", str(concurrency),
        "--request-rate", "inf", "--seed", "42", "--warmup-requests", str(min(concurrency, 4)),
        "--output-file", "/results/raw.jsonl",
    ]
    if phase == "D":
        command.append("--fake-prefill")
    save_json(point_dir / "command.json", {"argv": command})
    print(f"Running {point_dir.name}", flush=True)
    process = None
    attempts = []
    try:
        check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"])
        deadline = time.monotonic() + config["point_timeout_seconds"]
        for attempt in range(1, 2 if profile == "smoke" else 4):
            point["num_prompts"] = prompts
            command[command.index("--num-prompts") + 1] = str(prompts)
            attempts.append({"attempt": attempt, "num_prompts": prompts, "argv": list(command)})
            save_json(point_dir / "command.json", {"argv": command, "attempts": attempts})
            with (point_dir / "client.log").open("a", encoding="utf-8") as log:
                log.write(f"\nNative attempt {attempt}: {prompts} requests\n")
                log.flush()
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                while process.poll() is None:
                    if check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"]):
                        raise RuntimeError("External GPU interference removed; overlapping measurement is invalid")
                    if time.monotonic() > deadline:
                        raise RuntimeError("Native client exceeded point timeout")
                    time.sleep(5)
            if check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"]):
                raise RuntimeError("External GPU interference removed; overlapping measurement is invalid")
            if process.returncode != 0:
                raise RuntimeError(f"Native client exited {process.returncode}; inspect client.log")
            raw = json.loads((point_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            steady = json.loads((point_dir / "steady.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            if raw["completed"] != prompts:
                raise RuntimeError(f"Only {raw['completed']}/{prompts} requests completed")
            if raw["total_input_tokens"] != prompts * config["input_length"]:
                raise RuntimeError("Native input token count does not match the fixed-length workload")
            expected_output = prompts * point["output_length"]
            if raw["total_output_tokens"] != expected_output:
                raise RuntimeError(f"Output token count {raw['total_output_tokens']} != requested {expected_output}")
            measured = {key: raw[key] for key in (
                "duration", "completed", "total_input_tokens", "total_output_tokens",
                "input_throughput", "output_throughput", "mean_ttft_ms", "mean_tpot_ms",
                "mean_e2e_latency_ms",
            )}
            steady = {key: steady[key] for key in (
                "input_throughput", "output_throughput", "duration", "average_concurrency",
            )}
            if any(value is not None and not math.isfinite(value) for value in [*measured.values(), *steady.values()]):
                raise ValueError("Native metrics contain a non-finite number")
            if measured["duration"] <= 0 or not measured["mean_e2e_latency_ms"]:
                raise ValueError("Native duration and mean E2E latency must be positive")
            measurement = {
                "duration": measured["duration"],
                "input_throughput": measured["input_throughput"],
                "output_throughput": measured["output_throughput"],
                "average_concurrency": measured["completed"] * measured["mean_e2e_latency_ms"]
                / (1000 * measured["duration"]),
            }
            attempts[-1]["steady"] = steady
            attempts[-1]["measurement"] = measurement
            save_json(point_dir / "command.json", {"argv": command, "attempts": attempts})
            if profile == "smoke" or measurement["duration"] >= config["min_measurement_seconds"]:
                break
            next_prompts = min(16384, math.ceil(
                prompts * config["min_measurement_seconds"] * 1.25 / measurement["duration"] / concurrency
            ) * concurrency)
            if next_prompts <= prompts:
                break
            prompts = next_prompts
        point.update(status="ok", metrics=measured, steady=steady, measurement=measurement)
    except KeyboardInterrupt:
        point.update(status="cancelled", error="Cancelled")
        raise
    except (subprocess.CalledProcessError, RuntimeError, OSError, ValueError, KeyError) as exc:
        point.update(status="failed", error=str(exc))
    finally:
        subprocess.run(["docker", "rm", "-f", client_name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        if process is not None:
            process.wait(timeout=30)
        save_json(point_dir / "point.json", point)
    return point


def run_server(config: dict, engine: dict, phase: str, run_dir: Path,
               profile: str, manifest: dict) -> bool:
    name = f"aiak-bench-server-{run_dir.name}-{engine['id']}-{phase.lower()}"
    server_log = run_dir / f"{engine['id']}-{phase}-server.log"
    opener = build_opener(ProxyHandler({}))
    command = [
        "docker", "run", "-d", "--name", name, "--gpus",
        '"device=' + ",".join(map(str, config["gpu_ids"])) + '"',
        "--network", "host", "--shm-size", "32g", "--label", f"aiak-bench.run={run_dir.name}",
        *(["--security-opt", config["docker_security_opt"]] if config.get("docker_security_opt") else []),
        "-v", f"{config['model_path']}:/model:ro",
    ]
    if config.get("cache_root"):
        cache_dir = Path(config["cache_root"], engine["id"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        command.extend(["-v", f"{cache_dir}:/root/.cache"])
    for key, value in engine.get("env", {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([
        "--entrypoint", "python3", manifest["engines"][engine["id"]]["id"],
        "-m", "sglang.launch_server", "--model-path", "/model",
        "--served-model-name", config["model_name"], "--host", "127.0.0.1", "--port", str(config["port"]),
        *config["server_args"], *config["phase_args"][phase], *engine.get("extra_args", []),
    ])
    save_json(run_dir / f"{engine['id']}-{phase}-launch.json", {"argv": command})
    print(f"Starting {name}", flush=True)
    success = True
    try:
        check_gpu_ownership(terminate_foreign=config["terminate_foreign_gpu_processes"])
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + config["startup_timeout_seconds"]
        while time.monotonic() < deadline:
            state = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Running}}", name], text=True).strip()
            if state != "true":
                raise RuntimeError(f"Server exited during startup: {name}")
            check_gpu_ownership(name, config["terminate_foreign_gpu_processes"])
            try:
                with opener.open(f"http://127.0.0.1:{config['port']}/health", timeout=3) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass  # Readiness polling; deadline and container state are checked above.
            time.sleep(5)
        else:
            raise RuntimeError(f"Server readiness exceeded {config['startup_timeout_seconds']}s")
        with opener.open(f"http://127.0.0.1:{config['port']}/server_info", timeout=10) as response:
            save_json(run_dir / f"{engine['id']}-{phase}-server-info.json", json.load(response))
        for concurrency in config["concurrencies"]:
            point_config = dict(config)
            for repetition in range(1, config["repetitions"] + 1):
                point = run_point(point_config, engine, phase, concurrency, repetition, run_dir,
                                  manifest["client"]["id"], profile, manifest, name)
                summary = build_summary(run_dir.parent, run_dir.parent / "summary.json")
                if config.get("publish_summary_path"):
                    save_json(Path(config["publish_summary_path"]), summary)
                if point["status"] != "ok":
                    return False  # Do not continue pushing a failing or unhealthy engine.
                point_config["num_prompts"] = point["num_prompts"]
    except (RuntimeError, subprocess.CalledProcessError, URLError, OSError) as exc:
        save_json(run_dir / f"{engine['id']}-{phase}-failure.json", {
            "engine": engine["id"], "phase": phase, "run_id": run_dir.name,
            "status": "failed", "error": str(exc), "stage": "server",
        })
        print(str(exc), file=sys.stderr, flush=True)
        success = False
    finally:
        with server_log.open("w", encoding="utf-8") as log:
            subprocess.run(["docker", "logs", name], stdout=log, stderr=subprocess.STDOUT, check=False)
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--profile", choices=("smoke", "capacity"), default="smoke")
    parser.add_argument("--engine", choices=("all", "community", "aiak"), default="all")
    parser.add_argument("--phase", choices=("all", "P", "D"), default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--terminate-foreign-gpu-processes", action="store_true",
                        help="Authorized root runs only: terminate exact foreign GPU compute PIDs")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["terminate_foreign_gpu_processes"] = args.terminate_foreign_gpu_processes
    if args.terminate_foreign_gpu_processes and os.geteuid() != 0:
        raise PermissionError("--terminate-foreign-gpu-processes requires root")
    validate_config(config)
    engines = [engine for engine in config["engines"] if args.engine in ("all", engine["id"])]
    if not engines:
        raise ValueError("Requested engine is absent from config")
    if args.dry_run:
        print(json.dumps({"profile": args.profile, "engines": [e["id"] for e in engines],
                          "phase": args.phase, "config_valid": True}))
        return
    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # All configs share a host-level lock, including different checkouts and runners.
    lock_fd = os.open("/tmp/aiak-inference-bench.gpu.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    with os.fdopen(lock_fd, "r", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_gpu_ownership(terminate_foreign=config["terminate_foreign_gpu_processes"])
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", config["port"]))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        run_dir = output_dir / run_id
        run_dir.mkdir()
        hardware = subprocess.check_output([
            "nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader",
        ], text=True).strip()
        manifest = {
            "run_id": run_id, "profile": args.profile, "hardware": hardware,
            "model_config_sha256": hashlib.sha256(Path(config["model_path"], "config.json").read_bytes()).hexdigest(),
            "client": image_identity(config["client_image"]),
            "engines": {engine["id"]: image_identity(engine["image"]) for engine in engines},
            "config": config,
            "docker_version": subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"], text=True).strip(),
            "orchestrator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        }
        save_json(run_dir / "manifest.json", manifest)
        def stop_on_signal(signum: int, frame: object) -> None:
            raise KeyboardInterrupt(f"Received signal {signum}; cleaning this run's containers")
        signal.signal(signal.SIGTERM, stop_on_signal)
        all_ok = True
        phases = ["P", "D"] if args.phase == "all" else [args.phase]
        try:
            for engine in engines:
                for phase in phases:
                    all_ok = run_server(config, engine, phase, run_dir, args.profile, manifest) and all_ok
        finally:
            summary = build_summary(output_dir, output_dir / "summary.json")
            if config.get("publish_summary_path"):
                save_json(Path(config["publish_summary_path"]), summary)
        print(f"Run evidence: {run_dir}", flush=True)
        raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
