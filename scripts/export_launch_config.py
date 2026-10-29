"""Export the resolved launch arguments and environment per run and engine for the dashboard."""
import argparse
import json
from pathlib import Path

from run_benchmark import engine_args


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("run_ids", nargs="+")
    options = parser.parse_args()
    config = json.loads(Path(options.config).read_text(encoding="utf-8"))
    engines = {}
    for engine in config["engines"]:
        engines[engine["id"]] = {
            "image": engine["image"],
            "gpu_count": len(config["gpu_ids"]),
            "phases": {
                phase: {
                    "args": engine_args(config, engine, phase),
                    "env": {**engine.get("env", {}), **engine.get("launch_env", {}).get(phase, {})},
                }
                for phase in config["phase_concurrencies"]
            },
        }
    output = Path(options.output)
    existing = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    existing.update({run_id: engines for run_id in options.run_ids})
    output.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
