"""Local /health -> real OTLP collector smoke test. Never calls OpenAI.

Run after: docker compose -p model-budget-tracing -f compose.tracing.yaml up -d
This starts a temporary gateway process and always stops only that process.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.request import urlopen


def main():
    collector_command = ["docker", "compose", "-p", "model-budget-tracing", "-f", "compose.tracing.yaml"]
    # Readiness probing avoids assuming the container starts in a fixed sleep.
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 4318), timeout=1):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise SystemExit("Collector is not listening on localhost:4318. Start compose.tracing.yaml first.")
            time.sleep(0.2)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # Unique service name lets us find OUR trace in collector output.
    service = "model-budget-smoke-" + uuid.uuid4().hex
    env = os.environ.copy()
    env.update(OTEL_ENABLED="true", OTEL_SERVICE_NAME=service, OTEL_SAMPLE_RATIO="1.0",
               OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318/v1/traces")
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Temporary gateway failed to start. Check application configuration.")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Temporary gateway /health readiness timed out")
                time.sleep(0.2)
            deadline = time.monotonic() + 20
            while True:
                logs = subprocess.run(collector_command + ["logs", "--no-color", "collector"], capture_output=True, text=True, check=True).stdout
                if service in logs and "http.request" in logs and "/health" in logs:
                    print("Collector received the /health trace from this smoke test.")
                    print("No OpenAI request was sent. Temporary gateway will now stop.")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Collector did not report this service's /health trace within 20 seconds")
                time.sleep(0.5)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
