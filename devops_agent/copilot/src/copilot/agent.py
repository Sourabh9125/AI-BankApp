
import json
import os
from typing import Any

from anthropic import Anthropic
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException


# --------------------------------------------------
# Kubernetes client configuration
# --------------------------------------------------

IN_CLUSTER = os.getenv("IN_CLUSTER", "false").lower() == "true"

if IN_CLUSTER:
    config.load_incluster_config()
else:
    config.load_kube_config(
        config_file=os.getenv("KUBECONFIG", "~/.kube/config"),
        context=os.getenv("KUBE_CONTEXT", "kind-test-k8s"),
    )

k8s = client.CoreV1Api()
apps = client.AppsV1Api()


# --------------------------------------------------
# Anthropic client configuration
# --------------------------------------------------

api_key = os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    raise RuntimeError(
        "ANTHROPIC_API_KEY environment variable is not set."
    )

anthropic = Anthropic(api_key=api_key)

# Override this with ANTHROPIC_MODEL if your account uses another model.
MODEL = os.getenv(
    "ANTHROPIC_MODEL",
    "claude-sonnet-5-5",
)


# --------------------------------------------------
# Kubernetes diagnostic helper functions
# --------------------------------------------------

def get_pod(namespace: str, pod_name: str) -> dict[str, Any]:
    """Retrieve pod configuration and status."""
    try:
        pod = k8s.read_namespaced_pod(
            name=pod_name,
            namespace=namespace,
        )
        return pod.to_dict()
    except Exception as exc:
        return {"error": str(exc)}


def get_pod_logs(
    namespace: str,
    pod_name: str,
    container: str | None = None,
    previous: bool = False,
) -> str:
    """Retrieve current or previous container logs."""
    try:
        return k8s.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            previous=previous,
            tail_lines=200,
        )
    except Exception as exc:
        return f"Error retrieving logs: {exc}"


def get_events(
    namespace: str,
    pod_name: str,
) -> list[dict[str, Any]]:
    """Retrieve events associated with the specified pod."""
    try:
        events = k8s.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )

        return [event.to_dict() for event in events.items]
    except Exception as exc:
        return [{"error": str(exc)}]


def get_deployments(namespace: str) -> list[dict[str, Any]]:
    """Retrieve deployments in the namespace."""
    try:
        response = apps.list_namespaced_deployment(
            namespace=namespace
        )
        return [deployment.to_dict() for deployment in response.items]
    except Exception as exc:
        return [{"error": str(exc)}]


def get_services(namespace: str) -> list[dict[str, Any]]:
    """Retrieve services in the namespace."""
    try:
        response = k8s.list_namespaced_service(
            namespace=namespace
        )
        return [service.to_dict() for service in response.items]
    except Exception as exc:
        return [{"error": str(exc)}]


def get_endpoints(namespace: str) -> list[dict[str, Any]]:
    """Retrieve endpoints in the namespace."""
    try:
        response = k8s.list_namespaced_endpoints(
            namespace=namespace
        )
        return [endpoint.to_dict() for endpoint in response.items]
    except Exception as exc:
        return [{"error": str(exc)}]


def get_pvcs(namespace: str) -> list[dict[str, Any]]:
    """Retrieve persistent volume claims in the namespace."""
    try:
        response = k8s.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )
        return [pvc.to_dict() for pvc in response.items]
    except Exception as exc:
        return [{"error": str(exc)}]


# --------------------------------------------------
# Build diagnostic context
# --------------------------------------------------

def build_prompt(namespace: str, pod_name: str) -> str:
    """Collect diagnostic evidence and serialize it as JSON."""

    pod = get_pod(namespace, pod_name)

    # If possible, include logs from the previous container instance.
    previous_logs = get_pod_logs(
        namespace,
        pod_name,
        previous=True,
    )

    current_logs = get_pod_logs(
        namespace,
        pod_name,
    )

    events = get_events(namespace, pod_name)
    deployments = get_deployments(namespace)
    services = get_services(namespace)
    endpoints = get_endpoints(namespace)
    pvcs = get_pvcs(namespace)

    context = {
        "namespace": namespace,
        "pod_name": pod_name,
        "pod": pod,
        "current_logs": current_logs,
        "previous_logs": previous_logs,
        "events": events,
        "deployments": deployments,
        "services": services,
        "endpoints": endpoints,
        "persistent_volume_claims": pvcs,
    }

    return json.dumps(
        context,
        indent=2,
        default=str,
    )


# --------------------------------------------------
# Anthropic root-cause analysis
# --------------------------------------------------

