from fastapi import FastAPI

app = FastAPI(title="ModelBudget")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}