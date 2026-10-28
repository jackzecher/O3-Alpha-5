"""Launch the complete O3 workbench with one persistent project workspace."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import uuid

BUILD_ID = "0.9.1-km12-p3-prism2"
PREFIX = "O3_WORKSPACE_JSON="
STARTUP_SECONDS = 90
STOP_GRACE_SECONDS = 15
MAX_LINE = 8192
MAX_OUTPUT = 512 * 1024
REQUIRED_STARTUP = {"validation", "identity", "dependencies", "native", "engine", "ready"}


class LaunchBlocked(RuntimeError):
    pass


def status(text):
    if sys.stdout is not None:
        print(text, flush=True)


def sha256(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise LaunchBlocked("FILE_SIZE_LIMIT")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LaunchBlocked("LINKED_OR_REPARSE_PATH_UNSUPPORTED")
    return path.resolve()


def verify_kit(kit):
    if sys.platform == "win32" and (str(kit).startswith("\\\\") or kit.drive.casefold() != os.environ.get("SystemDrive", "C:").casefold()):
        raise LaunchBlocked("LOCAL_SYSTEM_DRIVE_REQUIRED")
    path = kit / "PACKAGE_MANIFEST.json"
    if path.stat().st_size > 512 * 1024:
        raise LaunchBlocked("MANIFEST_SIZE_LIMIT")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if (manifest.get("format_version") != 1 or manifest.get("build_id") != BUILD_ID
            or not isinstance(files, dict) or not 40 <= len(files) <= 240):
        raise LaunchBlocked("MANIFEST_INVALID")
    folded = set()
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None
                or relative.is_absolute() or ".." in relative.parts or ":" in name or "\\" in name
                or name.casefold() in folded or name == "PACKAGE_MANIFEST.json"):
            raise LaunchBlocked("MANIFEST_PATH_INVALID")
        folded.add(name.casefold())
        member = plain_path(kit / name)
        if not member.is_relative_to(kit) or not member.is_file() or sha256(member) != expected:
            raise LaunchBlocked("MANIFEST_MISMATCH")
    members = list(kit.rglob("*"))
    if any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400 for p in members):
        raise LaunchBlocked("LINKED_OR_REPARSE_PATH_UNSUPPORTED")
    actual = {p.relative_to(kit).as_posix() for p in members if p.is_file()}
    if actual != set(files) | {"PACKAGE_MANIFEST.json"}:
        raise LaunchBlocked("KIT_FILE_SET_MISMATCH")
    return manifest, sha256(path)


def verify_runtime(kit=None):
    """Accept only a build-matched, approved, distribution-owned Windows runtime.

    No system PATH, implicit Conda, public package registry, automatic install,
    or technician reference fallback. This fail-closed gate executes before any
    workspace state is opened, created or migrated.
    """
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise LaunchBlocked("ISOLATED_INVOCATION_REQUIRED")
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise LaunchBlocked("WINDOWS_X64_REQUIRED")
    kit = plain_path(Path(__file__).parent if kit is None else Path(kit))
    distribution = kit.parent
    path = distribution / "DEPLOYMENT_PROFILE.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 32768:
        raise LaunchBlocked("DEPLOYMENT_PROFILE_MISSING")
    deployment = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(deployment, dict) or deployment.get("format_version") != 1
            or deployment.get("build_id") != BUILD_ID
            or deployment.get("profile") not in ("NIPR", "NON_NIPR")):
        raise LaunchBlocked("DEPLOYMENT_PROFILE_INVALID")
    if (deployment.get("runtime_approved") is not True or
            deployment.get("dependency_distribution_approved") is not True):
        raise LaunchBlocked("RUNTIME_APPROVAL_REQUIRED")
    runtime_path = distribution / "runtime"
    if not runtime_path.is_dir() or not (runtime_path / "python.exe").is_file():
        raise LaunchBlocked("RUNTIME_NOT_FOUND")
    runtime = plain_path(runtime_path)
    executable = plain_path(runtime / "python.exe")
    expected_sha = deployment.get("python_executable_sha256")
    if not isinstance(expected_sha,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_sha):
        raise LaunchBlocked("RUNTIME_FINGERPRINT_MISSING")
    # Hash all runtime files from the independently published approved manifest.
    manifest_path = distribution / "RUNTIME_MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise LaunchBlocked("RUNTIME_MANIFEST_MISSING")
    runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = runtime_manifest.get("files")
    dependencies = runtime_manifest.get("dependencies")
    version_keys = ('rdflib_version', 'pyparsing_version', 'pyside6_version', 'qt_version', 'shiboken6_version')
    version_pattern = r"[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}"
    if (runtime_manifest.get("format_version") != 1 or runtime_manifest.get("build_id") != BUILD_ID
            or runtime_manifest.get("profile") != deployment['profile']
            or runtime_manifest.get('python_bits') != 64
            or not isinstance(runtime_manifest.get('python_version'), str)
            or re.fullmatch(version_pattern, runtime_manifest['python_version']) is None
            or type(dependencies) is not dict or set(dependencies) != set(version_keys)
            or any(type(dependencies[key]) is not str or re.fullmatch(version_pattern, dependencies[key]) is None
                   for key in version_keys)
            or type(files) is not dict or not 1 <= len(files) <= 20000
            or files.get('python.exe') != expected_sha):
        raise LaunchBlocked("RUNTIME_MANIFEST_INVALID")
    if (not isinstance(deployment.get('runtime_manifest_sha256'), str)
            or deployment['runtime_manifest_sha256'] != sha256(manifest_path)):
        raise LaunchBlocked('RUNTIME_MANIFEST_FINGERPRINT_MISMATCH')
    actual = set()
    for path_in_runtime in runtime.rglob("*"):
        if path_in_runtime.is_symlink() or getattr(path_in_runtime.lstat(), "st_file_attributes", 0) & 0x400:
            raise LaunchBlocked("RUNTIME_UNSAFE_PATH")
        if path_in_runtime.is_file():
            actual.add(path_in_runtime.relative_to(runtime).as_posix())
    if actual != set(files):
        raise LaunchBlocked("RUNTIME_FILE_SET_MISMATCH")
    for name, expected_digest in files.items():
        rel = PurePosixPath(name)
        if (not isinstance(name,str) or rel.is_absolute() or ".." in rel.parts
                or "\\" in name or ":" in name or not isinstance(expected_digest,str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
            raise LaunchBlocked("RUNTIME_MANIFEST_INVALID")
        file = runtime / name
        if not file.is_file():
            raise LaunchBlocked("RUNTIME_MANIFEST_INVALID")
        with file.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
                raise LaunchBlocked("RUNTIME_MANIFEST_MISMATCH")
    entry = plain_path(Path(sys.executable))
    if not any(entry.samefile(candidate) for candidate in (executable, runtime/"pythonw.exe") if candidate.is_file()):
        raise LaunchBlocked("SELECTED_RUNTIME_MISMATCH")
    if Path(sys.prefix).resolve() != runtime.resolve():
        raise LaunchBlocked("RUNTIME_PREFIX_MISMATCH")
    if sys.version.split()[0] != runtime_manifest['python_version'] or sha256(executable) != expected_sha:
        raise LaunchBlocked("RUNTIME_FINGERPRINT_CHANGED")
    return executable, {"classification": "WINDOWS_WORKSPACE", "python_version": runtime_manifest['python_version'],
        "python_bits": 64, "python_executable_sha256": expected_sha,
        "dependencies": dict(dependencies), "runtime_manifest_sha256": deployment['runtime_manifest_sha256'],
        "supervisor_entry": entry.name.lower(), "profile": deployment["profile"],
        "organizational_runtime_approval": "RECEIPT_REQUIRES_HOST_VERIFICATION"}


def load_module(kit, name):
    spec = importlib.util.spec_from_file_location(name, kit / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_report(path, report):
    data = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(".writing")
    temporary.write_text(data, encoding="utf-8")
    os.replace(temporary, path)
    lines = ["O3 PERSISTENT WORKSPACE SESSION"]
    for key, value in report.items():
        if key != "records":
            lines.append(key + "=" + (json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)))
    lines.append("\n[APPLICATION OBSERVATIONS]")
    for record in report["records"]:
        lines.append("stage={stage} result={result} code={code}".format(**record))
        lines.extend("  " + key + "=" + str(value) for key, value in sorted(record["details"].items()))
    lines.extend(["\n[LIMITS]", "Session readiness and shutdown are separate from source/workflow acceptance.",
                  "Application readiness alone is not NIPR/non-NIPR organizational deployment acceptance."])
    path.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prune_reports(logs):
    pattern = re.compile(r"O3_session_\d{8}T\d{6}Z-[0-9a-f]{8}\.json")
    previous = sorted((p for p in logs.iterdir() if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink()), key=lambda p: p.name)
    for path in previous[:-9]:
        path.unlink()
        companion = path.with_suffix(".txt")
        if companion.is_file() and not companion.is_symlink():
            companion.unlink()


def read_pipe(stream, events, label):
    total = 0
    stderr_reported = False
    try:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            total += len(line)
            if label == "stderr":
                if not stderr_reported:
                    events.put(("stderr", True))
                    stderr_reported = True
                continue
            if len(line) > MAX_LINE or total > MAX_OUTPUT:
                events.put(("error", "OUTPUT_LIMIT"))
                break
            events.put(("stdout", line))
    except (OSError, ValueError):
        events.put(("error", "OUTPUT_READ_FAILED"))
    finally:
        events.put((label + "_done", True))


def terminate_owned_child(child, report):
    if child.poll() is not None or report["owned_child_stop_unconfirmed"]:
        return
    report["forced_termination"] = True
    try:
        child.kill()
    except OSError:
        report["owned_child_stop_unconfirmed"] = child.poll() is None
        return
    try:
        child.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        report["owned_child_stop_unconfirmed"] = child.poll() is None


def supervise(kit, workspace, report_path, report, python, runtime_contract=None):
    protocol = load_module(kit, "_O3_WORKSPACE_APP")
    command = [str(python), "-I", "-B", "-u", "-X", "utf8", str(kit / "_O3_WORKSPACE_APP.py"),
               "--kit", str(kit), "--workspace", str(workspace)]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Explicit technician-only branch; production never receives this flag or
    # test contract and retains its approved runtime-manifest authority.
    if runtime_contract is not None and runtime_contract.get("classification") == "WINDOWS_TECHNICIAN_TEST":
        command.append("--technician-test")
        environment["O3_TECHNICIAN_CONTRACT"] = json.dumps(runtime_contract, sort_keys=True)
    elif runtime_contract is not None and runtime_contract.get("classification") == "WINDOWS_NIPR_ALPHA":
        # Explicit nonproduction user-alpha contract; never a production approval.
        command.append("--nipr-alpha")
        environment["O3_NIPR_ALPHA_CONTRACT"] = json.dumps(runtime_contract, sort_keys=True)
    elif runtime_contract is not None and runtime_contract.get("classification") == "WINDOWS_NONNIPR_ALPHA":
        # Independent non-NIPR nonproduction Alpha; not a production runtime approval.
        command.append("--non-nipr-alpha")
        environment["O3_NONNIPR_ALPHA_CONTRACT"] = json.dumps(runtime_contract, sort_keys=True)
    events = queue.Queue(maxsize=512)
    readers = []
    started = time.monotonic()
    ready_at = None
    stop_at = None
    reason = None
    done = set()
    report.update(supervisor="APPLICATION_STARTING", child_exit="NOT_OBSERVED", stderr_observed=False,
        forced_termination=False, owned_child_stop_unconfirmed=False)
    child = subprocess.Popen(command, cwd=kit, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    try:
        readers = [threading.Thread(target=read_pipe, args=(stream, events, label), daemon=True)
                   for stream, label in ((child.stdout, "stdout"), (child.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        write_report(report_path, report)
        while True:
            try:
                kind, value = events.get(timeout=0.1)
            except queue.Empty:
                kind, value = "idle", None
            if kind.endswith("_done"):
                done.add(kind)
            elif kind == "stderr":
                report["stderr_observed"] = True
            elif kind == "error":
                reason = reason or value
            elif kind == "stdout":
                if not value.startswith(PREFIX):
                    reason = reason or "UNEXPECTED_APPLICATION_OUTPUT"
                else:
                    try:
                        record = json.loads(value[len(PREFIX):])
                    except (ValueError, TypeError):
                        record = None
                    # A missing contract remains fail-closed for the real protocol.
                    # Optional-argument compatibility preserves accepted legacy
                    # supervisor failure tests using their one-argument fake.
                    valid = (protocol.validate_protocol_record(record, runtime_contract)
                             if runtime_contract is not None else protocol.validate_protocol_record(record)) if record is not None else False
                    if (not valid or record["details"].get("classification") != report["classification"]
                            or len(report["records"]) >= 256):
                        reason = reason or "APPLICATION_PROTOCOL_INVALID"
                    else:
                        report["records"].append(record)
                        status("O3: " + record["code"])
                        if record["result"] == "FAIL":
                            reason = reason or record["code"]
                        if record["stage"] == "ready" and record["result"] == "PASS" and ready_at is None:
                            passed = {r["stage"] for r in report["records"] if r["result"] == "PASS"}
                            if not REQUIRED_STARTUP <= passed:
                                reason = reason or "READINESS_EVIDENCE_INCOMPLETE"
                            else:
                                ready_at = time.monotonic()
                                report["supervisor"] = "APPLICATION_READY"
                                report["startup_seconds"] = round(ready_at - started, 2)
                        write_report(report_path, report)
            now = time.monotonic()
            if child.poll() is not None and {"stdout_done", "stderr_done"} <= done and events.empty():
                break
            protected_setup = (
                any(r['code'] == 'WORKSPACE_UPGRADE_REQUIRED' for r in report['records'])
                and any(r['stage'] == 'native' and r['result'] == 'PASS' for r in report['records']))
            if protected_setup and ready_at is None:
                report['supervisor'] = 'PROTECTED_UPGRADE_UI_VISIBLE_NOT_KM_READY'
            if reason is None and ready_at is None and not protected_setup and now - started > STARTUP_SECONDS:
                reason = "APPLICATION_STARTUP_TIMEOUT"
            # A healthy ready workspace has no session timer or synthetic test limit.
            if reason and stop_at is None and child.poll() is None:
                stop_at = now
                report["supervisor"] = reason
                try:
                    child.stdin.write("STOP\n"); child.stdin.flush()
                except (OSError, ValueError):
                    pass
                write_report(report_path, report)
            if stop_at is not None and child.poll() is None and now - stop_at > STOP_GRACE_SECONDS:
                terminate_owned_child(child, report)
                if report["owned_child_stop_unconfirmed"]:
                    break
    except KeyboardInterrupt:
        reason = "LAUNCHER_INTERRUPTED"
        try:
            child.stdin.write("STOP\n"); child.stdin.flush()
            child.wait(timeout=STOP_GRACE_SECONDS)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            terminate_owned_child(child, report)
    except Exception:
        reason = "SUPERVISOR_RUNTIME_ERROR"
    finally:
        if child.poll() is None and not report["owned_child_stop_unconfirmed"]:
            terminate_owned_child(child, report)
        for reader in readers:
            if reader.is_alive():
                reader.join(timeout=1)
    report["child_exit"] = child.poll() if child.poll() is not None else "UNCONFIRMED"
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    passed = {r["stage"] for r in report["records"] if r["result"] == "PASS"}
    complete = (not reason and child.poll() == 0 and REQUIRED_STARTUP | {"shutdown", "final"} <= passed
                and not any(r["result"] == "FAIL" for r in report["records"])
                and not report["forced_termination"] and not report["owned_child_stop_unconfirmed"])
    report["supervisor"] = "WORKSPACE_SESSION_COMPLETED" if complete else (reason or "APPLICATION_SESSION_INCOMPLETE")
    report["result"] = "PASS" if complete else "FAIL_OR_INCOMPLETE"
    report["source_and_workflow_acceptance"] = "NOT_INFERRED_FROM_SESSION_LIFECYCLE"
    report["production_qualification"] = "NOT_ESTABLISHED"
    write_report(report_path, report)
    status("O3: " + report["supervisor"])
    status("Report: " + str(report_path.with_suffix(".txt")))
    return 0 if complete else 1


def notify_error(code, report_path=None):
    safe_code = code if re.fullmatch(r"[A-Z0-9_]{1,80}", str(code)) else "APPLICATION_START_FAILED"
    if safe_code == "WORKSPACE_ALREADY_OPEN":
        message = "O3 is already open for this workspace. Use its existing window."
    elif "MANIFEST" in safe_code or safe_code == "KIT_FILE_SET_MISMATCH":
        message = "The O3 application files are incomplete or changed. Extract the complete application ZIP into a fresh application folder."
    elif "RUNTIME" in safe_code or "DEPENDENC" in safe_code or "DEPLOYMENT_PROFILE" in safe_code:
        message = "O3 requires a complete, approved, build-matched Windows runtime packaged beside O3_PRODUCT. Contact the authorized deployment team; no system Python or Conda fallback is used."
    elif "WORKSPACE" in safe_code or "STATE" in safe_code or "DATABASE" in safe_code:
        message = "O3 could not open the selected workspace. Check the workspace status before retrying."
    else:
        message = "O3 could not complete application startup or normal shutdown."
    message += "\n\nCode: " + safe_code
    if report_path:
        message += "\n\nSession report:\n" + str(report_path.with_suffix(".txt"))
    status(message)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "O3", 0x10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    args = parser.parse_args(argv)
    kit = plain_path(Path(__file__).parent)
    _, manifest_hash = verify_kit(kit)
    python, runtime = verify_runtime(kit)
    state_api = load_module(kit, "_O3_WORKSPACE_STATE")
    workspace = state_api.prepare_workspace(kit, args.project)
    _, logs = state_api.validate_workspace(kit, workspace)
    prune_reports(logs)
    name = "O3_session_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".json"
    report_path = logs / name
    report = {"build_id": BUILD_ID, "purpose": "PERSISTENT_WORKSPACE_SESSION",
        "started_at": datetime.now(timezone.utc).isoformat(), "classification": runtime["classification"],
        "manifest_code": "MANIFEST_VERIFIED", "manifest_sha256": manifest_hash, "runtime": runtime,
        "organizational_runtime_approval": "RECEIPT_REQUIRES_HOST_VERIFICATION", "dependency_distribution_approval": "RECEIPT_REQUIRES_HOST_VERIFICATION",
        "operational_source_authorization": "NOT_INFERRED", "workspace_mode": "PERSISTENT_PROJECT_WORKSPACE", "records": []}
    try:
        result = supervise(kit, workspace, report_path, report, python, runtime)
    except Exception:
        report.update(supervisor="APPLICATION_LAUNCH_FAILED", result="FAIL_OR_INCOMPLETE")
        write_report(report_path, report)
        result = 2
    if result:
        notify_error(report["supervisor"], report_path)
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, (LaunchBlocked, ValueError, RuntimeError)) else "LAUNCHER_CONFIGURATION_OR_IO_ERROR"
        notify_error(code)
        raise SystemExit(2)