def run_agent(namespace: str, pod_name: str) -> str:
    """Collect Kubernetes diagnostics and request RCA from Claude."""

    diagnostic_context = build_prompt(namespace, pod_name)

    user_message = f"""
You are an experienced Kubernetes Site Reliability Engineer
and incident-response copilot.

Analyze the diagnostic context below to investigate the pod failure.

Requirements:
1. Identify the most likely root cause.
2. Cite the specific logs, events, or pod status that support your finding.
3. Distinguish confirmed evidence from hypotheses.
4. Identify contributing factors.
5. Provide ordered diagnostic and remediation steps.
6. Include relevant read-only kubectl commands where useful.
7. Do not invent missing evidence.
8. Do not claim to have executed commands.
9. Do not perform cluster changes or recommend destructive actions
   without explaining the risks and obtaining human approval.

Respond using these sections:

## Incident Summary
## Root Cause
## Supporting Evidence
## Contributing Factors
## Recommended Actions
## Verification Steps
## Confidence Level

Kubernetes diagnostic context:

{diagnostic_context}
"""

    try:
        response = anthropic.messages.create(
            model=MODEL,
            max_tokens=1800,
            messages=[
                {
                    "role": "user",
                    "content": user_message,
                }
            ],
        )

        text_blocks = [
            block.text
            for block in response.content
            if block.type == "text"
        ]

        if not text_blocks:
            return "RCA failed: Claude returned no text content."

        return "\n".join(text_blocks)

    except Exception as exc:
        return f"RCA request failed: {type(exc).__name__}: {exc}"


# --------------------------------------------------
# Optional command-line entry point
# --------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python -m copilot.agent <namespace> <pod>")
        raise SystemExit(1)

    namespace = sys.argv[1]
    pod_name = sys.argv[2]

    print(run_agent(namespace, pod_name))























# import os
# import json
# from typing import Dict, List

# from kubernetes import client, config
# from kubernetes.config.config_exception import ConfigException
# from anthropic import Anthropic

# # Initialize Kubernetes client
# IN_CLUSTER = os.getenv("IN_CLUSTER", "false").lower() == "true"

# if IN_CLUSTER:
#     config.load_incluster_config()
# else:
#     config.load_kube_config(
#         config_file="/root/.kube/config",
#         context="kind-test-k8s",
#     )

# k8s = client.CoreV1Api()
# apps = client.AppsV1Api()

# # Initialize Anthropic client
# api_key = os.getenv("ANTHROPIC_API_KEY")
# if not api_key:
#     raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

# anthropic = Anthropic(api_key=api_key)
# # Helper functions to fetch data

# def get_pod(namespace: str, pod_name: str) -> Dict:
#     try:
#         pod = k8s.read_namespaced_pod(name=pod_name, namespace=namespace)
#         return pod.to_dict()
#     except Exception as e:
#         return {"error": str(e)}


# def get_pod_logs(namespace: str, pod_name: str, container: str | None = None, previous: bool = False) -> str:
#     try:
#         return k8s.read_namespaced_pod_log(name=pod_name, namespace=namespace, container=container, previous=previous)
#     except Exception as e:
#         return f"Error retrieving logs: {e}"


# def get_events(namespace: str, in_field_selector: str) -> List[Dict]:
#     try:
#         events = k8s.list_namespaced_event(namespace=namespace, field_selector=in_field_selector)
#         return [e.to_dict() for e in events.items]
#     except Exception:
#         return []


# def get_deployments(namespace: str) -> List[Dict]:
#     try:
#         r = apps.list_namespaced_deployment(namespace=namespace)
#         return [d.to_dict() for d in r.items]
#     except Exception:
#         return []


# def get_services(namespace: str) -> List[Dict]:
#     try:
#         r = k8s.list_namespaced_service(namespace=namespace)
#         return [s.to_dict() for s in r.items]
#     except Exception:
#         return []


# def get_endpoints(namespace: str) -> List[Dict]:
#     try:
#         r = k8s.list_namespaced_endpoints(namespace=namespace)
#         return [e.to_dict() for e in r.items]
#     except Exception:
#         return []


# def get_pvcs(namespace: str) -> List[Dict]:
#     try:
#         r = k8s.list_namespaced_persistent_volume_claim(namespace=namespace)
#         return [p.to_dict() for p in r.items]
#     except Exception:
#         return []


# def build_prompt(namespace: str, pod_name: str) -> str:
#     pod = get_pod(namespace, pod_name)
#     logs = get_pod_logs(namespace, pod_name)
#     events = get_events(namespace, f"involvedObject.Name={pod_name}")
#     deployments = get_deployments(namespace)
#     services = get_services(namespace)
#     endpoints = get_endpoints(namespace)
#     pvc = get_pvcs(namespace)

#     ctx = {
#         "pod": pod,
#         "logs": logs,
#         "events": events,
#         "deployments": deployments,
#         "services": services,
#         "endpoints": endpoints,
#         "pvc": pvc,
#     }
#     return json.dumps(ctx,
#                       indent=2,
#                       default=str)


# def run_agent(namespace: str, pod_name: str) -> str:
#     """Collect data and ask Anthropic for RCA."""
#     prompt = build_prompt(namespace, pod_name)
#     user_message = (
#         "You are an incident copilot for Kubernetes. Use the provided JSON context to identify the root cause of a pod failure, list contributing factors, and suggest mitigations.\n\n"
#         f"Context:\n{prompt}\n\n---\n"
#         "Answer with a short summary, root cause, and a list of action items."
#     )
#     response = anthropic.chat.completions.create(
#         model="gpt-oss:20b-cloud",
#         messages=[{"role": "user", "content": user_message}],
#     )
#     # The Anthropic client returns choices list
#     content = response.choices[0].message.content
#     return content


# if __name__ == "__main__":
#     import sys
#     if len(sys.argv) != 3:
#         print("Usage: python -m copilot.agent <namespace> <pod>")
#         sys.exit(1)
#     ns, pod = sys.argv[1], sys.argv[2]
#     print(run_agent(ns, pod))
