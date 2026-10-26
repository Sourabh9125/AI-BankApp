
import os
import time
import ssl
import smtplib
import logging
from datetime import datetime, timezone
from email.message import EmailMessage

from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
from anthropic import Anthropic
from .agent import run_agent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Kubernetes configuration
IN_CLUSTER = os.getenv("IN_CLUSTER", "true").lower() == "true"

if IN_CLUSTER:
    config.load_incluster_config()
else:
    config.load_kube_config(
        config_file=os.path.expanduser(
            os.getenv("KUBECONFIG", "~/.kube/config")
        ),
        context=os.getenv("KUBE_CONTEXT", "kind-test-k8s"),
    )

k8s = client.CoreV1Api()
apps = client.AppsV1Api()
NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "bankapp")

# Incident verification configuration
VERIFY_DELAY = int(os.getenv("INCIDENT_VERIFY_SECONDS", "300"))
RECHECK_INTERVAL = int(os.getenv("INCIDENT_RECHECK_SECONDS", "30"))

FAILURE_CONDITIONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
}

# In-memory queue. Entries are keyed by pod UID.
pending_incidents = {}
alerted_pods = set()


def pod_has_failure(pod) -> bool:
    """Detect actionable failures, not normal rollout termination."""

    if pod.status.phase == "Unknown":
        return True

    for cs in pod.status.container_statuses or []:
        state = cs.state

        if state and state.waiting:
            if state.waiting.reason in FAILURE_CONDITIONS:
                return True

        for container_state in (cs.last_state, state):
            if (
                container_state
                and container_state.terminated
                and container_state.terminated.reason == "OOMKilled"
            ):
                return True

    # Do not treat phase=Failed or exit code 143 alone as an incident.
    return False


def determine_severity(pod) -> str:
    """Basic severity classification; adjust to your workload's SLOs."""

    for cs in pod.status.container_statuses or []:
        state = cs.state

        if state and state.waiting:
            reason = state.waiting.reason
            if reason in {"CrashLoopBackOff", "OOMKilled"}:
                return "HIGH"
            if reason in {"ImagePullBackOff", "ErrImagePull"}:
                return "MEDIUM"

        for container_state in (cs.last_state, state):
            if (
                container_state
                and container_state.terminated
                and container_state.terminated.reason == "OOMKilled"
            ):
                return "HIGH"

    return "MEDIUM"


def send_incident_email(
    namespace: str,
    pod_name: str,
    severity: str,
    rca: str,
) -> bool:
    sender = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    recipient = os.getenv("ALERT_EMAIL_TO")

    if not all([sender, password, recipient]):
        logger.error("Email skipped: SMTP configuration is missing.")
        return False

    if not rca or not rca.strip():
        logger.error("Email skipped: RCA is empty.")
        return False

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = (
        f"[{severity}] Kubernetes Incident: {namespace}/{pod_name}"
    )
    message.set_content(
        f"""KUBERNETES INCIDENT REPORT

Severity: {severity}
Namespace: {namespace}
Pod: {pod_name}
Generated: {datetime.now(timezone.utc).isoformat()}

RCA AND VALIDATION
------------------
{rca}

Recommended next step:
Verify the supporting Kubernetes evidence before performing remediation.
This report is AI-assisted and is not a guarantee of root-cause accuracy.
"""
    )

    try:
        with smtplib.SMTP(
            os.getenv("SMTP_HOST", "smtp.gmail.com"),
            int(os.getenv("SMTP_PORT", "587")),
            timeout=20,
        ) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(sender, password)
            server.send_message(message)

        logger.info("Incident email sent for %s/%s", namespace, pod_name)
        return True

    except (smtplib.SMTPException, OSError, ValueError):
        logger.exception("Incident email delivery failed.")
        return False


