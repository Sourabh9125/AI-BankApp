# AI‑Powered Kubernetes Incident Copilot & RCA Agent

This project implements a lightweight Python service that monitors Kubernetes pod failures, collects relevant context (logs, events, deployments, services, endpoints, PVCs) and asks **Anthropic’s** `gpt-oss:20b-cloud` model to generate a root‑cause analysis (RCA) and action items.

## Features
- **Event‑driven watchdog** – automatically triggers on pod failure states.
- **CLI tool** – `copilot <namespace> <pod>` to manually request an RCA.
- **LangGraph orchestration** – neatly separates data collection from LLM inference.
- **Docker‑Compose** – a `copilot` service you can run locally for debugging.
- **Kubernetes manifests** – ServiceAccount + RBAC for read‑only access; a ready‑to‑apply Deployment if you want to run it inside your cluster.

## Prerequisites
- Docker & Docker‑Compose (v3.9+)
- A Kubernetes cluster with `kubectl` configured.
- **Anthropic API key** for the `gpt-oss:20b-cloud` model. Sign up at https://console.anthropic.com and create a key.

```bash
export ANTHROPIC_API_KEY=sk-........
```

## 1. Build the image
```bash
# From the root of this repository
# Build only the copilot image

docker compose build copilot
```

## 2. Run locally (Docker‑Compose)
The `copilot` service starts a watchdog that tails pods.
```bash
# Start the stack – copilot runs automatically, monitoring pod failures

docker compose up copilot
```

* In the logs you’ll see lines like:* `Detected failure on default/bankapp-ai-xyz. Triggering RCA…` followed by an RCA.
* To trigger a failure, delete a running pod in the cluster:
  ```bash
  kubectl delete pod -n default <pod-name>
  ```

## 3. Manual RCA via CLI
Ask for an RCA of a single pod without waiting for an event:
```bash

docker run --rm -it \
  -e ANTHROPIC_API_KEY \
  -v ~/.kube/config:/root/.kube/config:ro \
  copilot:local \
  python -m copilot.cli run <namespace> <pod-name>
```
Example:
```bash

docker run --rm -it -e ANTHROPIC_API_KEY -v ~/.kube/config:/root/.kube/config:ro \
  copilot:local python -m copilot.cli run default bankapp-ai-1234
```
The output is a JSON‑ish string containing:
- `root_cause`
- `contributors`
- `mitigations`

You can pipe it to `jq` for pretty printing.

## 4. Deploy in the cluster
Deploy the copilot permanently inside the cluster:

```bash
# 1) Create a secret that holds the Anthropic key
kubectl create secret generic copilot-secret \
  --from-literal=anthropic_key=$ANTHROPIC_API_KEY

# 2) Apply RBAC & service-account
kubectl apply -f devops_agent/copilot/k8s/copilot-service-account.yml
kubectl apply -f devops_agent/copilot/k8s/copilot-role.yml
kubectl apply -f devops_agent/copilot/k8s/copilot-rolebinding.yml

# 3) Deploy the Copilot
kubectl apply -f devops_agent/copilot/k8s/copilot-deployment.yml
```

The deployment runs as a pod with the `copilot-sa` service‑account. Watch logs with:
```bash
kubectl logs -f deployment/copilot
```
RCA results will be printed automatically whenever a pod goes into a failure state.

## 5. Extending / Customizing
- **Slack/Webhook notifier** – Add an HTTP post after the LLM response in `agent.py`.
- **Selective namespaces** – Modify the `watchdog.py` filter to exclude namespaces.
- **Model switch** – Point `anthropic chat` to another model or provider.
- **Unit tests** – Add tests under `src/copilot/tests` that mock `kubernetes` and `anthropic`.

## 6. Troubleshooting
| Issue | Likely Cause | Fix |
|-------|--------------|-----|
| “ANTHROPIC_API_KEY not set” | Env variable missing | Run `export ANTHROPIC_API_KEY=…` or create a Kubernetes secret. |
| ImportError `anthropic` | Dependency missing | Re‑build image after updating `requirements.txt`. |
| No RCA printed | Pod never reaches failure | Delete or restart the pod so it enters a failure state. |

---
*Version 1.0 – Anthropic implementation*