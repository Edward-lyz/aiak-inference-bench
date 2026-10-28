"""Run native SGLang clients against isolated Docker servers; retain evidence locally."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, ProxyHandler, build_opener, parse_http_list, parse_keqv_list

from summarize import build_summary


def validate_config(config: dict) -> None:
    required = (
        "model_name", "model_path", "model_revision", "precision", "gpu_ids",
        "port", "output_dir", "client_image", "engines", "server_args",
        "phase_args", "input_length", "decode_output_length", "concurrencies",
        "num_prompts", "repetitions", "min_measurement_seconds", "max_cv",
        "startup_timeout_seconds", "point_timeout_seconds",
        "protocol_id", "recipe_source", "dataset_path",
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
    for values in config.get("phase_concurrencies", {}).values():
        if not values or any(type(c) is not int or c <= 0 for c in values) or values != sorted(set(values)):
            raise ValueError("phase_concurrencies must contain unique increasing positive integers")
    if any(type(count) is not int or count <= 0 for count in config.get("paired_prompt_counts", {}).values()):
        raise ValueError("paired_prompt_counts must contain positive integers")
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
    if config["protocol_id"] != "sglang-cookbook-v2":
        raise ValueError("Retired protocol: use sglang-cookbook-v2")
    if not Path(config["dataset_path"]).is_file():
        raise ValueError("A pinned local ShareGPT dataset is required")
    for engine in config["engines"]:
        if engine["id"] not in ("community", "aiak"):
            raise ValueError("Engine ID must be community or aiak")
        if not isinstance(engine["image"], str) or not engine["image"]:
            raise ValueError("Each engine needs an image")
        if any("speculative" in arg or "ep-dispatch-algorithm" in arg for arg in engine.get("extra_args", [])):
            raise ValueError("Engine overrides cannot change the shared speculation/routing recipe")
    for engine in config["engines"]:
        for args in engine.get("launch_args", {}).values():
            if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise ValueError("launch_args must be arrays of strings")
            if any("disaggregation" in arg for arg in args):
                raise ValueError("launch_args must describe a single real-Prefill instance")
    for args in [config["server_args"], *config["phase_args"].values()]:
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("Server arguments must be arrays of strings")
    if "--disable-radix-cache" not in config["server_args"]:
        raise ValueError("This capacity profile requires --disable-radix-cache")
    for phase in ("P", "D"):
        args = config["server_args"] + config["phase_args"][phase]
        if any("disaggregation" in arg or "ep-dispatch-algorithm" in arg for arg in args):
            raise ValueError("Cookbook baseline requires real Prefill and native expert routing")
        if phase == "P" and any("speculative" in arg for arg in args):
            raise ValueError("Official high-throughput baseline disables speculation")
        if phase == "D":
            if "--speculative-algorithm" not in args or args[args.index("--speculative-algorithm") + 1] != "DSPARK":
                raise ValueError("Official low-latency baseline requires DSPARK")
            if "--speculative-dspark-block-size" not in args or args[args.index("--speculative-dspark-block-size") + 1] != "5":
                raise ValueError("Official low-latency baseline uses DSpark block size 5")


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
    package_version = subprocess.check_output([
        "docker", "run", "--rm", "--network", "none",
        "-e", "NVIDIA_VISIBLE_DEVICES=void", "-e", "CUDA_VISIBLE_DEVICES=",
        "--entrypoint", "python3", details["Id"], "-c",
        "import importlib.metadata; print(importlib.metadata.version('sglang'))",
    ], text=True).strip()
    return {"reference": image, "id": details["Id"], "repo_digests": details["RepoDigests"],
            "source_sha": source, "package_version": package_version}


def check_gpu_ownership(server_name: str | None = None, terminate_foreign: bool = False,
                        gpu_ids: list[int] | None = None) -> bool:
    selection = ["--id=" + ",".join(map(str, gpu_ids))] if gpu_ids else []
    active_pids = set(subprocess.check_output([
        "nvidia-smi", *selection, "--query-compute-apps=pid", "--format=csv,noheader,nounits",
    ], text=True).split())
    owned_pids = set()
    if server_name:
        owned_pids = set(subprocess.check_output(["docker", "top", server_name, "-eo", "pid"], text=True).split()[1:])
        container_id = subprocess.check_output(["docker", "inspect", "-f", "{{.Id}}", server_name], text=True).strip()
        for pid in active_pids - owned_pids:
            try:
                if container_id in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8"):
                    owned_pids.add(pid)  # Worker spawned after the docker top snapshot.
            except OSError:
                continue
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
            "nvidia-smi", *selection, "--query-compute-apps=pid", "--format=csv,noheader,nounits",
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
    fixed_prompts = config.get("paired_prompt_counts", {}).get(f"{phase}:{concurrency}") if profile == "capacity" else None
    prompts = fixed_prompts if fixed_prompts is not None else (
        config["num_prompts"] if profile == "smoke" else max(config["num_prompts"], 4 * concurrency)
    )
    point = {
        "schema_version": 1, "run_id": run_dir.name, "timestamp": datetime.now(timezone.utc).isoformat(),
        "engine": engine["id"], "engine_sha": manifest["engines"][engine["id"]]["source_sha"],
        "engine_variant": engine.get("variant", "unmodified"),
        "protocol_id": config["protocol_id"], "recipe_source": config["recipe_source"],
        "strategy": "high-throughput" if phase == "P" else "low-latency",
        "speculative_algorithm": None if phase == "P" else "DSPARK",
        "speculative_block_size": None if phase == "P" else 5,
        "dataset_sha256": manifest["dataset_sha256"],
        "image_id": manifest["engines"][engine["id"]]["id"], "client_image_id": client_image,
        "model": config["model_name"], "model_revision": config["model_revision"],
        "model_config_sha256": manifest["model_config_sha256"], "precision": config["precision"],
        "phase": phase, "measurement_mode": "client" if phase == "P" else "real-prefill-decode-latency",
        "fake_kv": False, "fake_ep": False, "mtp": phase == "D",
        "cache_mode": "off", "profile": profile, "input_length": config["input_length"],
        "output_length": 1 if phase == "P" else config["decode_output_length"],
        "concurrency": concurrency, "repetition": repetition, "num_prompts": prompts,
        "gpu_count": len(config["gpu_ids"]), "hardware": manifest["hardware"],
        "seed": 42, "status": "running", "error": None,
        "dataset": "sharegpt-repeat-truncate", "tokenized_prompt": True, "random_range_ratio": 1.0,
        "measurement_basis": "native-full-run",
        "min_measurement_seconds": config["min_measurement_seconds"], "max_cv": config["max_cv"],
        "server_args": engine_args(config, engine, phase),
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
        "-v", f"{config['dataset_path']}:/dataset.json:ro",
        "--entrypoint", "python3", client_image,
        "-m", "sglang.benchmark.steady_state_serving",
        "--steady-state-concurrency-ratio", "0.8",
        "--steady-state-output-file", "/results/steady.jsonl",
        "--backend", "sglang", "--base-url", f"http://127.0.0.1:{config['port']}",
        "--model", "/model", "--tokenizer", "/model", "--dataset-name", "random",
        "--dataset-path", "/dataset.json",
        "--tokenize-prompt",
        "--random-input-len", str(config["input_length"]),
        "--random-output-len", str(point["output_length"]), "--random-range-ratio", "1",
        "--num-prompts", str(prompts), "--max-concurrency", str(concurrency),
        "--request-rate", "inf", "--seed", "42", "--warmup-requests", str(min(concurrency, 4)),
        "--output-file", "/results/raw.jsonl",
    ]
    save_json(point_dir / "command.json", {"argv": command})
    print(f"Running {point_dir.name}", flush=True)
    process = None
    attempts = []
    try:
        check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"], config["gpu_ids"])
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
                    if check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"], config["gpu_ids"]):
                        raise RuntimeError("External GPU interference removed; overlapping measurement is invalid")
                    if time.monotonic() > deadline:
                        raise RuntimeError("Native client exceeded point timeout")
                    time.sleep(5)
            if check_gpu_ownership(server_name, config["terminate_foreign_gpu_processes"], config["gpu_ids"]):
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
            measured.update({key: raw.get(key) for key in ("accept_length", "median_tpot_ms", "p99_tpot_ms")})
            if phase == "D" and (not measured["accept_length"] or measured["accept_length"] <= 0):
                raise ValueError("DSpark acceptance was not reported by the native client")
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
            if profile == "smoke" or fixed_prompts is not None or measurement["duration"] >= config["min_measurement_seconds"]:
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


def engine_args(config: dict, engine: dict, phase: str) -> list[str]:
    release = engine.get("launch_args", {}).get(phase)
    if release is not None:
        return release
    return [*config["server_args"], *config["phase_args"][phase], *engine.get("extra_args", [])]


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
    for key, value in {**engine.get("env", {}), **engine.get("launch_env", {}).get(phase, {})}.items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([
        "--entrypoint", "python3", manifest["engines"][engine["id"]]["id"],
        "-m", "sglang.launch_server", "--model-path", "/model",
        "--served-model-name", config["model_name"], "--host", "127.0.0.1", "--port", str(config["port"]),
        *engine_args(config, engine, phase),
    ])
    save_json(run_dir / f"{engine['id']}-{phase}-launch.json", {"argv": command})
    print(f"Starting {name}", flush=True)
    success = True
    try:
        check_gpu_ownership(terminate_foreign=config["terminate_foreign_gpu_processes"], gpu_ids=config["gpu_ids"])
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + config["startup_timeout_seconds"]
        while time.monotonic() < deadline:
            state = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Running}}", name], text=True).strip()
            if state != "true":
                raise RuntimeError(f"Server exited during startup: {name}")
            check_gpu_ownership(name, config["terminate_foreign_gpu_processes"], config["gpu_ids"])
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
            server_info = json.load(response)
        save_json(run_dir / f"{engine['id']}-{phase}-server-info.json", server_info)
        resolved = server_info.get("server_args", server_info)
        launch = engine_args(config, engine, phase)
        expected_speculation = "DSPARK" if "--speculative-algorithm" in launch or phase == "D" else None
        if resolved.get("speculative_algorithm") != expected_speculation:
            raise RuntimeError("Resolved speculative algorithm differs from the cookbook recipe")
        if resolved.get("tp_size") != len(config["gpu_ids"]) or resolved.get("ep_size") != len(config["gpu_ids"]):
            raise RuntimeError("Resolved TP/EP topology differs from the cookbook GPU allocation")
        if phase == "D" and resolved.get("speculative_dspark_block_size") != 5:
            raise RuntimeError("Resolved DSpark block size differs from the cookbook recipe")
        config.setdefault("paired_prompt_counts", {})
        for concurrency in config.get("phase_concurrencies", {}).get(phase, config["concurrencies"]):
            point_config = dict(config)
            for repetition in range(1, config["repetitions"] + 1):
                point = run_point(point_config, engine, phase, concurrency, repetition, run_dir,
                                  manifest["client"]["id"], profile, manifest, name)
                summary = build_summary(run_dir.parent, run_dir.parent / "summary.json")
                if config.get("publish_summary_path"):
                    save_json(Path(config["publish_summary_path"]), summary)
                if point["status"] != "ok":
                    return False  # Do not continue pushing a failing or unhealthy engine.
                if profile == "capacity":
                    config["paired_prompt_counts"].setdefault(f"{phase}:{concurrency}", point["num_prompts"])
                    save_json(run_dir / "manifest.json", manifest)
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
    orchestrator_source = Path(__file__).read_bytes()
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
    if config.get("community_image_repository"):
        repository = config["community_image_repository"]
        registry, image_path = repository.split("/", 1)
        registry_url = f"https://{registry}"
        tags_url = f"{registry_url}/v2/{image_path}/tags/list?n=10000"
        registry_opener = build_opener(ProxyHandler({}))
        headers = {}
        try:
            with registry_opener.open(tags_url, timeout=30) as response:
                tag_page = json.load(response)
                next_link = response.headers.get("Link")
        except HTTPError as exc:
            challenge = exc.headers.get("WWW-Authenticate", "")
            if exc.code != 401 or not challenge.startswith("Bearer "):
                raise
            auth = parse_keqv_list(parse_http_list(challenge[len("Bearer "):]))
            token_url = auth["realm"] + "?" + urlencode({
                "service": auth["service"], "scope": f"repository:{image_path}:pull",
            })
            with registry_opener.open(token_url, timeout=30) as response:
                token = json.load(response)
            headers["Authorization"] = "Bearer " + token["token"]
            with registry_opener.open(Request(tags_url, headers=headers), timeout=30) as response:
                tag_page = json.load(response)
                next_link = response.headers.get("Link")
        tags = list(tag_page["tags"])
        while next_link:
            next_url = urljoin(registry_url, next_link.split(";")[0].strip("<>"))
            with registry_opener.open(Request(next_url, headers=headers), timeout=30) as response:
                tags.extend(json.load(response)["tags"])
                next_link = response.headers.get("Link")
        dated_tags = [
            (match.group(1), tag) for tag in tags
            if (match := re.fullmatch(r"nightly-dev-cu13-(\d{8})-[0-9a-f]{8}", tag))
        ]
        if not dated_tags:
            raise ValueError("No CUDA13 amd64 community-nightly tag found")
        newest_date = max(date for date, tag in dated_tags)
        headers["Accept"] = ", ".join((
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ))
        candidates = []
        for date, tag in dated_tags:
            if date != newest_date:
                continue
            manifest_url = f"{registry_url}/v2/{image_path}/manifests/{tag}"
            with registry_opener.open(Request(manifest_url, headers=headers), timeout=30) as response:
                manifest_bytes = response.read()
            digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            image_manifest = json.loads(manifest_bytes)
            if "manifests" in image_manifest:
                child = next(item for item in image_manifest["manifests"]
                             if item["platform"].get("os") == "linux" and item["platform"].get("architecture") == "amd64")
                child_url = f"{registry_url}/v2/{image_path}/manifests/{child['digest']}"
                with registry_opener.open(Request(child_url, headers=headers), timeout=30) as response:
                    image_manifest = json.load(response)
            config_url = f"{registry_url}/v2/{image_path}/blobs/{image_manifest['config']['digest']}"
            # curl strips registry Authorization on cross-host blob redirects.
            curl_config = f'header = {json.dumps("Authorization: " + headers["Authorization"])}\n' if headers.get("Authorization") else ""
            image_config = json.loads(subprocess.check_output(
                ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "30",
                 "--config", "-", config_url], input=curl_config, text=True,
            ))
            if image_config["architecture"] != "amd64" or image_config["os"] != "linux":
                raise ValueError(f"Latest tag {tag} is not linux/amd64")
            candidates.append((image_config["created"], tag, digest))
        created, tag, digest = max(candidates)
        pinned_image = f"{repository}@{digest}"
        config["community_image_selection"] = {"tag": tag, "digest": digest, "created": created}
        print(json.dumps({"event": "resolved_community_image", **config["community_image_selection"]}), flush=True)
        subprocess.run(["docker", "pull", "--platform", "linux/amd64", pinned_image], check=True)
        config["client_image"] = pinned_image
        for engine in config["engines"]:
            if engine["id"] == "community":
                engine["image"] = pinned_image
    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # All configs share a host-level lock, including different checkouts and runners.
    lock_fd = os.open("/tmp/aiak-inference-bench.gpu.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    with os.fdopen(lock_fd, "r", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_gpu_ownership(terminate_foreign=config["terminate_foreign_gpu_processes"], gpu_ids=config["gpu_ids"])
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", config["port"]))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        run_dir = output_dir / run_id
        run_dir.mkdir()
        (run_dir / "orchestrator.py").write_bytes(orchestrator_source)
        hardware = subprocess.check_output([
            "nvidia-smi", "--id=" + ",".join(map(str, config["gpu_ids"])),
            "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader",
        ], text=True).strip()
        dataset_hash = hashlib.sha256()
        with Path(config["dataset_path"]).open("rb") as dataset:
            while chunk := dataset.read(1024 * 1024):
                dataset_hash.update(chunk)
        manifest = {
            "run_id": run_id, "profile": args.profile, "hardware": hardware,
            "model_config_sha256": hashlib.sha256(Path(config["model_path"], "config.json").read_bytes()).hexdigest(),
            "dataset_sha256": dataset_hash.hexdigest(),
            "client": image_identity(config["client_image"]),
            "engines": {engine["id"]: image_identity(engine["image"]) for engine in engines},
            "config": config,
            "docker_version": subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"], text=True).strip(),
            "orchestrator_sha256": hashlib.sha256(orchestrator_source).hexdigest(),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        }
        provenance = config.get("image_version_provenance", {})
        for identity in [manifest["client"], *manifest["engines"].values()]:
            verified = provenance.get(identity["id"])
            if verified:
                if verified["source_sha"] != identity["source_sha"]:
                    raise ValueError("Version provenance does not match the selected image source")
                identity["version_provenance"] = verified
        save_json(run_dir / "manifest.json", manifest)
        def stop_on_signal(signum: int, frame: object) -> None:
            raise KeyboardInterrupt(f"Received signal {signum}; cleaning this run's containers")
        signal.signal(signal.SIGTERM, stop_on_signal)
        all_ok = True
        phases = ["P", "D"] if args.phase == "all" else [args.phase]
        image_holders = []
        try:
            image_ids = {manifest["client"]["id"], *(image["id"] for image in manifest["engines"].values())}
            for index, image_id in enumerate(sorted(image_ids)):
                holder = f"aiak-bench-image-{run_id}-{index}"
                image_holders.append(holder)
                subprocess.run([
                    "docker", "run", "-d", "--name", holder, "--network", "none",
                    "-e", "NVIDIA_VISIBLE_DEVICES=void", "-e", "CUDA_VISIBLE_DEVICES=",
                    "--label", f"aiak-bench.run={run_id}", "--entrypoint", "sleep", image_id, "infinity",
                ], check=True, stdout=subprocess.DEVNULL)
            for engine in engines:
                for phase in phases:
                    all_ok = run_server(config, engine, phase, run_dir, args.profile, manifest) and all_ok
        finally:
            for holder in image_holders:
                subprocess.run(["docker", "rm", "-f", holder], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            summary = build_summary(output_dir, output_dir / "summary.json")
            if config.get("publish_summary_path"):
                save_json(Path(config["publish_summary_path"]), summary)
        print(f"Run evidence: {run_dir}", flush=True)
        raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
