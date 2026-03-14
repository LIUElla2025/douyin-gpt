"""启动脚本 — streamlit run app.py"""
import subprocess
import sys
from pathlib import Path


def main():
    project_dir = Path(__file__).resolve().parent

    subprocess.run(
        [
            sys.executable, "-m", "streamlit", "run",
            str(project_dir / "app.py"),
            "--server.headless", "true",
        ],
        cwd=str(project_dir),
        check=True,
    )


if __name__ == "__main__":
    main()
