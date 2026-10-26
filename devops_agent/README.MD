# How to use the AI‑Powered Kubernetes Incident Copilot

> **TL;DR**: 1. Build the image. 2. Run locally or deploy to cluster. 3. Trigger an RCA via log event or CLI.

## 1. Install prerequisites

| Item | Command |
|------|---------|
| Docker + Docker‑Compose | `brew install docker docker-compose` |
| kubectl | `brew install kubectl` |
| Anthropic API key | `export ANTHROPIC_API_KEY=sk-...` |

## 2. Build the copilot Docker image
```bash
# From the repo root
# Builds the copilot image

docker compose build copilot
```

## 3. Run locally (for debugging)
```bash
# Launch the stack – copilot will watch for pod failures

docker compose up copilot
```

*Log output will contain*:
```
Detected failure on default/bankapp-ai-xyz. Triggering RCA…
--- RCA ---
Root cause: …
```

To stimulate a failure, kill a pod:
```bash
kubectl delete pod -n default <pod-name>
```

## 4. Manual RCA via CLI
```bash
# Run the copilot image directly with the CLI entrypoint

docker run --rm -it \
  -e ANTHROPIC_API_KEY \
  -v ~/.kube/config:/root/.kube/config:ro \
  copilot:local \
  python -m copilot.cli run <namespace> <pod-name>
```
You’ll see a JSON‑ish result with `root_cause`, `contributors`, `mitigations`.

## 5. Deploy to the cluster (production)
```bash
# 1) Create the secret containing the Anthropic key
kubectl create secret generic copilot-secret \
  --from-literal=anthropic_key=$ANTHROPIC_API_KEY

# 2) Apply RBAC and the service‑account
kubectl apply -f devops_agent/copilot/k8s/copilot-service-account.yml
kubectl apply -f devops_agent/copilot/k8s/copilot-role.yml
kubectl apply -f devops_agent/copilot/k8s/copilot-rolebinding.yml

# 3) Deploy the copilot pod
kubectl apply -f devops_agent/copilot/k8s/copilot-deployment.yml
```

Watch logs:
```bash
kubectl logs -f deployment/copilot
```

## 6. Extend or customize
| Feature | How |
|---------|-----|
| Add Slack webhook | Edit `agent.py` to POST to an endpoint after LLM result |
| Restrict namespaces | Update `watchdog.py` event filter |
| Switch LLM model | Change `agent.py` to use a different Anthropic model |
| Test suite | Add mocks in `src/copilot/tests` |

## 7. Troubleshooting
| Problem | Fix |
|---------|-----|
| `ANTHROPIC_API_KEY` missing | Export variable or create secret |
| No ACA output | Ensure a pod actually fails; check watch logs |
| ImportError `anthropic` | Run `docker compose build copilot` after changing `requirements.txt` |

---
*Version 1.0 – Anthropic (gpt‑oss:20b‑cloud)*