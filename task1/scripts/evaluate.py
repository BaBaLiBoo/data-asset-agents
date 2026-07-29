from __future__ import annotations

from app.config import Settings
from app.evaluation import EvaluationRunner


def main() -> int:
    settings = Settings.from_env()
    settings.require_embedding()
    report_dir = EvaluationRunner(settings).run()
    print(f"Evaluation report: {report_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