def verify_rca(first_rca: str, second_rca: str):
    """
    Cross-check two independent RCA runs.
    This is a consistency check, not a guarantee of correctness.
    """

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL")

    if not api_key or not model:
        logger.error(
            "RCA validation skipped: set ANTHROPIC_API_KEY and "
            "ANTHROPIC_MODEL explicitly."
        )
        return None

    try:
        claude = Anthropic(api_key=api_key)
        response = claude.messages.create(
            model=model,
            max_tokens=1800,
            messages=[
                {
                    "role": "user",
                    "content": f"""
You are a cautious Kubernetes incident RCA reviewer.

Compare the two independent analyses below. Do not invent evidence.
A pod disappearing or exiting with code 143 during a rollout is not
by itself proof of an application failure.

Rules:
- Distinguish confirmed facts from hypotheses.
- Identify disagreements and missing evidence.
- Never claim a root cause is confirmed unless the supplied analyses
  contain direct supporting evidence.
- If evidence is insufficient, say "INCONCLUSIVE".
- Provide a concise, evidence-based final assessment.
- Include these headings: Assessment, Confirmed Evidence,
  Likely Cause, Confidence, Recommended Actions.
- Confidence must be HIGH, MEDIUM, or LOW, based on evidence quality,
  not model certainty.
- HIGH confidence requires direct, consistent evidence.

FIRST RCA:
{first_rca}

SECOND RCA:
{second_rca}
""",
                }
            ],
        )

        text = "\n".join(
            block.text
            for block in response.content
            if block.type == "text"
        ).strip()

        if not text:
            return None

        required_headings = (
            "Assessment",
            "Confirmed Evidence",
            "Confidence",
        )
        if not all(h.lower() in text.lower() for h in required_headings):
            logger.error("Validation response is missing required sections.")
            return None

        return text

    except Exception:
        logger.exception("RCA validation request failed.")
        return None


