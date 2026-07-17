"""Value-free diagnostics for the isolated CV build boundary."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import NoReturn

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jobsearch_skill.cv_core import CVServiceBase, cv_build_error, now
from jobsearch_skill.cv_models import CVBuildResult
from jobsearch_skill.cv_security import (
    copy_bytes_atomic,
    is_safe_ref,
    isolated_latex_environment,
    reject_latexmk_rc,
    safe_read_relative,
)
from jobsearch_skill.errors import (
    CVBuildError,
    SchemaValidationError,
    StorageError,
)
from jobsearch_skill.runs import RunState


_BUILD_TIMEOUT_SECONDS = 120.0
_TERMINATE_GRACE_SECONDS = 2.0


_REASON_STAGES = {
    "cv_build_failed": "compiler",
    "cv_build_requires_tex": "preflight",
    "cv_build_timeout": "compiler",
    "cv_build_tool_error": "compiler",
    "cv_build_tool_missing": "compiler",
    "cv_evidence_mismatch": "preflight",
    "cv_evidence_missing": "preflight",
    "cv_facts_mismatch": "preflight",
    "cv_facts_missing": "preflight",
    "cv_generated_output_unsafe": "verification",
    "cv_identity_mismatch": "verification",
    "cv_identity_missing": "preflight",
    "cv_manifest_invalid": "preflight",
    "cv_pdf_verification": "verification",
    "cv_prepared_tampered": "preflight",
}


@dataclass(frozen=True)
class BuildDiagnostics:
    """Only non-sensitive primitive observations may cross into a build log."""

    return_code: int = -1
    timed_out: bool = False
    stdout_present: bool = False
    stderr_present: bool = False
    page_count: int = 0


def output_present(value: object) -> bool:
    """Record presence without retaining or transforming process output."""

    return isinstance(value, (str, bytes)) and bool(value)


def diagnostic_document(
    reason_code: str,
    diagnostics: BuildDiagnostics,
    *,
    artifact_present: bool,
) -> dict[str, object]:
    """Return a closed, value-free diagnostic record for a known failure."""

    stage = _REASON_STAGES.get(reason_code)
    if stage is None:
        raise CVBuildError(
            "cv_build_diagnostic_invalid: unsupported diagnostic reason",
            reason_code="cv_build_diagnostic_invalid",
        )
    if (
        type(diagnostics.return_code) is not int
        or type(diagnostics.page_count) is not int
        or diagnostics.page_count < 0
        or type(diagnostics.timed_out) is not bool
        or type(diagnostics.stdout_present) is not bool
        or type(diagnostics.stderr_present) is not bool
        or type(artifact_present) is not bool
    ):
        raise CVBuildError(
            "cv_build_diagnostic_invalid: unsupported diagnostic value",
            reason_code="cv_build_diagnostic_invalid",
        )
    return {
        "schema_version": 1,
        "reason_code": reason_code,
        "stage": stage,
        "return_code": diagnostics.return_code,
        "timed_out": diagnostics.timed_out,
        "stdout_present": diagnostics.stdout_present,
        "stderr_present": diagnostics.stderr_present,
        "output_present": artifact_present,
        "page_count": diagnostics.page_count,
    }


class CVBuildMixin(CVServiceBase):
    """Compile isolated LaTeX and verify the resulting private PDF."""

    def build(self, run_id: str) -> CVBuildResult:
        state = self.run_store.require_open(run_id)
        manifest_path, manifest = self._prepared_manifest(state)
        root = manifest_path.parent
        try:
            self._cleanup_compiler_workspaces(root)
        except CVBuildError:
            self._raise_build_failure(
                manifest_path, manifest, "cv_generated_output_unsafe"
            )
        already_verified = bool(
            manifest.get("status") == "verified" and manifest.get("output_pdf")
        )
        if not already_verified:
            try:
                self._cleanup_output_tree(root)
            except CVBuildError:
                self._raise_build_failure(
                    manifest_path, manifest, "cv_generated_output_unsafe"
                )
        self._verify_prepared_sources(root, manifest, manifest_path)
        self._require_expected_source_inventory(root, manifest, manifest_path)
        expected_tex = manifest.get("expected_tex")
        if not isinstance(expected_tex, str) or not expected_tex:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_build_requires_tex",
                decision="review_required",
            )
        tex = root / "source" / expected_tex
        self._require_beneath(tex, root / "source")
        self._require_regular_generated(tex)
        source_hash = manifest.get("source_hash")
        if not isinstance(source_hash, str):
            self._raise_build_failure(manifest_path, manifest, "cv_manifest_invalid")
        facts_path = self.facts_path(run_id, source_hash)
        try:
            facts = self.store.read_json(facts_path, "cv-facts.v1")
        except (StorageError, SchemaValidationError, OSError):
            self._raise_build_failure(manifest_path, manifest, "cv_facts_missing")
        manifest_hashes = manifest.get("source_hashes")
        if (
            not isinstance(manifest_hashes, Mapping)
            or facts.get("run_id") != run_id
            or facts.get("cv_name") != manifest.get("source_cv_name")
            or facts.get("source_hash") != source_hash
            or facts.get("source_hashes")
            != self._hash_entries(
                {
                    str(reference): str(digest)
                    for reference, digest in manifest_hashes.items()
                }
            )
        ):
            self._raise_build_failure(manifest_path, manifest, "cv_facts_mismatch")
        evidence_path = (
            self.run_store.runs_dir / run_id / f"cv-evidence-{source_hash}.json"
        )
        try:
            evidence_document = self.store.read_json(evidence_path, "cv-evidence.v1")
        except (StorageError, SchemaValidationError, OSError):
            self._raise_build_failure(manifest_path, manifest, "cv_evidence_missing")
        normalized_hashes = {
            str(reference): str(digest)
            for reference, digest in manifest_hashes.items()
        }
        try:
            evidence = self._evidence_from_document(
                evidence_path,
                evidence_document,
                normalized_hashes,
                run_id=run_id,
                cv_name=str(manifest.get("source_cv_name")),
                source_hash=source_hash,
                expected_sources=self._prepared_evidence_sources(root, manifest),
            )
        except CVBuildError:
            self._raise_build_failure(manifest_path, manifest, "cv_evidence_mismatch")
        if any(
            not anchor or anchor not in evidence.extracted_text
            for anchor in self._evidence_anchors(facts)
        ):
            self._raise_build_failure(manifest_path, manifest, "cv_facts_mismatch")
        identity = facts.get("identity")
        full_name = identity.get("full_name") if isinstance(identity, Mapping) else None
        expected_identity = (
            full_name.get("value") if isinstance(full_name, Mapping) else None
        )
        if not isinstance(expected_identity, str) or not expected_identity:
            self._raise_build_failure(manifest_path, manifest, "cv_identity_missing")
        output = self._expected_output_path(root, expected_tex)
        self._require_beneath(output, self.generated_root)
        if already_verified:
            if manifest.get("output_pdf") != output.relative_to(root).as_posix():
                self._raise_build_failure(
                    manifest_path, manifest, "cv_manifest_invalid"
                )
            return self._verified_result(
                state, manifest_path, manifest, output, expected_identity
            )
        reject_latexmk_rc(tex.parent)
        try:
            build_environment = isolated_latex_environment(root)
        except CVBuildError:
            self._raise_build_failure(manifest_path, manifest, "cv_build_tool_missing")

        command = [
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex.name,
        ]
        previous_umask = os.umask(0o077)
        compiled_pdf_bytes: bytes | None = None
        compiler_diagnostics = BuildDiagnostics()
        try:
            try:
                try:
                    workspace_manager = tempfile.TemporaryDirectory(
                        dir=root, prefix=".compiler-work-"
                    )
                except OSError:
                    self._raise_build_failure(
                        manifest_path, manifest, "cv_generated_output_unsafe"
                    )
                with workspace_manager as workspace_name:
                    workspace = Path(workspace_name)
                    try:
                        compile_tex = self._stage_compiler_workspace(
                            root, workspace, manifest, expected_tex
                        )
                    except CVBuildError:
                        self._raise_build_failure(
                            manifest_path, manifest, "cv_generated_output_unsafe"
                        )
                    compiler_output = compile_tex.with_suffix(".pdf")
                    try:
                        completed = self._run_compiler(
                            command,
                            cwd=compile_tex.parent,
                            env=build_environment,
                        )
                    except FileNotFoundError:
                        self._raise_build_failure(
                            manifest_path, manifest, "cv_build_tool_missing"
                        )
                    except subprocess.TimeoutExpired as error:
                        try:
                            self._secure_generated_tree(root)
                        except CVBuildError:
                            self._raise_build_failure(
                                manifest_path,
                                manifest,
                                "cv_generated_output_unsafe",
                            )
                        self._raise_build_failure(
                            manifest_path,
                            manifest,
                            "cv_build_timeout",
                            diagnostics=BuildDiagnostics(
                                timed_out=True,
                                stdout_present=output_present(error.stdout),
                                stderr_present=output_present(error.stderr),
                            ),
                        )
                    except OSError:
                        self._raise_build_failure(
                            manifest_path, manifest, "cv_build_tool_error"
                        )
                    try:
                        self._secure_generated_tree(root)
                    except CVBuildError:
                        self._raise_build_failure(
                            manifest_path, manifest, "cv_generated_output_unsafe"
                        )
                    compiler_diagnostics = BuildDiagnostics(
                        return_code=completed.returncode,
                        stdout_present=output_present(completed.stdout),
                        stderr_present=output_present(completed.stderr),
                    )
                    if completed.returncode != 0:
                        self._raise_build_failure(
                            manifest_path,
                            manifest,
                            "cv_build_failed",
                            diagnostics=compiler_diagnostics,
                        )
                    try:
                        compiled_pdf_bytes = safe_read_relative(
                            compiler_output.parent, Path(compiler_output.name)
                        )
                    except CVBuildError:
                        self._raise_build_failure(
                            manifest_path,
                            manifest,
                            "cv_pdf_verification",
                            diagnostics=compiler_diagnostics,
                        )
                try:
                    self._cleanup_compiler_workspaces(root)
                except CVBuildError:
                    self._raise_build_failure(
                        manifest_path, manifest, "cv_generated_output_unsafe"
                    )
            except CVBuildError:
                raise
            except OSError:
                self._raise_build_failure(
                    manifest_path, manifest, "cv_generated_output_unsafe"
                )
        finally:
            os.umask(previous_umask)
        if compiled_pdf_bytes is None:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_pdf_verification",
                diagnostics=compiler_diagnostics,
            )
        return self._verified_result(
            state,
            manifest_path,
            manifest,
            output,
            expected_identity,
            diagnostics=compiler_diagnostics,
            pdf_bytes=compiled_pdf_bytes,
        )

    def _stage_compiler_workspace(
        self,
        root: Path,
        workspace: Path,
        manifest: Mapping[str, object],
        expected_tex: str,
    ) -> Path:
        self._require_beneath(workspace, root)
        try:
            workspace.chmod(0o700)
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error
        source_hashes = manifest.get("source_hashes")
        expected_pdf = manifest.get("expected_pdf")
        if not isinstance(source_hashes, Mapping):
            raise cv_build_error("cv_path_unsafe")
        for reference, expected_digest in source_hashes.items():
            if reference == expected_pdf:
                continue
            if not isinstance(reference, str) or not isinstance(expected_digest, str):
                raise cv_build_error("cv_path_unsafe")
            relative = Path(reference)
            data = safe_read_relative(root / "source", relative)
            if hashlib.sha256(data).hexdigest() != expected_digest:
                raise cv_build_error("cv_path_unsafe")
            target = workspace / relative
            self._require_beneath(target, workspace)
            self._ensure_directory(target.parent, root=workspace)
            copy_bytes_atomic(data, target)
        compile_tex = workspace / expected_tex
        self._require_regular_generated(compile_tex)
        return compile_tex

    def _prepared_manifest(self, state: RunState) -> tuple[Path, dict[str, object]]:
        references = state.data.get("generated_artifacts")
        candidates: list[Path] = []
        if isinstance(references, Sequence) and not isinstance(
            references, (str, bytes)
        ):
            for reference in references:
                if not isinstance(reference, str) or not reference.endswith(
                    "/manifest.yaml"
                ):
                    continue
                path = self.home / reference
                try:
                    self._require_beneath(path, self.generated_root)
                except CVBuildError:
                    continue
                candidates.append(path)
        if len(candidates) != 1:
            raise CVBuildError(
                "cv_prepared_missing: prepared CV is unavailable",
                reason_code="cv_prepared_missing",
            )
        manifest = self._read_manifest(candidates[0])
        context = state.data.get("job_context")
        selected_cv = state.data.get("selected_cv")
        if (
            manifest.get("run_id") != state.run_id
            or not isinstance(context, Mapping)
            or manifest.get("job_fingerprint") != context.get("job_fingerprint")
            or not isinstance(selected_cv, Mapping)
            or manifest.get("source_cv_name") != selected_cv.get("name")
        ):
            raise CVBuildError(
                "cv_manifest_mismatch: prepared CV is unavailable",
                reason_code="cv_manifest_mismatch",
            )
        return candidates[0], manifest

    @staticmethod
    def _run_compiler(
        command: list[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=_BUILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            CVBuildMixin._terminate_process_group(process)
            raise subprocess.TimeoutExpired(
                command,
                _BUILD_TIMEOUT_SECONDS,
                output=error.output,
                stderr=error.stderr,
            ) from None
        return subprocess.CompletedProcess(
            command, process.returncode, stdout=stdout, stderr=stderr
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        process_group = process.pid
        term_deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            try:
                process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            return
        remaining = max(0.0, term_deadline - time.monotonic())
        try:
            process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
        if CVBuildMixin._wait_for_process_group_exit(process_group, term_deadline):
            return
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        try:
            process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        CVBuildMixin._wait_for_process_group_exit(process_group, kill_deadline)

    @staticmethod
    def _process_group_exists(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _wait_for_process_group_exit(process_group: int, deadline: float) -> bool:
        while CVBuildMixin._process_group_exists(process_group):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        return True

    def _verify_prepared_sources(
        self,
        root: Path,
        manifest: Mapping[str, object],
        manifest_path: Path,
    ) -> None:
        try:
            self._validate_manifest_semantics(manifest)
        except CVBuildError:
            self._raise_build_failure(manifest_path, manifest, "cv_manifest_invalid")
        source_hashes = manifest.get("source_hashes")
        assert isinstance(source_hashes, Mapping)
        source_dir = root / "source"
        for reference, expected in source_hashes.items():
            if not isinstance(reference, str) or not isinstance(expected, str):
                self._raise_build_failure(
                    manifest_path, manifest, "cv_manifest_invalid"
                )
            try:
                data = safe_read_relative(source_dir, Path(reference))
                actual = hashlib.sha256(data).hexdigest()
            except (OSError, CVBuildError):
                self._raise_build_failure(
                    manifest_path, manifest, "cv_prepared_tampered"
                )
            if actual != expected:
                self._raise_build_failure(
                    manifest_path, manifest, "cv_prepared_tampered"
                )

    def _verified_result(
        self,
        state: RunState,
        manifest_path: Path,
        manifest: Mapping[str, object],
        output: Path,
        expected_identity: str,
        *,
        diagnostics: BuildDiagnostics = BuildDiagnostics(),
        pdf_bytes: bytes | None = None,
    ) -> CVBuildResult:
        try:
            if pdf_bytes is None:
                self._require_regular_generated(output)
                pdf_bytes = safe_read_relative(output.parent, Path(output.name))
            if not pdf_bytes:
                raise OSError
            reader = PdfReader(BytesIO(pdf_bytes))
            if reader.is_encrypted:
                raise PyPdfError("encrypted")
            page_count = len(reader.pages)
            extracted_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
            if page_count < 1 or not extracted_text.strip():
                raise PyPdfError("missing text")
        except (
            AttributeError,
            CVBuildError,
            EOFError,
            IndexError,
            KeyError,
            OSError,
            PyPdfError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_pdf_verification",
                diagnostics=diagnostics,
            )
        pdf_digest = hashlib.sha256(pdf_bytes).hexdigest()
        if manifest.get("status") == "verified":
            verification = manifest.get("verification")
            expected_digest = (
                verification.get("pdf_sha256")
                if isinstance(verification, Mapping)
                else None
            )
            if expected_digest != pdf_digest:
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_pdf_verification",
                    diagnostics=BuildDiagnostics(
                        return_code=diagnostics.return_code,
                        timed_out=diagnostics.timed_out,
                        stdout_present=diagnostics.stdout_present,
                        stderr_present=diagnostics.stderr_present,
                        page_count=page_count,
                    ),
                )
        if expected_identity not in extracted_text:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_identity_mismatch",
                diagnostics=BuildDiagnostics(
                    return_code=diagnostics.return_code,
                    timed_out=diagnostics.timed_out,
                    stdout_present=diagnostics.stdout_present,
                    stderr_present=diagnostics.stderr_present,
                    page_count=page_count,
                ),
            )
        self._ensure_directory(output.parent, root=manifest_path.parent)
        copy_bytes_atomic(pdf_bytes, output)
        self._cleanup_compiler_outputs(
            manifest_path.parent, manifest, remove_artifact=False
        )
        updated = dict(manifest)
        updated.update(
            {
                "status": "verified",
                "output_pdf": output.relative_to(manifest_path.parent).as_posix(),
                "verification": {
                    "verified": True,
                    "page_count": page_count,
                    "identity_present": True,
                    "pdf_sha256": pdf_digest,
                    "log_ref": None,
                },
                "updated_at": now(),
            }
        )
        self._atomic_manifest(manifest_path, updated)
        pdf_reference = output.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            state.run_id,
            {"target_phase": state.phase, "generated_artifacts": [pdf_reference]},
        )
        return CVBuildResult(
            run_id=state.run_id,
            pdf=output,
            verified=True,
            page_count=page_count,
            extracted_text=extracted_text,
            manifest_path=manifest_path,
            pdf_reference=pdf_reference,
        )

    def _raise_build_failure(
        self,
        manifest_path: Path,
        manifest: Mapping[str, object],
        reason_code: str,
        *,
        diagnostics: BuildDiagnostics = BuildDiagnostics(),
        decision: str = "stop",
    ) -> NoReturn:
        root = manifest_path.parent
        log_path = root / "build-redacted.log"
        self._require_beneath(log_path, self.generated_root)
        artifact_present = self._build_output_present(root, manifest)
        self._cleanup_compiler_outputs(root, manifest, remove_artifact=True)
        record = diagnostic_document(
            reason_code,
            diagnostics,
            artifact_present=artifact_present,
        )
        self._atomic_text(log_path, json.dumps(record, sort_keys=True) + "\n")
        updated = dict(manifest)
        updated.update(
            {
                "status": "failed",
                "output_pdf": None,
                "verification": {
                    "verified": False,
                    "page_count": 0,
                    "identity_present": False,
                    "pdf_sha256": None,
                    "log_ref": log_path.relative_to(root).as_posix(),
                },
                "updated_at": now(),
            }
        )
        self._atomic_manifest(manifest_path, updated)
        raise CVBuildError(
            "cv_build_failed: CV artifact was not verified",
            reason_code=reason_code,
            log_path=log_path,
            decision=decision,
        )

    def _require_regular_generated(self, path: Path) -> None:
        self._require_beneath(path, self.generated_root)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise cv_build_error("cv_path_unsafe")

    def _secure_generated_tree(self, root: Path) -> None:
        self._require_beneath(root, self.generated_root)
        for path in (root, *root.rglob("*")):
            try:
                info = path.stat(follow_symlinks=False)
                if path.is_symlink():
                    raise OSError
                if stat.S_ISDIR(info.st_mode):
                    path.chmod(0o700)
                elif stat.S_ISREG(info.st_mode):
                    path.chmod(0o600)
                else:
                    raise OSError
            except OSError as error:
                raise cv_build_error("cv_path_unsafe") from error

    def _build_output_present(
        self, root: Path, manifest: Mapping[str, object]
    ) -> bool:
        expected_tex = manifest.get("expected_tex")
        if not isinstance(expected_tex, str) or not is_safe_ref(expected_tex):
            return False
        candidate = self._expected_output_path(root, expected_tex)
        try:
            self._require_beneath(candidate, root / "output")
            info = candidate.stat(follow_symlinks=False)
        except (OSError, CVBuildError):
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and not candidate.is_symlink()
            and info.st_size > 0
        )

    def _expected_output_path(self, root: Path, expected_tex: str) -> Path:
        relative = Path("output") / Path(expected_tex).with_suffix(".pdf")
        reference = relative.as_posix()
        if not is_safe_ref(reference):
            raise cv_build_error("cv_path_unsafe")
        output = root / relative
        self._require_beneath(output, root / "output")
        return output

    def _prepared_evidence_sources(
        self, root: Path, manifest: Mapping[str, object]
    ) -> list[dict[str, str]]:
        copied_files = manifest.get("copied_files")
        if not isinstance(copied_files, Sequence) or isinstance(
            copied_files, (str, bytes)
        ):
            raise cv_build_error("cv_prepare_conflict")
        expected_tex = manifest.get("expected_tex")
        expected_pdf = manifest.get("expected_pdf")
        expected_kinds = {expected_tex: "tex", expected_pdf: "pdf"}
        sources: list[dict[str, str]] = []
        for reference in copied_files:
            kind = expected_kinds.get(reference)
            if kind is None or not isinstance(reference, str):
                continue
            data = safe_read_relative(root / "source", Path(reference))
            text = data.decode("utf-8") if kind == "tex" else self._pdf_text(data)
            sources.append({"source_ref": reference, "kind": kind, "text": text})
        if not sources:
            raise cv_build_error("cv_prepare_conflict")
        return sources

    def _require_expected_source_inventory(
        self,
        root: Path,
        manifest: Mapping[str, object],
        manifest_path: Path,
    ) -> None:
        source_hashes = manifest.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            self._raise_build_failure(manifest_path, manifest, "cv_manifest_invalid")
        allowed = {str(reference) for reference in source_hashes}
        allowed_directories = {
            parent.as_posix()
            for reference in allowed
            for parent in Path(reference).parents
            if parent != Path(".")
        }
        source_dir = root / "source"
        try:
            for path in source_dir.rglob("*"):
                reference = path.relative_to(source_dir).as_posix()
                info = path.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if path.is_symlink() or reference not in allowed_directories:
                        raise OSError
                elif (
                    path.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or reference not in allowed
                ):
                    raise OSError
        except OSError:
            self._raise_build_failure(
                manifest_path, manifest, "cv_prepared_tampered"
            )

    def _cleanup_compiler_outputs(
        self,
        root: Path,
        manifest: Mapping[str, object],
        *,
        remove_artifact: bool,
    ) -> None:
        source_hashes = manifest.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            raise cv_build_error("cv_path_unsafe")
        keep = {str(reference) for reference in source_hashes}
        keep_directories = {
            parent.as_posix()
            for reference in keep
            for parent in Path(reference).parents
            if parent != Path(".")
        }
        source_dir = root / "source"
        self._require_beneath(source_dir, self.generated_root)
        try:
            paths = sorted(
                source_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True
            )
            for path in paths:
                reference = path.relative_to(source_dir).as_posix()
                info = path.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
                    if reference not in keep_directories:
                        path.rmdir()
                elif reference not in keep:
                    path.unlink()
            if remove_artifact:
                self._cleanup_output_tree(root)
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error

    def _cleanup_output_tree(self, root: Path) -> None:
        output_dir = root / "output"
        self._require_beneath(output_dir, self.generated_root)
        try:
            output_info = output_dir.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error
        if output_dir.is_symlink() or not stat.S_ISDIR(output_info.st_mode):
            raise cv_build_error("cv_path_unsafe")
        try:
            output_paths = sorted(
                output_dir.rglob("*"),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for path in output_paths:
                info = path.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
                    path.rmdir()
                elif stat.S_ISREG(info.st_mode) or path.is_symlink():
                    path.unlink()
                else:
                    raise OSError("unsafe generated output")
            output_dir.rmdir()
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error

    def _cleanup_compiler_workspaces(self, root: Path) -> None:
        self._require_beneath(root, self.generated_root)
        try:
            candidates = [
                path
                for path in root.iterdir()
                if path.name.startswith(".compiler-work-")
            ]
            for candidate in candidates:
                candidate_info = candidate.stat(follow_symlinks=False)
                if not stat.S_ISDIR(candidate_info.st_mode):
                    raise OSError("unsafe compiler workspace")
                entries = list(candidate.rglob("*"))
                for path in entries:
                    info = path.stat(follow_symlinks=False)
                    if not (
                        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                    ):
                        raise OSError("unsafe compiler workspace entry")
                for path in sorted(
                    entries, key=lambda item: len(item.parts), reverse=True
                ):
                    info = path.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        path.rmdir()
                    else:
                        path.unlink()
                candidate.rmdir()
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            path.chmod(0o600)
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
