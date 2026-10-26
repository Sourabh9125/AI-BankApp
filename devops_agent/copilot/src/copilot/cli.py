import os
import typer
from .agent import run_agent

app = typer.Typer()

@app.command()
def run(namespace: str, pod: str):
    """Manually trigger RCA for a pod."""
    result = run_agent(namespace, pod)
    typer.echo(result)

if __name__ == "__main__":
    app()