def get_live_pod(namespace: str, pod_name: str, expected_uid: str):
    """Fetch the current pod; reject deleted or replaced pod objects."""

    try:
        pod = k8s.read_namespaced_pod(
            name=pod_name,
            namespace=namespace,
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.info("Pod %s/%s was deleted; cancelling stale incident.",
                        namespace, pod_name)
            return None
        raise

    if expected_uid and pod.metadata.uid != expected_uid:
        logger.info("Pod %s/%s was replaced; cancelling stale incident.",
                    namespace, pod_name)
        return None

    return pod


def process_pending_incidents():
    """Recheck queued incidents without blocking the watch stream."""

    now = time.monotonic()

    for key, incident in list(pending_incidents.items()):
        if now < incident["next_check"]:
            continue

        incident["next_check"] = now + RECHECK_INTERVAL
        namespace = incident["namespace"]
        pod_name = incident["pod_name"]

        try:
            pod = get_live_pod(
                namespace,
                pod_name,
                incident["uid"],
            )

            if pod is None:
                pending_incidents.pop(key, None)
                continue

            if not pod_has_failure(pod):
                logger.info(
                    "Incident cleared for %s/%s; no email will be sent.",
                    namespace,
                    pod_name,
                )
                pending_incidents.pop(key, None)
                continue

            elapsed = now - incident["first_seen"]
            if elapsed < VERIFY_DELAY:
                logger.info(
                    "Incident still present for %s/%s: %.0f/%.0f seconds.",
                    namespace,
                    pod_name,
                    elapsed,
                    VERIFY_DELAY,
                )
                continue

            # Confirm the workload still has a problem before RCA.
            # A missing Deployment is not assumed to be a failure.
            owner_refs = pod.metadata.owner_references or []
            if owner_refs:
                owner = owner_refs[0]
                if owner.kind == "ReplicaSet":
                    rs = apps.read_namespaced_replica_set(
                        owner.name,
                        namespace,
                    )
                    rs_owners = rs.metadata.owner_references or []
                    deployment_owner = next(
                        (ref for ref in rs_owners if ref.kind == "Deployment"),
                        None,
                    )

                    if deployment_owner:
                        deployment = apps.read_namespaced_deployment(
                            deployment_owner.name,
                            namespace,
                        )
                        status = deployment.status
                        desired = deployment.spec.replicas or 0
                        ready = status.ready_replicas or 0

                        # A rollout may have replaced this pod while the
                        # Deployment is healthy. Do not alert on that alone.
                        if (
                            ready >= desired
                            and desired > 0
                            and not pod_has_failure(pod)
                        ):
                            pending_incidents.pop(key, None)
                            continue

            logger.warning(
                "Incident persisted for %s/%s; collecting RCA evidence.",
                namespace,
                pod_name,
            )

            # Fresh evidence collection: run the existing agent twice.
            first_rca = run_agent(namespace, pod_name)
            if not isinstance(first_rca, str) or not first_rca.strip():
                logger.error("First RCA was empty; will retry later.")
                continue

            # Ensure the target still exists before the second analysis.
            latest_pod = get_live_pod(namespace, pod_name, incident["uid"])
            if latest_pod is None or not pod_has_failure(latest_pod):
                pending_incidents.pop(key, None)
                continue

            second_rca = run_agent(namespace, pod_name)
            if not isinstance(second_rca, str) or not second_rca.strip():
                logger.error("Second RCA was empty; will retry later.")
                continue

            validated_rca = verify_rca(first_rca, second_rca)
            if not validated_rca:
                logger.error(
                    "RCA validation failed for %s/%s; email withheld.",
                    namespace,
                    pod_name,
                )
                continue

            # Last check before notifying: the same pod must still exist
            # and still have an actionable failure.
            final_pod = get_live_pod(namespace, pod_name, incident["uid"])
            if final_pod is None or not pod_has_failure(final_pod):
                pending_incidents.pop(key, None)
                continue

            severity = determine_severity(final_pod)
            email_sent = send_incident_email(
                namespace,
                pod_name,
                severity,
                validated_rca,
            )

            if email_sent:
                alerted_pods.add(key)
                pending_incidents.pop(key, None)

        except ApiException as exc:
            logger.warning(
                "Kubernetes API error while verifying %s/%s: %s",
                namespace,
                pod_name,
                exc,
            )
        except Exception:
            logger.exception(
                "Incident verification failed for %s/%s",
                namespace,
                pod_name,
            )


def main():
    print(f"Starting incident copilot for {NAMESPACE}...", flush=True)
    print(
        f"Verification delay: {VERIFY_DELAY}s; "
        f"recheck interval: {RECHECK_INTERVAL}s",
        flush=True,
    )

    while True:
        watcher = watch.Watch()

        try:
            for event in watcher.stream(
                k8s.list_namespaced_pod,
                namespace=NAMESPACE,
                timeout_seconds=30,
            ):
                event_type = event["type"]
                pod = event["object"]

                if event_type not in ("ADDED", "MODIFIED"):
                    process_pending_incidents()
                    continue

                if not pod.metadata or not pod.metadata.name:
                    process_pending_incidents()
                    continue

                namespace = pod.metadata.namespace or NAMESPACE
                pod_name = pod.metadata.name
                uid = pod.metadata.uid

                # Ignore the copilot itself.
                if pod_name.startswith("copilot-"):
                    process_pending_incidents()
                    continue

                key = f"{namespace}/{uid or pod_name}"

                if key in alerted_pods:
                    process_pending_incidents()
                    continue

                if pod_has_failure(pod) and key not in pending_incidents:
                    now = time.monotonic()
                    pending_incidents[key] = {
                        "namespace": namespace,
                        "pod_name": pod_name,
                        "uid": uid,
                        "first_seen": now,
                        "next_check": now + RECHECK_INTERVAL,
                    }
                    logger.warning(
                        "Queued potential incident %s/%s for verification.",
                        namespace,
                        pod_name,
                    )

                process_pending_incidents()

            # Also process the queue after a normal stream timeout.
            process_pending_incidents()

        except KeyboardInterrupt:
            print("Watchdog stopped.", flush=True)
            break
        except Exception:
            logger.exception("Kubernetes watch stream error.")
            time.sleep(5)
        finally:
            watcher.stop()

        time.sleep(1)


if __name__ == "__main__":
    main()








































# import os
# import time
# import ssl
# import smtplib
# import logging
# from email.message import EmailMessage

# from kubernetes import client, config, watch
# from .agent import run_agent


# # Logging
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)


