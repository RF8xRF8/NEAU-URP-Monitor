from pathlib import Path
from tempfile import TemporaryDirectory

from monitor import save_data


def test_gpa_save_ignores_fetch_time_only_change():
    old_gpa = {
        "status": 200,
        "msg": "OK",
        "data": [["智育学分绩", 3.0712, "14/66", "2026-05-04 03:00:25", "33/266"]],
        "fetch_time": "2026-05-05T00:08:04.365906",
    }
    new_gpa = {
        "status": 200,
        "msg": "OK",
        "data": [["智育学分绩", 3.0712, "14/66", "2026-05-05 03:00:25", "33/266"]],
        "fetch_time": "2026-05-05T08:00:00.000000",
    }

    with TemporaryDirectory() as tmp_dir:
        data_dir = Path(tmp_dir)

        save_data(str(data_dir), "gpa", old_gpa, old_gpa["fetch_time"])
        save_data(str(data_dir), "gpa", new_gpa, new_gpa["fetch_time"])

        archive_dir = data_dir / "archive" / "gpa"
        assert not archive_dir.exists() or not any(archive_dir.iterdir())

        saved = (data_dir / "gpa.json").read_text(encoding="utf-8")
        assert "2026-05-05T08:00:00.000000" in saved
