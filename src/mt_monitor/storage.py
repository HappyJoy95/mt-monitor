import json
from datetime import datetime


def save_raw(project_root, payload):
    """Archive the complete original API response under ``raw/``.

    This must run *before* any summarization so a later failure never loses
    the raw data.
    """
    raw_dir = project_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S%z")
    raw_path = raw_dir / f"{timestamp}-order-list.json"
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return raw_path


def save_summary(project_root, summary, kind=None):
    """Write the human-readable order summary under ``data/``.

    ``kind=None`` keeps the legacy single-file behaviour (used by ``import`` and
    tests); ``"new"`` / ``"processing"`` avoid the two live states overwriting
    each other.
    """
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if kind is None:
        summary_name = "latest-orders.json"
    else:
        summary_name = f"latest-{kind}-orders.json"
    summary_path = data_dir / summary_name
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary_path


def save_snapshot(project_root, payload, summary, kind=None):
    """Archive raw response + summary together (convenience wrapper)."""
    raw_path = save_raw(project_root, payload)
    summary_path = save_summary(project_root, summary, kind)
    return raw_path, summary_path