# # Configure Kubernetes client
# IN_CLUSTER = os.getenv("IN_CLUSTER", "true").lower() == "true"

# if IN_CLUSTER:
#     config.load_incluster_config()
# else:
#     config.load_kube_config(
#         config_file=os.getenv("KUBECONFIG", "~/.kube/config"),
#         context=os.getenv("KUBE_CONTEXT", "kind-test-k8s"),
#     )

# k8s = client.CoreV1Api()
# NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "bankapp")

# FAILURE_PHASES = {"Failed", "Unknown"}
# FAILURE_CONDITIONS = {
#     "CrashLoopBackOff",
#     "ImagePullBackOff",
#     "ErrImagePull",
# }

# # Prevent duplicate alerts during this process lifetime.
# alerted_pods = set()


# def pod_has_failure(pod) -> bool:
#     """Return True if a pod has a recognized failure condition."""

#     if pod.status.phase in FAILURE_PHASES:
#         return True

#     for container_status in pod.status.container_statuses or []:
#         state = container_status.state

#         if state and state.waiting:
#             if state.waiting.reason in FAILURE_CONDITIONS:
#                 return True

#         last_state = container_status.last_state
#         if (
#             last_state
#             and last_state.terminated
#             and last_state.terminated.reason == "OOMKilled"
#         ):
#             return True

#         # Also detect a currently terminated container.
#         if (
#             state
#             and state.terminated
#             and state.terminated.reason == "OOMKilled"
#         ):
#             return True

#     return False


# def determine_severity(pod) -> str:
#     """Basic severity classification based on pod status."""

#     for container_status in pod.status.container_statuses or []:
#         state = container_status.state

#         if state and state.terminated:
#             if state.terminated.reason == "OOMKilled":
#                 return "HIGH"

#         if state and state.waiting:
#             reason = state.waiting.reason

#             if reason in {"CrashLoopBackOff", "OOMKilled"}:
#                 return "HIGH"

#             if reason in {"ImagePullBackOff", "ErrImagePull"}:
#                 return "MEDIUM"

#         last_state = container_status.last_state
#         if (
#             last_state
#             and last_state.terminated
#             and last_state.terminated.reason == "OOMKilled"
#         ):
#             return "HIGH"

#     if pod.status.phase in {"Failed", "Unknown"}:
#         return "HIGH"

#     return "MEDIUM"


# def send_incident_email(
#     namespace: str,
#     pod_name: str,
#     severity: str,
#     rca: str,
# ) -> bool:
#     """Send an incident RCA email through Gmail SMTP."""

#     sender = os.getenv("SMTP_USERNAME")
#     password = os.getenv("SMTP_PASSWORD")
#     recipient = os.getenv("ALERT_EMAIL_TO")

#     if not all([sender, password, recipient]):
#         logger.error(
#             "Email alert skipped: SMTP_USERNAME, SMTP_PASSWORD, "
#             "or ALERT_EMAIL_TO is missing."
#         )
#         return False

#     if not rca or not rca.strip():
#         logger.error("Email alert skipped because the RCA is empty.")
#         return False

#     message = EmailMessage()
#     message["From"] = sender
#     message["To"] = recipient
#     message["Subject"] = (
#         f"[{severity}] Kubernetes Incident: {namespace}/{pod_name}"
#     )

#     message.set_content(
#         f"""KUBERNETES INCIDENT ALERT
# =========================

# Severity: {severity}
# Namespace: {namespace}
# Affected Pod: {pod_name}

# ROOT CAUSE ANALYSIS
# -------------------
# {rca}

