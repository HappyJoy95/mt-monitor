import json
from datetime import datetime


def save_snapshot(project_root, payload, summary):
    raw_dir = project_root / "raw"
    data_dir = project_root / "data"
    raw_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S%z")
    raw_path = raw_dir / f"{timestamp}-order-list.json"
    summary_path = data_dir / "latest-orders.json"

    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return raw_path, summary_path
