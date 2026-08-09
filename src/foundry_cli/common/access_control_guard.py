#!/usr/bin/env python3
"""AccessControlGuard — 8-step precedence model (SRS §4.2, ADR-007).

Implements the access control evaluation order:
1. Operation-level ENABLED
2. Namespace-level ENABLED
3. Operation-level READONLY override (false = permit write when parent READONLY=true)
4. Namespace-level READONLY override (false = permit write when parent READONLY=true)
5. Global READONLY (blocks writes)
6. Namespace-level METADATA_ONLY override (false = permit content when parent METADATA_ONLY=true)
7. Global METADATA_ONLY (blocks content reads not in allow-list)
8. Permit

Per ADR-007, operation-level READONLY=true is NOT independently supported.
Only READONLY=false override of parent READONLY=true is valid.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

from foundry_cli.common.config_loader import ConfigLoader
from foundry_cli.common.error_serializer import EXIT_ACCESS_CONTROL

logger = logging.getLogger(__name__)


# Write operations — heuristic: operations whose name contains known write verbs
# are classified as writes; all others are reads.
_WRITE_VERBS = frozenset(
    {
        "create",
        "delete",
        "put",
        "post",
        "patch",
        "replace",
        "update",
        "add",
        "remove",
        "commit",
        "abort",
        "upload",
        "download",
        "modify",
        "upsert",
        "cancel",
        "revoke",
        "preregister",
        "execute",
        "apply",
        "publish",
        "deploy",
        "run",
        "transform",
        "clear",
        "purge",
        "build",
        "blocking_continue",
        "streaming_continue",
        "rag_context",
        "messages",
        "embeddings",
    }
)

# Read-only metadata operations that are permitted even in METADATA_ONLY tier
# when NOT in the allow-list file (these are the minimal structural metadata)
_METADATA_VERBS = frozenset(
    {
        "get",
        "list",
        "search",
    }
)


class AccessControlError(Exception):
    """Access control policy violation (exit code 8).

    Attributes
    ----------
    message : str
        Human-readable error message.
    exit_code : int
        Exit code (always EXIT_ACCESS_CONTROL = 8).
    step : int
        Which step of the 8-step precedence model triggered the block.
    """

    def __init__(
        self,
        message: str,
        step: int = 0,
        blocked_rule: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = EXIT_ACCESS_CONTROL
        self.step = step
        self.blocked_rule = blocked_rule or {"step": step, "message": message}
        self.details = {"blocked_rule": self.blocked_rule}


class AccessControlGuard:
    """Evaluates access control using 8-step precedence model (SRS §4.2).

    Parameters
    ----------
    cfg : ConfigLoader
        Configuration instance.
    namespace : str
        Namespace name (e.g., "DATASETS").
    metadata_allowlist_path : str, optional
        Path to metadata allow-list file; if None, uses the canonical location.

    Usage
    -----
    >>> guard = AccessControlGuard(cfg, "DATASETS")
    >>> guard.check("dataset", "create")  # raises AccessControlError on block
    """

    def __init__(
        self,
        cfg: ConfigLoader,
        namespace: str,
        metadata_allowlist_path: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.namespace = namespace.upper()
        self._metadata_allowlist: set[str] | None = None
        self._metadata_allowlist_path = metadata_allowlist_path

    def _is_write_operation(self, operation: str) -> bool:
        """Classify an operation as a write operation.

        Heuristic: if the operation name (lowercase) starts with any known
        write verb, it's classified as a write.  This covers the 355 SDK
        operations without hardcoding a per-namespace list.

        Parameters
        ----------
        operation : str
            Operation name (e.g., "create", "get", "put_schema").

        Returns
        -------
        bool
            True if the operation is classified as a write.
        """
        op_lower = operation.lower()
        for verb in _WRITE_VERBS:
            if op_lower == verb or op_lower.startswith(verb + "_"):
                return True
        return False

    def _operation_env_key(self, resource: str, operation: str) -> str:
        """Build canonical env key suffix for an operation.

        Follows the canonical transformation rule (SRS §5.2): the entire
        SDK path ``{ns}.{resource}.{operation}`` is uppercased and joined
        with underscores, keeping the operation name verbatim (NOT
        reordered by verb). This ensures ``put_schema`` maps to
        ``DATASETS_DATASET_PUT_SCHEMA``, matching the canonical env-var
        reference table and the Step-3 ``_READONLY=false`` override.

        Parameters
        ----------
        resource : str
            Resource/class name (e.g., "dataset").
        operation : str
            Operation name (e.g., "put_schema", "get").

        Returns
        -------
        str
            Env key suffix such as ``DATASETS_DATASET_PUT_SCHEMA``.
        """
        return f"{self.namespace}_{resource.upper()}_{operation.upper()}"

    @staticmethod
    def _is_true_env(value: str | None) -> bool:
        return value is not None and value.lower() in ("true", "1", "yes", "on")

    @staticmethod
    def _is_false_env(value: str | None) -> bool:
        return value is not None and value.lower() in ("false", "0", "no", "off")

    def _get_global_readonly(self) -> bool:
        env_val = os.environ.get("FOUNDRY_AGENTIC_CLI_READONLY")
        if env_val is not None:
            return self._is_true_env(env_val)
        return bool(self.cfg.global_readonly)

    def _get_global_metadata_only(self) -> bool:
        env_val = os.environ.get("FOUNDRY_AGENTIC_CLI_METADATA_ONLY")
        if env_val is not None:
            return self._is_true_env(env_val)
        return bool(self.cfg.global_metadata_only)

    def _get_namespace_readonly(self) -> bool:
        """Check if namespace-level READONLY is explicitly set to true.

        Returns
        -------
        bool
            True if FOUNDRY_AGENTIC_CLI_{NS}_READONLY is explicitly "true".
        """
        ns_readonly_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_READONLY"
        ns_readonly_val = os.environ.get(ns_readonly_env)
        if ns_readonly_val is None:
            return False
        return ns_readonly_val.lower() in ("true", "1", "yes")

    def _get_namespace_metadata_only(self) -> bool:
        """Check if namespace-level METADATA_ONLY is explicitly set to true.

        Returns
        -------
        bool
            True if FOUNDRY_AGENTIC_CLI_{NS}_METADATA_ONLY is explicitly "true".
        """
        ns_metadata_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_METADATA_ONLY"
        ns_metadata_val = os.environ.get(ns_metadata_env)
        if ns_metadata_val is None:
            return False
        return ns_metadata_val.lower() in ("true", "1", "yes")

    def _log_decision(
        self,
        decision: str,
        resource: str,
        operation: str,
        step: int,
        reason: str,
    ) -> None:
        """Log access control decision as NDJSON (ADR-005).

        Parameters
        ----------
        decision : str
            "BLOCKED" or "PERMITTED".
        resource : str
            Resource name.
        operation : str
            Operation name.
        step : int
            Step number (1-8) in precedence model.
        reason : str
            Human-readable reason for the decision.
        """
        logger.info(
            "Access control decision: %s (Step %d) — %s",
            decision,
            step,
            reason,
            extra={
                "access_decision": decision,
                "op": f"{self.namespace.lower()}.{resource.lower()}.{operation.lower()}",
                "step": step,
                "reason": reason,
            },
        )

    def _load_metadata_allowlist(self) -> set[str]:
        """Load metadata allow-list from canonical file.

        Returns
        -------
        set of str
            Set of operation keys (e.g., "datasets.dataset.get") permitted
            in METADATA_ONLY tier.

        Notes
        -----
        The allow-list file is a Markdown table.  This parser is intentionally
        lightweight and extracts lines containing "PERMITTED" to build the set.
        """
        allowlist_path: Path | None
        if self._metadata_allowlist_path:
            allowlist_path = Path(self._metadata_allowlist_path)
        else:
            # Default: look for metadata-allow-list.md in .ept/docs
            allowlist_path = Path(
                ".ept/docs/deliverables/architecture/metadata-allow-list.md"
            )

        if not allowlist_path.exists():
            return set()

        permitted: set[str] = set()
        canonical_row = re.compile(
            r"^\|\s*`(?P<sdk_path>[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+)`\s*"
            r"\|\s*PERMITTED\s*\|",
        )
        text = allowlist_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = canonical_row.match(line.strip())
            if match:
                permitted.add(match.group("sdk_path").lower())
        return permitted

    def _is_in_metadata_allowlist(self, resource: str, operation: str) -> bool:
        """Check if an operation is in the metadata allow-list.

        Parameters
        ----------
        resource : str
            Resource name.
        operation : str
            Operation name.

        Returns
        -------
        bool
            True if the operation is permitted in METADATA_ONLY tier.
        """
        if self._metadata_allowlist is None:
            self._metadata_allowlist = self._load_metadata_allowlist()

        ns_lower = self.namespace.lower()
        key = f"{ns_lower}.{resource.lower()}.{operation.lower()}"
        return key in self._metadata_allowlist

    def check(
        self,
        resource: str,
        operation: str,
    ) -> None:
        """Evaluate access control for an operation (8-step precedence).

        Parameters
        ----------
        resource : str
            Resource/class name (e.g., "dataset", "branch").
        operation : str
            Operation name (e.g., "create", "get").

        Raises
        ------
        AccessControlError
            If access control blocks the operation.
        """
        full_key = self._operation_env_key(resource, operation)
        is_write = self._is_write_operation(operation)
        op_path = f"{self.namespace.lower()}.{resource.lower()}.{operation.lower()}"

        def _blocked(
            message: str, step: int, env_var: str, value: str
        ) -> AccessControlError:
            return AccessControlError(
                message,
                step=step,
                blocked_rule={
                    "step": step,
                    "operation": op_path,
                    "env_var": env_var,
                    "value": value,
                    "message": message,
                },
            )

        # Step 1: Operation-level ENABLED
        enabled_env = f"FOUNDRY_AGENTIC_CLI_{full_key}_ENABLED"
        enabled_val = os.environ.get(enabled_env)
        if enabled_val is not None and enabled_val.lower() == "false":
            self._log_decision(
                "BLOCKED", resource, operation, 1, f"ENABLED=false for {full_key}"
            )
            raise _blocked(
                f"Operation blocked: ENABLED=false for {full_key}",
                1,
                enabled_env,
                enabled_val,
            )

        # Step 2: Namespace-level ENABLED
        ns_enabled_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_ENABLED"
        ns_enabled_val = os.environ.get(ns_enabled_env)
        if ns_enabled_val is not None and ns_enabled_val.lower() == "false":
            self._log_decision(
                "BLOCKED", resource, operation, 2, f"ENABLED=false for {self.namespace}"
            )
            raise _blocked(
                f"Namespace blocked: ENABLED=false for {self.namespace}",
                2,
                ns_enabled_env,
                ns_enabled_val,
            )

        ns_metadata_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_METADATA_ONLY"
        ns_metadata_val = os.environ.get(ns_metadata_env)
        namespace_metadata_only = self._is_true_env(ns_metadata_val)
        namespace_metadata_override = self._is_false_env(ns_metadata_val)
        global_metadata_only = self._get_global_metadata_only()
        metadata_only_active = namespace_metadata_only or (
            global_metadata_only and not namespace_metadata_override
        )

        # Steps 3-5: READONLY evaluation (only for write operations)
        if is_write:
            parent_readonly = (
                self._get_global_readonly() or self._get_namespace_readonly()
            )

            if parent_readonly:
                # Step 3: Operation-level READONLY override
                op_readonly_env = f"FOUNDRY_AGENTIC_CLI_{full_key}_READONLY"
                op_readonly_val = os.environ.get(op_readonly_env)
                if op_readonly_val is not None and op_readonly_val.lower() == "false":
                    self._log_decision(
                        "PERMITTED",
                        resource,
                        operation,
                        3,
                        f"READONLY=false override for {full_key}",
                    )
                    return  # Explicit write permit for this operation

                # Step 4: Namespace-level READONLY override
                ns_readonly_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_READONLY"
                ns_readonly_val = os.environ.get(ns_readonly_env)
                if ns_readonly_val is not None and ns_readonly_val.lower() == "false":
                    self._log_decision(
                        "PERMITTED",
                        resource,
                        operation,
                        4,
                        f"READONLY=false override for {self.namespace}",
                    )
                    return  # Explicit write permit for this namespace

                if self._get_namespace_readonly():
                    self._log_decision(
                        "BLOCKED",
                        resource,
                        operation,
                        4,
                        f"READONLY=true for {self.namespace}",
                    )
                    raise _blocked(
                        "Operation blocked: namespace read-only mode active",
                        4,
                        ns_readonly_env,
                        "true",
                    )

            # Step 5: Global READONLY
            if self._get_global_readonly():
                self._log_decision(
                    "BLOCKED",
                    resource,
                    operation,
                    5,
                    "Global READONLY=true blocks write",
                )
                raise _blocked(
                    "Operation blocked: read-only mode active",
                    5,
                    "FOUNDRY_AGENTIC_CLI_READONLY",
                    "true",
                )

        # Steps 6-7: METADATA_ONLY evaluation.
        if global_metadata_only and namespace_metadata_override:
            self._log_decision(
                "PERMITTED",
                resource,
                operation,
                6,
                f"METADATA_ONLY=false override for {self.namespace}",
            )
            return

        if metadata_only_active:
            metadata_env = (
                ns_metadata_env
                if namespace_metadata_only
                else "FOUNDRY_AGENTIC_CLI_METADATA_ONLY"
            )
            if is_write:
                step = 6 if namespace_metadata_only else 7
                self._log_decision(
                    "BLOCKED", resource, operation, 7, "METADATA_ONLY=true blocks write"
                )
                raise _blocked(
                    "Operation blocked: metadata-only mode active",
                    step,
                    metadata_env,
                    "true",
                )
            if not self._is_in_metadata_allowlist(resource, operation):
                self._log_decision(
                    "BLOCKED",
                    resource,
                    operation,
                    7,
                    "Operation not in metadata allow-list",
                )
                raise _blocked(
                    "Operation blocked: not in metadata allow-list",
                    7,
                    metadata_env,
                    "true",
                )

        # Step 8: Permit
        self._log_decision("PERMITTED", resource, operation, 8, "Default full access")
        return

    def _is_metadata_operation(self, resource: str, operation: str) -> bool:
        """Check if an operation is a structural metadata operation.

        Heuristic: operations whose name starts with known metadata verbs
        (get, list, search) are structural metadata and are always permitted
        in METADATA_ONLY tier even if not explicitly in the allow-list.

        Parameters
        ----------
        resource : str
            Resource name.
        operation : str
            Operation name.

        Returns
        -------
        bool
            True if the operation is structural metadata.
        """
        op_lower = operation.lower()
        for verb in _METADATA_VERBS:
            if op_lower == verb or op_lower.startswith(verb + "_"):
                return True
        return False