# RECOMMENDED ACTIONS
# -------------------
# 1. Review the RCA and verify the suspected root cause.
# 2. Check pod events and container logs.
# 3. Apply the recommended remediation only after validation.
# 4. Confirm the pod and application return to a healthy state.

# Generated by: AI Kubernetes Incident Copilot
# """
#     )

#     try:
#         smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
#         smtp_port = int(os.getenv("SMTP_PORT", "587"))

#         with smtplib.SMTP(
#             smtp_host,
#             smtp_port,
#             timeout=20,
#         ) as server:
#             server.ehlo()
#             server.starttls(context=ssl.create_default_context())
#             server.ehlo()
#             server.login(sender, password)
#             server.send_message(message)

#         logger.info(
#             "Incident email sent for %s/%s",
#             namespace,
#             pod_name,
#         )
#         return True

#     except (smtplib.SMTPException, OSError, ValueError):
#         logger.exception("Failed to send incident email.")
#         return False


# def is_valid_rca(rca) -> bool:
#     """Reject empty responses and common RCA failure messages."""

#     if not isinstance(rca, str) or not rca.strip():
#         return False

#     if rca.strip().lower().startswith("rca failed:"):
#         return False

#     return True


# def main():
#     print(
#         f"Starting Kubernetes incident copilot for {NAMESPACE}...",
#         flush=True,
#     )

#     while True:
#         watcher = watch.Watch()

#         try:
#             for event in watcher.stream(
#                 k8s.list_namespaced_pod,
#                 namespace=NAMESPACE,
#                 timeout_seconds=60,
#             ):
#                 event_type = event["type"]
#                 pod = event["object"]

#                 if event_type not in ("MODIFIED", "ADDED"):
#                     continue

#                 if not pod.metadata or not pod.metadata.name:
#                     continue

#                 namespace = pod.metadata.namespace or NAMESPACE
#                 pod_name = pod.metadata.name

#                 # Do not diagnose the copilot's own pods.
#                 if pod_name.startswith("copilot-"):
#                     continue

#                 if not pod_has_failure(pod):
#                     continue

#                 # Use the UID so a replacement pod can alert independently.
#                 pod_uid = pod.metadata.uid or pod_name
#                 alert_key = f"{namespace}/{pod_uid}"

#                 if alert_key in alerted_pods:
#                     continue

#                 # Mark before processing to avoid duplicate concurrent events.
#                 alerted_pods.add(alert_key)

#                 severity = determine_severity(pod)

#                 print(
#                     f"Detected failure on {namespace}/{pod_name}. "
#                     f"Severity: {severity}. Triggering RCA...",
#                     flush=True,
#                 )

#                 try:
#                     rca = run_agent(namespace, pod_name)

#                     print("--- RCA ---", flush=True)
#                     print(rca, flush=True)

#                     if not is_valid_rca(rca):
#                         print(
#                             "Skipping email because RCA generation "
#                             "failed or returned no usable text.",
#                             flush=True,
#                         )
#                         # Allow a later event to retry RCA generation.
#                         alerted_pods.discard(alert_key)
#                         continue

#                     email_sent = send_incident_email(
#                         namespace=namespace,
#                         pod_name=pod_name,
#                         severity=severity,
#                         rca=rca,
#                     )

#                     if not email_sent:
#                         print(
#                             "RCA generated, but email delivery failed "
#                             "or was skipped. Check the logs and SMTP config.",
#                             flush=True,
#                         )

#                 except Exception:
#                     logger.exception(
#                         "Incident processing failed for %s/%s",
#                         namespace,
#                         pod_name,
#                     )
#                     alerted_pods.discard(alert_key)

#         except KeyboardInterrupt:
#             print("Watchdog stopped by user.", flush=True)
#             break

#         except Exception:
#             logger.exception("Kubernetes watch stream error.")
#             time.sleep(5)

#         finally:
#             watcher.stop()

#         # Reconnect if the stream ends normally after its timeout.
#         time.sleep(1)


# if __name__ == "__main__":
#     main()
