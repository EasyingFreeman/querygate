import argparse
import os
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from .app import create_app
from .config import ConfigurationError, load_settings
from .gateway import Gateway


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the QueryGate service or its local Web administration page.")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--datasource")
    args = parser.parse_args()
    config = Path(os.environ.get("QUERYGATE_CONFIG", "querygate.yaml"))
    try:
        web_root = Path(os.environ.get("QUERYGATE_WEB_ROOT") or "web/dist")
        if args.configure or (web_root / "index.html").is_file():
            from .admin import serve_local

            serve_local(
                config, args.env_file or Path(".env"), web_root, args.port,
                open_browser=args.configure, datasource=args.datasource,
            )
            return 0

        if args.env_file:
            load_dotenv(args.env_file, encoding="utf-8-sig", interpolate=False)
        app = create_app(Gateway.from_settings(load_settings(config), args.datasource))
    except yaml.YAMLError:
        parser.exit(1, f"QueryGate configuration error: invalid YAML in {config}.\n")
    except (ConfigurationError, ValidationError, OSError) as error:
        parser.exit(1, f"QueryGate configuration error:\n{error}\n\nOpen settings with Start-QueryGate.cmd --configure.\n")

    print(f"QueryGate API: http://127.0.0.1:{args.port}", flush=True)
    if os.environ.get("QUERYGATE_WEB_ROOT"):
        print(f"QueryGate workbench: http://127.0.0.1:{args.port}/workbench/", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
